"""Render the MCP tool reference from the live registrations.

The tool contract already exists, as docstrings and signatures on the functions the server
registers. Copying it into Markdown by hand would create a second copy that drifts the
first time a default changes, and nothing would notice: a stale parameter default in a
reference page reads exactly like a current one.

So the page is generated from a real `FastMCP` instance. Whatever the server would actually
advertise is what gets documented, and `test_toolsdoc.py` fails when the committed file and
a fresh render disagree.

Four of the tools are registered conditionally, which is the part a reader most needs and
the part a hand-written page is least likely to keep right. `_full_context` turns every one
of those conditions on, so the page covers all of them, and `AVAILABILITY` says what each
one needs.
"""
from __future__ import annotations

import asyncio
import inspect
import os
from typing import Any

from ..store.models import KnowledgeObject
from .app import ServiceContext, build_context
from .tools import register_all

HEADER = "fleetlens MCP tools"

#: What a tool needs before the server will register it. Tools absent from this map are
#: always registered. Kept here rather than read from the guards themselves because the
#: guards are three different kinds of check (a constructor argument, a row count and an
#: environment variable) with no common shape to introspect.
AVAILABILITY = {
    "discover_services": "an embedding model is configured (`--embed-model`)",
    "discover_interfaces": "an embedding model is configured (`--embed-model`)",
    "discover_libraries": "an embedding model is configured (`--embed-model`)",
    "get_engineering_guidance": "the index contains guidance objects",
    "get_source": "`--http` **and** `FLEETLENS_EXPOSE_SOURCE` is set",
}

_ALWAYS = "always"


class _StubEmbedder:
    """Stands in for a configured embedding model.

    `discovery.register` checks only that an embedder exists, so nothing here is called.
    Rendering the page must not need a model on disk or a network call.
    """

    def embed(self, texts):  # pragma: no cover - never invoked during a render
        raise NotImplementedError("the doc renderer never embeds anything")


def _full_context() -> ServiceContext:
    """An in-memory context with every conditional registration satisfied."""
    ctx = build_context(":memory:", embedder=_StubEmbedder())
    ctx.store.upsert_object(KnowledgeObject(
        object_type="guidance", object_id="sample:rule", name="Sample rule",
        summary="Present so the guidance tools register.", version="1",
        source="manual", generation_strategy="authored", last_generated_at=None,
        embed_text=None, payload={"status": "recommended"},
    ))
    return ctx


def _tools() -> list[Any]:
    os.environ["FLEETLENS_EXPOSE_SOURCE"] = "1"
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("fleetlens")
    ctx = _full_context()
    try:
        register_all(mcp, ctx, http=True)
        return sorted(asyncio.run(mcp.list_tools()), key=lambda t: t.name)
    finally:
        ctx.close()


def _type_of(spec: dict) -> str:
    """A JSON-schema property as one word a reader recognises."""
    if "anyOf" in spec:
        inner = [s.get("type") for s in spec["anyOf"] if s.get("type") not in (None, "null")]
        return f"{inner[0]} or null" if inner else "any"
    return spec.get("type", "any")


def _params(schema: dict) -> list[tuple[str, str, str]]:
    """(name, type, default) per parameter, required ones first and in signature order."""
    required = set(schema.get("required") or ())
    rows = []
    for name, spec in (schema.get("properties") or {}).items():
        default = "**required**" if name in required else f"`{spec['default']!r}`" \
            if "default" in spec else "-"
        rows.append((name, _type_of(spec), default))
    return sorted(rows, key=lambda r: r[2] != "**required**")


def _describe(text: str) -> str:
    """A docstring as prose.

    `inspect.cleandoc` rather than a manual dedent: the first line of these docstrings is
    flush against the quotes and the rest are indented to the function body, which is
    exactly the case it handles and a naive `textwrap.dedent` does not.
    """
    return inspect.cleandoc(text or "").strip()


def render() -> str:
    tools = _tools()
    out: list[str] = [
        f"# {HEADER}", "",
        "Generated from the server's own tool registrations by",
        "`python -m fleetlens.server.toolsdoc`. Do not edit by hand: a test regenerates this",
        "page and fails when it differs, so a signature or docstring change that is not",
        "reflected here turns CI red.", "",
        f"{len(tools)} tools. Descriptions are verbatim from the docstrings an agent reads",
        "over MCP, so what you see here is what it sees.", "",
        "## Availability", "",
        "Tools are registered only when they can answer. A tool that is absent is not an",
        "error: an advertised tool that always fails costs an agent a turn and teaches it to",
        "stop trusting the server. A default `fl serve` against an index with no guidance and",
        "no embeddings advertises "
        f"{sum(1 for t in tools if t.name not in AVAILABILITY)} of these {len(tools)}.", "",
        "| Tool | Registered when |", "|---|---|",
    ]
    for t in tools:
        out.append(f"| [`{t.name}`](#{t.name}) | {AVAILABILITY.get(t.name, _ALWAYS)} |")
    out.append("")

    for t in tools:
        out += ["---", "", f"## {t.name}", ""]
        if t.name in AVAILABILITY:
            out += [f"**Registered when** {AVAILABILITY[t.name]}.", ""]
        rows = _params(t.inputSchema or {})
        if rows:
            out += ["| Parameter | Type | Default |", "|---|---|---|"]
            out += [f"| `{n}` | {ty} | {d} |" for n, ty, d in rows]
        else:
            out.append("Takes no parameters.")
        out += ["", _describe(t.description), ""]

    return "\n".join(out).rstrip() + "\n"


if __name__ == "__main__":
    print(render(), end="")
