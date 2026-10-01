"""Semantic search: the two scoring paths must agree, and the cached block must not go stale.

Brute force is the right shape here. 14,000 vectors is a matrix-vector product, and an
approximate index would add a service and approximation error to save nothing. What was
wrong was doing that product in a Python loop, and re-reading every vector out of SQLite on
each query.
"""
from __future__ import annotations

import random

import fleetlens.store.sqlite as sq
from fleetlens.store.sqlite import SqliteStore

DIM = 48


def _store(n=200, seed=5):
    random.seed(seed)
    s = SqliteStore(":memory:")
    for i in range(n):
        s.upsert_enrichment(f"interface:svc:{i}", "interface", "m", DIM,
                            f"summary {i}", f"h{i}",
                            [random.random() for _ in range(DIM)])
    s.commit()
    return s


def _without_numpy(store, fn):
    real, sq._np = sq._np, None
    store._vec_cache.clear()
    try:
        return fn()
    finally:
        sq._np = real
        store._vec_cache.clear()


def test_both_scoring_paths_return_the_same_ranking():
    """numpy is an optional dependency, so the fallback has to be a speed difference and
    not a behaviour difference."""
    s = _store()
    random.seed(99)
    q = [random.random() for _ in range(DIM)]

    fast = s.search(q, "interface", "m", 10)
    slow = _without_numpy(s, lambda: s.search(q, "interface", "m", 10))

    assert [h[0] for h in fast] == [h[0] for h in slow]
    assert all(abs(a[1] - b[1]) < 1e-5 for a, b in zip(fast, slow))


def test_a_vector_finds_itself_first():
    s = _store()
    row = s._conn.execute("select object_id, vector from enrichments limit 1").fetchone()
    vec = sq._unpack(row[1])
    hits = s.search(vec, "interface", "m", 3)
    assert hits[0][0] == row[0]
    assert hits[0][1] > 0.999


def test_a_new_enrichment_is_visible_to_the_next_search():
    """The block is held in memory, so writing has to drop it or a search answers from
    vectors that no longer reflect the store."""
    s = _store(n=5)
    q = [1.0] * DIM
    before = s.search(q, "interface", "m", 10)

    s.upsert_enrichment("interface:svc:new", "interface", "m", DIM, "new", "hnew", [1.0] * DIM)
    s.commit()
    after = s.search(q, "interface", "m", 10)

    assert len(after) == len(before) + 1
    assert after[0][0] == "interface:svc:new"       # an exact match leads


def test_an_empty_or_unknown_kind_returns_nothing():
    s = _store(n=3)
    assert s.search([0.1] * DIM, "service", "m", 5) == []
    assert s.search([0.1] * DIM, "interface", "other-model", 5) == []


def test_the_limit_is_respected_and_never_exceeds_what_exists():
    s = _store(n=4)
    assert len(s.search([0.1] * DIM, "interface", "m", 10)) == 4
    assert len(s.search([0.1] * DIM, "interface", "m", 2)) == 2
