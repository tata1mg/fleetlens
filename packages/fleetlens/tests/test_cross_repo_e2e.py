"""Two repositories on disk, indexed, resolved, and read back through the MCP tools.

Every other cross-repo test builds both services as store objects and calls `resolve`
directly, which proves the resolver and nothing else. The claim the README makes is larger:
point fleetlens at a folder of repositories and it derives who calls whom. Between those two
lies the part nothing covered: discovering the repos, the adapters reading a route out of one
and an outbound call out of the other, the config binding that corroborates them, and the
fact that `index_all` does not resolve at all.

Deliberately free of the SCIP toolchain. Routes and outbound calls come from the adapters,
and the indexer's exit status is read rather than raised on, so this runs on a machine with
no scip-python and stays inside the suite's sub-second budget.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fleetlens import indexing
from fleetlens.resolve import resolve
from fleetlens.store.sqlite import SqliteStore


@pytest.fixture(autouse=True)
def _no_scip(monkeypatch):
    """Report the external indexer as unavailable instead of running it.

    Nothing here needs call edges, and letting scip-python actually run costs three seconds
    a test and fails anyway on a fixture that is not a git repository. The indexer's exit
    status is a value rather than an exception, so this is the same path a machine without
    the toolchain takes.
    """
    monkeypatch.setattr(indexing.cg_cli, "main", lambda *a, **kw: 2)


def _write(root: Path, files: dict) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


def _fleet(base: Path) -> Path:
    """Two repos: `orders` serves GET /orders/{id}, `gateway` calls it and says where."""
    _write(base / "orders", {
        "app/main.py":
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n\n"
            "@app.get('/orders/{order_id}')\n"
            "def get_order(order_id: str):\n"
            "    return {'id': order_id}\n",
    })
    _write(base / "gateway", {
        "app/main.py":
            "import requests\n"
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n\n"
            "@app.get('/checkout')\n"
            "def checkout():\n"
            "    return requests.get('http://orders/orders/42').json()\n",
        "config.json": '{"ORDERS_HOST": "orders", "PORT": "8080"}\n',
    })
    return base


def _indexed(tmp_path: Path, *, db: str = ":memory:") -> SqliteStore:
    fleet = _fleet(tmp_path / "fleet")
    store = SqliteStore(db)
    report = indexing.index_all(fleet, store)
    assert report["failed"] == [], report["failed"]
    assert len(report["ok"]) == 2, report["ok"]
    return store


def _calls_edges(store: SqliteStore) -> list:
    return [e for e in store.edges_batch(["service:gateway"], "calls", "out")]


def test_two_repos_on_disk_become_one_calls_edge(tmp_path):
    """The documented workflow end to end: index a folder, resolve, get the edge."""
    store = _indexed(tmp_path)

    assert {o.object_id for o in store.list_objects("service")} == {"orders", "gateway"}

    summary = resolve(store, store, store)
    assert summary["edges"] == 1, summary

    edges = _calls_edges(store)
    assert len(edges) == 1, edges
    assert edges[0].to_id == "service:orders"     # direction: gateway -> orders, not back


def test_the_edge_carries_the_call_site_that_proves_it(tmp_path):
    """An edge with no evidence is a claim. The path that produced it has to survive."""
    store = _indexed(tmp_path)
    resolve(store, store, store)

    meta = _calls_edges(store)[0].metadata
    assert "/orders/42" in meta.get("via_paths", []), meta
    # The host in the request URL names the provider, which agrees with the path match.
    # That agreement is what separates a corroborated edge from a bare path match.
    assert meta.get("confidence") == "corroborated", meta


def test_an_opaque_host_still_resolves_but_more_weakly(tmp_path):
    """A path match alone is an edge; it just does not get to claim corroboration.

    The address a real gateway calls is often a load balancer that names nothing, so this
    is the common case rather than the degenerate one.
    """
    _fleet(tmp_path / "fleet")
    (tmp_path / "fleet" / "gateway" / "app" / "main.py").write_text(
        "import requests\n"
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n\n"
        "@app.get('/checkout')\n"
        "def checkout():\n"
        "    return requests.get('http://internal-lb-7/orders/42').json()\n")
    (tmp_path / "fleet" / "gateway" / "config.json").unlink()

    store = SqliteStore(":memory:")
    indexing.index_all(tmp_path / "fleet", store)
    resolve(store, store, store)

    edges = _calls_edges(store)
    assert len(edges) == 1 and edges[0].to_id == "service:orders"
    assert edges[0].metadata.get("confidence") == "high", edges[0].metadata


def test_indexing_alone_produces_no_edges(tmp_path):
    """`index_all` does not resolve; only the CLI wires that in.

    Worth pinning: a caller who indexes and serves without resolving gets a fleet of
    services with no dependencies, and nothing says so, which is indistinguishable from a
    fleet that genuinely has none.
    """
    store = _indexed(tmp_path)
    assert _calls_edges(store) == []


def test_the_mcp_tool_returns_the_edge(tmp_path):
    """What an agent receives, through the registered tool and a reopened index.

    Goes through a file rather than `:memory:` so the edge is proved to survive the write
    and the read-only reopen that `fl serve` does, not just the store object in hand.
    """
    pytest.importorskip("mcp")
    from fleetlens.server.app import build_context
    from fleetlens.server.tools import register_all

    db = tmp_path / "fleet.db"
    store = _indexed(tmp_path, db=str(db))
    resolve(store, store, store)
    store.commit()
    store.close()

    ctx = build_context(str(db), read_only=True)
    tools = {}

    class _FakeMCP:
        def tool(self):
            def deco(fn):
                tools[fn.__name__] = getattr(fn, "__wrapped__", fn)
                return fn
            return deco

    register_all(_FakeMCP(), ctx)
    try:
        graph = tools["get_service_graph"]()
        matches = [e for e in graph["edges"]
                   if e["from"] == "service:gateway" and e["to"] == "service:orders"
                   and e["relationship"] == "calls"]
        assert len(matches) == 1, graph["edges"]
        assert matches[0]["confidence"] == "corroborated", matches[0]

        # The filter an agent uses to discard weak edges must keep this one.
        assert tools["get_service_graph"]("corroborated")["edge_count"] == 1
    finally:
        ctx.close()
