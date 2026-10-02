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
from collections.abc import Callable

import anyio.to_thread


def offloaded(fn: Callable) -> Callable:
    """`fn` as a coroutine function that runs it on a worker thread.

    `functools.wraps` carries the signature, annotations and docstring across, which is
    what FastMCP reads to build the tool's schema and description.
    """
    @functools.wraps(fn)
    async def run(*args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
    return run
