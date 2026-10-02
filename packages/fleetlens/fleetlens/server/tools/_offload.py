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

_sized = False


def _limiter():
    global _sized
    limiter = anyio.to_thread.current_default_thread_limiter()
    if not _sized:
        limiter.total_tokens = WORKERS
        _sized = True
    return limiter


def offloaded(fn: Callable) -> Callable:
    """`fn` as a coroutine function that runs it on a worker thread.

    `functools.wraps` carries the signature, annotations and docstring across, which is
    what FastMCP reads to build the tool's schema and description.
    """
    @functools.wraps(fn)
    async def run(*args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs),
                                              limiter=_limiter())
    return run
