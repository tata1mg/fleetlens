"""Reading source for an indexed symbol. Off unless an operator turns it on.

Not registered by default, and not offered over stdio at all. Over stdio the agent is
already on the machine holding the repositories and can open the file itself, so the tool
would add a lookup and no capability. Over HTTP it adds a real one, and with it a real
exposure: without this tool a leaked token costs you the shape of the fleet, and with it,
the source of every repository in the index.

That is a decision for whoever runs the server, not a default, so it is opt-in through the
environment and the tool is simply absent until then. Absent rather than registered-and-
refusing, for the reason the discovery tools give: an advertised tool that always fails
costs an agent a turn and teaches it to stop trusting the server.
"""
from __future__ import annotations

import os

from ._offload import offloaded

ENV_VAR = "FLEETLENS_EXPOSE_SOURCE"

_TRUTHY = {"1", "true", "yes", "on"}


def enabled() -> bool:
    return os.environ.get(ENV_VAR, "").strip().lower() in _TRUTHY


def register(mcp, ctx, *, http: bool = False) -> None:
    if not (http and enabled()):
        return

    @mcp.tool()
    @offloaded
    def get_source(symbol_id: str, context_lines: int = 0, max_lines: int = 200) -> dict:
        """The actual source of an indexed symbol, read from the repository.

        Use when the structure is not enough and the question is about what the code does:
        which validations run and in what order, which branch returns an error, what a
        handler calls before it writes. Every other tool here answers from the index and
        gives you a `file:line`; this returns the lines themselves.

        Takes a `code_symbol:...` id from `find_symbol`, `get_callers`, `get_callees` or
        `get_endpoint_call_graph`, so the usual path is to locate the symbol first and read
        it second.

        Scoped to the symbol's own span. `context_lines` widens it by a few lines either
        side, for a decorator or a signature that sits just outside, and is capped at 50.
        At most 500 lines come back in one response, and `truncated` says when that bit.
        There is no whole-file mode: read the symbols you need rather than the files they
        are in.

        The source is checked against a hash of the file the index actually parsed. It
        reads the commit the index was built from first, falls back to the working copy,
        and returns whichever matches, saying which in `read_from`; a pinned index reads
        at the pin's commit. `verified` is false only for indexes built before those
        hashes existed. When nothing matches you get `span_mismatch` and no source, which
        means the index is stale for that file and needs rebuilding: the lines would
        otherwise land on a neighbouring function and read perfectly.
        """
        return ctx.source.get_source(symbol_id, context_lines, max_lines)
