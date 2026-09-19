# governance-runner

Out-of-band governance authority runner for the WasmAgent organization.

This repository is the trust root for the `governance-root-authority` check
on `WasmAgent/.github`: it holds the trusted validators, the GitHub App
credential (secrets only — the App itself is installed on `WasmAgent/.github`
with Checks RW / Contents R / Pull requests R), and the sweeper that reviews
open governance PRs. Candidate repositories are treated ONLY as data — their
code is never executed, and their validators are never used to judge them.

See `validators/PROVENANCE.md` for the authority import record and
`docs` in `WasmAgent/.github` (ADR-governance-root-authority) for the design.

## Layout

```text
.github/workflows/governance-root.yml   sweeper (schedule + dispatch; secret-bearing)
.github/workflows/self-test.yml         no-secret self-test on every push/PR
scripts/sweep.py                        sweep + publish App-bound check runs
scripts/self-test.py                    fixture test of the vendored authority
validators/                             the authoritative validators
requirements.txt                        hash-locked validator dependencies
```

## Trust boundary

```text
owner account (telleroutlook) + governance-runner/main + App private key
        = the judge
candidate PR content = the judged (data only)
```

## Authority surface manifest

`authority-manifest.json` pins the SHA-256 of every file in the candidate's
judge code (`.github/workflows/**` and `scripts/**`) at an accepted
`WasmAgent/.github` revision. Before any claim/evidence validation, the
sweeper verifies the candidate's authority surface against the manifest:
tampered, deleted, or unmanifested judge code ⇒ `governance-root-authority`
= HOLD. This closes the "self-neutering workflow" hole: a candidate can no
longer weaken its own required checks and still pass the root check.

### Judge-code upgrade runbook (two-phase, pin-first)

1. Review the proposed `.github` workflow/scripts change.
2. Land the NEW expected hashes in `governance-runner` first:
   ```bash
   python scripts/build-manifest.py /path/to/reviewed/.github-checkout \
       --source-commit <reviewed-sha> --output authority-manifest.json
   ```
   open a PR here (self-test will run), merge.
3. Then merge the `.github` change. PRs based on the older main will HOLD
   until they rebase — fail closed by design; the window is short.
