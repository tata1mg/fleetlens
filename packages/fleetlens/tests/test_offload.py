"""Tool bodies run off the event loop, on a pool sized for what they wait on."""
from __future__ import annotations

import asyncio
import threading
import time

from fleetlens.server.tools._offload import IO_WORKERS, WORKERS, offloaded


def _counting(io_bound: bool, delay: float = 0.1):
    """(callable, peak) where peak[0] is the most that ever ran at once."""
    state = {"live": 0, "peak": 0}
    lock = threading.Lock()

    @offloaded(io_bound=io_bound)
    def fn():
        with lock:
            state["live"] += 1
            state["peak"] = max(state["peak"], state["live"])
        try:
            time.sleep(delay)
        finally:
            with lock:
                state["live"] -= 1

    return fn, state


def _run(fn, n):
    async def main():
        await asyncio.gather(*[fn() for _ in range(n)])
    asyncio.run(main())


def test_a_cpu_bound_tool_is_capped_at_the_small_pool():
    """Most of a tool's cost is building its response in Python, which holds the GIL, so
    extra threads cost switching rather than buying parallelism."""
    fn, state = _counting(io_bound=False)
    _run(fn, 10)
    assert state["peak"] <= WORKERS


def test_an_io_bound_tool_is_not():
    """A thread blocked on a socket holds no GIL, so the small pool is pure queueing. Ten
    concurrent semantic searches ran two at a time before this."""
    fn, state = _counting(io_bound=True)
    _run(fn, 10)
    assert state["peak"] > WORKERS
    assert state["peak"] <= IO_WORKERS


def test_the_two_pools_do_not_share_capacity():
    """A slow scan must not starve a search, and vice versa."""
    cpu, cpu_state = _counting(io_bound=False, delay=0.2)
    io, io_state = _counting(io_bound=True, delay=0.2)

    async def main():
        await asyncio.gather(*([cpu() for _ in range(6)] + [io() for _ in range(6)]))

    asyncio.run(main())
    assert cpu_state["peak"] <= WORKERS
    assert io_state["peak"] > WORKERS


def test_the_bare_decorator_still_works():
    """@offloaded with no arguments is the common case and must keep working."""
    @offloaded
    def plain():
        return "ok"

    assert asyncio.run(plain()) == "ok"


def test_the_signature_survives_the_wrapper():
    """FastMCP reads the wrapped function to build the tool schema and description."""
    @offloaded(io_bound=True)
    def documented(topic: str = "", service: str = "") -> dict:
        """What it does."""
        return {"topic": topic, "service": service}

    assert documented.__doc__ == "What it does."
    assert documented.__name__ == "documented"
    assert asyncio.run(documented(topic="x")) == {"topic": "x", "service": ""}


def test_each_event_loop_sizes_its_own_limiter():
    """anyio's default thread limiter belongs to the running loop. A module-level "already
    sized" flag sized the first loop and left every later one at anyio's default of 40."""
    fn, first = _counting(io_bound=False)
    _run(fn, 8)
    fn2, second = _counting(io_bound=False)
    _run(fn2, 8)                      # a second, separate event loop
    assert first["peak"] <= WORKERS
    assert second["peak"] <= WORKERS
