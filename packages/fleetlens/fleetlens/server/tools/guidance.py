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

#: A hit has to score at least this fraction of the best hit to be returned. Semantic search
#: ranks, it does not decide relevance, so a query matching nothing still comes back with a
#: full page of whatever was least unlike it. Measured on a real rulebook, correct hits
#: scored 0.31 to 0.51 and wrong ones 0.09 to 0.20, so the gap is wide and relative.
REL_FLOOR = float(os.environ.get("FLEETLENS_GUIDANCE_REL_FLOOR", "0.7"))
#: ...and an absolute floor, for when everything scores badly and the best of a bad set would
#: otherwise clear the relative one on its own. Model dependent, hence the override.
MIN_SCORE = float(os.environ.get("FLEETLENS_GUIDANCE_MIN_SCORE", "0.25"))

_SENTENCE = re.compile(r"(?<=[.!?])\s")


def _first_sentence(text: str, limit: int = 200) -> str:
    """Enough to choose a rule by, without paying for its body."""
    body = (text or "").strip()
    if not body:
        return ""
    first = _SENTENCE.split(body, 1)[0].strip()
    return first if len(first) <= limit else first[:limit].rsplit(" ", 1)[0] + "…"


def _rule(obj, *, body: bool, score: float = None) -> dict:
    pay = obj.payload or {}
    out = {"id": obj.object_id, "title": obj.name,
           "status": pay.get("status", ""), "scope": pay.get("scope", ""),
           "reviewed": pay.get("reviewed", "") or "never reviewed"}
    if pay.get("owner"):
        out["owner"] = pay["owner"]
    if score is not None:
        out["score"] = round(score, 3)
    if body:
        out["guidance"] = obj.summary
    else:
        out["excerpt"] = _first_sentence(obj.summary)
    return out


def _relevant(hits: list[dict]) -> list[dict]:
    """Hits that are actually about the query, or none."""
    scored = [h for h in hits if h.get("score") is not None]
    if not scored:
        return []
    best = max(h["score"] for h in scored)
    if best < MIN_SCORE:
        return []
    return [h for h in scored if h["score"] >= max(MIN_SCORE, best * REL_FLOOR)]


def register(mcp, ctx: ServiceContext) -> None:
    store = ctx.store

    # Nothing to answer with and no reason to advertise. A registered tool that always
    # returns "none ingested" costs an agent a turn and teaches it not to ask again.
    if not store.list_objects("guidance"):
        return

    def _all_rules() -> list[dict]:
        return [_rule(o, body=False) for o in store.list_objects("guidance")]

    @mcp.tool()
    @offloaded
    def get_engineering_guidance(topic: str = "", service: str = "",
                                 detail: str = "auto") -> dict:
        """Organisation engineering standards: which libraries, frameworks and patterns to
        use when writing code in this fleet. Human-authored, not derived from source.

        Call this BEFORE writing or reviewing code in any of these repositories. Conventions
        inferred by reading the fleet are the *existing* conventions, which during a
        migration are the ones being moved away from.

        `service` returns the rules governing that service in full, matched on what it
        declares. `topic` searches by meaning and returns titles and excerpts, so ask again
        with `topic=<id>` for the text of one. Neither lists everything. `detail` is "auto",
        "full" or "titles" when you want to override that."""
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
            hits = _relevant(ctx.discovery.discover(topic, "guidance", 5).get("results", []))
            if not hits:
                return {"status": "no_match", "topic": topic,
                        "hint": "no rule is about this. Call with no arguments to see every "
                                "rule, in case it is written in different words.",
                        "rules": []}
            rules = []
            for h in hits:
                obj = store.get(h["id"])
                if obj is not None:
                    rules.append(_rule(obj, body=want_full, score=h["score"]))
            return {"status": "ok", "topic": topic, "provenance": PROVENANCE,
                    "count": len(rules), "rules": rules,
                    "hint": None if want_full else
                            "call again with topic=<id> for the full text of one"}

        return {"status": "ok", "provenance": PROVENANCE, "rules": _all_rules(),
                "hint": "call again with topic=<id> for the full text, or service=<slug> "
                        "for what governs one service"}
