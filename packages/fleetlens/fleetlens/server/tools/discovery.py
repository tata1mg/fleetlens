"""Semantic discovery MCP tools (require the enrichment tier + an embedding model)."""
from __future__ import annotations

from ...server.app import ServiceContext
from ._offload import offloaded


def _limit(v) -> int:
    try:
        return max(1, min(int(v), 25))
    except (TypeError, ValueError):
        return 5


def register(mcp, ctx: ServiceContext) -> None:
    # Only advertise semantic search when it can actually answer. A registered-but-dead tool
    # is worse than a missing one: an agent asked "which service consumes queue X" reaches
    # for `discover_*` first, burns a turn on {"status": "unavailable"}, and falls back to
    # grep — never trying the deterministic tools that would have answered it.
    if ctx.discovery.embedder is None:
        return

    @mcp.tool()
    @offloaded
    def discover_interfaces(query: str, limit: int = 5) -> dict:
        """Find endpoints by meaning, e.g. "the endpoint that sends order-confirmation
        emails". Ranked semantic search over LLM-generated interface summaries (needs the
        enrichment tier). Returns interface ids + summaries + scores. Use get_endpoint_call_graph
        on a result to trace it."""
        return ctx.discovery.discover(query, "interface", _limit(limit))

    @mcp.tool()
    @offloaded
    def discover_services(query: str, limit: int = 5) -> dict:
        """Find services by what they do, in natural language. Ranked semantic search over
        LLM-generated service summaries (needs the enrichment tier). Returns service ids +
        summaries + scores."""
        return ctx.discovery.discover(query, "service", _limit(limit))

    @mcp.tool()
    @offloaded
    def discover_libraries(query: str, limit: int = 5) -> dict:
        """Find shared code libraries by what they provide, in natural language, e.g. "retry
        with exponential backoff" or "parsing feature-flag config". Use before writing a
        helper that may already exist. Ranked semantic search over LLM-generated library
        summaries (needs the enrichment tier). Returns library ids + summaries + scores; use
        find_symbol to reach the code."""
        return ctx.discovery.discover(query, "library", _limit(limit))
