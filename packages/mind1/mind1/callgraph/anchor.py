"""`handled_by` anchor: map each interface to the code symbol that implements it.

An interface artifact records `evidence` as `path:line` references pointing at the code
that exposes it (a route handler, a consumer function, …). The call-graph nodes carry
line spans, so we resolve each evidence line to the tightest enclosing function/method
node — that node *is* the handler. This is the join that turns a flat interface list into
"this endpoint, then everything its handler calls", which a contract-level index cannot
express.

Pure data: operates on the already-extracted node list (path + line span + kind), so it
lives with the call graph but needs neither tree-sitter nor SCIP at call time.
"""
from __future__ import annotations

from bisect import bisect_right

from ..symbols.resolver import parse_evidence

# Handler nodes are executable definitions (methods are recorded with kind "function").
_HANDLER_KINDS = frozenset({"function", "method"})


class _NodeIndex:
    def __init__(self, nodes: list[dict]):
        self._by_path: dict[str, list[tuple[int, int, str]]] = {}
        for n in nodes:
            if n.get("kind") in _HANDLER_KINDS:
                self._by_path.setdefault(n["path"], []).append(
                    (n["line_start"], n["line_end"], n["id"])
                )
        self._starts: dict[str, list[int]] = {}
        for path, rows in self._by_path.items():
            rows.sort()
            self._starts[path] = [r[0] for r in rows]

    def enclosing(self, path: str, line: int) -> str | None:
        rows = self._by_path.get(path)
        if not rows:
            return None
        hi = bisect_right(self._starts[path], line)
        best: tuple[int, int, str] | None = None
        for i in range(hi - 1, -1, -1):
            start, end, nid = rows[i]
            if end < line:
                continue
            if best is None or (end - start) < (best[1] - best[0]):
                best = (start, end, nid)
        return best[2] if best else None


def resolve_handlers(
    nodes: list[dict], interfaces: list[dict]
) -> list[tuple[str, str]]:
    """Return (interface_id, handler_node_id) pairs — one per resolved evidence hit.

    `interface_id` is the interface's repo-local id (as in interfaces.json), and
    `handler_node_id` is a call-graph node id (repo-local `path::qualname`). Interfaces
    whose evidence resolves to no handler (e.g. framework-provided endpoints documented
    only in a README) are simply omitted. De-duplicated per interface.
    """
    index = _NodeIndex(nodes)
    out: list[tuple[str, str]] = []
    for iface in interfaces:
        iface_id = iface.get("id")
        if not iface_id:
            continue
        seen: set[str] = set()
        for ref in iface.get("evidence", []) or []:
            parsed = parse_evidence(ref)
            if parsed is None:
                continue
            path, lo, _hi = parsed
            if lo is None:  # whole-file evidence can't pinpoint a handler
                continue
            nid = index.enclosing(path, lo)
            if nid and nid not in seen:
                seen.add(nid)
                out.append((iface_id, nid))
    return out
