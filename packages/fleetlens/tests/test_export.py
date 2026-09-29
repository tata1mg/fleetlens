"""export-graph: self-contained HTML render of the service mesh."""
from __future__ import annotations

from fleetlens.export import render
from fleetlens.store.models import KnowledgeObject, Relationship
from fleetlens.store.sqlite import SqliteStore


def _svc(slug, ic=0):
    return KnowledgeObject("service", slug, slug, None, "unknown", "static", "index", None,
                           None, {"interface_count": ic})


def test_render_contains_services_and_edges():
    s = SqliteStore(":memory:")
    for slug in ("gateway", "orders", "billing"):
        s.upsert_object(_svc(slug, ic=3))
    s.replace_edges("static", [], [
        Relationship("service:gateway", "calls", "service:orders", "static",
                     {"confidence": "high"}),
        Relationship("service:orders", "calls", "service:billing", "static",
                     {"confidence": "ambiguous"}),
    ])
    s.commit()

    out = render(s, s)
    assert out.startswith("<!doctype html>")
    assert "<svg" in out and "</svg>" in out
    for slug in ("gateway", "orders", "billing"):
        assert f">{slug}" in out
    assert "3 services" in out and "2 dependency edges" in out
    assert "stroke-dasharray" in out  # the ambiguous edge is dashed
    assert "cdn" not in out.lower() and "<script" not in out  # self-contained, no JS/CDN


def test_render_empty_store():
    s = SqliteStore(":memory:")
    out = render(s, s)
    assert "0 services" in out and "<svg" in out  # renders, doesn't crash
