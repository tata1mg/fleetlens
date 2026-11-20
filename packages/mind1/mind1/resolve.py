"""Cross-repo relationship resolver — the microservices differentiator.

Fleet-level post-process over the shared store: match every service's OUTBOUND calls
(stored on its service node) against the fleet-wide INTERFACE inventory, keyed on
(method, normalized-path). A match => a `calls` edge service:<consumer> -> service:<provider>.
No hostname→service alias registry needed: the endpoint path is the join key.

Confidence: an unambiguous (method, path) match is high; when the host hint corroborates it
is `corroborated`; an ambiguous path (exposed by several services) is emitted low-confidence.

Async: `event` interfaces (PUBLISH / CONSUME on a channel, from the messaging adapter) are
joined on channel name — every publisher of a channel gets a `publishes_to` edge to every
consumer of it. Confidence is `static` when both ends came from code, `config` when either
end came from a config file.
"""
from __future__ import annotations

import re

from .adapters.hosts import HostBinding, ResolveContext, resolve_host, slugish
from .store.base import ContextStore, KnowledgeStore, RelationshipStore
from .store.models import Relationship

SOURCE = "static"


_TEMPLATE_SEG = re.compile(r"^(\{[^}]*\}|<[^>]*>|:[A-Za-z_].*)$")
# Hosts that are plainly not ours: a dotted public name matching no service in the fleet.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def _slugish(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def is_external_host(host: str, slugs) -> bool:
    """True when a literal host is a third-party domain rather than a fleet service.

    Without this, any outbound URL whose PATH happens to collide with a fleet route invents
    a dependency: `https://drive.google.com/uc?export=download&id=..` matched a service's
    `/uc` route and manufactured a service->service edge.
    """
    if not host:
        return False
    h = host.lower().split("@")[-1].split(":")[0]
    if h in _LOCAL_HOSTS or "." not in h:
        return False          # bare/internal name — can't conclude it is foreign
    norm = _slugish(h)
    return not any(_slugish(s) and _slugish(s) in norm for s in slugs)


def strip_env_prefix(channel: str) -> str:
    """`stag-diagnostics-test_inventories` -> `diagnostics-test_inventories`.

    Queue names are routinely deployed per environment, so the same logical channel is
    spelled `stag-`, `pluto-`, `production-`, `<env_name>-`. Exact-string joining then finds
    nothing. Only strip when at least two segments remain, which keeps the result specific
    enough to be a join key.
    """
    head, sep, rest = (channel or "").partition("-")
    if sep and "-" in rest:
        return rest
    return channel


def normalize_path(path: str) -> str:
    if not path:
        return "/"
    p = re.sub(r"<[^>/]+>", "{}", path)      # <id:int> / <id>
    p = re.sub(r"\{[^}/]+\}", "{}", p)       # {id}
    p = re.sub(r":[A-Za-z_][A-Za-z0-9_]*", "{}", p)  # :id
    return p.rstrip("/") or "/"


def _pattern(path: str) -> tuple:
    """Path -> tuple of segments, with template segments as None (a wildcard that matches any
    concrete segment). So provider `/orders/{id}` matches consumer `/orders/99`."""
    segs = [s for s in (path or "/").split("/") if s]
    return tuple(None if _TEMPLATE_SEG.match(s) else s for s in segs)


def _slug_of(interface_id: str) -> str:
    # interface:<slug>:<iid>
    parts = interface_id.split(":", 2)
    return parts[1] if len(parts) >= 2 else interface_id


def _matches(consumer: tuple, provider: tuple) -> bool:
    if len(consumer) != len(provider):
        return False
    return all(p is None or p == c for p, c in zip(provider, consumer))


def resolve(knowledge: KnowledgeStore, rels: RelationshipStore, ctx: ContextStore) -> dict:
    """Recompute all cross-repo service->service `calls` edges from stored outbound + interfaces."""
    services = knowledge.list_objects("service")
    interfaces = knowledge.list_objects("interface")
    slugs = {s.object_id for s in services}

    # provider index pruned by (method, segment-count) -> [(pattern, slug)]
    index: dict[tuple[str, int], list[tuple[tuple, str]]] = {}
    # channel -> {PUBLISH|CONSUME: [(slug, framework)]}; keyed on the env-normalised name so
    # stag-/pluto-/production- spellings of one logical queue join
    channels: dict[str, dict[str, list[tuple[str, str]]]] = {}
    for iface in interfaces:
        p = iface.payload
        if p.get("type") == "event":
            key = strip_env_prefix(str(p.get("path", "")))
            channels.setdefault(key, {}).setdefault(
                str(p.get("method", "")).upper(), []).append((_slug_of(iface.id), p.get("framework") or ""))
            continue
        pat = _pattern(p.get("path", ""))
        index.setdefault((str(p.get("method", "")).upper(), len(pat)), []).append(
            (pat, _slug_of(iface.id)))

    edges: list[Relationship] = []
    unresolved = external = unspecific = 0
    for svc in services:
        consumer = svc.id  # service:<slug>
        cslug = svc.object_id
        best: dict[str, dict] = {}  # provider_slug -> edge metadata (dedup per pair)
        for call in svc.payload.get("outbound", []):
            if is_external_host(call.get("host") or "", slugs):
                external += 1
                continue
            path = call.get("path", "")
            cpat = _pattern(path)
            # "/" carries no information: every service has a root, so matching on it joins
            # unrelated services.
            if not [seg for seg in cpat if seg is not None]:
                unspecific += 1
                continue
            method = str(call.get("verb", "")).upper()
            providers = sorted({slug for pat, slug in index.get((method, len(cpat)), [])
                                if slug != cslug and _matches(cpat, pat)})
            if not providers:
                unresolved += 1
                continue
            host_hint = (call.get("host") or "").replace("_", "-")
            for prov in providers:
                meta = best.setdefault(prov, {"paths": [], "confidence":
                                              "high" if len(providers) == 1 else "ambiguous"})
                meta["paths"].append(call.get("path"))
                if host_hint and (host_hint == prov or prov in host_hint):
                    meta["confidence"] = "corroborated"
        for prov, meta in best.items():
            edges.append(Relationship(consumer, "calls", f"service:{prov}", SOURCE,
                                      {"via_paths": sorted(set(meta["paths"]))[:10],
                                       "confidence": meta["confidence"]}))

    # Config-declared dependencies: a service's config naming another service's address is
    # evidence of INTENT to call it. Weaker than an observed request path, so it lands at
    # `config` confidence and is upgraded where a path match corroborates it.
    declared_edges: dict[tuple[str, str], dict] = {}
    ports: dict[str, str] = {}
    aliases: dict[str, str] = {}
    for svc in services:
        ident = svc.payload.get("identity") or {}
        if ident.get("port"):
            ports.setdefault(str(ident["port"]), svc.object_id)
        if ident.get("name"):
            aliases.setdefault(slugish(ident["name"]), svc.object_id)
    for svc in services:
        cslug = svc.object_id
        ctx_h = ResolveContext(slugs=slugs, declared=svc.payload.get("declared_hosts") or {},
                               ports=ports, aliases=aliases)
        for b in svc.payload.get("host_bindings") or []:
            target = resolve_host(HostBinding(key=b.get("key", ""), value=b.get("value", ""),
                                              host=b.get("host", ""), port=b.get("port", "")), ctx_h)
            if not target or target == cslug:
                continue
            meta = declared_edges.setdefault((cslug, target), {"keys": [], "hosts": set()})
            meta["keys"].append(b.get("key"))
            meta["hosts"].add(b.get("host"))
    observed = {(e.from_id.split(":", 1)[1], e.to_id.split(":", 1)[1])
                for e in edges if e.relationship == "calls"}
    for (a, b), meta in declared_edges.items():
        if (a, b) in observed:
            continue          # already emitted from an observed request path
        edges.append(Relationship(f"service:{a}", "calls", f"service:{b}", SOURCE,
                                  {"via_config_keys": sorted(set(meta["keys"]))[:10],
                                   "via_hosts": sorted(meta["hosts"])[:5],
                                   "confidence": "config"}))
    for e in edges:
        if e.relationship == "calls" and e.metadata.get("confidence") in ("high", "ambiguous"):
            pair = (e.from_id.split(":", 1)[1], e.to_id.split(":", 1)[1])
            if pair in declared_edges:
                e.metadata["confidence"] = "corroborated"
                e.metadata["via_hosts"] = sorted(declared_edges[pair]["hosts"])[:5]

    # async: publisher -> consumer per channel (self-loops dropped: a service that both
    # publishes and consumes its own queue is a work queue, not a dependency)
    async_pairs: dict[tuple[str, str], dict] = {}
    shared_pairs: dict[tuple[str, str], set] = {}
    for channel, ends in channels.items():
        pubs, cons, uses = ends.get("PUBLISH", []), ends.get("CONSUME", []), ends.get("USES", [])
        # A definite publisher paired with anything on the other end gives a direction; two
        # direction-unknown ends give coupling without a direction.
        for pslug, pfw in pubs:
            for cslug, cfw in cons + uses:
                if pslug == cslug:
                    continue
                meta = async_pairs.setdefault((pslug, cslug), {"channels": set(), "confidence": "static"})
                meta["channels"].add(channel)
                if "config" in (pfw, cfw):
                    meta["confidence"] = "config"
        if not pubs:
            ends_unknown = sorted({s for s, _ in uses} | {s for s, _ in cons})
            for i, a in enumerate(ends_unknown):
                for b in ends_unknown[i + 1:]:
                    shared_pairs.setdefault((a, b), set()).add(channel)
    for (a, b), chans in shared_pairs.items():
        edges.append(Relationship(f"service:{a}", "shares_channel", f"service:{b}", SOURCE,
                                  {"channels": sorted(chans)[:20], "confidence": "direction-unknown"}))
    for (pslug, cslug), meta in async_pairs.items():
        edges.append(Relationship(f"service:{pslug}", "publishes_to", f"service:{cslug}", SOURCE,
                                  {"channels": sorted(meta["channels"])[:20],
                                   "confidence": meta["confidence"]}))

    known = rels.known_ids(list({e.from_id for e in edges} | {e.to_id for e in edges}))
    valid = [e for e in edges if e.from_id in known and e.to_id in known]
    # full recompute of this source's service->service edges
    rels.replace_edges(SOURCE, [s.id for s in services], valid)
    rels.commit()
    return {"services": len(services), "interfaces": len(interfaces),
            "edges": len(valid), "unresolved_calls": unresolved,
            "external_calls": external, "unspecific_calls": unspecific,
            "config_declared_edges": len(declared_edges),
            "async_edges": sum(1 for e in valid if e.relationship == "publishes_to"),
            "shared_channel_edges": sum(1 for e in valid if e.relationship == "shares_channel")}
