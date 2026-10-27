"""Serving a pinned index from the same server, chosen per request.

A pin is a database file, so serving one is a matter of pointing the tools at a different
file rather than running a second server. The tools never learn about any of this: they ask
their context for a store, and the context answers with whichever index this request is for.

Which index that is comes from the URL. `/mcp` is the live one; `/pins/<alias>/mcp` is that
snapshot. The alias is read out of the ASGI scope into a context variable, which survives
into the worker thread a tool body runs on, so the lookup works from inside a tool without
threading a parameter through every signature.

One MCP server serves both. FastMCP builds a single session manager and mounts it as one
ASGI app, and that app can be mounted at a second route like any other. Running a server per
pin would mean a session manager and a lifespan each, for no gain.
"""
from __future__ import annotations

import contextvars
import logging
import os
import threading
from collections import OrderedDict

log = logging.getLogger(__name__)

#: The pin this request is for, or None for the live index. Set from the URL by the ASGI
#: wrapper below and read by the routing context. A context variable rather than an argument
#: because the alternative is passing it through every tool signature, and because
#: `anyio.to_thread.run_sync` copies the context into the worker thread the tool runs on.
current_alias: contextvars.ContextVar = contextvars.ContextVar("fleetlens_alias", default=None)

#: How many pinned indexes stay open at once.
#:
#: Each one holds its own vector block: every enrichment vector for a type and model as a
#: single matrix. Measured at 1536 dimensions that is about 92 MB for 15,000 enriched
#: interfaces, and it is resident for as long as the index is cached. Four pins plus the
#: live index is therefore most of half a gigabyte before anything else, which is why this
#: is a hard cap rather than a hint.
#:
#: Building that block costs about 280 ms, paid on the first semantic search against a
#: freshly opened pin, so evicting a pin somebody is still using is not free either. Size
#: this by how many pins are genuinely in use at once rather than by spare memory.
MAX_OPEN_PINS = int(os.environ.get("FLEETLENS_MAX_OPEN_PINS", "0")) or 3


class UnknownPin(LookupError):
    """Asked for a pin that is not on disk. Carries the alias so the caller can say which."""


class ContextRegistry:
    """The live context, plus a bounded cache of contexts for pinned indexes.

    Evicted contexts are dropped rather than closed. A `SqliteStore` keeps its connections
    in a `threading.local`, so they belong to whichever worker threads touched that store,
    and closing them from the evicting thread is not allowed. Dropping the reference lets
    them be collected once no request is using them, which is the behaviour we want anyway:
    a request already running must not have its database closed underneath it.
    """

    def __init__(self, live, pins, build, max_open: int = MAX_OPEN_PINS):
        self._live = live
        self._pins = pins                      # a pins.PinStore
        self._build = build                    # alias -> ServiceContext, for a pin's db
        self._max_open = max(1, max_open)
        self._open: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    @property
    def live(self):
        return self._live

    def open_aliases(self) -> list:
        """Which pins are cached. The collector uses this to leave them alone."""
        with self._lock:
            return list(self._open)

    def forget(self, alias: str) -> None:
        """Drop a pin from the cache, for when it has been deleted or has expired."""
        with self._lock:
            self._open.pop(alias, None)

    def resolve(self, alias=None):
        """The context for `alias`, or the live one when `alias` is None.

        Raises UnknownPin when there is no such pin, which the caller turns into a 404
        rather than quietly serving the live index. Silently answering from the wrong index
        is the one failure this whole feature exists to prevent.
        """
        if not alias:
            return self._live
        with self._lock:
            hit = self._open.get(alias)
            if hit is not None:
                self._open.move_to_end(alias)
                return hit
        # Built outside the lock: opening an index reads from disk, and a slow open should
        # not block a request for a different pin.
        path = self._pins.snapshot_path(alias)
        if path is None:
            raise UnknownPin(alias)
        ctx = self._build(str(path))
        with self._lock:
            existing = self._open.get(alias)
            if existing is not None:
                # Another thread won the race. Use theirs and let ours be collected, so
                # two requests for one pin never hold two copies of its vector block.
                self._open.move_to_end(alias)
                return existing
            self._open[alias] = ctx
            while len(self._open) > self._max_open:
                evicted, _ = self._open.popitem(last=False)
                log.info("closed pinned index %s to stay within %d open pins",
                         evicted, self._max_open)
        return ctx


class RoutingContext:
    """A ServiceContext whose parts follow the request.

    The tools were written against a context built once at startup, with the database
    captured in a closure. Rather than rewrite every tool to take an index, this keeps the
    same attribute names and resolves them per request. A tool body is unchanged, and the
    cost is one context variable read and a dictionary lookup, about 50 ns against a tool
    latency measured in milliseconds.
    """

    def __init__(self, registry: ContextRegistry):
        self._registry = registry

    def _ctx(self):
        return self._registry.resolve(current_alias.get())

    @property
    def registry(self) -> ContextRegistry:
        return self._registry

    @property
    def store(self):
        return self._ctx().store

    @property
    def callgraph(self):
        return self._ctx().callgraph

    @property
    def relationships(self):
        return self._ctx().relationships

    @property
    def discovery(self):
        return self._ctx().discovery

    @property
    def source(self):
        # Resolved per request like the rest, which is what makes a pinned request read
        # source at the pin's commit rather than the live index's.
        return self._ctx().source

    def close(self) -> None:
        self._registry.live.close()


class AliasScoped:
    """`asgi_app` with the request's pin alias bound for its duration.

    Wraps rather than replaces the MCP app, so the same session manager serves both routes
    and the alias is decided per request from the path. Binding it to the MCP session
    instead would be wrong: the session id is the key the transport uses, not the URL, so a
    session opened against one pin could be reused against another.

    A class and not a function on purpose. Starlette's `Route` decides what an endpoint is
    by inspecting it: a function becomes a request handler and silently defaults to GET
    only, which answers every MCP POST with 405. An object with `__call__` is taken as an
    ASGI app and passed every method, which is how FastMCP mounts its own endpoint.
    """

    def __init__(self, asgi_app, registry: ContextRegistry):
        self._app = asgi_app
        self._registry = registry

    async def __call__(self, scope, receive, send):
        alias = (scope.get("path_params") or {}).get("alias")
        if alias:
            try:
                self._registry.resolve(alias)   # fail before the transport opens a session
            except UnknownPin:
                from starlette.responses import JSONResponse
                await JSONResponse(
                    {"error": "no such pin", "alias": alias,
                     "hint": "GET /pins lists the pins that exist"},
                    status_code=404)(scope, receive, send)
                return
        token = current_alias.set(alias)
        try:
            await self._app(scope, receive, send)
        finally:
            current_alias.reset(token)


def alias_scoped(asgi_app, registry: ContextRegistry) -> AliasScoped:
    return AliasScoped(asgi_app, registry)
