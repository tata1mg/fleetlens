"""`build-symbols` — write a repo's symbol index to .context/_symbols.json.

Deterministic, no AI. Run at index time (before diff-based regeneration) so the next
run can tell exactly which symbols changed. Exit codes: 0 ok, 2 usage/error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .indexer import build_index


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="build-symbols")
    ap.add_argument("repo", nargs="?", default=".", help="repo root to index (default: .)")
    ap.add_argument("-o", "--output", default=None,
                    help="output path (default: <repo>/.context/_symbols.json)")
    ap.add_argument("--stdout", action="store_true", help="write the index to stdout instead")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"build-symbols: not a directory: {repo}", file=sys.stderr)
        return 2
    try:
        index = build_index(repo)
    except ImportError:
        print("build-symbols: tree-sitter not installed — "
              "pip install 'fleetlens[symbols]'", file=sys.stderr)
        return 2

    payload = json.dumps(index, indent=2, ensure_ascii=False) + "\n"
    if args.stdout:
        sys.stdout.write(payload)
    else:
        out = Path(args.output) if args.output else repo / ".context" / "_symbols.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8")
        if not args.quiet:
            print(f"build-symbols: {index['symbol_count']} symbols "
                  f"in {index['file_count']} files -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
