#!/usr/bin/env python3
"""Validate claims/public-claims.yml and guard against claim overreach.

1.  Claims schema: schema_version 1, unique ids, known claim_class, and
    status restricted to the status enum (supported / deprecated /
    withdrawn).
2.  Class/ref coherence: externally_observed, independently_reproduced and
    formally_certified require `external_evidence_refs` pointing at existing
    records in evidence/external-validation.json whose evidence_type matches
    the class; internal_supported forbids external refs.
    - externally_observed     <- any external evidence record
    - independently_reproduced <- evidence_type independent_layered_run /
                                  independent_native_run
    - formally_certified      <- evidence_type formal_certification
3.  Overreach wording guard: forbidden endorsement/certification phrases are
    rejected in claims/, docs/, profile/, evidence/ and README/ORG files of
    this repository unless allowlisted in claims/claim-overreach-allowlist.json
    (each allowlist entry must carry approved_evidence — an empty
    justification is itself a failure).
4.  Profile consistency guard (PC-01..03): profile/README.md is the public
    trust surface for this registry. It must link the registry through its
    canonical URL (PC-01), show a claim counter equal to the TOTAL number
    of claims (PC-02a), and show the registry's last_reviewed date
    verbatim (PC-03) — the homepage cannot silently drift from the
    ledger. When the homepage asserts "all `supported`", that must be
    true: every claim status is then required to be `supported`
    (PC-02b), so all-supported wording can never outlive a
    non-supported claim.

Exit 0 on success, 1 on any failure. Requires PyYAML (installed in CI).
"""

from __future__ import annotations

import json
import os
import re
import sys

import yaml

DEFAULT_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_DIRS = ("claims", "docs", "profile", "evidence", "media", "releases")
SCAN_FILES = ("README.md", "ORG-FOCUS-2026Q3.md")

CERT_FIELDS = ("certifying_body", "certificate_id", "certificate_url", "scope", "valid_from")


def repo_root_from_args(argv: list[str]) -> str:
    """--repo-root lets a TRUSTED (pinned) copy of this validator inspect a
    candidate checkout's data without executing candidate code."""
    root = DEFAULT_REPO_ROOT
    i = 0
    while i < len(argv):
        if argv[i] == "--repo-root":
            root = os.path.abspath(argv[i + 1])
            i += 2
        else:
            i += 1
    return root
SCAN_SUFFIXES = (".md", ".yml", ".yaml", ".json")

CLASSES = {"internal_supported", "externally_observed", "independently_reproduced", "formally_certified"}
EXTERNAL_CLASSES = {"externally_observed", "independently_reproduced", "formally_certified"}
VALID_STATUSES = {"supported", "deprecated", "withdrawn"}
CLASS_TO_EVIDENCE = {
    "externally_observed": None,  # any external record
    "independently_reproduced": {"independent_layered_run", "independent_native_run"},
    "formally_certified": {"formal_certification"},
}

FORBIDDEN_PATTERNS = [
    r"certified\s+by\s+(the\s+)?linux\s+foundation",
    r"linux\s+foundation[-\s]certified",
    r"owasp[-\s]certified",
    r"owasp[-\s]endorsed",
    r"industry[-\s]standard\s+(certified|compliant|validated|endorsed)",
]


def load_json(path: str):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def iter_repo_files(repo_root: str):
    for directory in SCAN_DIRS:
        base = os.path.join(repo_root, directory)
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            for name in files:
                if name.endswith(SCAN_SUFFIXES):
                    yield os.path.join(root, name)
    for name in SCAN_FILES:
        path = os.path.join(repo_root, name)
        if os.path.isfile(path):
            yield path


def main() -> int:
    repo_root = repo_root_from_args(sys.argv[1:])
    claims_path = os.path.join(repo_root, "claims", "public-claims.yml")
    evidence_path = os.path.join(repo_root, "evidence", "external-validation.json")
    allowlist_path = os.path.join(repo_root, "claims", "claim-overreach-allowlist.json")
    failures: list[str] = []
    try:
        registry = yaml.safe_load(open(claims_path, encoding="utf-8"))
    except Exception as error:  # noqa: BLE001 - report and fail closed
        print(f"claims: cannot parse {claims_path}: {error}")
        return 1

    if registry.get("schema_version") != 1:
        failures.append("claims: schema_version must be 1")
    claims = registry.get("claims") or []
    if not claims:
        failures.append("claims: claims array must not be empty")

    evidence_ids: dict[str, dict] = {}
    if os.path.isfile(evidence_path):
        for record in load_json(evidence_path).get("records", []):
            evidence_ids[record.get("id")] = record

    seen: set[str] = set()
    for claim in claims:
        cid = str(claim.get("id", "<unknown>"))
        if cid in seen:
            failures.append(f"{cid}: duplicate claim id")
        seen.add(cid)

        claim_class = claim.get("claim_class", "internal_supported")
        if claim_class not in CLASSES:
            failures.append(f"{cid}: unknown claim_class '{claim_class}'")
        status = claim.get("status")
        if status not in VALID_STATUSES:
            failures.append(
                f"{cid}: unknown status '{status}' (must be one of {sorted(VALID_STATUSES)})"
            )
        refs = claim.get("external_evidence_refs") or []

        if claim_class == "internal_supported" and refs:
            failures.append(f"{cid}: internal_supported must not carry external_evidence_refs")
        if claim_class in EXTERNAL_CLASSES and not refs:
            failures.append(f"{cid}: claim_class '{claim_class}' requires external_evidence_refs")

        for ref in refs:
            record = evidence_ids.get(ref)
            if record is None:
                failures.append(f"{cid}: external_evidence_refs entry '{ref}' not found in the ledger")
                continue
            allowed = CLASS_TO_EVIDENCE.get(claim_class)
            if allowed is not None and record.get("evidence_type") not in allowed:
                failures.append(
                    f"{cid}: evidence '{ref}' has evidence_type "
                    f"'{record.get('evidence_type')}' which cannot support claim_class '{claim_class}'"
                )
            if record.get("state") not in ("merged", None) and claim_class in EXTERNAL_CLASSES:
                # unlanded evidence may support externally_observed framing only
                failures.append(
                    f"{cid}: evidence '{ref}' is state '{record.get('state')}' — unlanded "
                    "evidence cannot support externally_observed/independently_reproduced/"
                    "formally_certified claims"
                )

    # --- profile consistency guard (PC-01..03) ---
    # The org homepage (profile/README.md) is the public trust surface for
    # this registry. Its claim counter and registry review date must always
    # match the live registry, and the registry must be reachable through its
    # canonical URL — otherwise the homepage silently drifts from the ledger.
    profile_path = os.path.join(repo_root, "profile", "README.md")
    if not os.path.isfile(profile_path):
        failures.append("profile: profile/README.md not found — the claims trust surface is missing")
    else:
        with open(profile_path, encoding="utf-8") as handle:
            profile_norm = " ".join(handle.read().split())
        # PC-02a: the displayed counter is the TOTAL number of claims —
        # not the number of supported ones. Those coincide today, but the
        # homepage copy ("N public claims — all `supported`") makes two
        # distinct claims and each is checked separately below.
        if not re.search(rf"(?<!\d){len(claims)} public claims", profile_norm):
            failures.append(
                f"profile: claim counter must show {len(claims)} public claims "
                "(profile/README.md drifted from claims/public-claims.yml)"
            )
        # PC-02b: asserting "all `supported`" must be true at the moment of
        # assertion — a non-supported claim invalidates the wording.
        if "all `supported`" in profile_norm and any(
            claim.get("status") != "supported" for claim in claims
        ):
            failures.append(
                "profile: homepage claims 'all `supported`' but the registry "
                "contains a non-supported claim"
            )
        last_reviewed = registry.get("last_reviewed")
        # PyYAML parses an unquoted YYYY-MM-DD as datetime.date, not str.
        if hasattr(last_reviewed, "isoformat") and not isinstance(last_reviewed, str):
            last_reviewed = last_reviewed.isoformat()
        if not isinstance(last_reviewed, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", last_reviewed):
            failures.append("profile: registry last_reviewed must be a YYYY-MM-DD date string")
        elif f"Registry last reviewed **{last_reviewed}**" not in profile_norm:
            failures.append(
                f"profile: homepage must show 'Registry last reviewed **{last_reviewed}**' "
                "(profile/README.md drifted from claims/public-claims.yml)"
            )
        canonical_registry_url = "https://github.com/WasmAgent/.github/blob/main/claims/public-claims.yml"
        if canonical_registry_url not in profile_norm:
            failures.append("profile: homepage must link the canonical claims registry URL")

    # --- overreach wording guard ---
    # Allowlist entries are exceptions to the certification/endorsement
    # wording ban and therefore require REAL certification evidence:
    # approved_evidence must be a structured external-validation record id
    # whose record is a MERGED formal_certification with complete certificate
    # fields. Anything else (free text, a non-certification record, an
    # unlanded record) fails — the ledger currently contains no
    # formal_certification record, so any allowlist entry fails today.
    allowlist_entries = load_json(allowlist_path).get("allowlist", []) if os.path.isfile(allowlist_path) else []
    # HARD DISABLE: the ledger's formal_certification records are candidate
    # DATA — self-declared. Until the first certification has been verified
    # against primary sources (manual verification -> new trusted validator
    # rule -> trusted pin bump), the allowlist must remain EMPTY. The
    # per-entry checks below are kept for that future, but nothing passes
    # today.
    if allowlist_entries:
        failures.append(
            "allowlist: certification-allowlist entries are HARD-DISABLED — the ledger "
            "contains no independently verified formal certification, and self-declared "
            "candidate records cannot open the wording ban; requires manual primary-source "
            "verification plus a trusted validator rule and pin bump"
        )
    for entry in allowlist_entries:
        label = f"'{entry.get('phrase')}' in {entry.get('file')}"
        evidence_id = entry.get("approved_evidence")
        record = evidence_ids.get(evidence_id) if isinstance(evidence_id, str) else None
        if record is None:
            failures.append(
                f"allowlist: entry for {label} has approved_evidence {evidence_id!r} which is "
                "not a record in evidence/external-validation.json — allowlist entries "
                "require a structured certification-grade evidence id"
            )
            continue
        if record.get("evidence_type") != "formal_certification":
            failures.append(
                f"allowlist: entry for {label} cites {evidence_id} with evidence_type "
                f"'{record.get('evidence_type')}' — only formal_certification records can "
                "back a wording exception"
            )
            continue
        if record.get("state") != "merged":
            failures.append(
                f"allowlist: entry for {label} cites {evidence_id} with state "
                f"'{record.get('state')}' — only merged certification records can "
                "back a wording exception"
            )
            continue
        missing_fields = [f for f in CERT_FIELDS if not record.get(f)]
        if missing_fields:
            failures.append(
                f"allowlist: entry for {label} cites {evidence_id} which lacks "
                f"certificate field(s): {', '.join(missing_fields)}"
            )

    allowed_hits = {(e.get("file"), e.get("phrase", "").lower()) for e in allowlist_entries}

    patterns = [re.compile(pattern, re.IGNORECASE) for pattern in FORBIDDEN_PATTERNS]
    for path in iter_repo_files(repo_root):
        rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
        try:
            text = open(path, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        for pattern in patterns:
            for match in pattern.finditer(text):
                phrase = match.group(0).lower()
                if (rel, phrase) in allowed_hits or any(
                    file_part == rel and phrase_part in phrase for file_part, phrase_part in allowed_hits
                ):
                    print(f"ALLOWED (allowlisted): {rel}: '{match.group(0)}'")
                    continue
                failures.append(
                    f"{rel}: overreach phrase '{match.group(0)}' — certification/endorsement "
                    "wording requires an allowlist entry with approved_evidence"
                )

    if failures:
        for failure in failures:
            print(f"FAIL {failure}")
        print(f"\npublic-claims validation: {len(failures)} failure(s)")
        return 1

    print(f"public-claims validation: OK ({len(claims)} claim(s), {len(evidence_ids)} evidence record(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
