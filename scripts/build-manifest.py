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
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sweep import build_manifest_from_git  # noqa: E402  single shared implementation

SCHEMA_VERSION = 2
PREFIXES = (".github/workflows/", "scripts/", "policies/", "schemas/")
EXACT_FILES = (
    "golden-path/versions.lock.json",
    "claims/claim-overreach-allowlist.json",
)
SOURCE_REPOSITORY = "WasmAgent/.github"


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
