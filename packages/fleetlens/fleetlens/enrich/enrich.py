"""Enrichment producer — LLM capability summaries + embeddings for semantic discovery.

Opt-in, off the hot path. For each interface/service it:
  1. builds a small grounding string + a content hash of it,
  2. skips if an enrichment with that hash already exists (gating — only new/changed work),
  3. generates a one-line summary (LLM, output-capped),
  4. embeds the summary and stores summary + vector as an enrichment overlay (source="llm").

The deterministic objects are never mutated; enrichment lives in its own table.
"""
from __future__ import annotations

import hashlib

from ..store.base import KnowledgeStore, SemanticStore
from .providers import EmbeddingProvider, LLMProvider

_IFACE_SYS = "You write terse, one-sentence descriptions of what an API endpoint does."
_SVC_SYS = "You write terse, one-sentence descriptions of what a microservice does."
_MAX_TOKENS = 48


def _hash(*parts: str) -> str:
    return hashlib.sha1("|".join(p or "" for p in parts).encode()).hexdigest()[:16]


def _iface_ground(obj) -> tuple[str, str]:
    p = obj.payload
    method, path, handler = p.get("method", ""), p.get("path", ""), p.get("handler") or ""
    prompt = (f"Endpoint: {method} {path}\nHandler: {handler}\n"
              "In one short sentence, describe what this endpoint does. "
              "Reply with only the sentence.")
    return prompt, _hash(method, path, handler)


def _svc_ground(obj, knowledge: KnowledgeStore) -> tuple[str, str]:
    slug = obj.object_id
    ifaces = knowledge.list_objects("interface")
    mine = [i.payload.get("path", "") for i in ifaces if i.id.split(":")[1] == slug][:12]
    prompt = (f"Service: {slug}\nEndpoints: {', '.join(mine) or '(none discovered)'}\n"
              "In one short sentence, describe what this service is responsible for. "
              "Reply with only the sentence.")
    return prompt, _hash(slug, *sorted(mine))


#: Objects per batch. Each batch is one embedding request and one commit, so this trades
#: request size against how much work a crash can cost. 64 keeps the embedder payload small
#: enough for a local Ollama and the loss window under a couple of minutes.
BATCH = 64


def enrich(store, llm: LLMProvider, embedder: EmbeddingProvider, *,
           kinds: tuple[str, ...] = ("interface", "service"), progress=None) -> dict:
    """Enrich the given object kinds. `store` implements KnowledgeStore + SemanticStore.

    Work is committed in batches rather than at the end. On a fleet this runs for hours:
    one LLM call per object, and a large fleet has tens of thousands of them. Holding it all
    until the final commit meant a run killed at hour ten saved nothing, which also defeated
    the content-hash resumption that is supposed to make a second run cheap. Batching also
    keeps the embedding request to something a local model will accept, rather than posting
    every summary in the fleet as one payload.
    """
    knowledge: KnowledgeStore = store
    semantic: SemanticStore = store
    model = embedder.model
    generated = skipped = 0
    say = progress or (lambda *a: None)

    def flush(kind: str, batch: list) -> int:
        if not batch:
            return 0
        vectors = embedder.embed([s for _, s, _ in batch])
        if len(vectors) != len(batch):
            raise RuntimeError(
                f"embedder returned {len(vectors)} vectors for {len(batch)} texts; "
                f"model {model!r} may not support batching")
        for (obj, summary, chash), vec in zip(batch, vectors):
            semantic.upsert_enrichment(obj.id, kind, model, len(vec), summary, chash, vec)
        store.commit()
        return len(batch)

    for kind in kinds:
        existing = semantic.enrichment_hashes(kind, model)
        objs = knowledge.list_objects(kind)
        todo = len(objs)
        batch: list = []            # (obj, summary, content_hash)
        for n, obj in enumerate(objs, 1):
            prompt, chash = (_iface_ground(obj) if kind == "interface"
                             else _svc_ground(obj, knowledge))
            if existing.get(obj.id) == chash:
                skipped += 1
                say("enrich", {"kind": kind, "i": n, "n": todo, "done": generated,
                               "skipped": skipped, "id": obj.id, "state": "unchanged"})
                continue
            system = _IFACE_SYS if kind == "interface" else _SVC_SYS
            summary = llm.complete(prompt, system=system, max_tokens=_MAX_TOKENS).strip()
            summary = summary.split("\n")[0][:300]  # hard output cap
            batch.append((obj, summary, chash))
            say("enrich", {"kind": kind, "i": n, "n": todo, "done": generated,
                           "skipped": skipped, "id": obj.id, "state": "summarised"})
            if len(batch) >= BATCH:
                generated += flush(kind, batch)
                batch = []
        generated += flush(kind, batch)

    return {"generated": generated, "skipped": skipped, "model": model}
