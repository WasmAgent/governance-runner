#!/usr/bin/env python3
"""Self-test for the vendored out-of-band authority.

Runs on every push/PR of governance-runner (no secrets involved) and proves
the vendored validators still execute and still enforce their invariants:

  1. a minimal conforming candidate passes BOTH validators;
  2. flipping one claim to the legal status `deprecated` while the fixture
     homepage still asserts "all `supported`" makes validate-public-claims
     fail with EXACTLY ONE failure — the PC-02b message.

The fixture is built programmatically so the test cannot drift from the
validator contract.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

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


def build_candidate(root: Path) -> None:
    (root / "claims").mkdir(parents=True)
    (root / "evidence").mkdir()
    (root / "profile").mkdir()

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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
