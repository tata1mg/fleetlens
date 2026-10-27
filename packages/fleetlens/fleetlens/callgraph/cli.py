"""`build-callgraph` — write a repo's static call graph to .context/callgraph.json.

Deterministic, no AI. Pipeline:

    1. produce a SCIP index for the repo (per-language indexer, e.g. `scip-python`)
    2. load the tree-sitter symbol index (_symbols.json; built inline if absent)
    3. join them into callgraph/v1 (see extractor.py)

Run at index time, after `build-symbols`. Exit codes: 0 ok, 2 usage/toolchain error.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

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
        # Optional for the same reason as scip-ruby, and because `fl doctor` has always
        # reported it that way: when it is absent, routes, outbound calls and messaging are
        # still read and only the call edges are missing. Failing the whole repo instead
        # would lose the other three for a tool the doctor called optional.
        "optional": True,
        # --infer-tsconfig lets it work even without a committed tsconfig.json.
        "argv": lambda repo, out, name: [
            "scip-typescript", "index", "--infer-tsconfig", "--output", str(out)
        ],
    },
}


def _repo_venv_bin(repo: Path) -> Optional[str]:
    """The repo's own virtualenv bin directory, if it has one.

    Same four names `fl doctor` reports on, so a repo the doctor calls ready is a repo the
    indexer can actually use.
    """
    for name in ("venv", ".venv", "env", ".env"):
        for sub in ("bin", "Scripts"):
            d = repo / name / sub
            if d.is_dir():
                return str(d)
    return None


def _uv_pip_shim(venv_bin: str, stack: contextlib.ExitStack) -> Optional[str]:
    """A directory holding a `pip3` that forwards to `uv pip`, or None if not needed.

    scip-python learns a project's dependencies by shelling out to pip, and it looks for
    `pip3` then `pip` on PATH. A virtualenv created by `uv venv` contains neither: uv
    replaces pip with `uv pip` and installs no shim of its own. The result is that a repo
    `fl doctor` calls ready (it has a `bin/python`) fails every index with "Could not find
    valid pip command", which is a growing problem rather than a shrinking one as uv
    spreads.

    Installing pip into someone's virtualenv to fix this would be fleetlens mutating an
    environment it does not own. Forwarding is cheaper and reversible: scip-python runs
    exactly two commands, `pip list --format=json` and `pip show -f <pkgs>`, and `uv pip`
    answers both in the same shapes. VIRTUAL_ENV, set by the caller, is what points uv at
    the right environment.
    """
    if any((Path(venv_bin) / n).exists() for n in ("pip", "pip3")):
        return None                       # an ordinary virtualenv; nothing to do
    uv = shutil.which("uv")
    if uv is None:
        return None                       # nothing to forward to; scip will say so
    d = stack.enter_context(tempfile.TemporaryDirectory(prefix="fleetlens-pip-"))
    shim = Path(d) / "pip3"
    shim.write_text(f'#!/bin/sh\nexec {shlex.quote(uv)} pip "$@"\n')
    shim.chmod(0o755)
    return d


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
    # ...and it finds that pip on PATH, so the repo's own virtualenv has to be on it.
    # Without this the indexer resolves against whichever environment happens to be first,
    # which is the wrong set of dependencies, or dies outright where there is no `pip` at
    # all: a sweep run with a deliberately minimal PATH failed on every repository in a
    # fleet with "Could not find valid pip command", before reading a line of source.
    # `fl doctor` already looks for these same four names per repo.
    venv_bin = _repo_venv_bin(repo)
    with contextlib.ExitStack() as stack:
        if venv_bin:
            env["PATH"] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"
            env.pop("PYTHONHOME", None)     # would override the venv we just selected
            env["VIRTUAL_ENV"] = str(Path(venv_bin).parent)
            shim = _uv_pip_shim(venv_bin, stack)
            if shim:
                env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"
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
                      "line_end": sym.get("line_end", 0),
                      "file_sha": sym.get("file_sha", "")})
    return {"schema": "callgraph/v1", "slug": slug, "nodes": nodes, "edges": [],
            "stats": {"nodes": len(nodes), "edges": 0, "documents": 0,
                      "note": "no SCIP indexer for this language; symbols only"}}


def _symbol_index(repo: Path, context_dir: Path, precomputed: Optional[str] = None) -> dict:
    """Spans for this repo, parsed now unless the caller names a file to reuse.

    This used to reuse `.context/_symbols.json` whenever it happened to exist, with no
    check that it still described the working tree. Nothing writes that file during a
    normal index, so it only appears when someone has run `fl-symbols` by hand, and from
    then on every index of that repo silently reported spans frozen at the moment they did.
    The index kept recording the current commit beside them, so the drift was invisible and
    grew. Reuse is now something a caller asks for and owns.
    """
    if precomputed:
        return load_symbol_index(Path(precomputed))
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
    ap.add_argument("--symbols", default=None,
                    help="reuse a prebuilt symbol index instead of parsing the repo; "
                         "only correct if it describes the current working tree")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"build-callgraph: not a directory: {repo}", file=sys.stderr)
        return 2
    slug = args.slug or repo.name
    context_dir = repo / ".context"

    # Any run that gets this far owns the output. Clearing it first means a failure leaves
    # no call graph rather than the last successful one: `_load_service` loads whatever is
    # in `.context/` without consulting the exit status, so a stale file here becomes spans
    # in a fresh index, attached to today's commit.
    default_out = Path(args.output) if args.output else context_dir / "callgraph.json"
    if not args.stdout:
        default_out.unlink(missing_ok=True)

    # Symbols come from tree-sitter and owe the SCIP indexer nothing, so this failing is
    # the only reason to give up entirely.
    try:
        symbol_index = _symbol_index(repo, context_dir, args.symbols)
    except RuntimeError as exc:
        print(f"build-callgraph: {exc}", file=sys.stderr)
        return 2

    degraded = False
    try:
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
        # The indexer ran and failed, which is different from it being absent and is
        # usually an environment problem: a missing virtualenv, a broken toolchain. Losing
        # the call edges is unavoidable; losing the symbols with them is not, and it used
        # to happen invisibly because the previous run's file stayed on disk and was
        # loaded in their place. Write what we do have, and report the failure through the
        # exit status so the run still says the call graph is unavailable.
        print(f"build-callgraph: {exc}", file=sys.stderr)
        cg = _symbols_only(symbol_index, slug)
        degraded = True

    payload = json.dumps(cg, indent=2, ensure_ascii=False) + "\n"
    if args.stdout:
        sys.stdout.write(payload)
    else:
        out = default_out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8")
        if not args.quiet:
            s = cg["stats"]
            print(f"build-callgraph: {s['nodes']} nodes, {s['edges']} edges "
                  f"({s['documents']} docs) -> {out}")
    return 2 if degraded else 0


if __name__ == "__main__":
    raise SystemExit(main())
