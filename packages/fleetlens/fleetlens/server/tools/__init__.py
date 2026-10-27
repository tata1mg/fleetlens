"""MCP tool modules. Add a module with register(mcp, ctx) and list it in LIVE."""
from . import callgraph, discovery, guidance, service_graph, source

LIVE = [callgraph, service_graph, discovery, guidance]


def register_all(mcp, ctx, *, http: bool = False) -> None:
    for module in LIVE:
        module.register(mcp, ctx)
    # Not in LIVE: this one is off unless an operator opts in, and it is never offered over
    # stdio. It takes the transport because that is a property of how the server was
    # started, not of the index it is serving.
    source.register(mcp, ctx, http=http)
