"""Talk to a running fleetlens server the way an agent does, and check what comes back.

The rest of CI proves the process starts, the token is enforced and pins can be created.
None of that touches the MCP protocol: `/mcp` is only ever asked for a 401. So the one
thing the README actually promises, that an agent asks a question and gets a correct
cross-repository answer, was the part with no coverage.

This speaks real streamable HTTP, calls the tool an agent is told to call first, and
asserts the edge between two repositories is in the reply, with its evidence. Run against
the live index and again against a pin, because serving a snapshot goes through a different
context lookup and "the pin quietly served the live index" is the failure pinning exists to
prevent.

    python mcp_smoke.py --url http://localhost:8081 --token "$TOKEN" \
                        --expect-edge gateway:orders [--pin ci-pin]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

try:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
except ImportError:                                     # pragma: no cover
    raise SystemExit("needs the mcp client: pip install 'mcp>=1.2.0,<2.0'")


def _payload(result) -> dict:
    """A tool's JSON body, whichever way this mcp version returns it."""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        # Some versions wrap a non-object return under a single key.
        return structured.get("result", structured)
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError(f"no JSON payload in tool result: {result!r}")


async def _probe(base: str, token: str, want: tuple[str, str], pin: str | None) -> None:
    url = f"{base}/pins/{pin}/mcp" if pin else f"{base}/mcp"
    where = f"pin {pin!r}" if pin else "the live index"
    headers = {"Authorization": f"Bearer {token}"}

    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            names = {t.name for t in (await session.list_tools()).tools}
            assert "get_service_graph" in names, f"{where}: tools missing, got {sorted(names)}"

            graph = _payload(await session.call_tool("get_service_graph", {}))
            caller, callee = want
            match = [e for e in graph["edges"]
                     if e["from"] == f"service:{caller}"
                     and e["to"] == f"service:{callee}"
                     and e["relationship"] == "calls"]
            assert len(match) == 1, (
                f"{where}: expected one {caller} -> {callee} calls edge, "
                f"got {json.dumps(graph['edges'], indent=2)}")
            edge = match[0]
            # An edge with no evidence is a claim. The request path that produced it is
            # what a reader checks, so its absence is a failure even when the edge is right.
            assert edge.get("via_paths"), f"{where}: edge carries no evidence: {edge}"
            print(f"  {where}: {caller} -> {callee} "
                  f"({edge.get('confidence')}, via {edge['via_paths']})")

            info = _payload(await session.call_tool("get_index_info", {}))
            serving = info.get("serving") or {}
            if pin:
                assert serving.get("pin") == pin and serving.get("live") is False, (
                    f"asked for pin {pin!r} and was served {serving!r}; "
                    "silently answering from the live index is what pinning must prevent")
            else:
                assert serving.get("live") is not False, f"expected the live index, got {serving!r}"
            assert info.get("build_id"), f"{where}: no build id in {info!r}"
            print(f"  {where}: build {info['build_id']}, serving {serving}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="server base, e.g. http://localhost:8081")
    ap.add_argument("--token", required=True)
    ap.add_argument("--expect-edge", required=True, metavar="CALLER:CALLEE")
    ap.add_argument("--pin", default=None, help="probe this pin instead of the live index")
    args = ap.parse_args()

    caller, _, callee = args.expect_edge.partition(":")
    if not (caller and callee):
        raise SystemExit("--expect-edge takes CALLER:CALLEE")

    asyncio.run(_probe(args.url.rstrip("/"), args.token, (caller, callee), args.pin))
    print("mcp smoke: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
