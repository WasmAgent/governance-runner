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
WasmAgent organization owners
+ governance-runner admins (2026-09-19: telleroutlook, tellerlin, HainingYin)
+ App private key (single secret, stored only here)
        = the judge
candidate PR content = the judged (data only)
```

Mirrors the trust assumptions in
`WasmAgent/.github/docs/adr/ADR-governance-root-authority.md`.

## Authority surface manifest (code + policy/config closure)

`authority-manifest.json` (schema v2) pins the SHA-256 of the candidate's
judge CODE (`.github/workflows/**`, `scripts/**`) AND its judge
POLICY/CONFIG dependency closure (`policies/**`, `schemas/**`,
`golden-path/versions.lock.json`, `claims/claim-overreach-allowlist.json`)
at an accepted `WasmAgent/.github` revision. Before any claim/evidence
validation, the sweeper verifies the candidate's authority surface against
the manifest: tampered, deleted, or unmanifested authority files ⇒
`governance-root-authority` = HOLD. This closes both the "self-neutering
workflow" hole and the "weaken the policy the checker reads" hole
(e.g. `runtime_source_extensions: []`), while ordinary governed data
(`docs/`, `evidence/`, `profile/`, and most of `claims/`) remains free
to change — except explicitly manifested authority inputs such as
`claims/claim-overreach-allowlist.json`.

### Judge-code upgrade runbook (two-phase, pin-first)

1. Review the proposed `.github` workflow/scripts/policy/schema change.
2. Land the NEW expected hashes in `governance-runner` first:
   ```bash
   python scripts/build-manifest.py /path/to/.github-repo \
       --source-commit <reviewed-sha> --output authority-manifest.json
   ```
   The builder hashes the IMMUTABLE GIT TREE of `<reviewed-sha>` (`git
   ls-tree`/`git show`) — never the working tree — so a wrong checkout, a
   dirty tree, or a mismatched source_commit cannot poison the manifest:
   the metadata is by construction the tree that was hashed. Open a PR here
   (self-test will run, including the real-manifest contract check), merge.
3. Then merge the `.github` change. PRs based on the older main will HOLD
   until they rebase — fail closed by design; the window is short.

At sweep start the runner validates the checked-in manifest's contract
(schema v2, required prefixes/exact_files, exact ⊆ files, paths within the
surface, sha256 format, 40-hex source_commit); a violation fails the whole
sweep closed and every open PR visibly HOLDs. The self-test additionally
proves SOURCE BINDING: the checked-in manifest is rebuilt from its
`source_commit`'s immutable git tree and must match exactly — a
structurally-valid hash swap (source_commit kept) fails the binding.

## Authority epoch (P0d)

The check context carries the manifest's source_commit:

```text
governance-root-authority/<manifest-source-commit[:7]>
```

Any authority upgrade (any manifest change) therefore automatically changes
the required context name: verdicts produced by an older authority can
never satisfy the new epoch, closing the "old green check survives an
authority upgrade" race. The one manual step per upgrade is the protection
flip in `WasmAgent/.github` main protection — replace
`governance-root-authority/<old>` with `governance-root-authority/<new>`
(both app_id-bound to the Governance App) after the runner PR merges and
before relying on the new authority.
