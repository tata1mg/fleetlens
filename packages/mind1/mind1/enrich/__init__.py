"""Optional LLM enrichment tier: summaries + embeddings for semantic discovery, and the
gap-filler that resolves sites the deterministic adapters could not parse."""
from .enrich import enrich
from .gaps import fill_gaps
from .providers import build_embeddings, build_llm

__all__ = ["enrich", "fill_gaps", "build_llm", "build_embeddings"]
