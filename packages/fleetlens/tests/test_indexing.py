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
