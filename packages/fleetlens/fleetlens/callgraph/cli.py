"""`build-callgraph` — write a repo's static call graph to .context/callgraph.json.

Deterministic, no AI. Pipeline:

    1. produce a SCIP index for the repo (per-language indexer, e.g. `scip-python`)
    2. load the tree-sitter symbol index (_symbols.json; built inline if absent)
    3. join them into callgraph/v1 (see extractor.py)

Run at index time, after `build-symbols`. Exit codes: 0 ok, 2 usage/toolchain error.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .extractor import build_callgraph, load_symbol_index

# Per-language SCIP indexer invocation. Only the tool name + how to point it at an output
# file; extend as scip-typescript / scip-ruby are brought online.
_INDEXERS = {
    "python": {
        "tool": "scip-python",
        "argv": lambda repo, out, name: [
            "scip-python", "index", "--project-name", name, "--output", str(out)
        ],
    },
    "ruby": {
        "tool": "scip-ruby",
        # scip-ruby needs a Sorbet setup, which most Rails apps do not have. When it is
        # absent the call graph is skipped and everything else (routes, outbound calls,
        # messaging) still works; see _run_indexer.
        "optional": True,
        "argv": lambda repo, out, name: [
            "scip-ruby", "--index-file", str(out), "--gem-metadata", f"{name}@0.0.1"
        ],
    },
    "typescript": {
        "tool": "scip-typescript",
        # --infer-tsconfig lets it work even without a committed tsconfig.json.
        "argv": lambda repo, out, name: [
            "scip-typescript", "index", "--infer-tsconfig", "--output", str(out)
        ],
    },
}


class OptionalIndexerMissing(RuntimeError):
    """Raised when a language's SCIP indexer is absent but the language is still usable."""


def _run_indexer(language: str, repo: Path, out: Path, name: str) -> None:
    spec = _INDEXERS.get(language)
    if spec is None:
        raise RuntimeError(f"no SCIP indexer configured for language {language!r}")
    if shutil.which(spec["tool"]) is None:
        if spec.get("optional"):
            # No indexer for this language on this machine. Say so and carry on: interfaces
            # and cross-repo edges do not need the call graph, and a service in the graph
            # without a call graph is far more useful than one that is absent.
            raise OptionalIndexerMissing(spec["tool"])
        raise RuntimeError(
            f"{spec['tool']} not found on PATH, install it "
            f"(e.g. `npm install -g @sourcegraph/{spec['tool']}`)"
        )
    argv = spec["argv"](repo, out, name)
    # scip-python JSON.parses `pip list` from the target repo's venv; pip's "new release
    # available" notice lands in that output and breaks the parse.
    env = {**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    proc = subprocess.run(argv, cwd=repo, capture_output=True, text=True, env=env)
    if proc.returncode != 0 or not out.exists():
        # These indexers fail with a long Node stack trace. Printing it inline once is
        # merely unhelpful; printing it for every repo in a fleet sweep buries the progress
        # output and the summary underneath it. Keep the whole thing on disk and surface
        # one line, so the common case stays readable and the detail is still there.
        detail = (proc.stderr or proc.stdout or "").strip()
        where = ""
        try:
            # Next to the repo's own .context/, not beside `out`: `out` is usually a
            # temporary SCIP file that is deleted on the way out, taking the log with it.
            ctx = repo / ".context"
            ctx.mkdir(parents=True, exist_ok=True)
            log = ctx / f"{spec['tool']}-error.log"
            log.write_text(detail or "(no output)")
            where = f"; full output in {log}"
        except OSError:
            pass
        first = next((ln.strip() for ln in detail.splitlines() if ln.strip()), "no output")
        raise RuntimeError(
            f"{spec['tool']} failed (exit {proc.returncode}): {first[:160]}{where}")


def _symbols_only(symbol_index: dict, slug: str) -> dict:
    """A callgraph/v1 payload with nodes but no edges, for languages with no SCIP indexer."""
    nodes = []
    for sid, sym in (symbol_index.get("symbols") or {}).items():
        nodes.append({"id": sid, "path": sym.get("path", ""),
                      "qualname": sid.rsplit("::", 1)[-1],
                      "kind": sym.get("kind", "function"), "lang": sym.get("lang", ""),
                      "line_start": sym.get("line_start", 0),
                      "line_end": sym.get("line_end", 0)})
    return {"schema": "callgraph/v1", "slug": slug, "nodes": nodes, "edges": [],
            "stats": {"nodes": len(nodes), "edges": 0, "documents": 0,
                      "note": "no SCIP indexer for this language; symbols only"}}


def _symbol_index(repo: Path, context_dir: Path) -> dict:
    existing = context_dir / "_symbols.json"
    if existing.exists():
        return load_symbol_index(existing)
    # Build inline so the call graph is usable standalone (needs the `symbols` extra).
    try:
        from ..symbols.indexer import build_index
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "tree-sitter not installed — pip install 'fleetlens[callgraph]'"
        ) from exc
    return build_index(repo)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="build-callgraph")
    ap.add_argument("repo", nargs="?", default=".", help="repo root to index (default: .)")
    ap.add_argument("-l", "--language", default="python",
                    help="source language for the SCIP indexer (default: python)")
    ap.add_argument("--slug", default=None,
                    help="repository slug (default: repo folder name)")
    ap.add_argument("--scip", default=None,
                    help="use an existing .scip index instead of running the indexer")
    ap.add_argument("-o", "--output", default=None,
                    help="output path (default: <repo>/.context/callgraph.json)")
    ap.add_argument("--stdout", action="store_true", help="write to stdout instead")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"build-callgraph: not a directory: {repo}", file=sys.stderr)
        return 2
    slug = args.slug or repo.name
    context_dir = repo / ".context"

    try:
        symbol_index = _symbol_index(repo, context_dir)
        if args.scip:
            scip_path = Path(args.scip)
            if not scip_path.exists():
                print(f"build-callgraph: --scip not found: {scip_path}", file=sys.stderr)
                return 2
            cg = build_callgraph(scip_path, symbol_index, slug)
        else:
            with tempfile.TemporaryDirectory() as tmp:
                scip_path = Path(tmp) / f"{slug}.scip"
                try:
                    _run_indexer(args.language, repo, scip_path, slug)
                    cg = build_callgraph(scip_path, symbol_index, slug)
                except OptionalIndexerMissing as exc:
                    # symbols still index; only the call edges are unavailable
                    if not args.quiet:
                        print(f"build-callgraph: {exc} not installed, "
                              f"indexing {args.language} without a call graph",
                              file=sys.stderr)
                    cg = _symbols_only(symbol_index, slug)
    except RuntimeError as exc:
        print(f"build-callgraph: {exc}", file=sys.stderr)
        return 2

    payload = json.dumps(cg, indent=2, ensure_ascii=False) + "\n"
    if args.stdout:
        sys.stdout.write(payload)
    else:
        out = Path(args.output) if args.output else context_dir / "callgraph.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8")
        if not args.quiet:
            s = cg["stats"]
            print(f"build-callgraph: {s['nodes']} nodes, {s['edges']} edges "
                  f"({s['documents']} docs) -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
