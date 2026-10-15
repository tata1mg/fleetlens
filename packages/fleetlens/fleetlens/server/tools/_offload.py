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

#: How many tool bodies may run at once.
#:
#: anyio defaults to 40. Most of a tool's cost is building its response in Python, which
#: holds the GIL, so extra threads add switching rather than parallelism and the tools with
#: the largest responses get slower under load. Two hosts, both measured at concurrency 8
#: and 16, agree on the shape:
#:
#:     workers                    2       4       8      40
#:     get_service_graph, c=8    582     892    1263    1343  ms
#:     get_service_graph, c=16  1196    1781       -       -  ms
#:
#: Everything else moves by less than noise, except the full-scan case in `find_symbol`,
#: which is SQL rather than Python, releases the GIL and is about 10% happier with more
#: threads. Two is the better trade while `get_service_graph` is the entry point the tool
#: descriptions point clients at.
#:
#: The pool is not sized for parallelism, which the GIL caps anyway, but so that a slow
#: call does not stop the server. Two slots means one scan still leaves a free slot and two
#: concurrent scans will delay a cheap call; four gave more headroom there and cost a third
#: of `get_service_graph`. A host that mostly serves scans can raise it.
WORKERS = int(os.environ.get("FLEETLENS_TOOL_WORKERS", "0")) or 2

#: How many *I/O-bound* tool bodies may run at once.
#:
#: Every number above was measured on tools that read SQLite and build a response in
#: Python, where the GIL caps parallelism and extra threads cost more than they buy. A tool
#: that spends its time blocked on a socket is the opposite case: it holds no GIL while it
#: waits, so a second thread is free parallelism and the small pool is pure queueing.
#:
#: Semantic search is that case. It embeds the query through the provider before it can
#: search, which is a network round trip, and on the shared pool ten concurrent searches
#: ran two at a time: peak in flight 2, median latency rising 0.5s to 1.0s to 1.5s as
#: concurrency went 1 to 5 to 10, with throughput pinned near two over the call duration.
#:
#: Sized for a remote provider's appetite rather than for this host's cores. Lower it if
#: your provider rate-limits; fleetlens retries a 429 but waiting is not throughput.
IO_WORKERS = int(os.environ.get("FLEETLENS_TOOL_IO_WORKERS", "0")) or 16

#: Per event loop, not per process. anyio's default thread limiter belongs to the running
#: loop, so a module-level "already sized" flag sized the first loop and silently left every
#: later one at anyio's default of 40. One server has one loop, so this never bit in
#: production; it did make the first measurement of this very problem wrong.
_SIZED = RunVar("fleetlens_cpu_limiter_sized", False)
_IO_LIMITER: RunVar = RunVar("fleetlens_io_limiter")


def _limiter():
    limiter = anyio.to_thread.current_default_thread_limiter()
    if not _SIZED.get():
        limiter.total_tokens = WORKERS
        _SIZED.set(True)
    return limiter


def _io_limiter():
    try:
        return _IO_LIMITER.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(IO_WORKERS)
        _IO_LIMITER.set(limiter)
        return limiter


def offloaded(fn: Callable = None, *, io_bound: bool = False) -> Callable:
    """`fn` as a coroutine function that runs it on a worker thread.

    `io_bound` puts it on the larger pool, for a tool whose cost is waiting on something
    outside this process rather than building a response here.

    `functools.wraps` carries the signature, annotations and docstring across, which is
    what FastMCP reads to build the tool's schema and description.
    """
    def wrap(f: Callable) -> Callable:
        @functools.wraps(f)
        async def run(*args, **kwargs):
            limiter = _io_limiter() if io_bound else _limiter()
            return await anyio.to_thread.run_sync(functools.partial(f, *args, **kwargs),
                                                  limiter=limiter)
        return run
    return wrap(fn) if fn is not None else wrap
