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

    @property
    def real_batches(self):
        """Batches excluding the one-text warm-up probe enrich() makes at startup."""
        return self.batches[1:]


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

    total = BATCH * 4
    died_after = BATCH * 2 + 2           # two full batches, then a part-filled one
    store = SqliteStore(":memory:")
    _fleet(store, total)
    emb = _Embedder()

    with pytest.raises(RuntimeError):
        enrich(store, _CountingLLM(fail_after=died_after), emb, kinds=("service",))

    # the full batches, plus the partial one flushed on the way out: an LLM call was paid
    # for each of those summaries, so none of them are discarded
    assert emb.real_batches == [BATCH, BATCH, 2]
    assert len(store.enrichment_hashes("service", "test-embed")) == died_after


def test_a_second_run_only_does_what_is_left():
    from fleetlens.enrich.enrich import BATCH, enrich

    total = BATCH * 4
    died_after = BATCH * 2 + 2
    store = SqliteStore(":memory:")
    _fleet(store, total)

    with pytest.raises(RuntimeError):
        enrich(store, _CountingLLM(fail_after=died_after), _Embedder(), kinds=("service",))

    resumed = _CountingLLM()
    r = enrich(store, resumed, _Embedder(), kinds=("service",))

    assert r["skipped"] == died_after              # everything already stored is skipped
    assert resumed.calls == total - died_after     # only the remainder costs LLM time
    assert r["generated"] == total - died_after


def test_the_embedder_is_never_sent_the_whole_fleet_at_once():
    """13,818 summaries in a single request is a payload a local model will refuse."""
    from fleetlens.enrich.enrich import BATCH, enrich

    store = SqliteStore(":memory:")
    _fleet(store, BATCH * 3 + 7)
    emb = _Embedder()
    enrich(store, _CountingLLM(), emb, kinds=("service",))

    assert max(emb.real_batches) <= BATCH
    assert sum(emb.real_batches) == BATCH * 3 + 7


def test_service_grounding_does_not_scan_every_interface_in_the_fleet():
    """It runs once per service. Loading and JSON-parsing every interface each time cost
    155ms a service on a real index, 33 seconds before any LLM work began."""
    from fleetlens.enrich.enrich import _svc_ground

    store = SqliteStore(":memory:")
    for svc in ("orders", "billing"):
        store.upsert_object(KnowledgeObject(
            object_type="service", object_id=svc, name=svc, summary=None, version="1",
            source="static", generation_strategy="index", last_generated_at=None,
            embed_text=None, payload={}))
        for i in range(5):
            store.upsert_object(KnowledgeObject(
                object_type="interface", object_id=f"{svc}:GET:/{svc}/{i}", name=f"/{svc}/{i}",
                summary=None, version="1", source="static", generation_strategy="index",
                last_generated_at=None, embed_text=None, payload={"path": f"/{svc}/{i}"}))
    store.commit()

    scans = {"n": 0}
    real = store.list_objects

    def counted(kind, *a, **kw):
        scans["n"] += 1
        return real(kind, *a, **kw)

    store.list_objects = counted
    prompt, _, _vocab = _svc_ground(store.get("service:orders"), store)

    assert scans["n"] == 0                      # no full scan at all
    assert "/orders/0" in prompt                # and it still found this service's paths
    assert "/billing/0" not in prompt           # without picking up anyone else's


def test_the_embedding_model_is_loaded_before_any_summarising():
    """It is loaded lazily on first use, and that first use was the batch flush, 64 LLM
    calls in. An embedder that cannot load then costs minutes of finished work instead of a
    second at the start."""
    from fleetlens.enrich.enrich import enrich

    store = SqliteStore(":memory:")
    _fleet(store, 10)

    class DeadEmbedder:
        model = "bge-m3"

        def embed(self, texts):
            raise RuntimeError("model failed to load")

    llm = _CountingLLM()
    with pytest.raises(RuntimeError, match="failed to load"):
        enrich(store, llm, DeadEmbedder(), kinds=("service",))

    assert llm.calls == 0        # nothing was summarised before the failure surfaced


def test_an_http_error_carries_the_server_s_own_explanation(monkeypatch):
    """"HTTP Error 500: Internal Server Error" says nothing. Ollama puts the real cause in
    the body, and for a runner the kernel killed that is the whole diagnosis."""
    import io
    import urllib.error

    from fleetlens.enrich import providers

    def raise_500(*a, **kw):
        raise urllib.error.HTTPError(
            "http://x/api/generate", 500, "Internal Server Error", {},
            io.BytesIO(b'{"error":"llama runner process has terminated: signal: killed"}'))

    monkeypatch.setattr(providers.urllib.request, "urlopen", raise_500)
    monkeypatch.setattr(providers, "ATTEMPTS", 1)

    with pytest.raises(providers.ProviderError) as err:
        providers._post("http://x/api/generate", {})

    msg = str(err.value)
    assert "signal: killed" in msg          # the server's own words
    assert "out of memory" in msg           # and what that means on a shared box


def test_a_4xx_is_not_retried_but_a_5xx_is(monkeypatch):
    """A model that does not exist will not start existing because we asked again."""
    import io
    import urllib.error

    from fleetlens.enrich import providers

    for code, expected in ((404, 1), (500, 3)):
        tries = {"n": 0}

        def raise_it(*a, _c=code, **kw):
            tries["n"] += 1
            raise urllib.error.HTTPError("http://x", _c, "err", {}, io.BytesIO(b"nope"))

        monkeypatch.setattr(providers.urllib.request, "urlopen", raise_it)
        monkeypatch.setattr(providers, "ATTEMPTS", 3)
        monkeypatch.setattr(providers.time, "sleep", lambda _s: None)
        with pytest.raises(providers.ProviderError):
            providers._post("http://x", {})
        assert tries["n"] == expected, code


# --- concurrent summarising (--jobs) ------------------------------------------------


class SlowLLM(FakeLLM):
    """Records how many calls are in flight at once, so overlap can be asserted on."""

    def __init__(self, delay=0.05):
        super().__init__()
        self.delay = delay
        self.live = 0
        self.peak = 0
        self._lock = __import__("threading").Lock()

    def complete(self, prompt, *, system=None, max_tokens=64):
        import time
        with self._lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
        try:
            time.sleep(self.delay)
            return super().complete(prompt, system=system, max_tokens=max_tokens)
        finally:
            with self._lock:
                self.live -= 1


def _many_ifaces(n=12):
    s = SqliteStore(":memory:")
    for i in range(n):
        s.upsert_object(_iface("shop", f"get-{i}", "GET", f"/orders/{i}"))
    s.upsert_object(KnowledgeObject("service", "shop", "shop", None, "unknown", "static",
                                    "index", None, None, {}))
    s.commit()
    return s


def test_jobs_above_one_produces_the_same_enrichments():
    """Concurrency is a scheduling change, not a result change."""
    out = []
    for jobs in (1, 4):
        store = _many_ifaces()
        enrich(store, FakeLLM(), FakeEmbedder(), kinds=("interface",), jobs=jobs)
        rows = store._conn.execute(
            "SELECT object_id, summary, content_hash FROM enrichments ORDER BY object_id"
        ).fetchall()
        out.append([tuple(r) for r in rows])
    assert out[0] == out[1]
    assert len(out[0]) == 12


def test_jobs_above_one_actually_overlaps_requests():
    llm = SlowLLM()
    enrich(_many_ifaces(), llm, FakeEmbedder(), kinds=("interface",), jobs=4)
    assert llm.peak > 1, "requests were serialised despite jobs=4"
    assert llm.calls == 12


def test_jobs_of_one_stays_sequential():
    llm = SlowLLM()
    enrich(_many_ifaces(), llm, FakeEmbedder(), kinds=("interface",), jobs=1)
    assert llm.peak == 1


def test_concurrent_failure_keeps_what_was_already_paid_for():
    """A provider that dies mid-run must not throw away committed summaries."""
    class Dies(FakeLLM):
        def complete(self, prompt, *, system=None, max_tokens=64):
            if self.calls >= 8:
                raise RuntimeError("provider gave up")
            return super().complete(prompt, system=system, max_tokens=max_tokens)

    store = _many_ifaces()
    with pytest.raises(RuntimeError):
        enrich(store, Dies(), FakeEmbedder(), kinds=("interface",), jobs=4)
    kept = store._conn.execute("SELECT count(*) FROM enrichments").fetchone()[0]
    assert kept > 0


def test_concurrent_run_still_skips_unchanged_objects():
    store = _many_ifaces()
    first = enrich(store, FakeLLM(), FakeEmbedder(), kinds=("interface",), jobs=4)
    second = enrich(store, FakeLLM(), FakeEmbedder(), kinds=("interface",), jobs=4)
    assert first["generated"] == 12 and first["skipped"] == 0
    assert second["generated"] == 0 and second["skipped"] == 12
