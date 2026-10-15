"""Run a synchronous tool off the event loop.

FastMCP calls a tool and, if it is not a coroutine function, calls it directly:

    if fn_is_async:
        return await fn(**arguments)
    else:
        return fn(**arguments)

Every tool here reads SQLite and builds a response synchronously, so each call held the
event loop for its whole duration and the server answered one request at a time. Measured
on a four-core host, latency rose exactly in step with the number of concurrent sessions
and throughput never moved: `get_index_info` stayed at 7 requests a second whether one
client asked or sixteen, and one slow call delayed every other client by its full duration.

Wrapping each tool so it runs on a worker thread fixes the blocking. How much throughput
that buys depends on the tool: `sqlite3` releases the GIL around the C call, so a tool whose
cost is a query parallelises, while one whose cost is building a large Python response does
not. The head-of-line blocking goes either way, which is the part that matters on a server
several engineers share.

Safe because the served store is opened read-only and hands out a connection per thread;
SQLite allows any number of concurrent readers.
"""
from __future__ import annotations

import functools
import os
from collections.abc import Callable

import anyio.to_thread
from anyio.lowlevel import RunVar

#: How many tool bodies may run at once by default.
#:
#: The question a pool size answers is not "is this tool I/O" but "does it hold the GIL
#: while it works", and for almost every tool here the answer is no. A thread blocked on a
#: socket releases it. `sqlite3` releases it around the C call. numpy releases it for a
#: matrix product. What holds it is building a large response in Python, and exactly one
#: tool does enough of that to matter.
#:
#: So the pool is wide and the one tool that contends is annotated, rather than the other
#: way round. The earlier default of 2 was set for that one tool and charged to every
#: other: ten concurrent semantic searches ran two at a time, because each embeds its query
#: through the provider first and then waits on a socket that holds nothing.
#:
#:     concurrency                  1       5      10
#:     at 2 workers, median      0.52s   1.01s   1.51s      (peak in flight: 2)
#:     at this default, median   0.52s   0.50s   0.51s      (peak in flight: 10)
#:
#: Sized for waiting, not for cores: these threads are mostly parked. Beyond this the
#: constraint is the provider's rate limit rather than the host.
WORKERS = int(os.environ.get("FLEETLENS_TOOL_WORKERS", "0")) or 16

#: How many *GIL-bound* tool bodies may run at once, for tools marked `cpu_bound`.
#:
#: Measured on two hosts at concurrency 8 and 16, which agree on the shape:
#:
#:     workers                    2       4       8      40
#:     get_service_graph, c=8    582     892    1263    1343  ms
#:     get_service_graph, c=16  1196    1781       -       -  ms
#:
#: `get_service_graph` hands back the whole dependency graph, and assembling that response
#: is Python dict and list construction. Extra threads buy nothing the GIL will allow and
#: cost switching, so it gets slower under load. Everything else moved by less than noise,
#: and `find_symbol`'s full scan, which is SQL rather than Python, was about 10% happier
#: with more threads.
#:
#: This is not sized for parallelism, which the GIL caps anyway, but so that a slow call
#: does not stop the server: two slots mean one heavy response still leaves a free slot.
CPU_WORKERS = int(os.environ.get("FLEETLENS_TOOL_CPU_WORKERS", "0")) or 2

#: Per event loop, not per process. anyio's default thread limiter belongs to the running
#: loop, so a module-level "already sized" flag sized the first loop and silently left every
#: later one at anyio's default of 40. One server has one loop, so this never bit in
#: production; it did make the first measurement of this very problem wrong.
_SIZED = RunVar("fleetlens_default_limiter_sized", False)
_CPU_LIMITER: RunVar = RunVar("fleetlens_cpu_limiter")


def _limiter():
    limiter = anyio.to_thread.current_default_thread_limiter()
    if not _SIZED.get():
        limiter.total_tokens = WORKERS
        _SIZED.set(True)
    return limiter


def _cpu_limiter():
    try:
        return _CPU_LIMITER.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(CPU_WORKERS)
        _CPU_LIMITER.set(limiter)
        return limiter


def offloaded(fn: Callable = None, *, cpu_bound: bool = False) -> Callable:
    """`fn` as a coroutine function that runs it on a worker thread.

    Mark a tool `cpu_bound` when the bulk of its cost is building its response in Python,
    which holds the GIL: it then runs on a small pool of its own, so several of them cannot
    contend with each other or stall the cheap calls. Leave it off otherwise, including for
    anything that waits on a network, a subprocess or SQLite.

    `functools.wraps` carries the signature, annotations and docstring across, which is
    what FastMCP reads to build the tool's schema and description.
    """
    def wrap(f: Callable) -> Callable:
        @functools.wraps(f)
        async def run(*args, **kwargs):
            limiter = _cpu_limiter() if cpu_bound else _limiter()
            return await anyio.to_thread.run_sync(functools.partial(f, *args, **kwargs),
                                                  limiter=limiter)
        return run
    return wrap(fn) if fn is not None else wrap
