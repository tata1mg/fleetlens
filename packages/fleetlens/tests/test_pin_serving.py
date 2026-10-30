"""Serving a pinned index from the same server, chosen per request by URL."""
from __future__ import annotations

import threading

import pytest
from fleetlens.server.routing import (
    ContextRegistry,
    RoutingContext,
    UnknownPin,
    alias_scoped,
    current_alias,
)


class FakeStore:
    def __init__(self, name):
        self.name = name
        self.closed = False

    def close(self):
        self.closed = True


class FakeCtx:
    def __init__(self, name):
        self.store = FakeStore(name)
        self.callgraph = f"cg-{name}"
        self.relationships = f"rel-{name}"
        self.discovery = f"disc-{name}"
        self.source = f"src-{name}"

    def close(self):
        self.store.close()


class FakePins:
    """Stands in for PinStore: an alias exists if it is in `present`."""

    def __init__(self, present):
        self.present = set(present)

    def snapshot_path(self, alias):
        return f"/pins/{alias}.db" if alias in self.present else None


def _registry(present=("a", "b", "c", "d"), max_open=3):
    built = []

    def build(path):
        built.append(path)
        return FakeCtx(path)

    reg = ContextRegistry(FakeCtx("live"), FakePins(present), build, max_open=max_open)
    return reg, built


def test_no_alias_serves_the_live_index():
    reg, built = _registry()
    assert reg.resolve(None) is reg.live
    assert reg.resolve("") is reg.live
    assert built == [], "the live index must not be re-opened"


def test_an_alias_opens_that_pin_once_and_is_then_cached():
    reg, built = _registry()
    first = reg.resolve("a")
    assert reg.resolve("a") is first
    assert built == ["/pins/a.db"], "a cached pin must not be re-opened"


def test_an_unknown_pin_is_an_error_not_the_live_index():
    """Quietly answering from the wrong index is the failure pinning exists to prevent."""
    reg, _ = _registry(present=("a",))
    with pytest.raises(UnknownPin):
        reg.resolve("gone")


def test_the_cache_is_bounded_and_evicts_least_recently_used():
    """Each open pin holds its own vector block, about 92 MB at 1536 dimensions, so this
    is a hard cap rather than a hint."""
    reg, _ = _registry(max_open=2)
    a, b = reg.resolve("a"), reg.resolve("b")
    reg.resolve("a")                      # a is now the most recently used
    reg.resolve("c")                      # evicts b, not a
    assert sorted(reg.open_aliases()) == ["a", "c"]
    assert reg.resolve("a") is a
    assert reg.resolve("b") is not b, "b was evicted and must be rebuilt"


def test_eviction_drops_rather_than_closes():
    """A SqliteStore keeps per-thread connections, so the evicting thread cannot close
    them, and a request still running must not lose its database."""
    reg, _ = _registry(max_open=1)
    a = reg.resolve("a")
    reg.resolve("b")
    assert not a.store.closed


def test_a_concurrent_open_of_one_pin_keeps_a_single_copy():
    """Two requests for one pin must not hold two copies of its vector block."""
    reg, built = _registry()
    seen = []
    barrier = threading.Barrier(4)

    def go():
        barrier.wait()
        seen.append(reg.resolve("a"))

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(x) for x in seen}) == 1, "all callers must share one context"
    assert reg.open_aliases() == ["a"]


def test_the_routing_context_follows_the_current_alias():
    """The tools were registered against one context object and are unchanged; it resolves
    per request instead."""
    reg, _ = _registry()
    ctx = RoutingContext(reg)
    assert ctx.store.name == "live"

    token = current_alias.set("a")
    try:
        assert ctx.store.name == "/pins/a.db"
        assert ctx.relationships == "rel-/pins/a.db"
        assert ctx.discovery == "disc-/pins/a.db"
        # `source` reads files rather than rows, so following the alias is what keeps a
        # pinned read at the pin's commit instead of today's working tree.
        assert ctx.source == "src-/pins/a.db"
    finally:
        current_alias.reset(token)
    assert ctx.store.name == "live", "the alias must not leak past the request"


def test_closing_the_routing_context_closes_only_the_live_index():
    reg, _ = _registry()
    ctx = RoutingContext(reg)
    pinned = reg.resolve("a")
    ctx.close()
    assert reg.live.store.closed
    assert not pinned.store.closed


@pytest.mark.anyio
async def test_the_wrapper_binds_the_alias_for_the_request():
    reg, _ = _registry()
    saw = []

    class Inner:
        async def __call__(self, scope, receive, send):
            saw.append(current_alias.get())

    app = alias_scoped(Inner(), reg)
    await app({"type": "http", "path_params": {"alias": "a"}}, None, None)
    assert saw == ["a"]
    assert current_alias.get() is None, "the alias must be reset after the request"


def test_the_wrapper_is_an_asgi_app_not_a_request_handler():
    """Starlette decides by inspection: a function endpoint silently becomes GET-only, so
    every MCP POST would answer 405. This must be an object with __call__."""
    import inspect

    app = alias_scoped(object(), _registry()[0])
    assert not inspect.isfunction(app) and not inspect.ismethod(app)
    assert callable(app)


@pytest.fixture
def anyio_backend():
    return "asyncio"
