"""get_source: what it refuses to do, and which commit it reads.

Two properties carry this feature. It must not exist unless an operator asked for it, and
what it returns must match the index that pointed at it. The rest is bounds.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fleetlens import indexing
from fleetlens.server.app import build_context
from fleetlens.server.tools import register_all
from fleetlens.server.tools import source as source_tool
from fleetlens.store.sqlite import SqliteStore

ORIGINAL = """\
from fastapi import FastAPI

app = FastAPI()


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    if not order_id.isdigit():
        raise ValueError("order_id must be numeric")
    return {"id": order_id}
"""

REWRITTEN = """\
# a comment that pushes everything down
# and another


from fastapi import FastAPI

app = FastAPI()


def something_entirely_different():
    return "not the function the index recorded"
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _build(base: Path):
    """A one-repo fleet, committed and indexed, with the store still open.

    The real indexer runs rather than a stub, because symbols are what this feature reads
    and they come from the call-graph step. scip-python fails on a fixture with no
    virtualenv, which is fine: the tree-sitter symbol index still produces the spans.
    """
    repo = base / "orders"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "main.py").write_text(ORIGINAL)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")

    store = SqliteStore(":memory:")
    indexing.index_repo(repo, store, slug="orders")
    store.commit()
    return repo, store


@pytest.fixture(scope="module")
def indexed(tmp_path_factory):
    """Shared by the tests that only read. Indexing is seconds, so it happens once."""
    return _build(tmp_path_factory.mktemp("shared"))


@pytest.fixture
def indexed_fresh(tmp_path):
    """For the tests that rewrite or delete the checkout underneath themselves."""
    return _build(tmp_path)


def _symbol(store) -> str:
    """The id of the indexed `get_order`, in the prefixed form the tools hand around."""
    hits = [o.id for o in store.list_objects("code_symbol") if "get_order" in o.id]
    assert hits, [o.id for o in store.list_objects("code_symbol")]
    return hits[0]


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = getattr(fn, "__wrapped__", fn)
            return fn
        return deco


# -- registration ----------------------------------------------------------------------

def test_not_registered_by_default(monkeypatch):
    """An operator who has not asked for it must not find it in the tool list."""
    monkeypatch.delenv(source_tool.ENV_VAR, raising=False)
    m = _FakeMCP()
    register_all(m, build_context(":memory:"), http=True)
    assert "get_source" not in m.tools


def test_not_offered_over_stdio_even_when_enabled(monkeypatch):
    """Over stdio the agent can already open the file, so the tool adds exposure and no
    capability. Enabling it must not quietly turn it on for the local transport."""
    monkeypatch.setenv(source_tool.ENV_VAR, "1")
    m = _FakeMCP()
    register_all(m, build_context(":memory:"), http=False)
    assert "get_source" not in m.tools


def test_registered_over_http_when_enabled(monkeypatch):
    monkeypatch.setenv(source_tool.ENV_VAR, "1")
    m = _FakeMCP()
    register_all(m, build_context(":memory:"), http=True)
    assert "get_source" in m.tools


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("", False), ("maybe", False),
])
def test_only_an_explicit_yes_turns_it_on(monkeypatch, value, expected):
    monkeypatch.setenv(source_tool.ENV_VAR, value)
    assert source_tool.enabled() is expected


# -- reading ---------------------------------------------------------------------------

def test_returns_the_symbol_body_with_its_commit(indexed):
    repo, store = indexed
    from fleetlens.service.source import SourceService

    out = SourceService(store, store).get_source(_symbol(store))
    assert out["status"] == "ok", out
    assert "def get_order" in out["source"]
    assert "order_id must be numeric" in out["source"]      # the validation, the point
    assert out["stale"] is False
    assert out["commit"], "an indexed commit should have been used"


def test_reads_the_indexed_commit_not_the_working_tree(indexed_fresh):
    """The failure this design exists to prevent.

    Rewrite the file after indexing so the recorded span now covers something else. Reading
    the working copy would return a different function under the symbol's name, formatted
    correctly and confidently wrong. Reading at the recorded commit returns what the index
    actually described.
    """
    repo, store = indexed_fresh
    from fleetlens.service.source import SourceService
    svc = SourceService(store, store)
    sym = _symbol(store)

    before = svc.get_source(sym)
    (repo / "app" / "main.py").write_text(REWRITTEN)

    after = svc.get_source(sym)
    assert after["status"] == "ok", after
    assert after["source"] == before["source"]
    assert "something_entirely_different" not in after["source"]
    assert after["stale"] is False


def test_says_so_when_it_has_to_fall_back_to_disk(indexed, monkeypatch):
    """A commit that history no longer holds must degrade loudly, not silently."""
    repo, store = indexed
    from fleetlens.service import source as mod

    real = mod._git

    def no_history(args, cwd):
        return None if args and args[0] == "show" else real(args, cwd)

    monkeypatch.setattr(mod, "_git", no_history)
    out = mod.SourceService(store, store).get_source(_symbol(store))

    assert out["status"] == "ok"
    assert out["stale"] is True
    assert out["commit"] is None


# -- bounds ----------------------------------------------------------------------------

def test_context_lines_widen_the_span_and_are_capped(indexed):
    from fleetlens.service.source import MAX_CONTEXT_LINES, SourceService
    svc = SourceService(store=indexed[1], knowledge=indexed[1])
    sym = _symbol(indexed[1])

    tight = svc.get_source(sym, context_lines=0)
    padded = svc.get_source(sym, context_lines=2)
    assert padded["line_start"] <= tight["line_start"]
    assert padded["line_end"] >= tight["line_end"]

    absurd = svc.get_source(sym, context_lines=10_000)
    assert absurd["line_start"] >= 1
    assert absurd["line_end"] - absurd["line_start"] + 1 <= 500
    assert MAX_CONTEXT_LINES < 10_000          # the clamp is doing something


def test_max_lines_truncates_and_says_so(indexed):
    from fleetlens.service.source import SourceService
    svc = SourceService(indexed[1], indexed[1])
    out = svc.get_source(_symbol(indexed[1]), context_lines=0, max_lines=1)
    assert out["truncated"] is True
    assert len(out["source"].splitlines()) == 1


def test_an_unknown_symbol_is_a_not_found_with_a_hint(indexed):
    from fleetlens.service.source import SourceService
    out = SourceService(indexed[1], indexed[1]).get_source("code_symbol:orders:nope::nope")
    assert out["status"] == "not_found"
    assert "expected_format" in out


def test_a_missing_checkout_explains_itself(indexed_fresh):
    """An index outlives the repositories it was built from."""
    import shutil

    from fleetlens.service.source import SourceService
    repo, store = indexed_fresh
    sym = _symbol(store)
    shutil.rmtree(repo)

    out = SourceService(store, store).get_source(sym)
    assert out["status"] == "unavailable"
    assert "not on this machine" in out["reason"]
