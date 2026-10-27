"""The committed tool reference has to match what the server actually registers."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp", reason="needs the server extra")

from fleetlens.server import toolsdoc  # noqa: E402

DOC = Path(__file__).resolve().parents[3] / "docs" / "tools.md"

REGENERATE = (f"regenerate it:\n\n"
              f"    python -m fleetlens.server.toolsdoc > {DOC}\n")


def test_committed_page_matches_a_fresh_render():
    """The whole point of generating the page: it cannot quietly fall behind the code.

    A failure here is not a broken test. It means a tool's name, parameters, defaults or
    docstring changed and the page still describes the old contract.
    """
    assert DOC.exists(), f"{DOC} is missing; {REGENERATE}"
    assert DOC.read_text() == toolsdoc.render(), (
        f"{DOC.name} is out of date with the tool registrations; {REGENERATE}")


def test_every_registered_tool_is_on_the_page():
    """Guards the renderer itself, not just the file.

    `render` walking an empty or partial tool list would produce a page that matches a
    fresh render perfectly and documents nothing, so the test above would still pass.
    """
    names = [t.name for t in toolsdoc._tools()]
    assert len(names) == len(set(names))
    assert len(names) >= 15, f"only {len(names)} tools registered: {names}"
    page = DOC.read_text()
    for name in names:
        assert f"\n## {name}\n" in page, f"{name} is registered but not documented"


def test_conditional_tools_are_the_ones_that_need_a_condition():
    """`AVAILABILITY` is hand-maintained, so it can drift away from the real guards.

    Registering against a bare context, with no embedder, no guidance rows and stdio, leaves
    exactly the unconditional tools. Anything in one set and not the other is a stale entry
    or a new guard nobody wrote down.
    """
    from fleetlens.server.app import build_context
    from fleetlens.server.tools import register_all

    bare = mcp.server.fastmcp.FastMCP("fleetlens")
    ctx = build_context(":memory:")
    try:
        register_all(bare, ctx, http=False)
        import asyncio
        unconditional = {t.name for t in asyncio.run(bare.list_tools())}
    finally:
        ctx.close()

    everything = {t.name for t in toolsdoc._tools()}
    assert everything - unconditional == set(toolsdoc.AVAILABILITY)


def test_module_entry_point_prints_the_page():
    """`python -m fleetlens.server.toolsdoc` is what the page and the error message promise."""
    out = subprocess.run([sys.executable, "-m", "fleetlens.server.toolsdoc"],
                         capture_output=True, text=True, check=True)
    assert out.stdout == toolsdoc.render()
