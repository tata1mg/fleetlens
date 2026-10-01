"""Symbol lookup: ordering, and the shape that made it slow.

`find_symbol` is an obvious entry point for an agent, and on a real index it read every
symbol in the fleet. Half a million rows, `lower()` on each id, and then a sort of every
match before taking twenty.
"""
from __future__ import annotations

from fleetlens.store.models import KnowledgeObject
from fleetlens.store.sqlite import SqliteStore


def _sym(store, slug, path, name):
    store.upsert_object(KnowledgeObject(
        object_type="code_symbol", object_id=f"{slug}:{path}::{name}", name=name,
        summary=None, version="1", source="static", generation_strategy="index",
        last_generated_at=None, embed_text=None, payload={}))


def _store():
    s = SqliteStore(":memory:")
    _sym(s, "orders", "app/api.py", "handler")               # exact
    _sym(s, "orders", "app/api.py", "handler_v2")            # prefix
    _sym(s, "billing", "app/jobs.py", "retry_handler")       # substring only
    _sym(s, "billing", "app/jobs.py", "unrelated")
    s.commit()
    return s


def test_exact_then_prefix_then_substring():
    """Narrowest first, so the best match leads. The first two use the name index; the
    substring scan only runs when they have not filled the limit."""
    got = _store().find_ids("handler", "code_symbol", limit=10)
    assert got[0].endswith("::handler")                 # exact name wins
    assert got[1].endswith("::handler_v2")              # then the prefix
    assert any(g.endswith("::retry_handler") for g in got)   # substring still found
    assert not any(g.endswith("::unrelated") for g in got)


def test_the_limit_is_honoured_across_all_three_steps():
    assert len(_store().find_ids("handler", "code_symbol", limit=2)) == 2


def test_no_duplicates_when_a_symbol_matches_more_than_one_step():
    """`handler` matches exactly, by prefix, and as a substring of its own id."""
    got = _store().find_ids("handler", "code_symbol", limit=10)
    assert len(got) == len(set(got))


def test_a_term_that_matches_nothing_returns_nothing():
    assert _store().find_ids("zzz_nope", "code_symbol", limit=10) == []
    assert _store().find_ids("", "code_symbol") == []
    assert _store().find_ids("   ", "code_symbol") == []


def test_results_are_stable_between_identical_runs():
    s = _store()
    assert s.find_ids("handler", "code_symbol") == s.find_ids("handler", "code_symbol")


def test_the_name_index_exists_so_lookups_do_not_scan():
    s = _store()
    names = {r[0] for r in s._conn.execute("select name from sqlite_master where type='index'")}
    assert "idx_ko_type_name" in names
    plan = s._conn.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM knowledge_objects "
        "WHERE object_type=? AND name=? LIMIT 1", ("code_symbol", "handler")).fetchall()
    assert any("idx_ko_type_name" in str(row) for row in plan), plan
