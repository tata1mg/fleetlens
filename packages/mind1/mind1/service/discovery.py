"""DiscoveryService — semantic (natural-language) lookup over enrichments.

Embeds the query with the SAME model used at enrich time, brute-force cosine over the
stored enrichment vectors, then hydrates hits with name + summary. Requires an embedder
(the enrichment tier); without one, the tools report themselves unavailable.
"""
from __future__ import annotations

from typing import Any, Optional

from ..enrich.providers import EmbeddingProvider
from ..store.base import KnowledgeStore, SemanticStore


class DiscoveryService:
    def __init__(self, semantic: SemanticStore, knowledge: KnowledgeStore,
                 embedder: Optional[EmbeddingProvider]):
        self.semantic = semantic
        self.knowledge = knowledge
        self.embedder = embedder

    def discover(self, query: str, object_type: str, limit: int = 5) -> dict[str, Any]:
        if self.embedder is None:
            return {"status": "unavailable",
                    "reason": "semantic search needs an embedding model — run `fl enrich` "
                              "and start the server with --embed-model"}
        vec = self.embedder.embed([query])[0]
        hits = self.semantic.search(vec, object_type, self.embedder.model, limit)
        results = []
        for oid, score in hits:
            obj = self.knowledge.get(oid)
            results.append({"id": oid, "type": object_type,
                            "name": obj.name if obj else None,
                            "summary": self.semantic.summary_of(oid)
                            if hasattr(self.semantic, "summary_of") else None,
                            "score": round(score, 4)})
        return {"status": "ok", "query": query, "count": len(results), "results": results}
