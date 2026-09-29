"""Load a repo's callgraph.json into the store: code_symbol nodes + calls/handled_by edges.

Full-snapshot per-repo refresh: delete the repo's prior code_symbol nodes (edges cascade),
then upsert fresh nodes and edges. `source="static"` keeps this producer's writes separable.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..callgraph.anchor import resolve_handlers
from ..store.base import ContextStore, RelationshipStore
from ..store.models import KnowledgeObject, Relationship

SOURCE = "static"
NODE_TYPE = "code_symbol"


def node_prefix(slug: str) -> str:
    return f"{NODE_TYPE}:{slug}:"


def _node_object(slug: str, node: dict) -> KnowledgeObject:
    qual = node["qualname"]
    return KnowledgeObject(
        object_type=NODE_TYPE, object_id=f"{slug}:{node['id']}",
        name=qual.rsplit(".", 1)[-1], summary=None, version="unknown",
        source=SOURCE, generation_strategy="callgraph", last_generated_at=None,
        embed_text=None,
        payload={"path": node["path"], "qualname": qual, "kind": node["kind"],
                 "lang": node["lang"], "line_start": node["line_start"],
                 "line_end": node["line_end"]},
    )


def load(context_dir: Path, slug: str, ctx: ContextStore, rels: RelationshipStore) -> dict:
    """Load .context/callgraph.json (+ interfaces.json if present) for `slug`. Returns a
    small summary dict."""
    cg_path = Path(context_dir) / "callgraph.json"
    if not cg_path.exists():
        return {"nodes": 0, "edges": 0, "handled_by": 0, "skipped": True}
    cg = json.loads(cg_path.read_text())
    nodes = cg.get("nodes", [])

    def gid(node_id: str) -> str:
        return f"{NODE_TYPE}:{slug}:{node_id}"

    edges = [Relationship(gid(e["from"]), "calls", gid(e["to"]), SOURCE,
                          {"weight": e.get("weight", 1)}) for e in cg.get("edges", [])]

    handled = 0
    iface_path = Path(context_dir) / "interfaces.json"
    if iface_path.exists():
        interfaces = json.loads(iface_path.read_text()).get("interfaces", [])
        for iface_id, handler_node in resolve_handlers(nodes, interfaces):
            edges.append(Relationship(f"interface:{slug}:{iface_id}", "handled_by",
                                      gid(handler_node), SOURCE, {}))
            handled += 1

    # 1. clear prior snapshot (cascade drops its edges), then upsert nodes
    ctx.delete_objects_by_id_prefix(node_prefix(slug))
    for n in nodes:
        ctx.upsert_object(_node_object(slug, n))
    ctx.commit()

    # 2. edges — keep only those whose endpoints exist (interface may be un-indexed)
    known = rels.known_ids(list({e.from_id for e in edges} | {e.to_id for e in edges}))
    valid = [e for e in edges if e.from_id in known and e.to_id in known]
    rels.replace_edges(SOURCE, [], valid)
    rels.commit()

    calls = sum(1 for e in valid if e.relationship == "calls")
    return {"nodes": len(nodes), "edges": calls,
            "handled_by": sum(1 for e in valid if e.relationship == "handled_by"),
            "skipped_edges": len(edges) - len(valid)}
