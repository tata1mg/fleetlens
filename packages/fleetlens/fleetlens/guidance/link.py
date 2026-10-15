"""Join authored guidance to the services it governs, using what the index already derived.

The content of a rule is a claim. The link from a service to that rule is not: it comes from
the dependencies the service declares in its own manifest. Keeping those two separable is the
point of the design, so this module computes edges and never touches the text.

One relationship: `governed_by`, meaning this rule applies here, with the matched dependency
or language recorded as the evidence.

There is deliberately no "this service violates the rule" edge. A rule's `forbids` block is
parsed and kept, because it documents the rule for whoever reads it, but a declared dependency
is the wrong signal to report a violation from. The question an engineer has is whether the
code calls a forbidden library; a manifest entry is a proxy for that, and a poor one in the
direction that matters. A service reaching a package transitively, which is the common case,
declares nothing and would be reported clean. A report whose misses are invisible is worse
than no report. Answering it properly needs import sites, which the index does not hold yet.
"""
from __future__ import annotations

from ..store.models import Relationship

#: Source tag on every edge this module writes, so a re-link replaces exactly its own edges
#: and leaves the call graph and the service mesh alone.
SOURCE = "guidance_link"
GOVERNED_BY = "governed_by"


def _declared(service) -> set[str]:
    return {str(d).lower() for d in (service.payload or {}).get("dependencies", [])}


def _named(rule, block: str, key: str = "dependencies") -> set[str]:
    section = (rule.payload or {}).get(block) or {}
    return {str(d).lower() for d in (section.get(key) or [])}


def _languages(service) -> set[str]:
    """What the service is written in. Observed first; the declared value is a fallback for
    an index built before languages were recorded."""
    pay = service.payload or {}
    seen = {str(x).lower() for x in (pay.get("languages") or [])}
    if seen:
        return seen
    declared = str(pay.get("language", "")).lower()
    return {declared} if declared and declared != "auto" else set()


def applies(rule, service) -> bool:
    """Whether `rule` governs `service`.

    Each condition in `applies_to` narrows: a rule naming both a dependency and a language
    applies where both hold. A rule naming nothing applies to every service, which is how a
    rule about project layout or naming gets written and is the sensible default for "this
    is how we work here".

    Most rules need no language at all, because a package name already carries its
    ecosystem: no JavaScript repository declares a Python package and means the same thing.
    The language condition is for the rules anchored to no library, which would otherwise
    match the whole fleet. "New Python services use X" named no dependency, so it matched
    all 41 services including the Ruby and JavaScript ones.
    """
    deps = _named(rule, "applies_to")
    if deps and not (deps & _declared(service)):
        return False
    langs = _named(rule, "applies_to", "languages")
    if langs and not (langs & _languages(service)):
        return False
    return True


def link(store) -> dict:
    """(Re)compute every guidance edge. Replaces this module's own edges, nothing else."""
    rules = store.list_objects("guidance")
    services = store.list_objects("service")
    edges: list[Relationship] = []
    for service in services:
        for rule in rules:
            if not applies(rule, service):
                continue
            matched = sorted(_named(rule, "applies_to") & _declared(service)) \
                + sorted(_named(rule, "applies_to", "languages") & _languages(service))
            edges.append(Relationship(
                service.id, GOVERNED_BY, rule.id, SOURCE,
                # Why this edge exists, so nobody has to re-derive it. An empty list means the
                # rule applies to everything rather than that nothing matched.
                {"matched": matched, "status": (rule.payload or {}).get("status", "")}))
    store.replace_edges(SOURCE, [s.id for s in services], edges)
    store.commit()
    return {"rules": len(rules), "services": len(services), "edges": len(edges)}


def governing(store, service_id: str) -> list[dict]:
    """Rules that govern one service, most binding first, with the evidence for each."""
    edges = [e for e in store.edges_batch([service_id], relationship=GOVERNED_BY,
                                          direction="out")]
    out = []
    for e in edges:
        if e.relationship != GOVERNED_BY:
            continue
        rule = store.get(e.to_id)
        if rule is None:
            continue
        pay = rule.payload or {}
        out.append({
            "id": rule.object_id, "title": rule.name,
            "status": pay.get("status", ""), "scope": pay.get("scope", ""),
            "reviewed": pay.get("reviewed", ""),
            "because": e.metadata.get("matched") or "applies to every service",
        })
    rank = {"mandatory": 0, "recommended": 1, "contextual": 2}
    out.sort(key=lambda r: (rank.get(r["status"], 3), r["id"]))
    return out
