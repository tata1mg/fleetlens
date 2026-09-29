"""CallGraphService over SqliteStore: loader → traversal (callers/callees/endpoint)."""
from __future__ import annotations

import json
from pathlib import Path

from fleetlens.loaders import callgraph as cg_loader
from fleetlens.service.callgraph import CallGraphService
from fleetlens.store.models import KnowledgeObject
from fleetlens.store.sqlite import SqliteStore


def _write_ctx(tmp_path: Path) -> Path:
    ctx = tmp_path / ".context"
    ctx.mkdir()
    cg = {
        "schema": "callgraph/v1", "slug": "svc",
        "nodes": [
            {"id": "r.py::index", "path": "r.py", "qualname": "index", "kind": "function",
             "lang": "python", "line_start": 5, "line_end": 12},
            {"id": "m.py::Mgr.get", "path": "m.py", "qualname": "Mgr.get", "kind": "function",
             "lang": "python", "line_start": 3, "line_end": 6},
            {"id": "d.py::DB.q", "path": "d.py", "qualname": "DB.q", "kind": "function",
             "lang": "python", "line_start": 1, "line_end": 4},
        ],
        "edges": [
            {"from": "r.py::index", "to": "m.py::Mgr.get", "weight": 2},
            {"from": "m.py::Mgr.get", "to": "d.py::DB.q", "weight": 1},
        ],
    }
    (ctx / "callgraph.json").write_text(json.dumps(cg))
    (ctx / "interfaces.json").write_text(json.dumps(
        {"interfaces": [{"id": "get-index", "evidence": ["r.py:6"]}]}))
    return ctx


def _loaded(tmp_path):
    store = SqliteStore(":memory:")
    # the interface node must exist for the handled_by edge to attach
    store.upsert_object(KnowledgeObject("interface", "svc:get-index", "GET /index", None,
                                        "unknown", "static", "adapter", None, None, {}))
    summary = cg_loader.load(_write_ctx(tmp_path), "svc", store, store)
    return store, summary


def test_loader_counts(tmp_path):
    store, summary = _loaded(tmp_path)
    assert summary["nodes"] == 3 and summary["edges"] == 2 and summary["handled_by"] == 1


def test_callees_depth(tmp_path):
    store, _ = _loaded(tmp_path)
    svc = CallGraphService(store, store)
    d1 = svc.neighbors("code_symbol:svc:r.py::index", "out", 1)
    assert {n["id"] for n in d1["nodes"]} == {"code_symbol:svc:m.py::Mgr.get"}
    d2 = svc.neighbors("code_symbol:svc:r.py::index", "out", 2)
    assert {n["id"] for n in d2["nodes"]} == {
        "code_symbol:svc:m.py::Mgr.get", "code_symbol:svc:d.py::DB.q"}


def test_callers_inverse(tmp_path):
    store, _ = _loaded(tmp_path)
    svc = CallGraphService(store, store)
    up = svc.neighbors("code_symbol:svc:d.py::DB.q", "in", 5)
    assert {n["id"] for n in up["nodes"]} == {
        "code_symbol:svc:m.py::Mgr.get", "code_symbol:svc:r.py::index"}


def test_endpoint_trace(tmp_path):
    store, _ = _loaded(tmp_path)
    svc = CallGraphService(store, store)
    r = svc.endpoint_call_graph("interface:svc:get-index", max_depth=3)
    assert r["handlers"] == ["code_symbol:svc:r.py::index"]
    assert {n["id"] for n in r["nodes"]} == {
        "code_symbol:svc:r.py::index", "code_symbol:svc:m.py::Mgr.get",
        "code_symbol:svc:d.py::DB.q"}


def test_symbol_and_not_found(tmp_path):
    store, _ = _loaded(tmp_path)
    svc = CallGraphService(store, store)
    s = svc.symbol("code_symbol:svc:r.py::index")
    assert s["direct_callees"] == 1 and s["direct_callers"] == 0
    assert svc.symbol("code_symbol:svc:nope")["status"] == "not_found"


def test_reindex_replaces_cleanly(tmp_path):
    store, _ = _loaded(tmp_path)
    # re-run the loader → same counts, no duplicate edges (full-snapshot replace)
    summary2 = cg_loader.load(tmp_path / ".context", "svc", store, store)
    assert summary2["nodes"] == 3 and summary2["edges"] == 2
    assert len(store.list_objects("code_symbol")) == 3
