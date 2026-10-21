"""Tool bodies run off the event loop, on a pool sized for what they contend on."""
from __future__ import annotations

import asyncio
import threading
import time

from fleetlens.server.tools._offload import CPU_WORKERS, WORKERS, offloaded


def _counting(cpu_bound: bool, delay: float = 0.1):
    """(callable, state) where state["peak"] is the most that ever ran at once."""
    state = {"live": 0, "peak": 0}
    lock = threading.Lock()

    @offloaded(cpu_bound=cpu_bound)
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
    asyncio.run(_gather(fn, n))


async def _gather(fn, n):
    await asyncio.gather(*[fn() for _ in range(n)])


def test_tools_run_in_parallel_by_default():
    """The question is not whether a tool does I/O but whether it holds the GIL, and almost
    none of them do: a socket wait, a sqlite3 C call and a numpy product all release it.
    Ten concurrent semantic searches used to run two at a time."""
    fn, state = _counting(cpu_bound=False)
    _run(fn, 10)
    assert state["peak"] == 10


def test_a_cpu_bound_tool_is_held_to_a_small_pool():
    """Building a large response in Python does hold the GIL, and more threads then cost
    switching rather than buying parallelism: get_service_graph went 582ms to 1343ms as the
    pool went 2 to 40."""
    fn, state = _counting(cpu_bound=True)
    _run(fn, 10)
    assert state["peak"] <= CPU_WORKERS


def test_the_pools_do_not_share_capacity():
    """A heavy graph response must not stall the cheap calls, which is the reason the small
    pool exists at all."""
    cpu, cpu_state = _counting(cpu_bound=True, delay=0.2)
    rest, rest_state = _counting(cpu_bound=False, delay=0.2)

    async def main():
        await asyncio.gather(*([cpu() for _ in range(6)] + [rest() for _ in range(6)]))

    asyncio.run(main())
    assert cpu_state["peak"] <= CPU_WORKERS
    assert rest_state["peak"] == 6


def test_the_default_pool_is_wider_than_the_cpu_one():
    assert WORKERS > CPU_WORKERS


def test_the_bare_decorator_still_works():
    """@offloaded with no arguments is how every tool is written today."""
    @offloaded
    def plain():
        return "ok"

    assert asyncio.run(plain()) == "ok"


def test_the_signature_survives_the_wrapper():
    """FastMCP reads the wrapped function to build the tool schema and description."""
    @offloaded(cpu_bound=True)
    def documented(topic: str = "", service: str = "") -> dict:
        """What it does."""
        return {"topic": topic, "service": service}

    assert documented.__doc__ == "What it does."
    assert documented.__name__ == "documented"
    assert asyncio.run(documented(topic="x")) == {"topic": "x", "service": ""}


def test_each_event_loop_sizes_its_own_limiter():
    """anyio's default thread limiter belongs to the running loop. A module-level "already
    sized" flag sized the first loop and left every later one at anyio's default of 40,
    which is how a measurement of this very problem came out wrong."""
    fn, first = _counting(cpu_bound=True)
    _run(fn, 8)
    fn2, second = _counting(cpu_bound=True)
    _run(fn2, 8)                      # a second, separate event loop
    assert first["peak"] <= CPU_WORKERS
    assert second["peak"] <= CPU_WORKERS


def test_the_graph_tool_is_the_one_marked_cpu_bound():
    """If another tool is ever marked, it should be because it was measured, not copied."""
    import inspect

    from fleetlens.server.tools import service_graph

    src = inspect.getsource(service_graph)
    assert src.count("cpu_bound=True") == 1
    marked = src.split("cpu_bound=True", 1)[1].split("def ", 1)[1].split("(", 1)[0]
    assert marked == "get_service_graph"
