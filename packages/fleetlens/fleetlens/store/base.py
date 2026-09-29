"""Store abstractions. Producers and the server depend only on these, never on a concrete
database. The deterministic core needs three: write (ContextStore), read (KnowledgeStore),
and graph (RelationshipStore). The embedding/semantic layer is added separately, later.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from .models import KnowledgeObject, Relationship


class ContextStore(ABC):
    @abstractmethod
    def upsert_object(self, obj: KnowledgeObject) -> None:
        """Insert or update one Knowledge Object. Idempotent on id."""

    @abstractmethod
    def delete_objects_by_id_prefix(self, prefix: str) -> int:
        """Delete every object whose id starts with `prefix`; return the count. Edges
        referencing them cascade. Used by full-snapshot producers (e.g. the per-repo call
        graph) to clear a scope before re-inserting. Prefix-literal, not a pattern."""

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


class KnowledgeStore(ABC):
    @abstractmethod
    def get(self, object_id: str) -> Optional[KnowledgeObject]:
        ...

    @abstractmethod
    def get_many(self, object_ids: list[str]) -> list[KnowledgeObject]:
        """Fetch several by id; preserve input order, skip missing."""

    @abstractmethod
    def find_ids(self, substring: str, object_type: str, limit: int = 20) -> list[str]:
        """Ids of `object_type` whose id contains `substring` (case-insensitive). A
        deterministic discovery aid — e.g. a code_symbol id by method name."""

    @abstractmethod
    def list_objects(self, object_type: str, include_stubs: bool = False,
                     limit: Optional[int] = None, offset: int = 0) -> list[KnowledgeObject]:
        """Enumerate objects of a type, ordered by id. Excludes forward-ref stubs by default."""

    def close(self) -> None:
        return None


class SemanticStore(ABC):
    """Optional enrichment layer: LLM summaries + embeddings for semantic discovery.

    Deliberately NOT FK-cascaded to knowledge_objects: a full-snapshot re-index deletes and
    re-inserts a repo's objects, and we want enrichment to survive that so unchanged objects
    aren't needlessly re-embedded (gating is by content_hash). Orphans (target gone) are
    ignored at read time.
    """

    @abstractmethod
    def upsert_enrichment(self, object_id: str, object_type: str, model: str, dim: int,
                          summary: Optional[str], content_hash: str,
                          vector: list[float]) -> None:
        ...

    @abstractmethod
    def enrichment_hashes(self, object_type: str, model: str) -> dict[str, str]:
        """object_id -> content_hash, for gating (skip re-enriching unchanged objects)."""

    @abstractmethod
    def search(self, vector: list[float], object_type: str, model: str,
               limit: int = 5) -> list[tuple[str, float]]:
        """(object_id, cosine score) for the closest enrichments of a type, best first."""

    def close(self) -> None:
        return None


class RelationshipStore(ABC):
    @abstractmethod
    def edges_of(self, object_id: str, direction: str = "both") -> list[Relationship]:
        """Edges touching object_id. direction: 'out' | 'in' | 'both'."""

    @abstractmethod
    def edges_batch(self, ids: list[str], relationship: Optional[str] = None,
                    direction: str = "out") -> list[Relationship]:
        """Edges for a whole frontier at once (one query per BFS level). 'out' matches
        from_id IN ids, 'in' matches to_id IN ids. Optional single-verb filter."""

    @abstractmethod
    def known_ids(self, ids: list[str]) -> set[str]:
        """Subset of ids that exist as objects (for FK-safe edge writes)."""

    @abstractmethod
    def ensure_stubs(self, ids: list[str]) -> set[str]:
        """Insert-if-absent a placeholder for each id, so a forward-referenced edge target
        survives. Never clobbers a real object. Returns the ids now present."""

    @abstractmethod
    def replace_edges(self, source: str, from_scope: list[str],
                      edges: list[Relationship]) -> None:
        """Idempotent scoped refresh: delete where source=… AND from_id IN from_scope, then
        insert `edges`. from_scope=[] skips the delete (pure insert)."""

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None
