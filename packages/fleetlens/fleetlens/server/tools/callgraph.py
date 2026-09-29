"""Call-graph MCP tools: endpoint trace, callers/callees, symbol lookup, find.

Served from the deterministic static call graph. Traversal is bounded (depth + node caps).
"""
from __future__ import annotations

from ...server.app import ServiceContext

_MAX_DEPTH = 6


def _clamp(value, default: int, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(v, hi))


def register(mcp, ctx: ServiceContext) -> None:
    @mcp.tool()
    def get_endpoint_call_graph(interface_id: str, max_depth: int = 3) -> dict:
        """Trace an endpoint into the code: from an interface to its handler and everything
        that handler transitively calls. Use for impact analysis ("what does changing this
        endpoint touch?"). `interface_id` is a fully-qualified interface id (e.g.
        "interface:orders:post-orders"). `max_depth` bounds the walk (default 3, capped 6).
        Returns resolved handlers, reached nodes (with path + line), and `calls` edges."""
        return ctx.callgraph.endpoint_call_graph(interface_id, _clamp(max_depth, 3, 1, _MAX_DEPTH))

    @mcp.tool()
    def get_callees(symbol_id: str, max_depth: int = 2) -> dict:
        """What a function/method calls, transitively — its downstream call tree. `symbol_id`
        is a fully-qualified code_symbol id (from find_symbol or an endpoint trace). Only
        in-repo calls are edges; stdlib/third-party calls are intentionally omitted."""
        return ctx.callgraph.neighbors(symbol_id, "out", _clamp(max_depth, 2, 1, _MAX_DEPTH))

    @mcp.tool()
    def get_callers(symbol_id: str, max_depth: int = 2) -> dict:
        """Who calls a function/method, transitively — its upstream callers (the blast radius
        of changing it). `symbol_id` is a fully-qualified code_symbol id."""
        return ctx.callgraph.neighbors(symbol_id, "in", _clamp(max_depth, 2, 1, _MAX_DEPTH))

    @mcp.tool()
    def get_symbol(symbol_id: str) -> dict:
        """Look up one code symbol: location (path + line) and how many direct callers/callees
        it has. Returns a not_found response (with did_you_mean) if unknown."""
        return ctx.callgraph.symbol(symbol_id)

    @mcp.tool()
    def find_symbol(query: str, limit: int = 20) -> dict:
        """Find code_symbol ids by a substring of their id — a method name, class, or file
        fragment (e.g. "OrderManager.get" or "managers/db.py"). The entry point to the other
        call-graph tools when you know a name but not the full id."""
        return ctx.callgraph.find(query, _clamp(limit, 20, 1, 100))
