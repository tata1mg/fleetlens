"""Service-graph MCP tools: enumerate services and read the cross-repo dependency graph."""
from __future__ import annotations

from ...server.app import ServiceContext
from ._offload import offloaded


def register(mcp, ctx: ServiceContext) -> None:
    @mcp.tool()
    @offloaded
    def get_service_graph(min_confidence: str = "") -> dict:
        """THE WHOLE microservice dependency graph in one call — every service and every
        service-to-service edge across all repositories.

        Use this FIRST for any fleet-wide or cross-repository question: which service is
        depended on by the most others, what depends on X, what would break if X changed,
        which services are unused or isolated, how many services exist, which talk over
        HTTP vs queues. Answers these without reading any source files.

        Each service carries depends_on_count / depended_on_by_count, so ranking and
        fan-in/fan-out questions are answered directly from this one response. Each edge
        carries `relationship` (calls | publishes_to | shares_channel), a `confidence`, and
        the evidence that produced it (request paths, queue names, config keys).

        `min_confidence` optionally filters weaker edges: "high" or "corroborated".

        Check `likely_duplicate_deployments` before ranking: one repository checked out
        several times appears as several services, which inflates raw counts.
        """
        return ctx.relationships.graph(min_confidence)

    @mcp.tool()
    @offloaded
    def get_index_info() -> dict:
        """When this index was built, and how much it covers.

        Worth checking before you trust an answer on a shared server: the index is a
        snapshot of the repositories at the moment it was built, so anything merged since
        is invisible to every other tool here. Also tells you how many services are
        covered, which is how you find out a repository was never indexed at all."""
        return {"status": "ok", **ctx.store.index_info()}

    @mcp.tool()
    @offloaded
    def list_services() -> dict:
        """List every indexed microservice in this codebase, with interface and symbol
        counts. Use to discover what services exist before drilling in. For dependencies
        between them, prefer get_service_graph, which returns the whole mesh at once."""
        return ctx.relationships.list_services()

    @mcp.tool()
    @offloaded
    def list_interfaces(service_id: str) -> dict:
        """Every interface a service exposes: id, method, path, handler, and provenance
        (`source` = static from the parsers, or llm from the grounded gap-filler). The
        entry point to get_endpoint_call_graph when you know the service but not the
        endpoint. `service_id` is fully-qualified (e.g. "service:orders")."""
        return ctx.relationships.list_interfaces(service_id)

    @mcp.tool()
    @offloaded
    def get_service_relationships(service_id: str) -> dict:
        """Dependencies of ONE service: `downstream` (services it calls or publishes to) and
        `upstream` (services that call it). Use for a single service; for questions spanning
        the whole fleet use get_service_graph instead, which returns everything in one call.

        `service_id` is fully-qualified (e.g. "service:orders"). Edges are derived
        deterministically from source and config, and each carries a confidence and the
        evidence that produced it."""
        return ctx.relationships.for_object(service_id)
