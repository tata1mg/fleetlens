"""Engineering guidance MCP tools.

Separate from every other tool on purpose. What these return was written by a person and can
be wrong in a way an extracted endpoint cannot, so each response says it is authored and when
a human last reviewed it. A reader who cannot tell a claim from an extraction will eventually
treat one as the other.
"""
from __future__ import annotations

from ...guidance.link import governing, violations
from ...server.app import ServiceContext
from ._offload import offloaded

#: On every response. The caller may be an agent that will act on this, and the one thing it
#: must not do is treat a rule as the same kind of fact as a route read out of source.
PROVENANCE = ("human-authored engineering guidance, not derived from source; "
              "check `reviewed` before relying on it")


def _rule(obj, *, body: bool) -> dict:
    pay = obj.payload or {}
    out = {"id": obj.object_id, "title": obj.name,
           "status": pay.get("status", ""), "scope": pay.get("scope", ""),
           "reviewed": pay.get("reviewed", ""), "owner": pay.get("owner", "")}
    if body:
        out["guidance"] = obj.summary
    return out


def register(mcp, ctx: ServiceContext) -> None:
    store = ctx.store

    # Nothing to answer with and no reason to advertise. A registered tool that always
    # returns "none ingested" costs an agent a turn and teaches it not to ask again.
    if not store.list_objects("guidance"):
        return

    @mcp.tool()
    @offloaded
    def get_engineering_guidance(topic: str = "", service: str = "") -> dict:
        """Organisation engineering standards: which libraries, frameworks and patterns to
        use when writing code in this fleet. Human-authored, not derived from source.

        Call this BEFORE writing or reviewing code in any of these repositories. Conventions
        inferred by reading the fleet are the *existing* conventions, which during a migration
        are the ones being moved away from.

        With `service`, returns the rules that govern that service, matched on the
        dependencies it declares. With `topic`, finds rules by meaning. With neither, lists
        every rule with its title and status so you can ask for one by id."""
        if service:
            slug = service.split(":", 1)[-1]
            if store.get(f"service:{slug}") is None:
                return {"status": "not_found", "service": slug,
                        "hint": "use list_services to see indexed service ids"}
            rules = governing(store, f"service:{slug}")
            for r in rules:
                obj = store.get(f"guidance:{r['id']}")
                r["guidance"] = obj.summary if obj else ""
            return {"status": "ok", "service": slug, "provenance": PROVENANCE,
                    "count": len(rules), "rules": rules}

        if topic:
            exact = store.get(f"guidance:{topic}")
            if exact is not None:
                return {"status": "ok", "provenance": PROVENANCE, "count": 1,
                        "rules": [_rule(exact, body=True)]}
            if ctx.discovery.embedder is None:
                # Say which lookup is unavailable and which still works, rather than failing
                # the whole call: listing every rule is a usable answer at this size.
                return {"status": "ok", "provenance": PROVENANCE,
                        "note": "semantic lookup needs an embedding model; listing all rules",
                        "rules": [_rule(o, body=False)
                                  for o in store.list_objects("guidance")]}
            found = ctx.discovery.discover(topic, "guidance", 5)
            ids = [h["id"] for h in found.get("results", [])]
            rules = [_rule(store.get(i), body=True) for i in ids if store.get(i)]
            return {"status": "ok", "topic": topic, "provenance": PROVENANCE,
                    "count": len(rules), "rules": rules}

        return {"status": "ok", "provenance": PROVENANCE,
                "rules": [_rule(o, body=False) for o in store.list_objects("guidance")],
                "hint": "call again with topic=<id> for the full text, or service=<slug> "
                        "for what governs one service"}

    @mcp.tool()
    @offloaded
    def find_guidance_violations(guidance: str = "", service: str = "") -> dict:
        """Services that declare a dependency a mandatory rule governing them forbids.

        Evidence only: the rule, the service, and the package names in that service's own
        manifest that triggered it. A match means the dependency is declared, not that it is
        used on a live path, so treat each as a question to ask rather than a defect."""
        rows = violations(store)
        if guidance:
            rows = [r for r in rows if r["guidance"] == guidance.split(":", 1)[-1]]
        if service:
            rows = [r for r in rows if r["service"] == service.split(":", 1)[-1]]
        return {"status": "ok", "provenance": PROVENANCE, "count": len(rows),
                "findings": rows,
                "basis": "declared dependencies in each repository's own manifest"}
