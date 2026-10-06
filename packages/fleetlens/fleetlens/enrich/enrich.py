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
import os

from ..store.base import KnowledgeStore, SemanticStore
from .providers import EmbeddingProvider, LLMProvider

_IFACE_SYS = "You write terse, one-sentence descriptions of what an API endpoint does."
_SVC_SYS = ("You describe what a microservice is responsible for, in plain prose, for an "
            "engineer who has never seen it. Cover the distinct areas of functionality it "
            "owns. Use only what you are given; do not invent capabilities.")

#: Output budget per summary, by kind.
#:
#: An endpoint is one thing and a sentence describes it. A service is not: one sentence for
#: a service with thirty-four endpoints described whichever third of them the prompt
#: happened to include, and the rest of what it does was simply absent from search. There
#: are two orders of magnitude fewer services than interfaces, so the longer budget costs
#: a few percent of a run that is otherwise all interfaces.
TOKENS = {
    "interface": int(os.environ.get("FLEETLENS_SUMMARY_TOKENS_INTERFACE", "48")),
    "service": int(os.environ.get("FLEETLENS_SUMMARY_TOKENS_SERVICE", "350")),
}
#: Hard cap on stored text, in characters, after generation. Roughly four characters per
#: token with headroom, so it bounds a runaway model without truncating a complete answer.
CHARS = {k: v * 6 for k, v in TOKENS.items()}
#: How many endpoints to describe to the service summariser. The old limit of twelve was
#: the reason a service's summary covered only part of it.
SVC_ENDPOINTS = int(os.environ.get("FLEETLENS_SUMMARY_ENDPOINTS", "60"))
#: How many endpoint paths ride along in the embedded text, and how long that text may be.
#:
#: Embedding cost rises with sequence length, and on a host without a GPU it rises enough
#: to matter: 60 characters embed in 0.33s on four CPU cores where 2000 take 7.9s. Measured
#: against one service's text, capping at 1500 characters cost about 5% of similarity on
#: two queries and improved a third, while the tail beyond it bought almost nothing.
EMBED_PATHS = int(os.environ.get("FLEETLENS_EMBED_PATHS", "30"))
EMBED_CHARS = int(os.environ.get("FLEETLENS_EMBED_CHARS", "1500"))


def _hash(*parts: str) -> str:
    return hashlib.sha1("|".join(p or "" for p in parts).encode()).hexdigest()[:16]


def _iface_ground(obj) -> tuple[str, str]:
    """Prompt and content hash for one endpoint.

    The handler's own docstring is the best description of an endpoint that exists, it is
    written by someone who knew what the code does, and the adapter already stores it. It
    was not being passed to the summariser, so a model was inferring from a URL what a
    human had spelled out two lines above the function.
    """
    p = obj.payload
    method, path, handler = p.get("method", ""), p.get("path", ""), p.get("handler") or ""
    doc = (obj.summary or "").strip()
    lines = [f"Endpoint: {method} {path}", f"Handler: {handler}"]
    if doc:
        lines.append(f"Docstring: {doc}")
    lines.append("In one short sentence, describe what this endpoint does, naming the "
                 "domain concepts involved. Reply with only the sentence.")
    return "\n".join(lines), _hash(method, path, handler, doc), ""


def _svc_ground(obj, knowledge: KnowledgeStore, semantic=None) -> tuple[str, str]:
    """Prompt and content hash for one service.

    Enrichment runs interfaces before services, so by the time a service is summarised
    every endpoint it owns already has its own grounded one-liner. Synthesising from those
    means the model is condensing rather than inventing, which is what lets the output be
    long without becoming fiction: ask for three hundred tokens from a slug and twelve URL
    paths and a model will pad confidently with responsibilities the service does not have.

    Handler names carry meaning a path does not -- `personalised_package_abnormal_params`
    says more than `/pkd/personalised-pkg-abnormal-params` -- and outbound calls and queues
    say what the service is for in a way its own endpoints cannot.
    """
    slug = obj.object_id
    # A prefix range, not a scan: this runs once per service, and loading every interface
    # in the fleet each time cost 155ms a service before any LLM work began.
    under = getattr(knowledge, "list_objects_under", None)
    ifaces = (under(f"interface:{slug}:") if under
              else [i for i in knowledge.list_objects("interface")
                    if i.id.split(":")[1] == slug])
    summary_of = getattr(semantic, "summary_of", None)

    lines, shape = [], []
    for i in ifaces[:SVC_ENDPOINTS]:
        pay = i.payload
        path = pay.get("path", "")
        bits = [f"{pay.get('method', '')} {path}".strip()]
        if pay.get("handler"):
            bits.append(f"handler {pay['handler']}")
        said = (summary_of(i.id) if summary_of else None) or (i.summary or "")
        if said:
            bits.append(said.strip())
        lines.append("  " + " — ".join(bits))
        shape.append(f"{path}:{said[:40]}")

    outbound = sorted({o.get("host") or o.get("path", "")
                       for o in (obj.payload.get("outbound") or [])})[:20]
    queues = sorted({i.payload.get("path", "") for i in ifaces
                     if i.payload.get("type") == "event"})[:20]

    prompt = [f"Service: {slug}", "", "Endpoints it exposes:"]
    prompt += lines or ["  (none discovered)"]
    if len(ifaces) > SVC_ENDPOINTS:
        prompt.append(f"  … and {len(ifaces) - SVC_ENDPOINTS} more")
    if queues:
        prompt += ["", "Message channels: " + ", ".join(queues)]
    if outbound:
        prompt += ["", "It calls out to: " + ", ".join(outbound)]
    prompt += ["",
               "Describe what this service is responsible for. Cover each distinct area "
               "of functionality above, not just the first few. Name the domain concepts "
               "an engineer would search for. Plain prose, no preamble, no closing "
               "summary paragraph, and nothing that is not supported by the list above."]
    # Endpoint paths are vocabulary a summary will not fully contain, and they are what a
    # query like "payment refunds" actually matches on. Carried separately so they reach
    # the vector without being shown to a reader.
    vocab = " ".join(i.payload.get("path", "") for i in ifaces[:EMBED_PATHS])
    return ("\n".join(prompt), _hash(slug, *sorted(shape), *outbound, *queues), vocab)


_SENTENCE_END = (". ", ".\n", "! ", "? ", ".", "!", "?")


def _whole_sentences(text: str, limit: int) -> str:
    """`text` cut to the last sentence that finishes inside `limit`.

    A budget is a budget, so a model that keeps writing gets cut off somewhere. Cutting at
    a character count leaves a dangling half-sentence in the stored summary, which reads as
    a bug to anyone who sees it and adds a fragment to what gets embedded. Cutting at the
    last full stop costs a few words and never looks broken.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut = max((window.rfind(e) + len(e.rstrip()) for e in _SENTENCE_END), default=-1)
    return (window[:cut].strip() if cut > limit // 3 else window.strip())


def _embed_text(obj, kind: str, summary: str, vocab: str = "") -> str:
    """What gets vectorised, which is not the same as what gets shown.

    Only the summary used to be embedded, so anything the model left out of its sentence
    was unreachable by search even though the index held it. A service named
    `payment_engine` with endpoints under `/refunds` lost a query about payment refunds to
    a service whose sentence happened to use the word. The stored summary is still what a
    client reads; this is the text the vector is built from.
    """
    parts = [obj.name or "", summary or "", vocab or ""]
    pay = obj.payload or {}
    if kind == "interface":
        parts += [f"{pay.get('method', '')} {pay.get('path', '')}",
                  pay.get("handler") or "", (obj.summary or "")]
    else:
        parts.append(obj.id.split(":", 1)[-1].replace("_", " "))
    return " ".join(p for p in (x.strip() for x in parts) if p)[:EMBED_CHARS]


#: Objects per batch. Each batch is one embedding request and one commit, so this trades
#: request overhead against how much work a crash can cost. Small: on a local model a
#: summary takes seconds, so 5 keeps the loss window under half a minute on a run that
#: lasts hours, and an embedding request of 5 short texts is cheap enough that the extra
#: round trips do not show against the LLM time they sit between.
BATCH = int(os.environ.get("FLEETLENS_ENRICH_BATCH", "5"))


def _owned_by(obj, kind: str, slug: str) -> bool:
    """Whether `obj` belongs to the service `slug`.

    An interface id is `interface:<slug>:<rest>`; a service's own id is its slug.
    """
    if kind == "service":
        return obj.object_id == slug
    parts = obj.id.split(":")
    return len(parts) > 1 and parts[1] == slug


def enrich(store, llm: LLMProvider, embedder: EmbeddingProvider, *,
           kinds: tuple[str, ...] = ("interface", "service"), only_slug: str = "",
           progress=None) -> dict:
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

    # Load the embedding model before any summarising. It is loaded lazily on first use,
    # and that first use is the batch flush, 64 LLM calls in: a model that cannot load, or
    # is slow to, then costs minutes of finished work rather than a second at the start.
    say("enrich", {"kind": "startup", "i": 0, "n": 0, "done": 0, "pending": 0,
                   "skipped": 0, "id": model, "state": "loading embedding model"})
    probe = embedder.embed(["warm"])
    if not probe or not probe[0]:
        raise RuntimeError(f"embedding model {model!r} returned nothing for a test input")

    def flush(kind: str, batch: list) -> int:
        if not batch:
            return 0
        vectors = embedder.embed([_embed_text(o, kind, s, v) for o, s, _, v in batch])
        if len(vectors) != len(batch):
            raise RuntimeError(
                f"embedder returned {len(vectors)} vectors for {len(batch)} texts; "
                f"model {model!r} may not support batching")
        for (obj, summary, chash, _v), vec in zip(batch, vectors):
            semantic.upsert_enrichment(obj.id, kind, model, len(vec), summary, chash, vec)
        store.commit()
        return len(batch)

    for kind in kinds:
        existing = semantic.enrichment_hashes(kind, model)
        objs = knowledge.list_objects(kind)
        # Narrowing to one service is what makes iterating on a prompt affordable: a change
        # to the wording alters every content hash, so without this each experiment costs a
        # summary for every object in the fleet.
        if only_slug:
            objs = [o for o in objs if _owned_by(o, kind, only_slug)]
        todo = len(objs)
        batch: list = []            # (obj, summary, content_hash)
        try:
            for n, obj in enumerate(objs, 1):
                prompt, chash, vocab = (_iface_ground(obj) if kind == "interface"
                                        else _svc_ground(obj, knowledge, semantic))
                if existing.get(obj.id) == chash:
                    skipped += 1
                    say("enrich", {"kind": kind, "i": n, "n": todo, "done": generated,
                                   "pending": len(batch), "skipped": skipped,
                                   "id": obj.id, "state": "unchanged"})
                    continue
                # Announced before the call, not after. The first request to a local model
                # loads several gigabytes of weights and can take minutes, which is exactly
                # the stretch where a silent run looks like a hung one.
                say("enrich", {"kind": kind, "i": n, "n": todo, "done": generated,
                               "pending": len(batch), "skipped": skipped,
                               "id": obj.id, "state": "summarising"})
                system = _IFACE_SYS if kind == "interface" else _SVC_SYS
                summary = llm.complete(prompt, system=system,
                                       max_tokens=TOKENS.get(kind, 48)).strip()
                # An endpoint answer is one line; a service answer is prose and may run to
                # several, so only the first line is kept where that is the shape asked for.
                if kind == "interface":
                    summary = summary.split("\n")[0]
                summary = _whole_sentences(summary, CHARS.get(kind, 300))
                batch.append((obj, summary, chash, vocab))
                if len(batch) >= BATCH:
                    generated += flush(kind, batch)
                    batch = []
            generated += flush(kind, batch)
        except Exception:
            # Keep what has already been paid for. Each summary in the part-filled batch
            # cost an LLM call, and on a model that answers in seconds that is minutes of
            # work the next run would otherwise buy again.
            try:
                generated += flush(kind, batch)
            except Exception:       # noqa: BLE001 - the original failure is the useful one
                pass
            raise

    return {"generated": generated, "skipped": skipped, "model": model}
