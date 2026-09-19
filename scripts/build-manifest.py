#!/usr/bin/env python3
"""Build authority-manifest.json from a WasmAgent/.github checkout.

Operator tool (not used at sweep time): after a LEGITIMATE change to the
candidate's judge code (".github/workflows/**" or "scripts/**") has been
reviewed, run this against the reviewed tree and land the updated manifest
in governance-runner BEFORE merging the candidate change:

    judge code upgrade first -> candidate adoption second

Usage:
    python scripts/build-manifest.py /path/to/.github-checkout \
        [--source-commit <sha>] [--output authority-manifest.json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

SCHEMA_VERSION = 1
PREFIXES = (".github/workflows/", "scripts/")
SOURCE_REPOSITORY = "WasmAgent/.github"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def build_manifest(tree: Path, source_commit: str) -> dict:
    files: dict[str, str] = {}
    for prefix in PREFIXES:
        base = tree / prefix
        if not base.is_dir():
            raise SystemExit(f"missing authority directory in source tree: {prefix}")
        for path in sorted(base.rglob("*")):
            if path.is_file():
                rel = path.relative_to(tree).as_posix()
                files[rel] = sha256_file(path)
    if not files:
        raise SystemExit("authority surface is empty — refusing to build a manifest")
    return {
        "schema_version": SCHEMA_VERSION,
        "authority_surface": {
            "source_repository": SOURCE_REPOSITORY,
            "source_commit": source_commit,
            "prefixes": list(PREFIXES),
            "files": files,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tree", type=Path, help="checkout of WasmAgent/.github")
    parser.add_argument("--source-commit", default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    tree = args.tree.resolve()
    source_commit = args.source_commit
    if source_commit is None:
        source_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tree, capture_output=True, text=True, check=True
        ).stdout.strip()

    manifest = build_manifest(tree, source_commit)
    rendered = json.dumps(manifest, indent=2, sort_keys=True) + "\n"

    if args.output:
        args.output.write_text(rendered)
        print(f"wrote {args.output} ({len(manifest['authority_surface']['files'])} files, "
              f"source {source_commit[:12]})")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
