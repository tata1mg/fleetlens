"""CallGraphService — deterministic, bounded traversal of the static call graph.

Answers callers/callees of a symbol and the downstream call tree of an endpoint (interface
-> handler -> everything it transitively calls). Breadth-first over `calls` edges, one
batched query per depth level, with depth + node caps so a hub symbol can't return an
unbounded response. No ranking, no reasoning.
"""
from __future__ import annotations

from typing import Any

from ..store.base import KnowledgeStore, RelationshipStore
from ..store.models import Relationship

NODE_TYPE = "code_symbol"
_DEFAULT_MAX_NODES = 300


class CallGraphService:
    def __init__(self, relationships: RelationshipStore, knowledge: KnowledgeStore):
        self.relationships = relationships
        self.knowledge = knowledge

    # -- traversal ----------------------------------------------------------
    def _walk(self, seeds: list[str], direction: str, max_depth: int,
              max_nodes: int) -> tuple[list[Relationship], set[str], bool]:
        visited: set[str] = set(seeds)
        frontier = list(seeds)
        edges: list[Relationship] = []
        seen: set[tuple[str, str]] = set()
        reached: set[str] = set()
        truncated = False
        for _ in range(max_depth):
            if not frontier:
                break
            for e in self.relationships.edges_batch(frontier, "calls", direction):
                key = (e.from_id, e.to_id)
                if key not in seen:
                    seen.add(key)
                    edges.append(e)
            nxt: list[str] = []
            for e in edges:
                neighbor = e.to_id if direction == "out" else e.from_id
                anchor = e.from_id if direction == "out" else e.to_id
                if anchor in frontier and neighbor not in visited:
                    if len(visited) >= max_nodes:
                        truncated = True
                        continue
                    visited.add(neighbor)
                    reached.add(neighbor)
                    nxt.append(neighbor)
            frontier = nxt
        return edges, reached, truncated

    def _nodes(self, ids: set[str]) -> list[dict[str, Any]]:
        objs = {o.id: o for o in self.knowledge.get_many(list(ids))}
        out = []
        for oid in sorted(ids):
            o = objs.get(oid)
            p = o.payload if o else {}
            out.append({"id": oid, "name": o.name if o else None,
                        "qualname": p.get("qualname"), "path": p.get("path"),
                        "kind": p.get("kind"), "line_start": p.get("line_start")})
        return out

    @staticmethod
    def _edge_cards(edges: list[Relationship]) -> list[dict[str, Any]]:
        return [{"from": e.from_id, "to": e.to_id, "weight": e.metadata.get("weight", 1)}
                for e in edges]

    def _not_found(self, given: str, expected: str) -> dict[str, Any]:
        seg = given.split(":")[-1]
        resp = {"status": "not_found", "id": given, "expected_format": expected}
        hits = self.knowledge.find_ids(seg, NODE_TYPE, limit=5)
        if hits:
            resp["did_you_mean"] = hits
        return resp

    # -- operations ---------------------------------------------------------
    def neighbors(self, symbol_id: str, direction: str, max_depth: int) -> dict[str, Any]:
        if self.knowledge.get(symbol_id) is None:
            return self._not_found(symbol_id, "code_symbol:<slug>:<path>::<qualname>")
        depth = max(1, min(max_depth, _DEFAULT_MAX_NODES))
        edges, reached, truncated = self._walk([symbol_id], direction, depth, _DEFAULT_MAX_NODES)
        return {"status": "ok", "root": symbol_id,
                "direction": "callees" if direction == "out" else "callers",
                "max_depth": depth, "count": len(reached), "nodes": self._nodes(reached),
                "edges": self._edge_cards(edges), "truncated": truncated}

    def endpoint_call_graph(self, interface_id: str, max_depth: int = 3) -> dict[str, Any]:
        iface = self.knowledge.get(interface_id)
        if iface is None:
            return self._not_found(interface_id, "interface:<slug>:<interface-id>")
        handled = self.relationships.edges_batch([interface_id], "handled_by", "out")
        handlers = [e.to_id for e in handled]
        if not handlers:
            return {"status": "ok", "interface_id": interface_id, "handlers": [],
                    "note": "no handler resolved (framework-provided, or evidence did not "
                            "pinpoint code)", "nodes": [], "edges": [], "truncated": False}
        depth = max(1, min(max_depth, _DEFAULT_MAX_NODES))
        edges, reached, truncated = self._walk(handlers, "out", depth, _DEFAULT_MAX_NODES)
        node_ids = set(handlers) | reached
        return {"status": "ok", "interface_id": interface_id, "interface_name": iface.name,
                "handlers": handlers, "max_depth": depth, "count": len(node_ids),
                "nodes": self._nodes(node_ids), "edges": self._edge_cards(edges),
                "truncated": truncated}

    def symbol(self, symbol_id: str) -> dict[str, Any]:
        obj = self.knowledge.get(symbol_id)
        if obj is None:
            return self._not_found(symbol_id, "code_symbol:<slug>:<path>::<qualname>")
        callees = self.relationships.edges_batch([symbol_id], "calls", "out")
        callers = self.relationships.edges_batch([symbol_id], "calls", "in")
        return {"status": "ok", "id": symbol_id, "name": obj.name, "payload": obj.payload,
                "direct_callees": len(callees), "direct_callers": len(callers)}

    def find(self, query: str, limit: int = 20) -> dict[str, Any]:
        ids = self.knowledge.find_ids(query, NODE_TYPE, limit=limit)
        return {"status": "ok", "query": query, "count": len(ids), "symbols": ids}
