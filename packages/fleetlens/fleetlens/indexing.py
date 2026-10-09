"""Indexing orchestration — build a repo's call graph and load it into a store.

Shared by `fl index` (one repo) and `fl index-all` (a directory of service repos → one DB).
Deterministic, no LLM. Multi-repo is resilient: one repo failing never aborts the sweep.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .adapters import registry as iface_registry
from .adapters.deps import declared_dependencies
from .adapters.hosts import config_hosts, service_identity
from .adapters.outbound import discover_outbound as _py_outbound
from .adapters.ruby_outbound import discover_outbound as _rb_outbound
from .adapters.ts_outbound import discover_outbound as _ts_outbound
from .callgraph import cli as cg_cli
from .loaders import callgraph as cg_loader
from .loaders import interfaces as iface_loader
from .manifest import ServiceSpec, resolve_services
from .store.models import KnowledgeObject
from .store.sqlite import SqliteStore

# Immediate subdirs never treated as a service repo.
_SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".context", ".idea", ".vscode"}


def detect_language(repo: Path) -> str:
    """Best-effort primary language, used to pick the SCIP indexer."""
    repo = Path(repo)
    if (repo / "Gemfile").exists() or (repo / "config" / "routes.rb").exists():
        return "ruby"
    if (repo / "tsconfig.json").exists():
        return "typescript"
    if (repo / "package.json").exists() and _has(repo, "*.ts"):
        return "typescript"
    return "python"


@contextmanager
def _noop_step(_label: str):
    yield


def _has(repo: Path, pattern: str) -> bool:
    for p in repo.rglob(pattern):
        if "node_modules" not in p.parts and ".venv" not in p.parts:
            return True
    return False


def index_repo(repo: Path, store: SqliteStore, *, slug: Optional[str] = None,
               language: str = "auto", llm=None, progress=None, stats=None) -> list[dict]:
    """Index every service a repo declares (via fleetlens.yaml), or the repo itself as one
    service by default. Returns one summary per service.

    With `llm`, sites the parsers could not resolve are filled right after each service is
    indexed (see enrich.gaps) — so a re-index never silently drops them.
    Raises on toolchain/usage failure (caller decides whether to continue).

    `progress`, if given, is called as progress(event, detail) while work happens. Indexing
    a fleet takes minutes and gap filling can take hours, so a caller that cannot say what
    is happening looks indistinguishable from one that has hung.
    """
    repo = Path(repo).resolve()
    specs = resolve_services(repo, default_name=slug)
    return [index_service(repo, spec, store, default_language=language, llm=llm,
                          progress=progress, stats=stats) for spec in specs]


def _extract_service(repo: Path, spec: ServiceSpec, *, default_language: str = "auto",
                     progress=None, stats=None) -> dict:
    """Everything that can be worked out from source, touching no store.

    Split out so a fleet sweep can run this for several repositories at once. It is where
    effectively all the time goes, and it is subprocess- and file-bound rather than holding
    the GIL. Loading the result into the store stays on one thread, because the store is a
    single SQLite connection and making it concurrent would buy nothing: loading is
    milliseconds against tens of seconds of extraction.
    """
    repo = Path(repo).resolve()
    root = (repo / spec.path).resolve()
    if not root.is_dir():
        raise RuntimeError(f"service '{spec.name}': path not found: {spec.path}")
    slug = spec.name
    language = spec.language if spec.language != "auto" else default_language
    if language == "auto":
        language = detect_language(root)
    _say = progress or (lambda *a: None)
    tick = stats.step if stats is not None else _noop_step

    # The SCIP indexer is an external process and, on a Python service, two thirds of the
    # wall clock. Nothing our own adapters do depends on it: interfaces, outbound calls and
    # config hosts are read straight from source. So start it first and parse alongside it
    # rather than after it. Being a subprocess, it holds no GIL, so a thread is enough.
    _say("phase", {"slug": slug, "phase": "call graph (in background) + interfaces"})
    with ThreadPoolExecutor(max_workers=1) as pool:
        with tick("call graph (external indexer, overlapped)"):
            cg_future = pool.submit(
                cg_cli.main, [str(root), "--slug", slug, "--language", language, "--quiet"])

            with tick("interfaces (adapters)"):
                if not spec.library:
                    iface_registry.build_interfaces(root, slug)

            # A service node carrying its outbound HTTP calls — the consumer side the fleet
            # resolver later joins against every service's interfaces.
            _say("phase", {"slug": slug, "phase": "outbound calls"})
            seen, calls, skipped_out = set(), [], []
            with tick("outbound calls"):
                for c in (_py_outbound(root, skipped_out) + _ts_outbound(root, skipped_out)
                          + _rb_outbound(root, skipped_out)):  # language-agnostic merge
                    if (c.verb, c.path) not in seen:
                        seen.add((c.verb, c.path))
                        calls.append(c)
            outbound = [{"verb": c.verb, "path": c.path, "host": c.host, "evidence": c.evidence}
                        for c in calls]
            # Service addresses this repo declares in config. Generic twelve-factor
            # convention; which service each address denotes is decided by the resolvers.
            # Declared dependencies, for matching a service against authored guidance. Read
            # from the manifest rather than from import sites: a manifest names packages in a
            # global namespace, and some ecosystems barely import their dependencies by name.
            with tick("declared dependencies"):
                dependencies = declared_dependencies(root)

            with tick("config hosts + identity"):
                host_bindings = [{"key": b.key, "host": b.host, "port": b.port,
                                  "value": b.value, "file": b.file} for b in config_hosts(root)]
                identity = service_identity(root)   # the port/name peers address this by

            with tick("waiting on the external indexer"):
                call_graph_ok = cg_future.result() == 0

    # Sites the adapters saw but could not resolve — the only input the LLM gap-filler
    # (`fl enrich --kinds gaps`) works from. Kept with the repo root so it can ground answers.
    sk_file = root / ".context" / "skipped.json"
    skipped = (json.loads(sk_file.read_text()).get("sites", []) if sk_file.exists() else []) \
        + [asdict(sk) for sk in skipped_out]
    return {"slug": slug, "root": root, "spec": spec, "outbound": outbound,
            "host_bindings": host_bindings, "identity": identity, "skipped": skipped,
            "dependencies": dependencies, "call_graph_ok": call_graph_ok}


def _load_service(x: dict, store: SqliteStore, *, llm=None, progress=None, stats=None) -> dict:
    """Write one extracted service into the store. Serial by design; milliseconds."""
    tick = stats.step if stats is not None else _noop_step
    slug, root, spec = x["slug"], x["root"], x["spec"]
    # Interfaces load before the call graph so their nodes exist when the call-graph loader
    # attaches handled_by edges (interface -> handler = the endpoint trace).
    with tick("interfaces (load to store)"):
        iface_summary = ({"interfaces": 0} if spec.library
                         else iface_loader.load(root / ".context", slug, store))
    with tick("call graph (load to store)"):
        summary = cg_loader.load(root / ".context", slug, store, store)

    if spec.library:
        # Its code is in the index and `find_symbol` reaches it, but it is not a node in the
        # mesh: nothing calls a shared package over the network, and listing it as a service
        # would inflate the service count and make it read as an endpoint-less service.
        store.commit()
        summary.update(slug=slug, library=True, interfaces=0, outbound=0,
                       host_bindings=0, skipped=0,
                       call_graph="ok" if x["call_graph_ok"] else "unavailable")
        return summary

    store.upsert_object(KnowledgeObject(
        object_type="service", object_id=slug, name=slug, summary=None, version="unknown",
        source="static", generation_strategy="index", last_generated_at=None, embed_text=None,
        payload={"symbol_count": summary["nodes"],
                 "interface_count": iface_summary.get("interfaces", 0),
                 "outbound": x["outbound"], "host_bindings": x["host_bindings"],
                 "declared_hosts": dict(spec.hosts or {}), "identity": x["identity"],
                 "root": str(root), "skipped": x["skipped"],
                 "dependencies": x.get("dependencies", [])}))
    store.commit()

    summary["slug"] = slug
    summary["call_graph"] = "ok" if x["call_graph_ok"] else "unavailable"
    summary["interfaces"] = iface_summary.get("interfaces", 0)
    summary["outbound"] = len(x["outbound"])
    summary["host_bindings"] = len(x["host_bindings"])
    # Files nobody could parse are counted apart from unresolved sites. An unresolved site
    # is a route we saw and could not finish reading; an unreadable file is a route we may
    # never have seen at all, which bounds every other number in the summary.
    unreadable = [s for s in x["skipped"] if s.get("reason") == "unparseable-file"]
    summary["unreadable"] = len(unreadable)
    summary["skipped"] = len(x["skipped"]) - len(unreadable)
    if llm is not None and x["skipped"]:
        from .enrich.gaps import fill_gaps
        if progress:
            progress("phase", {"slug": slug, "phase": "filling gaps"})
        g = fill_gaps(store, llm, only_slug=slug, progress=progress)
        summary["gaps"] = g
        summary["interfaces"] += g["interfaces"]
        summary["handled_by"] += g["handled_by"]
    return summary


def index_service(repo: Path, spec: ServiceSpec, store: SqliteStore, *,
                  default_language: str = "auto", llm=None, progress=None, stats=None) -> dict:
    """Index one declared service (scoped to spec.path within repo) into the store.

    `stats`, if given, is a Stats collector that records how long each step took."""
    x = _extract_service(repo, spec, default_language=default_language,
                         progress=progress, stats=stats)
    return _load_service(x, store, llm=llm, progress=progress, stats=stats)


def _looks_like_repo(d: Path) -> bool:
    if not d.is_dir() or d.name.startswith(".") or d.name in _SKIP:
        return False
    # any source we can index, or a VCS/manifest marker. Every supported language needs a
    # marker here: Ruby support was added without one, so a Rails app with no .git
    # directory was silently passed over by a fleet sweep.
    for marker in (".git", "pyproject.toml", "setup.py", "requirements.txt", "package.json",
                   "Gemfile", "Gemfile.lock", "config/routes.rb"):
        if (d / marker).exists():
            return True
    return any(d.rglob("*.py")) or any(d.rglob("*.ts")) or any(d.rglob("*.rb"))


def index_all(base_dir: Path, store: SqliteStore, *, language: str = "auto", llm=None,
              progress=None, jobs: int = 1) -> dict:
    """Index every service repo directly under `base_dir` into one shared store.

    Resilient: a repo that fails to index is recorded and skipped; the sweep continues.
    Returns {ok: [...summaries], failed: [(slug, error)...]}.

    With `jobs` above 1, extraction runs for several repositories at once and the results
    are loaded into the store one at a time. Extraction is almost all of the work and is
    spent waiting on external indexer processes, so this is close to free; loading is
    milliseconds and the store is one SQLite connection, so it stays on this thread.

    Results are loaded, and reported, in completion order. Load order does not affect the
    store, because interface ids are assigned during extraction and every row is keyed by
    id; see the comment on the drain loop for why draining in repository order was worse.
    """
    base_dir = Path(base_dir).resolve()
    repos = sorted(d for d in base_dir.iterdir() if _looks_like_repo(d))
    ok, failed = [], []

    def extract(repo: Path) -> tuple:
        """(repo, extracted-services, error). Runs on a worker; touches no store."""
        try:
            specs = resolve_services(repo.resolve(), default_name=None)
            return repo, [_extract_service(repo, spec, default_language=language,
                                           progress=progress) for spec in specs], None
        except Exception as exc:  # noqa: BLE001 - one repo must not abort the fleet sweep
            return repo, [], exc

    def absorb(repo: Path, extracted: list, err) -> None:
        if err is not None:
            failed.append((repo.name, str(err)))
            if progress:
                progress("repo-failed", {"slug": repo.name, "error": str(err)})
            return
        try:
            rs = [_load_service(x, store, llm=llm, progress=progress) for x in extracted]
        except Exception as exc:  # noqa: BLE001
            failed.append((repo.name, str(exc)))
            if progress:
                progress("repo-failed", {"slug": repo.name, "error": str(exc)})
            return
        ok.extend(rs)
        if progress:
            for r in rs:
                progress("repo-done", r)

    if jobs <= 1:
        for i, repo in enumerate(repos, 1):
            if progress:
                progress("repo", {"i": i, "n": len(repos), "slug": repo.name})
            absorb(*extract(repo))
        return {"ok": ok, "failed": failed, "considered": len(repos)}

    done = 0
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(extract, r): r for r in repos}
        # Loaded as they finish, not in repository order. An earlier version drained in
        # order so the store was written deterministically, which held every result behind
        # the slowest early repository: with 200 repos the database stayed empty well past
        # the halfway mark and the pending results accumulated in memory. Load order does
        # not affect the result anyway, because interface ids are assigned during extraction
        # and every row is keyed by id, so ordering bought nothing for that cost.
        for fut in as_completed(futures):
            repo, extracted, err = fut.result()
            done += 1
            if progress:
                progress("repo", {"i": done, "n": len(repos), "slug": repo.name})
            absorb(repo, extracted, err)

    # Summaries in repository order, so the printed report reads the same either way.
    ok.sort(key=lambda s: s["slug"])
    failed.sort()
    return {"ok": ok, "failed": failed, "considered": len(repos)}
