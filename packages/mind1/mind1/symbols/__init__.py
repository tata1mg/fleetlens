"""Symbol indexing — language-agnostic definition inventory with content hashes."""
from .indexer import Symbol, build_index, diff_index, index_file
from .resolver import EvidenceResolver, parse_evidence

__all__ = [
    "Symbol", "build_index", "diff_index", "index_file",
    "EvidenceResolver", "parse_evidence",
]
