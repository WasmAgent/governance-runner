#!/usr/bin/env python3
"""Build authority-manifest.json from a WasmAgent/.github COMMIT.

Operator tool (not used at sweep time): after a LEGITIMATE change to the
candidate's judge CODE (".github/workflows/**", "scripts/**") or judge
POLICY/CONFIG ("policies/**", "schemas/**",
"golden-path/versions.lock.json", "claims/claim-overreach-allowlist.json")
has been reviewed, run this against the REVIEWED COMMIT and land the
updated manifest in governance-runner BEFORE merging the candidate change:

    judge code upgrade first -> candidate adoption second

The tool hashes the IMMUTABLE GIT TREE of --source-commit (via
`git ls-tree -r` + `git show <sha>:<path>`) — never the working tree — so a
wrong checkout, a dirty tree, or a hand-copied source_commit cannot poison
the manifest: source_commit is by construction the tree that was hashed.

Usage:
    python scripts/build-manifest.py /path/to/.github-repo \
        --source-commit <reviewed-sha> [--output authority-manifest.json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

SCHEMA_VERSION = 2
PREFIXES = (".github/workflows/", "scripts/", "policies/", "schemas/")
EXACT_FILES = (
    "golden-path/versions.lock.json",
    "claims/claim-overreach-allowlist.json",
)
SOURCE_REPOSITORY = "WasmAgent/.github"


def _git(repo: Path, *args: str, binary: bool = False):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"git {' '.join(args)} failed:\n{result.stderr.decode(errors='replace')}"
        )
    return result.stdout if binary else result.stdout.decode().strip()


def sha256_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def build_manifest_from_git(repo: Path, source_commit: str) -> dict:
    # source_commit must be a real commit in this repository — the metadata
    # can never refer to a tree other than the one actually hashed.
    _git(repo, "cat-file", "-e", f"{source_commit}^{{commit}}")

    listing = _git(repo, "ls-tree", "-r", "--name-only", "-z", source_commit, binary=True)
    tree_paths = [p.decode("utf-8") for p in listing.split(b"\0") if p]

    files: dict[str, str] = {}
    for prefix in PREFIXES:
        matched = sorted(p for p in tree_paths if p.startswith(prefix))
        if not matched:
            raise SystemExit(f"missing authority directory in commit tree: {prefix}")
        for rel in matched:
            files[rel] = sha256_bytes(_git(repo, "show", f"{source_commit}:{rel}", binary=True))
    for rel in EXACT_FILES:
        if rel not in tree_paths:
            raise SystemExit(f"exact authority file missing from commit tree: {rel}")
        files[rel] = sha256_bytes(_git(repo, "show", f"{source_commit}:{rel}", binary=True))

    if not files:
        raise SystemExit("authority surface is empty — refusing to build a manifest")
    return {
        "schema_version": SCHEMA_VERSION,
        "authority_surface": {
            "source_repository": SOURCE_REPOSITORY,
            "source_commit": source_commit,
            "prefixes": list(PREFIXES),
            "exact_files": list(EXACT_FILES),
            "files": files,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tree", type=Path, help="checkout (any) of WasmAgent/.github — only its .git is used")
    parser.add_argument("--source-commit", required=True,
                        help="reviewed commit whose immutable tree is hashed")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    tree = args.tree.resolve()
    source_commit = args.source_commit
    if source_commit is None:
        source_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tree, capture_output=True, text=True, check=True
        ).stdout.strip()

    manifest = build_manifest_from_git(tree, source_commit)
    rendered = json.dumps(manifest, indent=2, sort_keys=True) + "\n"

    if args.output:
        args.output.write_text(rendered)
        print(f"wrote {args.output} ({len(manifest['authority_surface']['files'])} files, "
              f"source {source_commit[:12]} — hashed from the immutable git tree)")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
