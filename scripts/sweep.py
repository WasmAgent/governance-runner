#!/usr/bin/env python3
"""Out-of-band governance sweep for WasmAgent/.github open PRs.

For every open PR: download the candidate tarball, verify the candidate's
AUTHORITY SURFACE against this repository's manifest (judge code may not be
changed by a candidate), run the vendored trusted validators against the
candidate as DATA ONLY, re-verify the PR head (TOCTOU guard), then publish a
check run as the governance GitHub App.

Security properties:
  - never executes candidate shell, workflows, or Python
  - never imports candidate code; validators come from this repository
  - candidate cannot alter the judge CODE its PR is judged with
    (".github/workflows/**", "scripts/**") NOR the judge POLICY/CONFIG
    dependency closure ("policies/**", "schemas/**",
    "golden-path/versions.lock.json", "claims/claim-overreach-allowlist.json"):
    everything must hash-match authority-manifest.json (two-phase upgrade:
    manifest first, candidate second)
  - the App token held here can only read the candidate and write checks
  - infrastructure failures publish a failure check (fail closed)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "authority-manifest.json"
API_VERSION = "2022-11-28"

# Populated from the environment in main(); importable without env for tests.
TOKEN = ""
APP_ID = 0
TARGET_REPO = "WasmAgent/.github"
CHECK_NAME = "governance-root-authority"
API = "https://api.github.com"
SERVER = "https://github.com"
RUNNER_REPO = ""
RUNNER_SHA = ""
RUN_ID = ""
DETAILS_URL = ""


def load_environment() -> None:
    global TOKEN, APP_ID, TARGET_REPO, CHECK_NAME, API, SERVER
    global RUNNER_REPO, RUNNER_SHA, RUN_ID, DETAILS_URL
    TOKEN = os.environ["GH_TOKEN"]
    APP_ID = int(os.environ["GOVERNANCE_APP_ID"])
    TARGET_REPO = os.environ.get("TARGET_REPO", TARGET_REPO)
    CHECK_NAME = os.environ.get("CHECK_NAME", CHECK_NAME)
    API = os.environ["GITHUB_API_URL"].rstrip("/")
    SERVER = os.environ["GITHUB_SERVER_URL"].rstrip("/")
    RUNNER_REPO = os.environ["RUNNER_REPOSITORY"]
    RUNNER_SHA = os.environ["RUNNER_SHA"]
    RUN_ID = os.environ["RUN_ID"]
    DETAILS_URL = f"{SERVER}/{RUNNER_REPO}/actions/runs/{RUN_ID}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


MANIFEST_PREFIXES = (".github/workflows/", "scripts/", "policies/", "schemas/")
MANIFEST_EXACT_FILES = (
    "golden-path/versions.lock.json",
    "claims/claim-overreach-allowlist.json",
)
MANIFEST_SOURCE_REPOSITORY = "WasmAgent/.github"
_HEX40 = re.compile(r"^[0-9a-f]{40}$")


def build_manifest(
    tree: Path,
    source_commit: str,
    prefixes: tuple[str, ...] = MANIFEST_PREFIXES,
    exact_files: tuple[str, ...] = MANIFEST_EXACT_FILES,
) -> dict:
    """Same contract as scripts/build-manifest.py — shared by the self-test."""
    files: dict[str, str] = {}
    for prefix in prefixes:
        base = tree / prefix
        for path in sorted(base.rglob("*")):
            if path.is_file():
                rel = path.relative_to(tree).as_posix()
                files[rel] = sha256_file(path)
    for rel in exact_files:
        path = tree / rel
        if not path.is_file():
            raise SystemExit(f"exact authority file missing from source tree: {rel}")
        files[rel] = sha256_file(path)
    return {
        "authority_surface": {
            "source_commit": source_commit,
            "prefixes": list(prefixes),
            "exact_files": list(exact_files),
            "files": files,
        }
    }


def validate_manifest_contract(manifest: dict) -> list[str]:
    """Validate the CHECKED-IN manifest's structural contract (P0c).

    The sweeper refuses to run on a manifest that does not preserve the v2
    closure: without this, a weakened checked-in manifest (dropped prefix,
    unpinned exact file) would silently downgrade the runtime authority
    while the self-test's synthetic manifest stayed green.
    """
    problems: list[str] = []

    def fail(message: str) -> None:
        problems.append(message)

    if manifest.get("schema_version") != 2:
        fail(f"schema_version must be 2, got {manifest.get('schema_version')!r}")

    surface = manifest.get("authority_surface")
    if not isinstance(surface, dict):
        fail("authority_surface object missing")
        return problems

    if surface.get("source_repository") != MANIFEST_SOURCE_REPOSITORY:
        fail(f"source_repository must be {MANIFEST_SOURCE_REPOSITORY!r}, "
             f"got {surface.get('source_repository')!r}")

    source_commit = surface.get("source_commit")
    if not isinstance(source_commit, str) or not _HEX40.match(source_commit):
        fail("source_commit must be a 40-character hex sha")

    prefixes = surface.get("prefixes")
    if list(prefixes or []) != list(MANIFEST_PREFIXES):
        fail(f"prefixes must be exactly {list(MANIFEST_PREFIXES)}, got {prefixes!r}")

    exact = surface.get("exact_files")
    if list(exact or []) != list(MANIFEST_EXACT_FILES):
        fail(f"exact_files must be exactly {list(MANIFEST_EXACT_FILES)}, got {exact!r}")

    files = surface.get("files")
    if not isinstance(files, dict) or not files:
        fail("files must be a non-empty mapping of path -> sha256")
        return problems

    for path in sorted(exact or []):
        if path not in files:
            fail(f"exact_files declares {path!r} but files does not pin it")

    for path in sorted(files):
        value = files[path]
        if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            fail(f"files[{path!r}] must be 'sha256:<64 hex>'")
        if not any(path.startswith(prefix) for prefix in MANIFEST_PREFIXES) \
                and path not in MANIFEST_EXACT_FILES:
            fail(f"files entry outside the authority surface: {path}")

    return problems


def build_manifest_from_git(repo: Path, source_commit: str) -> dict:
    """Canonical manifest for an immutable .github commit tree (P0c).

    Hashes the git tree of source_commit (ls-tree/show) — never a working
    tree — so the result is by construction bound to source_commit.
    """
    def _git(*args: str, binary: bool = False):
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed: "
                f"{result.stderr.decode(errors='replace')}"
            )
        return result.stdout if binary else result.stdout.decode().strip()

    _git("cat-file", "-e", f"{source_commit}^{{commit}}")
    listing = _git("ls-tree", "-r", "--name-only", "-z", source_commit, binary=True)
    tree_paths = [item.decode("utf-8") for item in listing.split(b"\0") if item]

    files: dict[str, str] = {}
    for prefix in MANIFEST_PREFIXES:
        matched = sorted(p for p in tree_paths if p.startswith(prefix))
        if not matched:
            raise RuntimeError(f"missing authority directory in commit tree: {prefix}")
        for rel in matched:
            content = _git("show", f"{source_commit}:{rel}", binary=True)
            files[rel] = f"sha256:{hashlib.sha256(content).hexdigest()}"
    for rel in MANIFEST_EXACT_FILES:
        if rel not in tree_paths:
            raise RuntimeError(f"exact authority file missing from commit tree: {rel}")
        content = _git("show", f"{source_commit}:{rel}", binary=True)
        files[rel] = f"sha256:{hashlib.sha256(content).hexdigest()}"

    return {
        "schema_version": 2,
        "authority_surface": {
            "source_repository": MANIFEST_SOURCE_REPOSITORY,
            "source_commit": source_commit,
            "prefixes": list(MANIFEST_PREFIXES),
            "exact_files": list(MANIFEST_EXACT_FILES),
            "files": files,
        }
    }


def verify_manifest_source_binding(manifest: dict, repo: Path) -> list[str]:
    """P0c closure: the checked-in manifest must equal the canonical manifest
    rebuilt from its own source_commit's git tree. Structural contract
    validity alone does not prove the hashes actually pin that commit.
    """
    problems: list[str] = []
    surface = manifest.get("authority_surface") or {}
    source_commit = surface.get("source_commit") or ""
    try:
        canonical = build_manifest_from_git(repo, source_commit)
    except RuntimeError as error:
        return [f"source binding unverifiable: {error}"]

    canonical_files = canonical["authority_surface"]["files"]
    checked_files = surface.get("files") or {}
    for path in sorted(set(canonical_files) | set(checked_files)):
        if path not in checked_files:
            problems.append(f"source binding: manifest omits {path} "
                            f"(pinned by {source_commit[:12]})")
        elif path not in canonical_files:
            problems.append(f"source binding: manifest pins {path} which does not "
                            f"exist in {source_commit[:12]}")
        elif checked_files[path] != canonical_files[path]:
            problems.append(
                f"source binding: {path} hash does not match "
                f"{source_commit[:12]} (manifest {checked_files[path][:19]}…, "
                f"tree {canonical_files[path][:19]}…)"
            )
    return problems


def verify_authority_surface(candidate: Path, manifest: dict) -> list[str]:
    """Return a list of authority-surface problems (empty == candidate OK).

    Every file the manifest knows must exist in the candidate with the exact
    manifest hash; every file the candidate carries under the manifest's
    prefixes must be present in the manifest (no unmanifested judge code).
    """
    surface = manifest["authority_surface"]
    prefixes = tuple(surface["prefixes"])
    expected: dict[str, str] = surface["files"]
    problems: list[str] = []

    seen: set[str] = set()
    for prefix in prefixes:
        base = candidate / prefix
        if not base.is_dir():
            problems.append(f"missing authority directory: {prefix}")
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file():
                rel = path.relative_to(candidate).as_posix()
                seen.add(rel)

    for path in sorted(expected):
        f = candidate / path
        if not f.is_file():
            problems.append(f"missing authority file: {path}")
        elif sha256_file(f) != expected[path]:
            problems.append(
                f"authority file differs from manifest: {path} "
                f"(expected {expected[path][:19]}…, got {sha256_file(f)[:19]}…)"
            )

    for rel in sorted(seen):
        if rel not in expected:
            problems.append(f"unmanifested authority file: {rel}")
    return problems


def api_bytes(
    method: str,
    path: str,
    payload: dict | None = None,
) -> bytes:
    url = f"{API}/{path.lstrip('/')}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    request = Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "WasmAgent-governance-runner",
        },
    )

    try:
        with urlopen(request, timeout=45) as response:
            return response.read()
    except HTTPError as error:
        body = error.read().decode(
            "utf-8",
            errors="replace",
        )
        raise RuntimeError(
            f"GitHub API {method} {path} "
            f"failed: {error.code}: {body}"
        ) from error


def api_json(
    method: str,
    path: str,
    payload: dict | None = None,
):
    raw = api_bytes(method, path, payload)
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def list_open_prs() -> list[dict]:
    result: list[dict] = []
    page = 1

    while True:
        batch = api_json(
            "GET",
            (
                f"repos/{TARGET_REPO}/pulls"
                f"?state=open&per_page=100&page={page}"
            ),
        )

        if not batch:
            break

        result.extend(batch)

        if len(batch) < 100:
            break

        page += 1

    return result


def current_pr(number: int) -> dict:
    return api_json(
        "GET",
        f"repos/{TARGET_REPO}/pulls/{number}",
    )


def extract_candidate(
    sha: str,
    working: Path,
) -> Path:
    archive = working / "candidate.tar.gz"
    archive.write_bytes(
        api_bytes(
            "GET",
            f"repos/{TARGET_REPO}/tarball/{sha}",
        )
    )

    destination = working / "candidate"
    destination.mkdir()

    with tarfile.open(
        archive,
        mode="r:gz",
    ) as bundle:
        bundle.extractall(
            destination,
            filter="data",
        )

    roots = [
        item
        for item in destination.iterdir()
        if item.is_dir()
    ]

    if len(roots) != 1:
        raise RuntimeError(
            f"unexpected archive root count: {len(roots)}"
        )

    return roots[0]


def run_validator(
    validator: Path,
    candidate: Path,
) -> tuple[int, str]:
    process = subprocess.run(
        [
            sys.executable,
            str(validator),
            "--repo-root",
            str(candidate),
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=90,
        check=False,
    )

    return (
        process.returncode,
        process.stdout,
    )


def validate_candidate(
    candidate: Path,
    manifest: dict,
) -> tuple[bool, str]:
    problems = verify_authority_surface(candidate, manifest)

    validators = [
        ROOT
        / "validators"
        / "validate-external-evidence.py",
        ROOT
        / "validators"
        / "validate-public-claims.py",
    ]

    success = not problems
    output: list[str] = []

    if problems:
        output.append(
            "## authority surface\n"
            "exit=1\n\n"
            "FAIL — candidate judge code does not match authority-manifest.json.\n"
            "A legitimate judge-code change lands in governance-runner FIRST\n"
            "(update authority-manifest.json via PR), then the candidate change.\n\n"
            + "\n".join(f"- {problem}" for problem in problems)
        )

    for validator in validators:
        code, text = run_validator(
            validator,
            candidate,
        )

        output.append(
            f"## {validator.name}\n"
            f"exit={code}\n\n"
            f"{text.strip()}"
        )

        if code != 0:
            success = False

    return success, "\n\n".join(output)


def existing_own_check(
    sha: str,
) -> dict | None:
    query = urlencode(
        {
            "check_name": CHECK_NAME,
            "filter": "latest",
            "per_page": 100,
        }
    )

    result = api_json(
        "GET",
        (
            f"repos/{TARGET_REPO}/commits/"
            f"{sha}/check-runs?{query}"
        ),
    )

    for check in result.get(
        "check_runs",
        [],
    ):
        app = check.get("app") or {}
        if app.get("id") == APP_ID:
            return check

    return None


def publish_check(
    *,
    pr_number: int,
    sha: str,
    success: bool,
    summary: str,
) -> None:
    conclusion = (
        "success"
        if success
        else "failure"
    )

    title = (
        "Governance root authority: PASS"
        if success
        else "Governance root authority: HOLD"
    )

    safe_summary = (
        "Trusted out-of-band authority\n\n"
        f"- PR: #{pr_number}\n"
        f"- candidate SHA: `{sha}`\n"
        f"- authority SHA: `{RUNNER_SHA}`\n\n"
        + summary
    )

    # Avoid candidate-controlled markdown
    # terminating a diagnostic fence.
    safe_summary = safe_summary.replace(
        "```",
        "'''",
    )[:60000]

    common = {
        "status": "completed",
        "conclusion": conclusion,
        "details_url": DETAILS_URL,
        "output": {
            "title": title,
            "summary": safe_summary,
        },
    }

    existing = existing_own_check(sha)

    if existing is not None:
        api_json(
            "PATCH",
            (
                f"repos/{TARGET_REPO}/check-runs/"
                f"{existing['id']}"
            ),
            common,
        )
        return

    payload = {
        "name": CHECK_NAME,
        "head_sha": sha,
        "external_id": (
            f"{TARGET_REPO}:"
            f"pr-{pr_number}:"
            f"{sha}"
        ),
        **common,
    }

    api_json(
        "POST",
        f"repos/{TARGET_REPO}/check-runs",
        payload,
    )


def main() -> int:
    load_environment()

    # P0c: the checked-in manifest is itself a validated authority artifact.
    # A contract violation fails the whole sweep closed (no PR is judged by a
    # downgraded runtime authority) and every open PR visibly HOLDs.
    open_prs = []
    try:
        manifest = json.loads(MANIFEST_PATH.read_text())
        contract_problems = validate_manifest_contract(manifest)
    except (OSError, json.JSONDecodeError) as error:
        manifest, contract_problems = None, [
            f"authority manifest unreadable: {type(error).__name__}: {error}"
        ]

    if isinstance(manifest, dict) and not contract_problems:
        # P0d: the check context carries the manifest's source_commit — an
        # authority upgrade changes the required context name itself, so
        # verdicts from an older authority can never satisfy the new one.
        global CHECK_NAME
        CHECK_NAME = (
            f"governance-root-authority/"
            f"{manifest['authority_surface']['source_commit'][:7]}"
        )

    if contract_problems:
        print("FAIL: authority manifest contract violation — sweep fails closed:")
        for problem in contract_problems:
            print(f"  - {problem}")
        try:
            for pr in list_open_prs():
                try:
                    publish_check(
                        pr_number=int(pr["number"]),
                        sha=pr["head"]["sha"],
                        success=False,
                        summary=(
                            "Runner authority manifest contract violation — "
                            "sweep failed closed.\n\n" + "\n".join(contract_problems)
                        ),
                    )
                except Exception:
                    pass
        except Exception:
            pass
        return 1

    open_prs = list_open_prs()

    if not open_prs:
        print("No open target PRs.")
        return 0

    infrastructure_failures = 0

    for pr in open_prs:
        number = int(pr["number"])
        sha = pr["head"]["sha"]

        print(
            f"Checking PR #{number} "
            f"at {sha}"
        )

        try:
            with tempfile.TemporaryDirectory() as tmp:
                candidate = extract_candidate(
                    sha,
                    Path(tmp),
                )

                success, summary = validate_candidate(
                    candidate,
                    manifest,
                )

            # TOCTOU guard:
            # only publish for the exact PR head
            # that was inspected.
            latest = current_pr(number)

            if (
                latest.get("state") != "open"
                or latest["head"]["sha"] != sha
            ):
                print(
                    f"PR #{number} advanced or closed; "
                    "discarding stale result."
                )
                continue

            publish_check(
                pr_number=number,
                sha=sha,
                success=success,
                summary=summary,
            )

            print(
                f"PR #{number}: "
                f"{'PASS' if success else 'HOLD'}"
            )

        except Exception as error:
            infrastructure_failures += 1

            message = (
                "Runner infrastructure failure.\n\n"
                f"{type(error).__name__}: {error}"
            )

            try:
                latest = current_pr(number)
                if (
                    latest.get("state") == "open"
                    and latest["head"]["sha"] == sha
                ):
                    publish_check(
                        pr_number=number,
                        sha=sha,
                        success=False,
                        summary=message,
                    )
            except Exception:
                pass

            print(
                f"INFRA FAILURE PR #{number}: {error}",
                file=sys.stderr,
            )

    return 1 if infrastructure_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
