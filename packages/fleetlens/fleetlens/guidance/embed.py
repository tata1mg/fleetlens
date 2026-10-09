"""Embed authored guidance so a question finds the rule without naming it.

No LLM is involved. Every other kind is summarised first because its grounding is a list of
URLs; a rule arrives already written by the person who meant it, and asking a model to
paraphrase that would introduce drift into the one content fleetlens is not allowed to
reword. So this embeds the authored text as written and stores it unchanged.
"""
from __future__ import annotations

import os

#: Rules per embedding request. Larger than the enrichment batch because guidance is tens of
#: objects rather than tens of thousands, and one request is simpler to reason about.
BATCH = int(os.environ.get("FLEETLENS_GUIDANCE_BATCH", "32"))


def embed_guidance(store, embedder, *, progress=None) -> dict:
    """Embed every guidance object whose text has changed since the last run.

    Gated on the content hash the loader already computed, so re-running after an unrelated
    reindex costs nothing.
    """
    say = progress or (lambda *a: None)
    rules = store.list_objects("guidance")
    if not rules:
        return {"embedded": 0, "skipped": 0, "model": embedder.model}

    existing = store.enrichment_hashes("guidance", embedder.model)
    todo = [r for r in rules if existing.get(r.id) != (r.payload or {}).get("content_hash")]
    skipped = len(rules) - len(todo)

    done = 0
    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        say("guidance", {"i": done, "n": len(todo), "state": "embedding"})
        vectors = embedder.embed([r.embed_text or r.name for r in batch])
        if len(vectors) != len(batch):
            raise RuntimeError(
                f"embedder returned {len(vectors)} vectors for {len(batch)} rules")
        for rule, vec in zip(batch, vectors):
            store.upsert_enrichment(
                rule.id, "guidance", embedder.model, len(vec),
                # The stored summary is the authored body, not a generated one.
                rule.summary, (rule.payload or {}).get("content_hash", ""), vec)
        store.commit()
        done += len(batch)
    return {"embedded": done, "skipped": skipped, "model": embedder.model}
