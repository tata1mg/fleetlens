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
