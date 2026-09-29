"""MCP tool modules. Add a module with register(mcp, ctx) and list it in LIVE."""
from . import callgraph, discovery, service_graph

LIVE = [callgraph, service_graph, discovery]


def register_all(mcp, ctx):
    for module in LIVE:
        module.register(mcp, ctx)
