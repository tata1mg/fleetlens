"""Enrichment tier: gated summary+embed, semantic discovery ranking, survives re-index."""
from __future__ import annotations

import pytest
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


class _CountingLLM:
    """Counts calls and can fail partway, standing in for a run that gets killed."""

    def __init__(self, fail_after=None):
        self.calls, self.fail_after = 0, fail_after

    def complete(self, prompt, system=None, max_tokens=None):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RuntimeError("killed")
        return f"summary {self.calls}"


class _Embedder:
    model = "test-embed"

    def __init__(self):
        self.batches = []

    def embed(self, texts):
        self.batches.append(len(texts))
        return [[0.1, 0.2, 0.3] for _ in texts]


def _fleet(store, n):
    from fleetlens.store.models import KnowledgeObject
    for i in range(n):
        store.upsert_object(KnowledgeObject(
            object_type="service", object_id=f"svc{i:03d}", name=f"svc{i:03d}", summary=None,
            version="1", source="static", generation_strategy="index",
            last_generated_at=None, embed_text=None, payload={}))
    store.commit()


def test_work_is_committed_in_batches_not_only_at_the_end():
    """A fleet run is one LLM call per object over tens of thousands of objects. Holding
    everything until a final commit meant a run killed at hour ten saved nothing, which also
    defeated the content-hash resumption that makes a second run cheap."""
    from fleetlens.enrich.enrich import BATCH, enrich

    store = SqliteStore(":memory:")
    _fleet(store, BATCH * 2 + 5)
    emb = _Embedder()

    with pytest.raises(RuntimeError):
        enrich(store, _CountingLLM(fail_after=BATCH + 10), emb, kinds=("service",))

    # the first full batch was embedded and written before the failure
    assert emb.batches == [BATCH]
    assert len(store.enrichment_hashes("service", "test-embed")) == BATCH


def test_a_second_run_only_does_what_is_left():
    from fleetlens.enrich.enrich import BATCH, enrich

    store = SqliteStore(":memory:")
    total = BATCH * 2 + 5
    _fleet(store, total)
    emb = _Embedder()

    with pytest.raises(RuntimeError):
        enrich(store, _CountingLLM(fail_after=BATCH + 10), emb, kinds=("service",))

    resumed = _CountingLLM()
    r = enrich(store, resumed, _Embedder(), kinds=("service",))

    assert r["skipped"] == BATCH                 # the committed batch is not redone
    assert resumed.calls == total - BATCH        # only the remainder costs LLM time
    assert r["generated"] == total - BATCH


def test_the_embedder_is_never_sent_the_whole_fleet_at_once():
    """13,818 summaries in a single request is a payload a local model will refuse."""
    from fleetlens.enrich.enrich import BATCH, enrich

    store = SqliteStore(":memory:")
    _fleet(store, BATCH * 3 + 7)
    emb = _Embedder()
    enrich(store, _CountingLLM(), emb, kinds=("service",))

    assert max(emb.batches) <= BATCH
    assert sum(emb.batches) == BATCH * 3 + 7
