"""RelationshipService + service listing — reads over the cross-repo service graph."""
from __future__ import annotations

from typing import Any

from ..store.base import KnowledgeStore, RelationshipStore


class RelationshipService:
    def __init__(self, relationships: RelationshipStore, knowledge: KnowledgeStore):
        self.relationships = relationships
        self.knowledge = knowledge

    def list_services(self) -> dict[str, Any]:
        objs = self.knowledge.list_objects("service")
        return {"status": "ok", "count": len(objs),
                "services": [{"id": o.id, "name": o.name,
                              "interfaces": o.payload.get("interface_count", 0),
                              "symbols": o.payload.get("symbol_count", 0)} for o in objs]}

    def graph(self, min_confidence: str = "") -> dict[str, Any]:
        """The whole service mesh in one response: every service and every edge.

        Exists because answering "which service is depended on most" by calling
        get_service_relationships once per service took 18-26 round trips in benchmarking.
        Fleet-wide questions want the fleet, not a walk over it.
        """
        order = {"config": 0, "direction-unknown": 0, "ambiguous": 1, "high": 2,
                 "corroborated": 3, "static": 2}
        floor = order.get(min_confidence, -1)
        svcs = self.knowledge.list_objects("service")
        ids = [o.id for o in svcs]
        edges, seen = [], set()
        for e in self.relationships.edges_batch(ids, None, "out"):
            if not (e.from_id.startswith("service:") and e.to_id.startswith("service:")):
                continue
            if e.from_id == e.to_id:
                continue
            conf = e.metadata.get("confidence", "")
            if order.get(conf, 99) < floor:
                continue
            key = (e.from_id, e.relationship, e.to_id)
            if key in seen:
                continue
            seen.add(key)
            row = {"from": e.from_id, "relationship": e.relationship, "to": e.to_id,
                   "confidence": conf}
            for k in ("via_paths", "channels", "via_hosts", "via_config_keys"):
                if e.metadata.get(k):
                    row[k] = e.metadata[k][:6]
            edges.append(row)
        inbound: dict[str, int] = {}
        outbound: dict[str, int] = {}
        for e in edges:
            outbound[e["from"]] = outbound.get(e["from"], 0) + 1
            inbound[e["to"]] = inbound.get(e["to"], 0) + 1
        # Teams routinely check the same repository out several times to deploy it as a web
        # service and a worker. Those appear here as separate services, so raw fan-in counts
        # double-count them. Identical symbol AND interface counts is a strong signal of the
        # same codebase, and saying so is better than letting a caller rank on inflated
        # numbers. Declare the grouping explicitly in fleetlens.yaml to remove the ambiguity.
        groups: dict = {}
        for o in svcs:
            sig = (o.payload.get("symbol_count", 0), o.payload.get("interface_count", 0))
            if sig != (0, 0):
                groups.setdefault(sig, []).append(o.id)
        dupes = [sorted(v) for v in groups.values() if len(v) > 1]

        return {
            "status": "ok",
            "service_count": len(svcs), "edge_count": len(edges),
            "likely_duplicate_deployments": dupes,
            "note": ("Services in likely_duplicate_deployments share a symbol and interface "
                     "count, so they are probably one codebase deployed more than once. "
                     "Counts below treat each as separate; collapse them before ranking."
                     ) if dupes else "",
            "services": [{"id": o.id, "name": o.name,
                          "interfaces": o.payload.get("interface_count", 0),
                          "symbols": o.payload.get("symbol_count", 0),
                          "depends_on_count": outbound.get(o.id, 0),
                          "depended_on_by_count": inbound.get(o.id, 0)} for o in svcs],
            "edges": sorted(edges, key=lambda r: (r["from"], r["to"])),
        }

    def list_interfaces(self, service_id: str) -> dict[str, Any]:
        svc = self.knowledge.get(service_id)
        if svc is None or svc.object_type != "service":
            seg = service_id.split(":")[-1]
            resp = {"status": "not_found", "id": service_id, "expected_format": "service:<slug>"}
            hits = self.knowledge.find_ids(seg, "service", limit=5)
            if hits:
                resp["did_you_mean"] = hits
            return resp
        prefix = f"interface:{svc.object_id}:"
        objs = [o for o in self.knowledge.list_objects("interface") if o.id.startswith(prefix)]
        objs.sort(key=lambda o: (o.payload.get("type") or "", o.payload.get("path") or "",
                                 o.payload.get("method") or ""))
        rows = []
        for o in objs:
            p = o.payload
            row = {"id": o.id, "name": o.name, "type": p.get("type"), "method": p.get("method"),
                   "path": p.get("path"), "handler": p.get("handler"), "source": o.source,
                   "evidence": p.get("evidence") or []}
            if p.get("confidence"):
                row["confidence"] = p["confidence"]
            rows.append(row)
        return {"status": "ok", "service_id": service_id, "count": len(rows), "interfaces": rows}

    def for_object(self, object_id: str) -> dict[str, Any]:
        edges = self.relationships.edges_of(object_id, "both")
        neighbor_ids = {(e.to_id if e.from_id == object_id else e.from_id) for e in edges}
        neighbors = {o.id: o for o in self.knowledge.get_many(list(neighbor_ids))}

        def card(nid: str, e) -> dict[str, Any]:
            o = neighbors.get(nid)
            out = {"id": nid, "type": o.object_type if o else None,
                   "name": o.name if o else None, "relationship": e.relationship,
                   "source": e.source}
            if e.metadata:
                out["metadata"] = e.metadata
            return out

        if self.knowledge.get(object_id) is None:
            seg = object_id.split(":")[-1]
            hits = self.knowledge.find_ids(seg, "service", limit=5)
            resp = {"status": "not_found", "id": object_id,
                    "expected_format": "service:<slug>"}
            if hits:
                resp["did_you_mean"] = hits
            return resp

        return {"status": "ok", "id": object_id,
                "downstream": [card(e.to_id, e) for e in edges if e.from_id == object_id],
                "upstream": [card(e.from_id, e) for e in edges if e.to_id == object_id]}
