#!/usr/bin/env python3
"""Self-test for the vendored out-of-band authority.

Runs on every push/PR of governance-runner (no secrets involved) and proves
the vendored validators still execute and still enforce their invariants,
AND that the authority-surface manifest check behaves:

  1. a minimal conforming candidate passes BOTH validators;
  2. flipping one claim to the legal status `deprecated` while the fixture
     homepage still asserts "all `supported`" makes validate-public-claims
     fail with EXACTLY ONE failure — the PC-02b message;
  3. the authority-surface manifest accepts the untouched candidate;
  4. tampered judge CODE (workflow hash change) is rejected;
  5. unmanifested judge code is rejected;
  6. deleted judge code is rejected;
  7. tampered judge POLICY (policies/*.yml) is rejected;
  8. tampered normative SCHEMA (schemas/*.json) is rejected;
  9. tampered pinned exact file (golden-path/versions.lock.json) is rejected;
 10. ordinary governed data (docs/, claims/) may still change freely;
 11. the CHECKED-IN authority-manifest.json satisfies the runtime contract
     (schema v2, required prefixes/exact_files, exact⊆files, paths within
     surface, sha256 format, 40-hex source_commit);
 12. a manifest that drops a required prefix fails the contract;
 13. a manifest that unpins an exact_files entry from files fails the
     contract (both the keep-exact_files and drop-everywhere variants).

The fixture is built programmatically so the test cannot drift from the
validator contract.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import sweep  # noqa: E402  (imports clean: env reads happen in main())

CANONICAL_URL = (
    "https://github.com/WasmAgent/.github/blob/main/claims/public-claims.yml"
)

LEDGER_RECORD = """    {
      "id": "EXT-TEST-0001",
      "subject": "test subject",
      "venue": {"org": "Example Org", "repo": "example/repo", "issue": 1},
      "evidence_type": "upstream_contribution",
      "state": "open",
      "claim_ceiling": "Open upstream discussion only; no adoption or endorsement is implied.",
      "prohibited_claims": ["example_adopted", "example_endorsed"],
      "anchors": {"issue_url": "https://github.com/example/repo/issues/1"},
      "limitations": ["Fixture record for the runner self-test only."]
    }"""

CLAIM = """  - id: WA-C-{n}
    claim_class: internal_supported
    domain: evidence
    claim: >-
      Fixture claim for the governance-runner self-test.
    evidence:
      repo: example/repo
    status: supported
    recorded: 2026-01-01
"""

WORKFLOW = (
    "name: fixture workflow\n"
    "on: push\n"
    "jobs:\n"
    "  fixture-job:\n"
    "    runs-on: ubuntu-latest\n"
    "    steps:\n"
    "      - run: echo fixture\n"
)

JUDGE = "# fixture judge code\n"


def build_candidate(root: Path) -> None:
    (root / "claims").mkdir(parents=True)
    (root / "evidence").mkdir()
    (root / "profile").mkdir()
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / "scripts").mkdir()

    claims = "# fixture claims\nschema_version: 1\norg: WasmAgent\nlast_reviewed: 2026-01-01\nclaims:\n"
    claims += CLAIM.format(n="0001") + CLAIM.format(n="0002")
    (root / "claims" / "public-claims.yml").write_text(claims)

    ledger = (
        '{"schema_version": 1, "last_reviewed": "2026-01-01", '
        '"records": [\n' + LEDGER_RECORD + "\n]}\n"
    )
    (root / "evidence" / "external-validation.json").write_text(ledger)

    readme = (
        "# fixture profile\n\n"
        "**2 public claims — all `supported`** · "
        "Registry last reviewed **2026-01-01**\n\n"
        f"[`public-claims.yml`]({CANONICAL_URL})\n"
    )
    (root / "profile" / "README.md").write_text(readme)

    (root / ".github" / "workflows" / "fixture.yml").write_text(WORKFLOW)
    (root / "scripts" / "judge.py").write_text(JUDGE)

    # judge policy/config dependency closure (manifest v2)
    (root / "policies").mkdir()
    (root / "schemas").mkdir()
    (root / "golden-path").mkdir()
    (root / "policies" / "repository-ownership.yml").write_text(
        "runtime_source_extensions:\n  - .py\n"
    )
    (root / "policies" / "repository-assurance.yml").write_text("repo_classes: []\n")
    (root / "schemas" / "external-validation.schema.json").write_text("{}\n")
    (root / "schemas" / "external-outbound-preflight.schema.json").write_text("{}\n")
    (root / "golden-path" / "versions.lock.json").write_text("{}\n")
    (root / "claims" / "claim-overreach-allowlist.json").write_text('{"allowlist": []}\n')


def flip_one_claim(root: Path) -> None:
    path = root / "claims" / "public-claims.yml"
    text = path.read_text()
    assert text.count("    status: supported\n") == 2
    path.write_text(text.replace("    status: supported\n", "    status: deprecated\n", 1))


def run_validator(validator: str, root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "validators" / validator), "--repo-root", str(root)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "candidate"
        root.mkdir()
        build_candidate(root)

        for validator in ("validate-external-evidence.py", "validate-public-claims.py"):
            result = run_validator(validator, root)
            if result.returncode != 0:
                print(f"FAIL valid fixture rejected by {validator}:\n{result.stdout}")
                return 1
        print("PASS valid fixture accepted by both validators")

        flip_one_claim(root)

        result = run_validator("validate-external-evidence.py", root)
        if result.returncode != 0:
            print(f"FAIL evidence validator must be unaffected by claim status:\n{result.stdout}")
            return 1

        result = run_validator("validate-public-claims.py", root)
        out = result.stdout
        failures = [line for line in out.splitlines() if line.startswith("FAIL ")]
        if result.returncode == 0:
            print("FAIL PC-02b mutation was accepted — validator copy is stale or weakened")
            return 1
        if len(failures) != 1 or "contains a non-supported claim" not in failures[0]:
            print(f"FAIL expected exactly one PC-02b failure, got:\n{out}")
            return 1
        print("PASS mutated fixture held with exactly one PC-02b failure")

        # --- authority-surface manifest behaviour (code + policy/config closure) ---
        manifest = sweep.build_manifest(root, "fixture")

        problems = sweep.verify_authority_surface(root, manifest)
        if problems:
            print(f"FAIL untouched fixture must match its own manifest:\n{problems}")
            return 1
        print("PASS authority surface: untouched candidate accepted")

        (root / "docs").mkdir()
        (root / "docs" / "ordinary-note.md").write_text("ordinary governed data\n")
        claims = root / "claims" / "public-claims.yml"
        claims.write_text(claims.read_text() + CLAIM.format(n="0003").replace("status: supported", "status: supported"))
        problems = sweep.verify_authority_surface(root, manifest)
        if problems:
            print(f"FAIL ordinary governed data must change freely:\n{problems}")
            return 1
        print("PASS authority surface: ordinary governed data still allowed")

        (root / ".github" / "workflows" / "fixture.yml").write_text(WORKFLOW.replace("echo fixture", "run: true"))
        problems = sweep.verify_authority_surface(root, manifest)
        if not any("differs from manifest" in p and ".github/workflows/fixture.yml" in p for p in problems):
            print(f"FAIL tampered judge code not detected:\n{problems}")
            return 1
        print("PASS authority surface: tampered workflow rejected")

        (root / "scripts" / "extra.py").write_text("# unmanifested\n")
        problems = sweep.verify_authority_surface(root, manifest)
        if not any("unmanifested authority file: scripts/extra.py" in p for p in problems):
            print(f"FAIL unmanifested judge code not detected:\n{problems}")
            return 1
        print("PASS authority surface: unmanifested judge code rejected")

        (root / "scripts" / "judge.py").unlink()
        problems = sweep.verify_authority_surface(root, manifest)
        if not any("missing authority file: scripts/judge.py" in p for p in problems):
            print(f"FAIL deleted judge code not detected:\n{problems}")
            return 1
        print("PASS authority surface: deleted judge code rejected")

        (root / "policies" / "repository-ownership.yml").write_text(
            "runtime_source_extensions: []\n"
        )
        problems = sweep.verify_authority_surface(root, manifest)
        if not any("differs from manifest" in p and "policies/repository-ownership.yml" in p for p in problems):
            print(f"FAIL tampered judge policy not detected:\n{problems}")
            return 1
        print("PASS authority surface: tampered policy rejected")

        (root / "schemas" / "external-validation.schema.json").write_text('{"weakened": true}\n')
        problems = sweep.verify_authority_surface(root, manifest)
        if not any("differs from manifest" in p and "schemas/external-validation.schema.json" in p for p in problems):
            print(f"FAIL tampered normative schema not detected:\n{problems}")
            return 1
        print("PASS authority surface: tampered schema rejected")

        (root / "golden-path" / "versions.lock.json").write_text('{"tampered": true}\n')
        problems = sweep.verify_authority_surface(root, manifest)
        if not any("differs from manifest" in p and "golden-path/versions.lock.json" in p for p in problems):
            print(f"FAIL tampered exact authority file not detected:\n{problems}")
            return 1
        print("PASS authority surface: tampered versions.lock rejected")

    # --- checked-in manifest contract (P0c): the REAL artifact is tested,
    # not only synthetic manifests built by build_manifest() ---
    import copy
    import json

    real = json.loads((ROOT / "authority-manifest.json").read_text())
    problems = sweep.validate_manifest_contract(real)
    if problems:
        print(f"FAIL checked-in manifest violates its contract:\n{problems}")
        return 1
    print("PASS real authority-manifest.json satisfies the runtime contract")

    mutated = copy.deepcopy(real)
    mutated["authority_surface"]["prefixes"] = [
        p for p in mutated["authority_surface"]["prefixes"] if p != "policies/"
    ]
    problems = sweep.validate_manifest_contract(mutated)
    if not any("prefixes" in p for p in problems):
        print(f"FAIL dropped required prefix not caught:\n{problems}")
        return 1
    print("PASS contract: dropped required prefix rejected")

    mutated = copy.deepcopy(real)
    del mutated["authority_surface"]["files"]["golden-path/versions.lock.json"]
    problems = sweep.validate_manifest_contract(mutated)
    if not any("exact_files declares" in p for p in problems):
        print(f"FAIL unpinned exact file (exact_files kept) not caught:\n{problems}")
        return 1
    print("PASS contract: unpinned exact_files entry rejected")

    mutated = copy.deepcopy(real)
    del mutated["authority_surface"]["files"]["golden-path/versions.lock.json"]
    mutated["authority_surface"]["exact_files"] = [
        e for e in mutated["authority_surface"]["exact_files"]
        if e != "golden-path/versions.lock.json"
    ]
    problems = sweep.validate_manifest_contract(mutated)
    # rejected either as an exact_files list mismatch or as a files entry
    # outside the authority surface — both are contract violations.
    if not problems or not any(
        "outside the authority surface" in p or "exact_files must be exactly" in p
        for p in problems
    ):
        print(f"FAIL fully-dropped exact file not caught:\n{problems}")
        return 1
    print("PASS contract: fully-dropped exact file rejected (outside surface)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
