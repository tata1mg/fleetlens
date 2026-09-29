"""Enrichment tier: gated summary+embed, semantic discovery ranking, survives re-index."""
from __future__ import annotations

from fleetlens.enrich.enrich import enrich
from fleetlens.service.discovery import DiscoveryService
from fleetlens.store.models import KnowledgeObject
from fleetlens.store.sqlite import SqliteStore

_VOCAB = ["orders", "billing", "email", "health", "user"]


class FakeLLM:
    """Summary echoes the endpoint path so it carries the path's words for ranking."""
    def __init__(self):
        self.calls = 0

    def complete(self, prompt, *, system=None, max_tokens=64):
        self.calls += 1
        # pull the path token out of the grounding prompt
        line = next((ln for ln in prompt.splitlines() if ln.startswith("Endpoint:")), "")
        return f"handles requests for {line.replace('Endpoint:', '').strip()}"


class FakeEmbedder:
    model = "fake-embed"

    def embed(self, texts):
        return [[float(t.lower().count(w)) for w in _VOCAB] for t in texts]


def _iface(slug, iid, method, path):
    return KnowledgeObject("interface", f"{slug}:{iid}", f"{method} {path}", None, "unknown",
                           "static", "adapter", None, None, {"method": method, "path": path})


def _store_with_ifaces():
    s = SqliteStore(":memory:")
    s.upsert_object(_iface("shop", "get-orders", "GET", "/orders"))
    s.upsert_object(_iface("shop", "post-billing", "POST", "/billing/charge"))
    s.upsert_object(KnowledgeObject("service", "shop", "shop", None, "unknown", "static",
                                    "index", None, None, {}))
    s.commit()
    return s


def test_enrich_generates_then_gates():
    s = _store_with_ifaces()
    llm = FakeLLM()
    r1 = enrich(s, llm, FakeEmbedder(), kinds=("interface",))
    assert r1["generated"] == 2 and r1["skipped"] == 0
    calls_after_first = llm.calls
    # re-run: nothing changed -> all gated, no new LLM calls
    r2 = enrich(s, llm, FakeEmbedder(), kinds=("interface",))
    assert r2["generated"] == 0 and r2["skipped"] == 2
    assert llm.calls == calls_after_first


def test_semantic_discovery_ranks_by_meaning():
    s = _store_with_ifaces()
    enrich(s, FakeLLM(), FakeEmbedder(), kinds=("interface",))
    disc = DiscoveryService(s, s, FakeEmbedder())
    res = disc.discover("orders", "interface", limit=2)
    assert res["status"] == "ok"
    assert res["results"][0]["id"] == "interface:shop:get-orders"
    assert "orders" in (res["results"][0]["summary"] or "")


def test_discovery_unavailable_without_embedder():
    s = _store_with_ifaces()
    assert DiscoveryService(s, s, None).discover("x", "interface")["status"] == "unavailable"


def test_enrichment_survives_reindex_delete():
    s = _store_with_ifaces()
    enrich(s, FakeLLM(), FakeEmbedder(), kinds=("interface",))
    # simulate a re-index deleting the repo's objects (full snapshot)
    s.delete_objects_by_id_prefix("interface:shop:")
    s.commit()
    # enrichment (no FK cascade) survives, so gating still skips unchanged on next enrich
    assert s.enrichment_hashes("interface", "fake-embed") != {}


def test_content_change_triggers_reenrich():
    s = _store_with_ifaces()
    enrich(s, FakeLLM(), FakeEmbedder(), kinds=("interface",))
    # the interface's path changes -> content hash differs -> regenerate just that one
    s.upsert_object(_iface("shop", "get-orders", "GET", "/orders/v2"))
    s.commit()
    r = enrich(s, FakeLLM(), FakeEmbedder(), kinds=("interface",))
    assert r["generated"] == 1 and r["skipped"] == 1


def test_ollama_model_match_requires_exact_tag():
    from fleetlens.enrich.providers import ollama_has_model
    installed = ["qwen2.5-coder:7b-instruct-q8_0", "qwen2.5-coder:14b", "bge-m3:latest"]
    assert ollama_has_model("qwen2.5-coder", installed)            # untagged: any tag
    assert ollama_has_model("bge-m3", installed)
    assert ollama_has_model("qwen2.5-coder:14b", installed)
    assert not ollama_has_model("qwen2.5-coder:7b", installed)     # sibling tag != installed
    assert not ollama_has_model("llama3.2", installed)
