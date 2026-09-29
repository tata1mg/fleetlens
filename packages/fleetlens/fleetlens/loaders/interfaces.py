"""Load a repo's interfaces.json into the store as `interface:<slug>:<id>` nodes.

Deterministic producer (source="static"). Full-snapshot per repo: clear the repo's prior
interface nodes (edges cascade), then upsert. Loading these BEFORE the call graph lets the
call-graph loader attach `handled_by` edges (interface -> handler) — the endpoint trace.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..store.base import ContextStore
from ..store.models import KnowledgeObject

SOURCE = "static"
NODE_TYPE = "interface"


def node_prefix(slug: str) -> str:
    return f"{NODE_TYPE}:{slug}:"


def _object(slug: str, rec: dict) -> KnowledgeObject:
    return KnowledgeObject(
        object_type=NODE_TYPE, object_id=f"{slug}:{rec['id']}", name=rec.get("name") or rec["id"],
        summary=rec.get("summary"), version="unknown", source=SOURCE,
        generation_strategy="adapter", last_generated_at=None,
        embed_text=None,  # semantic indexing is a later opt-in tier
        payload={k: rec.get(k) for k in
                 ("type", "method", "path", "handler", "framework", "evidence")},
    )


def load(context_dir: Path, slug: str, ctx: ContextStore) -> dict:
    path = Path(context_dir) / "interfaces.json"
    if not path.exists():
        return {"interfaces": 0, "skipped": True}
    records = json.loads(path.read_text()).get("interfaces", [])
    ctx.delete_objects_by_id_prefix(node_prefix(slug))
    for rec in records:
        ctx.upsert_object(_object(slug, rec))
    ctx.commit()
    return {"interfaces": len(records)}
