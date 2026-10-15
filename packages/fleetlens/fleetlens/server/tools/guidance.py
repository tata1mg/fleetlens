"""Engineering guidance MCP tools.

Separate from every other tool on purpose. What these return was written by a person and can
be wrong in a way an extracted endpoint cannot, so each response says it is authored and when
a human last reviewed it. A reader who cannot tell a claim from an extraction will eventually
treat one as the other.
"""
from __future__ import annotations

import os
import re

from ...guidance.link import governing, matchable
from ...server.app import ServiceContext
from ._offload import offloaded

#: On every response. The caller may be an agent that will act on this, and the one thing it
#: must not do is treat a rule as the same kind of fact as a route read out of source.
PROVENANCE = ("human-authored engineering guidance, not derived from source; "
              "check `reviewed` before relying on it")

#: How many ranked rules a topic search returns. A count, not a threshold, because every
#: cut-off we tried turned out to depend on the embedding model.
#:
#: Measured on one 15-rule book. An absolute floor: with `all-minilm` the on-topic top scores
#: ran 0.157 to 0.499 and the off-topic ones 0.013 to 0.313, overlapping, so no value
#: separated them; with `bge-m3` the same sets were 0.507 to 0.603 and 0.262 to 0.497,
#: separating by 0.009. A *relative* floor is no better despite looking scale-free: keeping
#: hits within 70% of the best score returned a median of 2 results on `all-minilm` and 5 on
#: `bge-m3`, because cosine similarity has a model-specific baseline and a ratio does not
#: remove it. Normalising against the rulebook's own score distribution overlapped too.
#:
#: Nor is the *spread* portable, which was the last idea standing. On `bge-m3` a correct
#: top hit scored 0.56 against 0.54 and 0.52, and a nonsense query scored 0.36 against 0.33
#: and 0.32: the same near-identical shape either way. "Close scores mean nothing stood out"
#: is true of `all-minilm` and false of `bge-m3`, so telling a caller to read the spread is
#: shipping the same model dependency in a sentence instead of in code.
#:
#: What survives across models is the ranking and the text. So the response carries the
#: nearest rules in order, with their titles and excerpts, and no score. A caller that asks
#: about banana bread and is handed "Redis access goes through cache_wrapper" can tell, and
#: does not need a number to do it.
TOPIC_RESULTS = int(os.environ.get("FLEETLENS_GUIDANCE_RESULTS", "3"))
#: The one opt-in. Off by default and only worth setting once you have measured your own
#: model against your own rules; `calibrate_guidance.py` in the docs prints the value.
MIN_SCORE = float(os.environ.get("FLEETLENS_GUIDANCE_MIN_SCORE", "0"))

_SENTENCE = re.compile(r"(?<=[.!?])\s")


def _first_sentence(text: str, limit: int = 200) -> str:
    """Enough to choose a rule by, without paying for its body."""
    body = (text or "").strip()
    if not body:
        return ""
    first = _SENTENCE.split(body, 1)[0].strip()
    return first if len(first) <= limit else first[:limit].rsplit(" ", 1)[0] + "…"


def _rule(obj, *, body: bool) -> dict:
    pay = obj.payload or {}
    out = {"id": obj.object_id, "title": obj.name,
           "status": pay.get("status", ""), "scope": pay.get("scope", ""),
           "reviewed": pay.get("reviewed", "") or "never reviewed"}
    if pay.get("owner"):
        out["owner"] = pay["owner"]
    if body:
        out["guidance"] = obj.summary
    else:
        out["excerpt"] = _first_sentence(obj.summary)
    return out


def _ranked(hits: list[dict]) -> list[dict]:
    """The top candidates, in order. No relevance judgement is made here by default."""
    scored = [h for h in hits if h.get("score") is not None]
    if MIN_SCORE:
        scored = [h for h in scored if h["score"] >= MIN_SCORE]
    return scored[:TOPIC_RESULTS]


def register(mcp, ctx: ServiceContext) -> None:
    store = ctx.store

    # Nothing to answer with and no reason to advertise. A registered tool that always
    # returns "none ingested" costs an agent a turn and teaches it not to ask again.
    if not store.list_objects("guidance"):
        return

    def _all_rules() -> list[dict]:
        return [_rule(o, body=False) for o in store.list_objects("guidance")]

    @mcp.tool()
    # A topic search embeds the query through the provider before it can search, so most of
    # this call is spent waiting on a socket rather than holding the GIL.
    @offloaded(io_bound=True)
    def get_engineering_guidance(topic: str = "", service: str = "",
                                 detail: str = "auto") -> dict:
        """Organisation engineering standards: which libraries, frameworks and patterns to
        use when writing code in this fleet. Human-authored, not derived from source.

        Call this BEFORE writing or reviewing code in any of these repositories. Conventions
        inferred by reading the fleet are the *existing* conventions, which during a
        migration are the ones being moved away from.

        `service` returns the rules governing that service in full, matched on what it
        declares. `topic` searches by meaning and returns the nearest few rules as titles,
        excerpts and scores, so ask again with `topic=<id>` for the text of one. Neither
        lists everything. `detail` is "auto", "full" or "titles" to override that.

        A topic search always returns its nearest matches, which are not necessarily
        relevant ones. Read the titles and decide: if none of them covers the question, the
        rulebook does not cover it, and you should say so rather than apply the closest
        one."""
        want_full = detail == "full"
        want_titles = detail == "titles"

        if service:
            slug = service.split(":", 1)[-1]
            svc = store.get(f"service:{slug}")
            if svc is None:
                return {"status": "not_found", "service": slug,
                        "hint": "use list_services to see indexed service ids"}
            # An empty list here would read as "nothing governs this service", which is a
            # different claim from "this index cannot tell", and only one of them is true.
            if not matchable(svc):
                return {"status": "unknown", "service": slug,
                        "reason": "this service was indexed before dependencies and "
                                  "languages were recorded, so no rule can be matched "
                                  "against it",
                        "fix": "re-index it (`fl index <repo>`), then ask again",
                        "rules": []}
            rules = governing(store, f"service:{slug}")
            if not want_titles:
                for r in rules:
                    obj = store.get(f"guidance:{r['id']}")
                    r["guidance"] = obj.summary if obj else ""
            return {"status": "ok", "service": slug, "provenance": PROVENANCE,
                    "count": len(rules), "rules": rules}

        if topic:
            exact = store.get(f"guidance:{topic}")
            if exact is not None:
                return {"status": "ok", "provenance": PROVENANCE, "count": 1,
                        "rules": [_rule(exact, body=not want_titles)]}
            if ctx.discovery.embedder is None:
                return {"status": "ok", "provenance": PROVENANCE,
                        "note": "semantic lookup needs an embedding model; listing all rules",
                        "rules": _all_rules()}
            found = ctx.discovery.discover(topic, "guidance", max(TOPIC_RESULTS, 5))
            hits = _ranked(found.get("results", []))
            if not hits:
                return {"status": "no_match", "topic": topic,
                        "hint": "nothing scored above this deployment's configured floor. "
                                "Call with no arguments to see every rule, in case it is "
                                "written in different words.",
                        "rules": []}
            rules = []
            for h in hits:
                obj = store.get(h["id"])
                if obj is not None:
                    rules.append(_rule(obj, body=want_full))
            out = {"status": "ok", "topic": topic, "provenance": PROVENANCE,
                   "count": len(rules), "rules": rules,
                   "ranking": "nearest rules by meaning, closest first. Nearest is not the "
                              "same as relevant: if none of these covers your question, "
                              "none of them is the answer."}
            if not want_full:
                out["hint"] = "call again with topic=<id> for the full text of one"
            return out

        return {"status": "ok", "provenance": PROVENANCE, "rules": _all_rules(),
                "hint": "call again with topic=<id> for the full text, or service=<slug> "
                        "for what governs one service"}
