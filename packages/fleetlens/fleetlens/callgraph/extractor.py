"""Build a repo-local static call graph from a SCIP index + the tree-sitter symbol index.

Why the two-source join: SCIP gives *type-accurate name resolution* (a reference at a
given position resolves to a globally-unique symbol id — `x.get()` binds to the real
`get`, not every `get` in the repo), but its symbol ids are opaque and its granularity is
per-occurrence. Our tree-sitter symbol index (`_symbols.json`) gives *stable, readable
node identities* (`path::qualname`) with line spans. Joining them by source position:

  * **nodes** come from the tree-sitter index (functions / methods / classes) — the same
    ids used everywhere else in the platform, so the call graph lines up with contracts,
    interfaces, and incremental invalidation.
  * **edges** come from SCIP: each *reference* occurrence whose target is a symbol
    *defined in this repo* becomes a `caller -> callee` edge, where caller = the tightest
    function/method enclosing the reference's line and callee = the node at the target's
    definition line.

References to symbols with no in-repo definition (stdlib, third-party, framework) resolve
to nothing and correctly produce no edge — that precision is exactly what a name-matching
heuristic cannot achieve.
"""
from __future__ import annotations

import json
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .scip_reader import read_documents

# Node kinds (as recorded by the tree-sitter indexer) eligible to be a caller vs a callee.
# A "method" is recorded with kind "function" (a nested function_definition), so callers
# and callees are functions/methods. Classes are nodes (look-up + `handled_by` anchor
# targets) but not call-edge *targets*: SCIP cannot distinguish a call from an attribute
# read or a type annotation, so a reference to a class symbol (`Foo.CONST`, `x: Foo`) is
# usually not a call. Restricting callees to functions/methods keeps the graph a true
# *call* graph (a constructor `Foo()` resolves to the class symbol, not `__init__`, so it
# is intentionally out of scope for v1).
_CALLER_KINDS = frozenset({"function", "method"})
_CALLEE_KINDS = frozenset({"function", "method"})


@dataclass(frozen=True)
class _Span:
    line_start: int
    line_end: int
    symbol_id: str
    kind: str

    @property
    def size(self) -> int:
        return self.line_end - self.line_start


class _PathIndex:
    """Per-file spans, queryable for the tightest definition enclosing a given line."""

    def __init__(self, symbols: dict[str, dict]):
        self._by_path: dict[str, list[_Span]] = {}
        for sid, s in symbols.items():
            self._by_path.setdefault(s["path"], []).append(
                _Span(s["line_start"], s["line_end"], sid, s["kind"])
            )
        # sort each file's spans by start line for a bounded scan
        self._starts: dict[str, list[int]] = {}
        for path, spans in self._by_path.items():
            spans.sort(key=lambda sp: sp.line_start)
            self._starts[path] = [sp.line_start for sp in spans]

    def enclosing(self, path: str, line: int, kinds: frozenset[str]) -> Optional[str]:
        """Tightest span of an allowed kind whose [line_start, line_end] contains `line`."""
        spans = self._by_path.get(path)
        if not spans:
            return None
        # only spans starting at or before `line` can contain it
        hi = bisect_right(self._starts[path], line)
        best: Optional[_Span] = None
        for i in range(hi - 1, -1, -1):
            sp = spans[i]
            if sp.line_end < line:
                continue
            if sp.kind not in kinds:
                continue
            if best is None or sp.size < best.size:
                best = sp
        return best.symbol_id if best else None


def _is_global(symbol: str) -> bool:
    """A SCIP global symbol (has a definition site we can resolve repo-wide).

    `local N` symbols are document-scoped (params, locals, comprehension vars) — never a
    tracked function/method/class, so they are ignored for edges.
    """
    return not symbol.startswith("local ")


def build_callgraph(scip_path: Path, symbol_index: dict, slug: str) -> dict:
    """Return the serializable callgraph/v1 artifact (repo-local node ids)."""
    symbols = symbol_index.get("symbols", {})
    index = _PathIndex(symbols)
    data = Path(scip_path).read_bytes()
    documents = list(read_documents(data))

    # Pass 1: where is each in-repo global symbol defined? symbol -> (path, def_line 1-based)
    scip_def: dict[str, tuple[str, int]] = {}
    for doc in documents:
        for occ in doc.occurrences:
            if occ.is_definition and _is_global(occ.symbol):
                scip_def.setdefault(occ.symbol, (doc.relative_path, occ.start_line + 1))

    # Pass 2: references to in-repo symbols become edges.
    edge_weight: dict[tuple[str, str], int] = {}
    unresolved_callee = 0  # ref to internal symbol but no tree-sitter node at its def line
    for doc in documents:
        for occ in doc.occurrences:
            if occ.is_definition or not _is_global(occ.symbol):
                continue
            target = scip_def.get(occ.symbol)
            if target is None:
                continue  # external symbol (stdlib / third-party) — correctly no edge
            callee = index.enclosing(target[0], target[1], _CALLEE_KINDS)
            if callee is None:
                unresolved_callee += 1
                continue
            caller = index.enclosing(doc.relative_path, occ.start_line + 1, _CALLER_KINDS)
            if caller is None or caller == callee:
                continue  # top-level/import-time reference, or self-recursion
            edge_weight[(caller, callee)] = edge_weight.get((caller, callee), 0) + 1

    nodes = [
        {
            "id": sid,
            "path": s["path"],
            "qualname": sid.split("::", 1)[1] if "::" in sid else sid,
            "kind": s["kind"],
            "lang": s["lang"],
            "line_start": s["line_start"],
            "line_end": s["line_end"],
        }
        for sid, s in sorted(symbols.items())
    ]
    edges = [
        {"from": f, "to": t, "weight": w}
        for (f, t), w in sorted(edge_weight.items())
    ]
    return {
        "schema": "callgraph/v1",
        "root": symbol_index.get("root"),
        "slug": slug,
        "symbol_index": "_symbols.json",
        "stats": {
            "nodes": len(nodes),
            "edges": len(edges),
            "documents": len(documents),
            "internal_symbols": len(scip_def),
            "unresolved_callee": unresolved_callee,
        },
        "nodes": nodes,
        "edges": edges,
    }


def load_symbol_index(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
