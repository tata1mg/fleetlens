"""Join authored guidance to the services it governs, using what the index already derived.

The content of a rule is a claim. The link from a service to that rule is not: it comes from
the dependencies the service declares in its own manifest. Keeping those two separable is the
point of the design, so this module computes edges and never touches the text.

Two relationships, and the difference matters:

  governed_by   this rule applies here. The service declares something `applies_to` names, or
                the rule applies to everything.
  contradicts   this rule applies here AND the service declares something it `forbids`. The
                evidence is a package name in a manifest, so the finding is checkable rather
                than a judgement.
"""
from __future__ import annotations

from ..store.models import Relationship

#: Source tag on every edge this module writes, so a re-link replaces exactly its own edges
#: and leaves the call graph and the service mesh alone.
SOURCE = "guidance_link"
GOVERNED_BY = "governed_by"
CONTRADICTS = "contradicts"


def _declared(service) -> set[str]:
    return {str(d).lower() for d in (service.payload or {}).get("dependencies", [])}


def _named(rule, block: str, key: str = "dependencies") -> set[str]:
    section = (rule.payload or {}).get(block) or {}
    return {str(d).lower() for d in (section.get(key) or [])}


def applies(rule, service) -> bool:
    """Whether `rule` governs `service`.

    A rule that names no dependencies applies to every service: that is how a rule about
    project layout or naming is written, and it is the sensible default for "this is how we
    work here". A rule that does name them applies only where one is declared.
    """
    wanted = _named(rule, "applies_to")
    return not wanted or bool(wanted & _declared(service))


def contradicted_by(rule, service) -> list[str]:
    """The forbidden packages this service declares, if the rule governs it at all.

    Returned rather than a boolean, because "violates the cache rule" is an accusation and
    "declares `redis` and `aioredis`" is evidence someone can check in thirty seconds.
    """
    if not applies(rule, service):
        return []
    return sorted(_named(rule, "forbids") & _declared(service))


def link(store) -> dict:
    """(Re)compute every guidance edge. Replaces this module's own edges, nothing else."""
    rules = store.list_objects("guidance")
    services = store.list_objects("service")
    edges: list[Relationship] = []
    violations = 0
    for service in services:
        for rule in rules:
            if not applies(rule, service):
                continue
            matched = sorted(_named(rule, "applies_to") & _declared(service))
            edges.append(Relationship(
                service.id, GOVERNED_BY, rule.id, SOURCE,
                # Why this edge exists, so nobody has to re-derive it. An empty list means the
                # rule applies to everything rather than that nothing matched.
                {"matched": matched, "status": (rule.payload or {}).get("status", "")}))
            found = contradicted_by(rule, service)
            if found and (rule.payload or {}).get("status") == "mandatory":
                edges.append(Relationship(
                    service.id, CONTRADICTS, rule.id, SOURCE, {"declares": found}))
                violations += 1
    store.replace_edges(SOURCE, [s.id for s in services], edges)
    store.commit()
    return {"rules": len(rules), "services": len(services),
            "edges": len(edges), "violations": violations}


def governing(store, service_id: str) -> list[dict]:
    """Rules that govern one service, most binding first, with the evidence for each."""
    edges = [e for e in store.edges_batch([service_id], direction="out")
             if e.relationship in (GOVERNED_BY, CONTRADICTS)]
    broken = {e.to_id: e.metadata.get("declares", []) for e in edges
              if e.relationship == CONTRADICTS}
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
            "contradicted_by": broken.get(e.to_id, []),
        })
    rank = {"mandatory": 0, "recommended": 1, "contextual": 2}
    out.sort(key=lambda r: (rank.get(r["status"], 3), r["id"]))
    return out


def violations(store) -> list[dict]:
    """Every service that declares something a mandatory rule governing it forbids.

    This is a report about people's repositories, so it says only what the manifests say: the
    rule, the service, and the package names that triggered it. No severity, no score.
    """
    out = []
    for e in store.edges_batch([s.id for s in store.list_objects("service")],
                               relationship=CONTRADICTS, direction="out"):
        rule = store.get(e.to_id)
        out.append({
            "service": e.from_id.split(":", 1)[-1],
            "guidance": e.to_id.split(":", 1)[-1],
            "title": rule.name if rule else "",
            "declares": e.metadata.get("declares", []),
        })
    out.sort(key=lambda r: (r["guidance"], r["service"]))
    return out
