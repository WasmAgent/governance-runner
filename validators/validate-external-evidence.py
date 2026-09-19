#!/usr/bin/env python3
"""Validate evidence/external-validation.json (EXT-01..07).

The external-validation ledger is the machine-readable boundary between what
external parties actually observed and what WasmAgent is allowed to claim. This
validator enforces:

1.  EXT-01  every record satisfies the structural contract in
            schemas/external-validation.schema.json (required fields, enums,
            id format) — a stdlib subset check; the JSON Schema file is the
            normative document.
2.  EXT-02  every anchor URL is present and syntactically valid https.
3.  EXT-03  evidence_type formal_certification requires certifying_body,
            certificate_id, certificate_url, scope and valid_from. Organization
            affiliation (Linux Foundation, OWASP, ...) can never substitute for
            these fields.
4.  EXT-04  an LF-affiliated venue cannot carry evidence_type
            formal_certification without those explicit certificate fields.
5.  EXT-05  an OWASP upstream_contribution record must explicitly list
            endorsement/certification wording in prohibited_claims.
6.  EXT-06  records whose state is proposed / open / pending_merge must state
            merge-or-acceptance limitations — unlanded evidence cannot be
            presented as landed.
7.  EXT-07  a layer labeled author-produced (Mode B) must not be labeled
            independent in the same key.

Exit code 0 on success, 1 on any validation failure. Uses only the Python
standard library so it runs in any CI image with Python.
"""

from __future__ import annotations

import json
import os
import re
import sys
from urllib.parse import urlparse

DEFAULT_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER_PATH = os.path.join(DEFAULT_REPO_ROOT, "evidence", "external-validation.json")

EVIDENCE_TYPES = {
    "external_observation",
    "independent_layered_run",
    "independent_native_run",
    "upstream_contribution",
    "published_reference",
    "formal_certification",
}
STATES = {"proposed", "open", "pending_merge", "merged", "superseded", "withdrawn"}
UNLANDED_STATES = {"proposed", "open", "pending_merge"}
ID_PATTERN = re.compile(r"^EXT-[A-Z0-9]+-\d{4}$")
CERT_FIELDS = ("certifying_body", "certificate_id", "certificate_url", "scope", "valid_from")


class Failure(Exception):
    pass


def check(failures: list[str], ext_id: str, rule: str, ok: bool, detail: str) -> None:
    if not ok:
        failures.append(f"{rule} [{ext_id}]: {detail}")


def validate_anchor_urls(record: dict, failures: list[str]) -> None:
    anchors = record.get("anchors")
    ext_id = record.get("id", "<unknown>")
    check(failures, ext_id, "EXT-02", isinstance(anchors, dict) and bool(anchors),
          "anchors object with at least one URL is required")
    if not isinstance(anchors, dict):
        return
    for key, url in anchors.items():
        ok = isinstance(url, str) and url.startswith("https://")
        if ok:
            parsed = urlparse(url)
            ok = bool(parsed.netloc) and bool(parsed.path or parsed.query)
        check(failures, ext_id, "EXT-02", ok, f"anchors.{key} must be a valid https URL")


def validate_record(record: dict, failures: list[str]) -> None:
    ext_id = record.get("id", "<unknown>")
    check(failures, ext_id, "EXT-01", bool(ID_PATTERN.match(str(record.get("id", "")))),
          "id must match EXT-<SUBJECT>-NNNN")
    check(failures, ext_id, "EXT-01", record.get("evidence_type") in EVIDENCE_TYPES,
          f"evidence_type must be one of {sorted(EVIDENCE_TYPES)}")
    check(failures, ext_id, "EXT-01", record.get("state") in STATES,
          f"state must be one of {sorted(STATES)}")
    check(failures, ext_id, "EXT-01", isinstance(record.get("claim_ceiling"), str)
          and len(record.get("claim_ceiling", "")) >= 20,
          "claim_ceiling must be a non-trivial statement of the maximum allowed claim")
    check(failures, ext_id, "EXT-01", isinstance(record.get("prohibited_claims"), list)
          and len(record["prohibited_claims"]) >= 1,
          "prohibited_claims must list at least one forbidden claim label")
    validate_anchor_urls(record, failures)

    venue = record.get("venue") if isinstance(record.get("venue"), dict) else {}
    affiliation = str(venue.get("affiliation", "")).lower()
    evidence_type = record.get("evidence_type")
    has_cert_fields = all(isinstance(record.get(field), str) and record.get(field)
                          for field in CERT_FIELDS)

    if evidence_type == "formal_certification":
        check(failures, ext_id, "EXT-03", has_cert_fields,
              "formal_certification requires certifying_body, certificate_id, "
              "certificate_url, scope and valid_from")

    if ("linux foundation" in affiliation or affiliation.startswith("lf")) and evidence_type == "formal_certification":
        check(failures, ext_id, "EXT-04", has_cert_fields,
              "LF affiliation alone cannot produce certification wording")

    if evidence_type == "upstream_contribution" and "owasp" in str(venue.get("org", "")).lower():
        prohibited = [str(c).lower() for c in record.get("prohibited_claims", [])]
        check(failures, ext_id, "EXT-05",
              "owasp_endorsed" in prohibited or "owasp_certified" in prohibited,
              "OWASP contribution must prohibit endorsement/certification wording")

    if record.get("state") in UNLANDED_STATES:
        limitations = record.get("limitations") or []
        check(failures, ext_id, "EXT-06", len(limitations) >= 1,
              f"unlanded state '{record.get('state')}' requires explicit limitations")

    layers = record.get("layers")
    if isinstance(layers, dict):
        for key, value in layers.items():
            label = f"{key}={value}".lower()
            independent = "independent" in key or "independent" in str(value).lower()
            author_produced = "author_produced" in label or "author-produced" in label
            check(failures, ext_id, "EXT-07", not (independent and author_produced),
                  f"layer '{key}' cannot be both author-produced and independent")


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


def main() -> int:
    repo_root = repo_root_from_args(sys.argv[1:])
    ledger_path = os.path.join(repo_root, "evidence", "external-validation.json")
    failures: list[str] = []
    try:
        with open(ledger_path, encoding="utf-8") as handle:
            ledger = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        print(f"EXT-00 [ledger]: cannot parse {ledger_path}: {error}")
        return 1

    check(failures, "ledger", "EXT-01", ledger.get("schema_version") == 1,
          "schema_version must be 1")
    records = ledger.get("records")
    if not isinstance(records, list) or not records:
        print("EXT-01 [ledger]: records must be a non-empty array")
        return 1

    seen_ids: set[str] = set()
    for record in records:
        ext_id = str(record.get("id", "<unknown>"))
        check(failures, "ledger", "EXT-01", ext_id not in seen_ids,
              f"duplicate record id {ext_id}")
        seen_ids.add(ext_id)
        validate_record(record, failures)

    if failures:
        for failure in failures:
            print(f"FAIL {failure}")
        print(f"\nexternal-evidence validation: {len(failures)} failure(s)")
        return 1

    print(f"external-evidence validation: OK ({len(records)} record(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
