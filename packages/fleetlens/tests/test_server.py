"""Server wiring: tools register against a fake MCP, and route to the service."""
from __future__ import annotations

from fleetlens.server.app import build_context
from fleetlens.server.tools import register_all


class _FakeMCP:
    """Collects the registered tools, unwrapped.

    Each tool is wrapped so it runs on a worker thread rather than blocking the event
    loop, which makes the registered object a coroutine function. `functools.wraps` keeps
    the original reachable through `__wrapped__`, so these tests exercise the same body
    without needing an event loop to do it.
    """

    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = getattr(fn, "__wrapped__", fn)
            return fn
        return deco


def test_registers_all_callgraph_tools():
    ctx = build_context(":memory:")
    m = _FakeMCP()
    register_all(m, ctx)
    # no embedder -> semantic tools are not advertised at all
    assert set(m.tools) == {
        "get_endpoint_call_graph", "get_callees", "get_callers", "get_symbol", "find_symbol",
        "list_services", "list_interfaces", "get_service_relationships", "get_service_graph",
        "get_index_info",
    }


def test_semantic_tools_registered_only_with_an_embedder():
    class FakeEmbedder:
        model = "fake"
        def embed(self, texts): return [[0.0]]
    ctx = build_context(":memory:", embedder=FakeEmbedder())
    m = _FakeMCP()
    register_all(m, ctx)
    assert {"discover_interfaces", "discover_services"} <= set(m.tools)


def test_tools_route_to_service():
    ctx = build_context(":memory:")
    m = _FakeMCP()
    register_all(m, ctx)
    # empty store -> not_found with the expected shape (proves the wiring, not just registration)
    r = m.tools["get_symbol"]("code_symbol:svc:nope")
    assert r["status"] == "not_found"
    r = m.tools["find_symbol"]("anything")
    assert r["status"] == "ok" and r["symbols"] == []


def test_list_interfaces_enumerates_by_service_with_provenance():
    from fleetlens.store.models import KnowledgeObject
    ctx = build_context(":memory:")
    st = ctx.store
    st.upsert_object(KnowledgeObject("service", "shop", "shop", None, "unknown", "static", "index",
                                     None, None, {"interface_count": 2}))
    st.upsert_object(KnowledgeObject("interface", "shop:get-orders", "GET /orders", None, "unknown",
                                     "static", "adapter", None, None,
                                     {"type": "rest", "method": "GET", "path": "/orders",
                                      "handler": "list_orders", "evidence": ["api.py:3"]}))
    st.upsert_object(KnowledgeObject("interface", "shop:post-send", "POST /send", None, "unknown",
                                     "llm", "gap-fill", None, None,
                                     {"type": "rest", "method": "POST", "path": "/send",
                                      "handler": "send", "evidence": ["r.py:9"], "confidence": "llm-grounded"}))
    st.upsert_object(KnowledgeObject("interface", "other:get-x", "GET /x", None, "unknown",
                                     "static", "adapter", None, None, {"type": "rest", "method": "GET", "path": "/x"}))
    st.commit()
    m = _FakeMCP()
    register_all(m, ctx)
    r = m.tools["list_interfaces"]("service:shop")
    assert r["status"] == "ok" and r["count"] == 2
    assert [(i["method"], i["path"], i["source"]) for i in r["interfaces"]] == \
        [("GET", "/orders", "static"), ("POST", "/send", "llm")]
    assert r["interfaces"][1]["confidence"] == "llm-grounded"
    assert m.tools["list_interfaces"]("service:nope")["status"] == "not_found"


def test_get_service_graph_returns_whole_mesh_in_one_call():
    """Fleet-wide questions want the fleet, not 26 round trips over it."""
    from fleetlens.store.models import KnowledgeObject, Relationship
    ctx = build_context(":memory:")
    st = ctx.store
    for slug in ("web", "orders", "billing"):
        st.upsert_object(KnowledgeObject("service", slug, slug, None, "unknown", "static",
                                         "index", None, None, {"interface_count": 2}))
    st.replace_edges("static", [], [
        Relationship("service:web", "calls", "service:orders", "static", {"confidence": "high"}),
        Relationship("service:billing", "calls", "service:orders", "static", {"confidence": "config"}),
    ])
    st.commit()
    m = _FakeMCP()
    register_all(m, ctx)
    g = m.tools["get_service_graph"]()
    assert g["service_count"] == 3 and g["edge_count"] == 2
    byid = {s["id"]: s for s in g["services"]}
    # fan-in is precomputed, so "most depended-on" needs no extra calls
    assert byid["service:orders"]["depended_on_by_count"] == 2
    assert byid["service:web"]["depends_on_count"] == 1
    assert m.tools["get_service_graph"]("corroborated")["edge_count"] == 0
