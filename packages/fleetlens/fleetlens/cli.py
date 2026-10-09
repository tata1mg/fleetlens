"""`fl`, the one command for fleetlens.

    fl doctor     [repo]                                      # check the setup
    fl index      <repo>  [--db DB] [--slug NAME] [-l LANG]   # one repo
    fl index-all  <dir>   [--db DB] [-l LANG]                 # a folder of service repos
    fl serve              [--db DB] [--http]                  # run the MCP server (stdio, or HTTP for a team)

Deterministic core: no LLM, no API key. The DB is a single SQLite file (default
./fleetlens.db) shared across all indexed repos. That shared DB is what makes the graph
cross-repo.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from .export import render as render_graph
from .indexing import index_all, index_repo
from .resolve import resolve
from .stats import Stats
from .store.sqlite import SqliteStore


def _default_db() -> str:
    """Where the index lives when `--db` is not given.

    A laptop wants the file in the working directory. A deployment wants one directory it
    owns, so $FLEETLENS_HOME puts the index (and anything else fleetlens keeps) under it
    without every command line having to repeat the path. Repositories are addressed
    separately and can live anywhere: the only thing fleetlens writes into them is the
    per-repo .context/ directory.
    """
    home = os.environ.get("FLEETLENS_HOME", "").strip()
    return str(Path(home).expanduser() / "data" / "fleetlens.db") if home else "fleetlens.db"


_DEFAULT_DB = _default_db()


def _llm_args(p, *, with_embed: bool) -> None:
    p.add_argument("--provider", default="ollama", help="LLM provider: ollama | openai")
    p.add_argument("--model", default="qwen2.5-coder:7b", help="LLM model")
    if with_embed:
        p.add_argument("--embed-provider", default="ollama", dest="embed_provider")
        p.add_argument("--embed-model", default="bge-m3", dest="embed_model")
    p.add_argument("--base-url", default="", dest="base_url",
                   help="override endpoint (Ollama or OpenAI-compatible)")
    p.add_argument("--api-key-env", default="OPENAI_API_KEY", dest="api_key_env",
                   help="env var holding the API key for remote providers")
    p.add_argument("--pull", action="store_true", help="auto `ollama pull` any missing model")


def _build_llm(args, cmd: str, *, need_embed: bool):
    """Preflight (fail fast + loud, before touching the store) and build the LLM — and the
    embedder when `need_embed`. Returns (llm, embedder) or None after printing the error."""
    from .enrich import build_embeddings, build_llm
    from .enrich.providers import (
        ProviderError,
        ollama_has_model,
        ollama_installed_models,
    )

    api_key = os.environ.get(args.api_key_env, "") if args.api_key_env else ""
    embed_provider = getattr(args, "embed_provider", "ollama")
    embed_model = getattr(args, "embed_model", "")
    if args.provider == "ollama" or (need_embed and embed_provider == "ollama"):
        try:
            installed = ollama_installed_models(args.base_url or "http://localhost:11434")
        except ProviderError as exc:
            print(f"{cmd}: Ollama not reachable — {exc}\n"
                  "  start it (`ollama serve`) or use --provider openai.", file=sys.stderr)
            return None
        need = ([args.model] if args.provider == "ollama" else []) + \
               ([embed_model] if embed_provider == "ollama" and need_embed else [])
        missing = [m for m in need if not ollama_has_model(m, installed)]
        if missing and args.pull:
            import subprocess
            for m in missing:
                print(f"  pulling {m} …")
                subprocess.run(["ollama", "pull", m], check=False)
        elif missing:
            print(f"{cmd}: missing Ollama model(s). Run:", file=sys.stderr)
            for m in missing:
                print(f"  ollama pull {m}", file=sys.stderr)
            near = [i for i in installed if i.split(":")[0] in {m.split(":")[0] for m in missing}]
            if near:
                print(f"  or pick one you have: --model {'  |  --model '.join(near)}", file=sys.stderr)
            return None
    elif not api_key:
        print(f"{cmd}: no API key — set ${args.api_key_env} (or --api-key-env).", file=sys.stderr)
        return None

    llm = build_llm(args.provider, args.model, args.base_url, api_key)
    embedder = build_embeddings(embed_provider, embed_model, args.base_url, api_key) if need_embed else None
    return llm, embedder


def _gap_line(g: dict) -> str:
    return (f"{g['resolved']} resolved (+{g['interfaces']} interfaces, +{g['outbound']} outbound), "
            f"{g['unresolved']} unresolvable, {g['rejected']} rejected by grounding")


def _count_sources(repo: Path) -> int:
    """Roughly how much source the run had to read, for a files/second figure."""
    from .adapters._walk import iter_files
    return sum(1 for _ in iter_files(Path(repo), (".py", ".ts", ".tsx", ".rb", ".js")))


def _enrich_printer():
    """Progress for the enrichment tier, which is the longest-running thing fleetlens does.

    One LLM call per object and tens of thousands of objects on a real fleet, so a run that
    prints only at the end is indistinguishable from a hang for most of a day. On a terminal
    this rewrites one line; in a log it prints a line every batch, which is frequent enough
    to see movement and rare enough not to fill a disk.
    """
    tty = sys.stderr.isatty()
    width = 0
    last = {"i": -1, "at": 0.0}
    started = time.time()

    def render(_event: str, d: dict) -> None:
        nonlocal width
        i, n = d["i"], d["n"]
        rate = i / max(1e-9, time.time() - started)
        left = (n - i) / rate if rate else 0
        # "committed" and "summarised" are different numbers, and showing only the first
        # reads as nothing happening for the whole of the first batch: 64 completed LLM
        # calls reported as "0 enriched".
        pend = d.get("pending", 0)
        waiting = f" (+{pend} awaiting commit)" if pend else ""
        line = (f"  {d['kind']}: {i}/{n}  {d['done']} committed{waiting}"
                f"  {d['skipped']} unchanged  ~{left / 60:.0f} min left")
        if not tty:
            # Print the first one immediately, then on a timer rather than a count. A
            # count-based interval means the first line of a slow run is minutes away, and
            # a job that has printed nothing is indistinguishable from one that has hung.
            now = time.time()
            if last["i"] < 0 or now - last["at"] >= 30 or i == n:
                last.update(i=i, at=now)
                print(line, file=sys.stderr, flush=True)
            return
        pad = max(0, width - len(line))
        print("\r" + line + " " * pad, end="", file=sys.stderr, flush=True)
        width = len(line)

    def done() -> None:
        nonlocal width
        if tty and width:
            print("\r" + " " * width + "\r", end="", file=sys.stderr, flush=True)
            width = 0

    render.done = done
    return render


def _tag(i: int, n: int) -> str:
    """`[12/208] `, padded so the columns after it line up for the whole sweep."""
    return f"[{i:>{len(str(n))}}/{n}] " if n else ""


def _repo_row(s: dict, i: int = 0, n: int = 0) -> str:
    """One durable line for a repository that finished."""
    g = s.get("gaps")
    tail = (f"  {g['resolved']}/{s['skipped']} gaps filled" if g
            else f"  {s.get('skipped', 0):2} unresolved")
    if s.get("unreadable"):
        tail += f"  {s['unreadable']} unreadable"
    # A repo indexed without a call graph still carries interfaces and edges, so it is a
    # success, but silently reporting "0 symbols" would read as a parser failure.
    mark = "ok  " if s.get("call_graph") != "unavailable" else "part"
    if s.get("library"):
        # Marked apart so a sweep does not read as a service with no endpoints.
        mark, tail = "lib ", "  code only, not a mesh service"
    elif mark == "part":
        tail += "  (no call graph)"
    return (f"{_tag(i, n)}{mark}  {s['slug']:30} {s['nodes']:4} symbols  "
            f"{s['edges']:4} calls  {s['interfaces']:3} interfaces{tail}")


def _progress_printer():
    """Render indexing progress.

    Every repository that finishes prints its own line and keeps it, on a terminal and in a
    log file alike. An earlier version accumulated the results and printed the table after
    the sweep returned, which over a 200-repo fleet meant half an hour of one rewritten line
    with nothing to scroll back through, and nothing at all if the run was killed.

    Transient updates -- which repo is in flight, which phase it is in -- are a terminal
    affordance: they rewrite a single line on a tty and are dropped otherwise, so a log
    holds one line per repository and no control characters.
    """
    tty = sys.stderr.isatty()
    width = 0
    at = {"i": 0, "n": 0}

    def wipe() -> None:
        """Clear the transient line so a durable one can be printed over it."""
        nonlocal width
        if tty and width:
            print("\r" + " " * width + "\r", end="", file=sys.stderr, flush=True)
            width = 0

    def render(event: str, d: dict) -> None:
        nonlocal width
        if event == "repo":
            at.update(i=d["i"], n=d["n"])
            line = f"{_tag(d['i'], d['n'])}{d['slug']}"
        elif event == "phase":
            line = f"  {d['slug']}: {d['phase']}"
        elif event == "gap":
            line = f"  {d['slug']}: gap {d['i']}/{d['n']} ({d['resolved']} resolved)"
        elif event == "repo-done":
            wipe()
            # Results on stdout, as the end-of-run table was, so `fl index-all > repos.txt`
            # still collects them and the progress chatter still goes to the terminal.
            print(_repo_row(d, at["i"], at["n"]), flush=True)
            return
        elif event == "repo-failed":
            wipe()
            print(f"{_tag(at['i'], at['n'])}fail  {d['slug']:30} {d['error']}",
                  file=sys.stderr, flush=True)
            return
        else:
            return

        if not tty:
            return
        pad = max(0, width - len(line))
        print("\r" + line + " " * pad, end="", file=sys.stderr, flush=True)
        width = len(line)

    render.done = wipe
    return render


def _cmd_index(args) -> int:
    repo = Path(args.repo)
    if not repo.is_dir():
        print(f"fl index: not a directory: {repo}", file=sys.stderr)
        return 2
    llm = None
    if args.fill_gaps:
        built = _build_llm(args, "fl index", need_embed=False)
        if built is None:
            return 2
        llm = built[0]
    store = SqliteStore(args.db)
    try:
        prog = _progress_printer()
        stats = Stats() if getattr(args, "stats", False) else None
        if stats is not None:
            with stats.overall():
                results = index_repo(repo, store, slug=args.slug, language=args.language,
                                     llm=llm, progress=prog, stats=stats)
        else:
            results = index_repo(repo, store, slug=args.slug, language=args.language,
                                 llm=llm, progress=prog)
        prog.done()
    except RuntimeError as exc:  # ProviderError is a RuntimeError too
        print(f"fl index: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()
    for s in results:
        if s.get("guidance"):
            g = s["guidance"]
            print(f"fl index: {s['slug']} -> {args.db}  ({g['loaded']} guidance rules; "
                  f"declared in fleetlens.yaml, so no service node and no symbols)")
            for problem in g["problems"]:
                print(f"  {problem}", file=sys.stderr)
            continue
        if s.get("library"):
            print(f"fl index: {s['slug']} -> {args.db}  "
                  f"({s['nodes']} symbols, {s['edges']} calls; library, so no service "
                  f"node and no interfaces)")
            continue
        print(f"fl index: {s['slug']} -> {args.db}  "
              f"({s['nodes']} symbols, {s['edges']} calls, "
              f"{s['interfaces']} interfaces, {s['handled_by']} handled_by"
              + (f", {s['skipped']} unresolved sites" if s.get("skipped") else "")
              + (f", {s['unreadable']} UNREADABLE file(s)" if s.get("unreadable") else "")
              + (", NO call graph" if s.get("call_graph") == "unavailable" else "") + ")")
        if s.get("call_graph") == "unavailable":
            print(f"fl index: {s['slug']} indexed without a call graph; interfaces and "
                  f"cross-repo edges are present, callers/callees are not. "
                  f"`fl doctor {args.repo}` reports why.", file=sys.stderr)
        if s.get("gaps"):
            print(f"fl index: {s['slug']} gaps — {_gap_line(s['gaps'])}")
    if len(results) > 1:
        print(f"fl index: {len(results)} services from one repo (via manifest)")
    if stats is not None:
        print("\nfl index: where the time went\n")
        print(stats.render(files=_count_sources(repo)))
    return 0


def _cmd_index_all(args) -> int:
    base = Path(args.dir)
    if not base.is_dir():
        print(f"fl index-all: not a directory: {base}", file=sys.stderr)
        return 2
    llm = None
    if args.fill_gaps:
        built = _build_llm(args, "fl index-all", need_embed=False)
        if built is None:
            return 2
        llm = built[0]
    store = SqliteStore(args.db)
    try:
        prog = _progress_printer()
        res = index_all(base, store, language=args.language, llm=llm, progress=prog,
                        jobs=args.jobs)
        prog.done()
    finally:
        store.close()
    # Each repo printed its own line as it finished, so there is no table to print here.
    # cross-repo resolve needs the whole fleet's interfaces, so run it once at the end
    store = SqliteStore(args.db)
    try:
        r = resolve(store, store, store)
    finally:
        store.close()
    partial = sum(1 for s in res["ok"] if s.get("call_graph") == "unavailable")
    note = f", {partial} without a call graph" if partial else ""
    print(f"fl index-all: {len(res['ok'])}/{res['considered']} indexed into {args.db} "
          f"({len(res['failed'])} skipped{note}) | {r['edges']} cross-repo edges "
          f"({r['async_edges']} async)")
    _guidance_report(res.get("guidance"), "fl index-all")
    if partial:
        print(f"  {partial} repo(s) indexed without a call graph, so get_callers/get_callees "
              f"will be empty for them. Run `fl doctor <repo>` to see why.", file=sys.stderr)
    return 0


def _cmd_ingest_guidance(args) -> int:
    from .guidance import GuidanceError, ingest

    store = SqliteStore(args.db)
    try:
        r = ingest(store, args.directory)
    except GuidanceError as exc:
        print(f"fl ingest-guidance: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()

    if not r["roots"]:
        print(f"fl ingest-guidance: no rulebook under {args.directory}.\n"
              "  A repo holds rules when its fleetlens.yaml declares "
              "`guidance: [{path: ...}]`,\n"
              "  and a rule file declares `kind: guidance` in its frontmatter.",
              file=sys.stderr)
        return 1
    print(f"fl ingest-guidance: {r['loaded']} rules from "
          f"{len(r['roots'])} rulebook(s)"
          + (f", {r['removed']} withdrawn" if r["removed"] else "")
          + f" — {r['edges']} edges to {r['services']} services")
    for gid in r["ids"]:
        print(f"  {gid}")
    if r.get("violations"):
        print(f"fl ingest-guidance: {r['violations']} service(s) declare something a "
              f"mandatory rule forbids")
    # Printed last and to stderr, because a run that loaded fourteen rules and skipped one is
    # a success with a problem in it, not a failure, and the problem must not scroll away.
    if r["problems"]:
        print(f"\n{len(r['problems'])} file(s) skipped:", file=sys.stderr)
        for problem in r["problems"]:
            print(f"  {problem}", file=sys.stderr)
    return 0 if r["loaded"] or not r["problems"] else 1


def _guidance_report(g: dict, cmd: str) -> None:
    """One line for the rules a sweep picked up, and every problem on stderr."""
    if not g:
        return
    print(f"{cmd}: {g['loaded']} guidance rules from {len(g.get('roots', []) or [])} "
          f"rulebook(s)" + (f", {g['removed']} withdrawn" if g.get("removed") else "")
          + (f" — {g['edges']} edges to {g['services']} services"
             if g.get("edges") else ""))
    if g.get("violations"):
        print(f"{cmd}: {g['violations']} service(s) declare something a mandatory rule "
              f"forbids — `find_guidance_violations` has the detail")
    for problem in g.get("problems", []):
        print(f"  {problem}", file=sys.stderr)


def _cmd_resolve(args) -> int:
    store = SqliteStore(args.db)
    try:
        r = resolve(store, store, store)
    finally:
        store.close()
    print(f"fl resolve: {r['edges']} service->service edges, {r['async_edges']} of them async "
          f"({r['services']} services, {r['unresolved_calls']} outbound calls unresolved)")
    return 0


def _cmd_export_graph(args) -> int:
    store = SqliteStore(args.db)
    try:
        html = render_graph(store, store)
    finally:
        store.close()
    Path(args.output).write_text(html, encoding="utf-8")
    print(f"fl export-graph: wrote {args.output}")
    return 0


def _cmd_enrich(args) -> int:
    from .enrich import enrich, fill_gaps
    from .enrich.providers import ProviderError

    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
    summary_kinds = tuple(k for k in kinds if k not in ("gaps", "guidance"))
    built = _build_llm(args, "fl enrich",
                       need_embed=bool(summary_kinds) or "guidance" in kinds)
    if built is None:
        return 2
    llm, embedder = built

    store = SqliteStore(args.db)
    try:
        if "gaps" in kinds:
            prog = _progress_printer()
            g = fill_gaps(store, llm, only_slug=args.service or None, progress=prog)
            prog.done()
            print(f"fl enrich: gaps — {g['sites']} skipped sites: {_gap_line(g)}, "
                  f"{g['skipped']} already done")
            if g["outbound"] or g["interfaces"]:
                r = resolve(store, store, store)
                print(f"fl enrich: re-resolved fleet — {r['edges']} service->service edges "
                      f"({r['async_edges']} async)")
        if "guidance" in kinds:
            # No LLM call: a rule arrives already written by the person who meant it, so it
            # is embedded as authored rather than paraphrased.
            from .guidance import embed_guidance
            g = embed_guidance(store, embedder)
            print(f"fl enrich: guidance — {g['embedded']} embedded, "
                  f"{g['skipped']} unchanged ({g['model']})")
        if summary_kinds:
            eprog = _enrich_printer()
            try:
                r = enrich(store, llm, embedder, kinds=summary_kinds,
                           only_slug=args.service or "", progress=eprog,
                           jobs=max(1, args.jobs))
            except ProviderError as exc:
                # Hours of work may already be committed. A traceback buries both what was
                # achieved and what to do about it.
                eprog.done()
                print(f"\nfl enrich: stopped — {exc}", file=sys.stderr)
                print("  Work already committed is kept; re-running resumes from there.",
                      file=sys.stderr)
                return 2
            eprog.done()
            print(f"fl enrich: {r['generated']} enriched, {r['skipped']} unchanged "
                  f"(model={r['model']}, kinds={','.join(summary_kinds)})")
    finally:
        store.close()
    return 0


def _cmd_doctor(args) -> int:
    from .doctor import render, run
    rep = run(Path(args.repo) if args.repo else None, args.base_url or "http://localhost:11434")
    print(render(rep))
    return 1 if rep.failed else 0


def _guidance_instructions(ctx) -> str:
    """The line about engineering guidance, or nothing when none is ingested.

    Said here rather than left to the tool's own description, because an agent that does not
    know fleet-wide standards exist will not go looking for them: it will read the
    repositories and infer conventions, which during a migration means inferring the one
    being migrated away from.
    """
    try:
        rules = ctx.store.list_objects("guidance")
    except Exception:       # noqa: BLE001 - instructions must never fail a server start
        return ""
    if not rules:
        return ""
    return ("\n\nThis fleet also carries human-authored engineering standards: which "
            f"libraries, frameworks and patterns to use when writing code here ({len(rules)} "
            "rules). Call `get_engineering_guidance` BEFORE writing or reviewing code in "
            "these repositories. Conventions inferred by reading the fleet are the EXISTING "
            "conventions, which are not always the intended ones.")


def _cmd_serve(args) -> int:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        # Three different failures land here and they need three different fixes. Guessing
        # between them is worse than saying nothing: a message naming the wrong package
        # sends someone to reinstall something that was never the problem. ImportError
        # carries the module that actually failed, so use it rather than assume.
        import importlib.util
        failed = (exc.name or "").split(".")[0]
        if importlib.util.find_spec("mcp") is None:
            print("fl serve: MCP is not installed.\n"
                  "  pip install 'fleetlens[server]'", file=sys.stderr)
        elif failed and failed != "mcp":
            # mcp is here, but something it depends on is missing or too old. The classic
            # is pydantic v1 in the environment, where `TypeAdapter` does not exist.
            print(f"fl serve: MCP is installed, but importing it failed on '{failed}'.\n"
                  f"  {exc}\n"
                  f"  That is a dependency problem, not an MCP version problem. Try:\n"
                  f"    pip install -U '{failed}'      and then      pip check",
                  file=sys.stderr)
        else:
            print(f"fl serve: the installed MCP is not compatible.\n"
                  f"  {exc}\n"
                  "  fleetlens needs mcp>=1.2.0,<2.0; 2.0 removed mcp.server.fastmcp.\n"
                  "  pip install 'mcp>=1.2.0,<2.0'", file=sys.stderr)
        return 2
    from .server.app import build_context
    from .server.tools import register_all

    embedder = None
    if args.embed_model:  # semantic discovery is opt-in; needs the same model used to enrich
        from .enrich import build_embeddings
        key = os.environ.get(args.api_key_env, "") if args.api_key_env else ""
        embedder = build_embeddings(args.embed_provider, args.embed_model, args.base_url, key)

    ctx = build_context(args.db, embedder=embedder, read_only=args.http)
    # Server instructions are surfaced by clients even when individual tools are deferred
    # behind a tool-search step. Benchmarking showed the agent used fleetlens in only 9 of
    # 24 runs, and in every one of those 9 it had first searched for tools — so telling the
    # client what this server is FOR is the lever that decides whether it gets used at all.
    # Host and port go in at construction, not after. The SDK fixes its DNS-rebinding
    # policy from the host it is built with, so setting them later left a server bound to
    # 0.0.0.0 enforcing a loopback-only Host allowlist.
    server_kwargs: dict = {}
    if args.http:
        from .server.http import security_settings
        server_kwargs = {"host": args.host, "port": args.port,
                         "transport_security": security_settings(
                             args.host, [h for h in (args.allowed_host or []) if h])}
    mcp = FastMCP("fleetlens", **server_kwargs, instructions=(
        "Cross-repository microservice context for this codebase: the service dependency "
        "graph, every service's HTTP endpoints and message queues, and the call graph "
        "behind them — derived from source, not from a hand-written catalog.\n\n"
        "Reach for these tools whenever a question spans MORE THAN ONE repository or "
        "service, for example: which services call X; what breaks if X changes; which "
        "service is depended on most; which services are unused; who publishes or consumes "
        "a queue; what does service X expose; which code runs for endpoint Y.\n\n"
        "Start with `get_service_graph` — it returns every service and every dependency "
        "edge in ONE call and answers most fleet-wide questions outright. Use "
        "`list_interfaces` for one service's endpoints and queues, "
        "`get_endpoint_call_graph` to trace an endpoint into code, and "
        "`get_callers`/`get_callees` for blast radius.\n\n"
        "Prefer these over grepping the repositories: grep cannot see across repository "
        "boundaries, and a confident guess about a service boundary is indistinguishable "
        "from a fact. Every answer here carries evidence and a confidence label."
        + _guidance_instructions(ctx)))
    register_all(mcp, ctx)

    if args.http:
        import logging

        from .server.http import resolve_token, serve
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        token = resolve_token(args.token_env, args.insecure)
        serve(mcp, args.host, args.port, token, ctx.store)
        return 0

    mcp.run()  # stdio
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("index", help="index one repo into the store")
    p.add_argument("repo")
    p.add_argument("--db", default=_DEFAULT_DB)
    p.add_argument("--slug", default=None)
    p.add_argument("--language", "-l", default="auto")
    p.add_argument("--fill-gaps", action="store_true", dest="fill_gaps",
                   help="after indexing, LLM-resolve the sites the parsers could not (grounded)")
    p.add_argument("--stats", action="store_true",
                   help="report how long each indexing step took")
    _llm_args(p, with_embed=False)
    p.set_defaults(func=_cmd_index)

    p = sub.add_parser("index-all", help="index every service repo under a directory")
    p.add_argument("dir")
    p.add_argument("--db", default=_DEFAULT_DB)
    p.add_argument("--language", "-l", default="auto")
    p.add_argument("--fill-gaps", action="store_true", dest="fill_gaps",
                   help="after indexing each repo, LLM-resolve the sites the parsers could not")
    p.add_argument("--jobs", "-j", type=int, default=1, metavar="N",
                   help="index N repositories at once. Most of the time is spent waiting on "
                        "external indexer processes, so this scales well past the core count")
    _llm_args(p, with_embed=False)
    p.set_defaults(func=_cmd_index_all)

    p = sub.add_parser("ingest-guidance",
                       help="load human-authored engineering guidance from a directory "
                            "of Markdown rules")
    p.add_argument("directory", help="directory of Markdown rules, one rule per file")
    p.add_argument("--db", default=_DEFAULT_DB)
    p.set_defaults(func=_cmd_ingest_guidance)

    p = sub.add_parser("resolve", help="(re)compute cross-repo service->service edges")
    p.add_argument("--db", default=_DEFAULT_DB)
    p.set_defaults(func=_cmd_resolve)

    p = sub.add_parser("export-graph", help="render the service mesh to a self-contained HTML file")
    p.add_argument("--db", default=_DEFAULT_DB)
    p.add_argument("--output", "-o", default="fleetlens-graph.html")
    p.set_defaults(func=_cmd_export_graph)

    p = sub.add_parser("enrich", help="optional LLM tier: summaries + embeddings, or gap-filling (opt-in)")
    p.add_argument("--db", default=_DEFAULT_DB)
    _llm_args(p, with_embed=True)
    p.add_argument("--kinds", default="interface,service",
                   help="comma list of: interface, service (summaries+embeddings), "
                        "guidance (embed authored rules; no LLM), "
                        "gaps (LLM-resolve sites the parsers could not; grounded, no embeddings)")
    p.add_argument("--service", default="",
                   help="limit to one service slug, for every kind")
    p.add_argument("--jobs", "-j", type=int, default=1, metavar="N",
                   help="summarise N objects at once. For a remote provider most of the "
                        "time per object is a round trip, so this scales well; for a local "
                        "model one request already saturates the hardware, so leave it at 1")
    p.set_defaults(func=_cmd_enrich)

    p = sub.add_parser("doctor", help="check this machine can index, and that a repo can be")
    p.add_argument("repo", nargs="?", default=None,
                   help="optional repo to check for indexability")
    p.add_argument("--base-url", default="", dest="base_url")
    p.set_defaults(func=_cmd_doctor)

    p = sub.add_parser("serve", help="run the MCP server over the store (stdio, or --http)")
    p.add_argument("--db", default=_DEFAULT_DB)
    p.add_argument("--http", action="store_true",
                   help="serve over streamable-HTTP for a shared/remote deployment "
                        "instead of stdio. Opens the index read-only and requires a "
                        "shared bearer token.")
    p.add_argument("--host", default="127.0.0.1",
                   help="with --http: bind address (default loopback; use 0.0.0.0 to "
                        "accept connections from other machines)")
    p.add_argument("--port", type=int, default=8081, help="with --http: bind port")
    p.add_argument("--token-env", default="FLEETLENS_TOKEN", dest="token_env",
                   help="with --http: env var holding the shared bearer token")
    p.add_argument("--insecure", action="store_true",
                   help="with --http: serve with NO authentication. Local trials only.")
    p.add_argument("--allowed-host", action="append", default=[], metavar="HOST",
                   help="with --http: accept only these Host headers (repeatable). "
                        "Off by default when binding a non-loopback address, where the "
                        "bearer token is the control and Host validation guards nothing.")
    p.add_argument("--embed-model", default="", dest="embed_model",
                   help="enable semantic discover_* (use the SAME model you enriched with)")
    p.add_argument("--embed-provider", default="ollama", dest="embed_provider")
    p.add_argument("--base-url", default="", dest="base_url")
    p.add_argument("--api-key-env", default="OPENAI_API_KEY", dest="api_key_env")
    p.set_defaults(func=_cmd_serve)

    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
