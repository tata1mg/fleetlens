#!/usr/bin/env python3
"""Latency and throughput of the fleetlens MCP server, per tool, under load.

Measures what an agent actually experiences: the official MCP client over streamable HTTP,
including transport and session overhead, against a running server. Not a microbenchmark of
the store — `fl index --stats` covers that.

Two classes of tool, reported apart because their latencies differ by orders of magnitude
and averaging them would hide both:

  deterministic   SQLite reads: list_services, get_symbol, get_callers, ...
  semantic        vector search plus an embedding call: discover_services, discover_interfaces

Arguments are sampled from the index itself through the server, so the run exercises many
different services, symbols and interfaces rather than measuring a cache hit on one id.

    python bench/mcp_perf.py --url http://host:8081/mcp --token "$FLEETLENS_TOKEN" \
        --concurrency 1,4,8,16 --requests 60 --note "8 vCPU, 16 GiB, Ollama on same host"

Writes a JSON record and prints a Markdown table ready for the README.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import random
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
except ImportError:  # pragma: no cover
    raise SystemExit("needs the mcp client: pip install 'mcp>=1.2.0'")

#: Tools whose cost is a vector comparison and an embedding call, not a SQLite read.
SEMANTIC = {"discover_services", "discover_interfaces"}

#: Variants that exercise one tool under a second workload.
LABELS = {"find_symbol (no match)": "find_symbol"}

#: Free-text queries for the search tools. Deliberately ordinary words rather than terms
#: lifted from this fleet, so the file carries no one organisation's vocabulary.
QUERIES = ["order", "payment", "user", "notification", "search", "cart", "inventory",
           "auth", "report", "webhook"]

#: Terms chosen to match nothing. A substring search that finds nothing cannot stop early,
#: so it is the worst case for `find_symbol` and is reported on its own line: blending it
#: with hits would hide both that lookups are sub-millisecond and that misses are not.
MISSES = ["qqzzxx", "nosuchsymbolanywhere", "zzzz1234", "absent_identifier"]


# --- sampling ------------------------------------------------------------------------

async def _call(session: ClientSession, tool: str, args: dict):
    """One tool call, returning (seconds, ok). Errors are counted, never raised: a server
    that fails under load is a result, not a reason to abandon the run."""
    t = time.perf_counter()
    try:
        res = await session.call_tool(tool, args)
        ok = not getattr(res, "isError", False)
    except Exception:                                   # noqa: BLE001
        ok = False
    return time.perf_counter() - t, ok


def _payload(result) -> dict:
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {}
    return {}


async def sample_inputs(session: ClientSession, width: int) -> dict:
    """Realistic arguments for every tool, discovered through the server.

    Asking the index what exists keeps the harness runnable against any fleet, and keeps a
    run from measuring the same row repeatedly.
    """
    services = _payload(await session.call_tool("list_services", {})).get("services", [])
    ids = [s["id"] for s in services if s.get("id")]
    random.shuffle(ids)
    service_ids = ids[:width] or ["service:unknown"]

    interface_ids: list = []
    for sid in service_ids[:width]:
        got = _payload(await session.call_tool("list_interfaces", {"service_id": sid}))
        interface_ids += [i["id"] for i in got.get("interfaces", []) if i.get("id")]
        if len(interface_ids) >= width:
            break

    symbol_ids: list = []
    for q in QUERIES:
        got = _payload(await session.call_tool("find_symbol", {"query": q, "limit": 10}))
        found = got.get("symbols", [])
        symbol_ids += [s if isinstance(s, str) else s.get("id") for s in found]
        if len(symbol_ids) >= width:
            break
    symbol_ids = [s for s in symbol_ids if s]

    def cycle(pool, fallback):
        return pool or [fallback]

    return {
        "list_services": [{}],
        "get_index_info": [{}],
        "get_service_graph": [{}],
        "list_interfaces": [{"service_id": s} for s in service_ids],
        "get_service_relationships": [{"service_id": s} for s in service_ids],
        "find_symbol": [{"query": q, "limit": 20} for q in QUERIES],
        "find_symbol (no match)": [{"query": q, "limit": 20} for q in MISSES],
        "get_symbol": [{"symbol_id": s} for s in cycle(symbol_ids, "code_symbol:x:y")],
        "get_callers": [{"symbol_id": s, "max_depth": 2}
                        for s in cycle(symbol_ids, "code_symbol:x:y")],
        "get_callees": [{"symbol_id": s, "max_depth": 2}
                        for s in cycle(symbol_ids, "code_symbol:x:y")],
        "get_endpoint_call_graph": [{"interface_id": i, "max_depth": 3}
                                    for i in cycle(interface_ids, "interface:x:y")],
        "discover_services": [{"query": q, "limit": 5} for q in QUERIES],
        "discover_interfaces": [{"query": q, "limit": 5} for q in QUERIES],
    }


# --- running -------------------------------------------------------------------------

class Client:
    """One MCP session. Concurrency is modelled as N of these, because that is what N
    agents are: separate sessions, not pipelined calls down one."""

    def __init__(self, url: str, headers: dict):
        self.url, self.headers = url, headers
        self._cm = self._session = None

    async def __aenter__(self):
        self._cm = streamablehttp_client(self.url, headers=self.headers)
        read, write, _ = await self._cm.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        return self._session

    async def __aexit__(self, *exc):
        try:
            await self._session.__aexit__(*exc)
        finally:
            await self._cm.__aexit__(*exc)


async def measure(url: str, headers: dict, label: str, args_pool: list,
                  concurrency: int, requests: int) -> dict:
    """`requests` calls spread over `concurrency` sessions, as one timed batch.

    `label` may name a variant rather than a tool, so one tool can be reported under more
    than one workload.
    """
    tool = LABELS.get(label, label)
    per = max(1, requests // concurrency)
    results: list = []

    async def worker(n: int):
        async with Client(url, headers) as session:
            for i in range(per):
                results.append(await _call(session, tool, args_pool[(n + i) % len(args_pool)]))

    started = time.perf_counter()
    await asyncio.gather(*(worker(n) for n in range(concurrency)))
    wall = time.perf_counter() - started

    lat = sorted(ms * 1000 for ms, ok in results if ok)
    errors = sum(1 for _, ok in results if not ok)
    if not lat:
        return {"tool": label, "concurrency": concurrency, "calls": len(results),
                "errors": errors, "note": "every call failed"}
    pct = lambda p: lat[min(len(lat) - 1, int(len(lat) * p))]        # noqa: E731
    return {
        "tool": label, "concurrency": concurrency, "calls": len(results), "errors": errors,
        "p50_ms": round(statistics.median(lat), 2), "p95_ms": round(pct(0.95), 2),
        "p99_ms": round(pct(0.99), 2), "max_ms": round(lat[-1], 2),
        "throughput_rps": round(len(results) / wall, 1), "wall_s": round(wall, 2),
    }


async def run(args) -> dict:
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    levels = [int(c) for c in args.concurrency.split(",")]

    async with Client(args.url, headers) as session:
        index = _payload(await session.call_tool("get_index_info", {}))
        pool = await sample_inputs(session, args.width)
        tools = [t.name for t in (await session.list_tools()).tools]
        # First semantic call builds the in-memory vector block; charge that to a cold
        # measurement rather than to whichever tool happened to run first.
        cold = {}
        for tool in (t for t in tools if t in SEMANTIC):
            secs, ok = await _call(session, tool, pool[tool][0])
            cold[tool] = {"first_call_ms": round(secs * 1000, 2), "ok": ok}

    rows = []
    for tool in pool:
        if LABELS.get(tool, tool) not in tools:
            continue
        for c in levels:
            rows.append(await measure(args.url, headers, tool, pool[tool], c, args.requests))
            print(f"  {tool:28} c={c:<3} "
                  f"p50={rows[-1].get('p50_ms', '-'):>8}ms  "
                  f"p95={rows[-1].get('p95_ms', '-'):>8}ms  "
                  f"{rows[-1].get('throughput_rps', '-'):>6} rps  "
                  f"err={rows[-1]['errors']}")

    return {
        "when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "url": args.url, "note": args.note, "client": platform.platform(),
        "index": index, "cold_start": cold, "requests_per_cell": args.requests,
        "semantic_available": sorted(set(tools) & SEMANTIC), "rows": rows,
    }


# --- reporting -----------------------------------------------------------------------

def markdown(report: dict) -> str:
    idx = report.get("index", {})
    out = [
        "### MCP server performance", "",
        f"Index under test: {idx.get('services', '?')} services, "
        f"{idx.get('interfaces', '?')} interfaces, {idx.get('symbols', '?')} symbols, "
        f"{idx.get('relationships', '?')} relationships.",
    ]
    if report.get("note"):
        out += ["", f"Host: {report['note']}."]
    levels = sorted({r["concurrency"] for r in report["rows"]})
    for title, keep in (("Deterministic tools", False), ("Semantic tools", True)):
        tools = sorted({r["tool"] for r in report["rows"]
                        if (r["tool"] in SEMANTIC) == keep})
        if not tools:
            continue
        out += ["", f"**{title}** — p50 / p95 in ms, by concurrent sessions", "",
                "| tool | " + " | ".join(f"c={c}" for c in levels) + " | peak rps |",
                "|---|" + "---|" * (len(levels) + 1)]
        for tool in tools:
            cells, rps = [], 0.0
            for c in levels:
                r = next((x for x in report["rows"]
                          if x["tool"] == tool and x["concurrency"] == c), None)
                cells.append("err" if not r or "p50_ms" not in r
                             else f"{r['p50_ms']:.1f} / {r['p95_ms']:.1f}")
                rps = max(rps, (r or {}).get("throughput_rps", 0))
            out.append(f"| `{tool}` | " + " | ".join(cells) + f" | {rps:.0f} |")
    if report.get("cold_start"):
        first = ", ".join(f"`{k}` {v['first_call_ms']:.0f}ms"
                          for k, v in report["cold_start"].items())
        out += ["", f"First semantic call after start, which builds the vector block: {first}."]
    if not report.get("semantic_available"):
        out += ["", "Semantic tools were not exposed by this server, so they are not "
                    "measured here. They are registered only when an embedding model is "
                    "configured (`fl serve --embed-model ...`)."]
    errs = sum(r["errors"] for r in report["rows"])
    out += ["", f"{sum(r['calls'] for r in report['rows'])} calls, {errs} errors."]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=os.environ.get("FLEETLENS_URL",
                                                    "http://127.0.0.1:8081/mcp"))
    ap.add_argument("--token", default=os.environ.get("FLEETLENS_TOKEN", ""))
    ap.add_argument("--concurrency", default="1,4,8,16")
    ap.add_argument("--requests", type=int, default=60, help="calls per tool per level")
    ap.add_argument("--width", type=int, default=20, help="distinct ids to sample per kind")
    ap.add_argument("--note", default="", help="host description for the report")
    ap.add_argument("--out", default="bench/results")
    args = ap.parse_args()

    random.seed(0)                       # same sample of ids between runs
    report = asyncio.run(run(args))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (out / f"mcp_perf_{stamp}.json").write_text(json.dumps(report, indent=1) + "\n")
    md = markdown(report)
    (out / f"mcp_perf_{stamp}.md").write_text(md + "\n")
    print("\n" + md)
    print(f"\nwritten to {out}/mcp_perf_{stamp}.{{json,md}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
