"""Indexing behaviour that users depend on when their toolchain is incomplete."""
from __future__ import annotations

from fleetlens.store.sqlite import SqliteStore


def test_call_graph_failure_still_yields_interfaces(tmp_path, monkeypatch):
    """A repo whose call graph cannot be built must still be indexed.

    scip-python fails on a Python repo with no virtualenv, which is the common case on a
    first run. `fl doctor` tells users they can "index anyway to get interfaces and queues
    without a call graph", and for a while the indexer raised instead, dropping the whole
    repo including interfaces it had already proved.
    """
    from fleetlens import indexing

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n\n"
        "@app.get('/orders')\n"
        "def list_orders():\n"
        "    return []\n")

    monkeypatch.setattr(indexing.cg_cli, "main", lambda *a, **kw: 2)  # toolchain failure

    store = SqliteStore(":memory:")
    results = indexing.index_repo(tmp_path, store, slug="orders")

    assert len(results) == 1
    s = results[0]
    assert s["call_graph"] == "unavailable"
    assert s["nodes"] == 0                      # no symbols, as expected
    assert s["interfaces"] >= 1                 # but the route survived
    assert store.list_objects("service")        # and the service exists at all


def test_progress_is_reported_per_repo_and_phase(tmp_path, monkeypatch):
    """Indexing a fleet takes minutes and gap filling can take hours. Without progress,
    a working run is indistinguishable from a hung one."""
    from fleetlens import indexing

    for name in ("alpha", "beta"):
        d = tmp_path / name / "app"
        d.mkdir(parents=True)
        (d / "main.py").write_text(
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n\n"
            f"@app.get('/{name}')\n"
            "def h():\n    return []\n")

    monkeypatch.setattr(indexing.cg_cli, "main", lambda *a, **kw: 0)

    events = []
    indexing.index_all(tmp_path, SqliteStore(":memory:"),
                       progress=lambda e, d: events.append((e, d)))

    repos = [d for e, d in events if e == "repo"]
    assert [r["slug"] for r in repos] == ["alpha", "beta"]
    assert repos[0]["i"] == 1 and repos[0]["n"] == 2       # countable, so it reads as N/M
    phases = " | ".join(d["phase"] for e, d in events if e == "phase")
    assert "call graph" in phases and "interfaces" in phases and "outbound calls" in phases
    assert [d["slug"] for e, d in events if e == "repo-done"] == ["alpha", "beta"]


def test_a_rails_repo_without_git_is_still_found(tmp_path):
    """Every supported language needs a marker in the repo sniffer. Ruby was added without
    one, so a Rails app that happened not to be a git checkout was skipped in silence."""
    from fleetlens.indexing import _looks_like_repo

    rails = tmp_path / "billing"
    (rails / "config").mkdir(parents=True)
    (rails / "Gemfile").write_text("source 'https://rubygems.org'\n")
    (rails / "config" / "routes.rb").write_text(
        "Rails.application.routes.draw do\n  get '/billing', to: 'billing#index'\nend\n")
    assert _looks_like_repo(rails)

    bare = tmp_path / "lib_only"
    bare.mkdir()
    (bare / "thing.rb").write_text("class Thing; end\n")
    assert _looks_like_repo(bare)


def _fixture_fleet(tmp_path, n=6):
    for i in range(n):
        d = tmp_path / f"svc{i:02d}" / "app"
        d.mkdir(parents=True)
        (d / "main.py").write_text(
            "import requests\n"
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n\n"
            f"@app.get('/svc{i:02d}/items')\n"
            "def items():\n"
            f"    return requests.get('http://svc{(i + 1) % n:02d}/svc{(i + 1) % n:02d}/items')\n")
        (d.parent / "config.json").write_text(
            f'{{"SVC{(i + 1) % n:02d}_HOST": "svc{(i + 1) % n:02d}", "PORT": "80{i}0"}}')
    return tmp_path


def _snapshot(store):
    objs = [(o.id, o.object_type, o.name, sorted(o.payload.items(), key=str))
            for t in ("service", "interface", "code_symbol")
            for o in sorted(store.list_objects(t), key=lambda o: o.id)]
    return objs


def test_parallel_and_sequential_sweeps_agree(tmp_path, monkeypatch):
    """A fleet sweep with workers must produce the store a serial one would.

    Interface ids are assigned per repo in discovery order, and the index is something
    people diff between runs, so "the order probably does not matter" is not good enough.
    """
    from fleetlens import indexing

    _fixture_fleet(tmp_path)
    monkeypatch.setattr(indexing.cg_cli, "main", lambda *a, **kw: 0)

    serial_store = SqliteStore(":memory:")
    serial = indexing.index_all(tmp_path, serial_store, jobs=1)

    parallel_store = SqliteStore(":memory:")
    parallel = indexing.index_all(tmp_path, parallel_store, jobs=4)

    assert serial["considered"] == parallel["considered"] == 6
    assert not serial["failed"] and not parallel["failed"]
    # summaries arrive in repository order either way
    assert [s["slug"] for s in serial["ok"]] == [s["slug"] for s in parallel["ok"]]
    assert _snapshot(serial_store) == _snapshot(parallel_store)


def test_a_failing_repo_does_not_sink_a_parallel_sweep(tmp_path, monkeypatch):
    from fleetlens import indexing

    _fixture_fleet(tmp_path, n=4)
    monkeypatch.setattr(indexing.cg_cli, "main", lambda *a, **kw: 0)

    real = indexing._extract_service

    def explode(repo, spec, **kw):
        if spec.name == "svc02":
            raise RuntimeError("toolchain exploded")
        return real(repo, spec, **kw)

    monkeypatch.setattr(indexing, "_extract_service", explode)
    res = indexing.index_all(tmp_path, SqliteStore(":memory:"), jobs=3)

    assert [n for n, _ in res["failed"]] == ["svc02"]
    assert sorted(s["slug"] for s in res["ok"]) == ["svc00", "svc01", "svc03"]


def test_a_slow_early_repo_does_not_hold_back_everyone_else(tmp_path, monkeypatch):
    """Results are loaded as they finish, not in repository order.

    An earlier version drained in order so the store was written deterministically. That
    held every result behind the slowest alphabetically-early repository: on a 200 repo
    sweep the database stayed empty well past the halfway mark, and the pending results
    piled up in memory. Ordering bought nothing, because ids are assigned during extraction.
    """
    import threading

    from fleetlens import indexing

    _fixture_fleet(tmp_path, n=4)
    monkeypatch.setattr(indexing.cg_cli, "main", lambda *a, **kw: 0)

    released = threading.Event()
    real = indexing._extract_service

    def slow_first(repo, spec, **kw):
        if spec.name == "svc00":                 # sorts first, finishes last
            assert released.wait(timeout=10), "later repos never loaded"
        return real(repo, spec, **kw)

    loaded: list = []
    real_load = indexing._load_service

    def watch(x, store, **kw):
        out = real_load(x, store, **kw)
        loaded.append(x["slug"])
        if len(loaded) == 3:                     # the other three got through
            released.set()
        return out

    monkeypatch.setattr(indexing, "_extract_service", slow_first)
    monkeypatch.setattr(indexing, "_load_service", watch)

    res = indexing.index_all(tmp_path, SqliteStore(":memory:"), jobs=4)

    assert loaded[-1] == "svc00"                 # the blocker loaded last, not first
    assert sorted(loaded) == ["svc00", "svc01", "svc02", "svc03"]
    # the printed report is still in repository order whatever order they finished in
    assert [s["slug"] for s in res["ok"]] == ["svc00", "svc01", "svc02", "svc03"]


def test_each_repo_prints_a_line_as_it_finishes(capsys):
    """A fleet sweep takes half an hour. Its output has to be readable while it runs, and
    has to survive the run being killed, so a repository reports when it finishes rather
    than being held back for a table printed at the end."""
    from fleetlens.cli import _progress_printer

    prog = _progress_printer()                 # capsys makes stderr a pipe, so: not a tty
    prog("repo", {"i": 1, "n": 2, "slug": "orders"})
    prog("phase", {"slug": "orders", "phase": "call graph"})
    prog("repo-done", {"slug": "orders", "nodes": 90, "edges": 40, "interfaces": 7,
                       "skipped": 0})
    mid = capsys.readouterr()

    # printed before the second repo is touched, not after the sweep
    assert "orders" in mid.out
    assert "[1/2]" in mid.out and "90 symbols" in mid.out
    assert "\r" not in mid.out and "\r" not in mid.err   # no control bytes in a log file
    assert "call graph" not in mid.out                   # transient phase is tty-only

    prog("repo-failed", {"slug": "billing", "error": "no manifest"})
    end = capsys.readouterr()
    assert "no manifest" in end.err and end.out == ""    # failures on stderr, as before


def test_a_library_is_indexed_as_a_library_not_a_service(tmp_path, monkeypatch):
    """A library gets an object of its own type, so enrichment and semantic search can
    reach it, while every reading of the service graph still leaves it out."""
    from fleetlens import indexing

    (tmp_path / "fleetlens.yaml").write_text('libraries:\n  - path: "."\n')
    (tmp_path / "retry.py").write_text("def retry_with_backoff():\n    pass\n")
    monkeypatch.setattr(indexing.cg_cli, "main", lambda *a, **kw: 0)

    store = SqliteStore(":memory:")
    indexing.index_repo(tmp_path, store)

    assert store.list_objects("service") == []
    libs = store.list_objects("library")
    assert [lib.object_id for lib in libs] == [tmp_path.name]
    assert libs[0].payload["root"] == str(tmp_path.resolve())
    assert store.index_info()["libraries"] == 1
