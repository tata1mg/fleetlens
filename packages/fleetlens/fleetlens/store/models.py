"""Knowledge Object + edge model — the common envelope every context type maps onto.

One generic table serves every type: a type-agnostic identity + metadata envelope plus the
complete JSON payload as the authoritative representation. Producers (call graph, interface
adapters, …) map their artifact onto this, so the store stays type-agnostic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class KnowledgeObject:
    object_type: str                 # code_symbol | service | interface | contract | ...
    object_id: str                   # slug-namespaced local id (not globally unique alone)
    name: str
    summary: Optional[str]
    version: str
    source: str                      # provenance class: static | llm | manual | ...
    generation_strategy: str
    last_generated_at: Optional[str]
    embed_text: Optional[str]        # text to embed, or None if not semantically indexed
    payload: dict[str, Any]

    @property
    def id(self) -> str:
        """Globally unique identity across all types (`<type>:<object_id>`)."""
        return f"{self.object_type}:{self.object_id}"

    @property
    def semantic_indexed(self) -> bool:
        return self.embed_text is not None


@dataclass
class Relationship:
    """A directed, typed edge between two Knowledge Objects (by global id)."""

    from_id: str
    relationship: str                # calls | handled_by | depends_on | publishes_to | ...
    to_id: str
    source: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchHit:
    object_id: str
    object_type: str
    score: float


STUB_SOURCE = "relationship_stub"


def make_stub_object(global_id: str) -> KnowledgeObject:
    """Placeholder for a forward-referenced edge target not yet indexed.

    Sequential indexing means an edge can be written before its target's own object exists.
    Rather than drop the edge, insert this minimal stub at the target's stable id so the FK
    holds; the real object upserts over it in place when it lands. Never embedded.
    """
    otype, _, local = global_id.partition(":")
    name = local.rsplit(":", 1)[-1] if local else global_id
    return KnowledgeObject(
        object_type=otype, object_id=local, name=name, summary=None, version="unknown",
        source=STUB_SOURCE, generation_strategy=STUB_SOURCE, last_generated_at=None,
        embed_text=None,
        payload={"stub": True, "reason": "forward-referenced edge target; awaiting its own index"},
    )
