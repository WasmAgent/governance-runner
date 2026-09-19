#!/usr/bin/env python3
"""Out-of-band governance sweep for WasmAgent/.github open PRs.

For every open PR: download the candidate tarball, run the vendored trusted
validators against it as DATA ONLY, re-verify the PR head (TOCTOU guard),
then publish a check run as the governance GitHub App.

Security properties:
  - never executes candidate shell, workflows, or Python
  - never imports candidate code; validators come from this repository
  - the App token held here can only read the candidate and write checks
  - infrastructure failures publish a failure check (fail closed)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]

TOKEN = os.environ["GH_TOKEN"]
APP_ID = int(os.environ["GOVERNANCE_APP_ID"])
TARGET_REPO = os.environ.get("TARGET_REPO", "WasmAgent/.github")
CHECK_NAME = os.environ.get(
    "CHECK_NAME",
    "governance-root-authority",
)

API = os.environ["GITHUB_API_URL"].rstrip("/")
SERVER = os.environ["GITHUB_SERVER_URL"].rstrip("/")
RUNNER_REPO = os.environ["RUNNER_REPOSITORY"]
RUNNER_SHA = os.environ["RUNNER_SHA"]
RUN_ID = os.environ["RUN_ID"]

DETAILS_URL = (
    f"{SERVER}/{RUNNER_REPO}/actions/runs/{RUN_ID}"
)

API_VERSION = "2022-11-28"


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
) -> tuple[bool, str]:
    validators = [
        ROOT
        / "validators"
        / "validate-external-evidence.py",
        ROOT
        / "validators"
        / "validate-public-claims.py",
    ]

    success = True
    output: list[str] = []

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
                    candidate
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
