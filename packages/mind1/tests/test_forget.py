"""`mind1 forget`: taking a repository back out of an index.

The property under test throughout is the one the module promises, that forgetting leaves
the index in the state a full rebuild without that repository would have produced. Each
test names the piece of state that a hand-written removal gets wrong.
"""
from __future__ import annotations

from mind1.forget import forget, plan
from mind1.store.models import KnowledgeObject, Relationship
from mind1.store.sqlite import SqliteStore


def _obj(store, gid: str, payload: dict | None = None, kind: str | None = None) -> None:
    otype, _, local = gid.partition(":")
    store.upsert_object(KnowledgeObject(kind or otype, local, local, None, "unknown",
                                        "static", "index", None, None, payload or {}))


def _service(store, slug, root, *, kind="service", symbols=2, interfaces=1) -> None:
    """A service as indexing leaves it: the object, its interfaces and its symbols."""
    _obj(store, f"{kind}:{slug}", {"root": str(root), "symbol_count": symbols,
                                   "interface_count": interfaces})
    for i in range(interfaces):
        _obj(store, f"interface:{slug}:get-{i}")
    for i in range(symbols):
        _obj(store, f"code_symbol:{slug}:a.py::f{i}")


def _fleet(tmp_path) -> SqliteStore:
    """Two repos. `shop` calls `cart`, and `cart` also ships a worker from its own checkout."""
    s = SqliteStore(str(tmp_path / "fleet.db"))
    _service(s, "shop", tmp_path / "repos" / "shop")
    _service(s, "cart", tmp_path / "repos" / "cart" / "services" / "api")
    _service(s, "cart-worker", tmp_path / "repos" / "cart" / "services" / "worker")
    s.replace_edges("static", [], [
        Relationship("service:shop", "calls", "service:cart", "static",
                     {"confidence": "high"}),
    ])
    s.stamp_build({"shop": "a" * 40, "cart": "b" * 40})
    s.commit()
    return s


def _run(store, *names):
    return forget(store, plan(store, list(names)))


def test_forgetting_a_service_removes_everything_it_owns(tmp_path):
    s = _fleet(tmp_path)
    r = _run(s, "cart")

    assert r["slugs"] == ["cart"]
    assert s.get("service:cart") is None
    assert s.list_objects_under("interface:cart:") == []
    assert s.list_objects_under("code_symbol:cart:") == []
    # The object, one interface and two symbols.
    assert r["objects"] == 4


def test_an_edge_into_a_forgotten_service_goes_with_it(tmp_path):
    """`resolve` matches callers against indexed services, so a rebuild would not have
    produced this edge either. Keeping it would leave the graph naming a service that is
    not in it."""
    s = _fleet(tmp_path)
    _run(s, "cart")

    assert s.edges_of("service:shop") == []


def test_enrichments_go_even_though_nothing_cascades_them(tmp_path):
    """The enrichment table has no foreign key on purpose, so it survives a re-index. That
    is right for re-indexing and wrong for forgetting."""
    s = _fleet(tmp_path)
    s.upsert_enrichment("service:cart", "service", "m", 2, "a cart", "h1", [0.1, 0.2])
    s.upsert_enrichment("interface:cart:get-0", "interface", "m", 2, "a route", "h2",
                        [0.3, 0.4])
    s.upsert_enrichment("service:shop", "service", "m", 2, "a shop", "h3", [0.5, 0.6])
    s.commit()

    r = _run(s, "cart")

    assert r["enrichments"] == 2
    assert s.enrichment_hashes("service", "m") == {"service:shop": "h3"}


def test_a_slug_is_not_a_prefix(tmp_path):
    """`service:cart` as a prefix would also match `service:cart-worker`, which is a
    different service in a different directory."""
    s = _fleet(tmp_path)
    _run(s, "cart")

    assert s.get("service:cart-worker") is not None
    assert len(s.list_objects_under("code_symbol:cart-worker:")) == 2


def test_the_repository_stays_listed_while_anything_from_it_survives(tmp_path):
    """One repository can declare several services, so forgetting one of them does not
    mean the checkout is gone."""
    s = _fleet(tmp_path)
    r = _run(s, "cart")

    assert r["repos"] == []
    assert sorted(s.repo_shas()) == ["cart", "shop"]


def test_forgetting_the_last_service_of_a_repository_drops_its_commit(tmp_path):
    s = _fleet(tmp_path)
    r = _run(s, "cart", "cart-worker")

    assert r["repos"] == ["cart"]
    assert sorted(s.repo_shas()) == ["shop"]


def test_a_slug_wins_over_a_repository_of_the_same_name(tmp_path):
    """`cart` is both a service and the checkout that service and its worker came from.
    The slug is the narrower reading, and the one that removes less."""
    s = _fleet(tmp_path)
    r = _run(s, "cart")

    assert r["slugs"] == ["cart"]
    assert s.get("service:cart-worker") is not None


def test_a_repository_whose_name_is_not_a_slug_takes_its_services(tmp_path):
    s = SqliteStore(str(tmp_path / "x.db"))
    _service(s, "api", tmp_path / "repos" / "billing" / "api")
    _service(s, "worker", tmp_path / "repos" / "billing" / "worker")
    _service(s, "shop", tmp_path / "repos" / "shop")
    s.stamp_build({"billing": "c" * 40, "shop": "a" * 40})
    s.commit()

    r = _run(s, "billing")

    assert r["slugs"] == ["api", "worker"]
    assert r["repos"] == ["billing"]
    assert list(s.repo_shas()) == ["shop"]
    assert s.get("service:shop") is not None


def test_a_library_is_forgettable_too(tmp_path):
    s = SqliteStore(str(tmp_path / "x.db"))
    _service(s, "commonutils", tmp_path / "repos" / "commonutils", kind="library",
             interfaces=0)
    s.stamp_build({"commonutils": "d" * 40})
    s.commit()

    r = _run(s, "commonutils")

    assert r["slugs"] == ["commonutils"] and r["repos"] == ["commonutils"]
    assert s.get("library:commonutils") is None
    assert s.list_objects_under("code_symbol:commonutils:") == []


def test_guidance_goes_with_the_repository_that_declared_it(tmp_path):
    """Guidance is keyed by an id its rulebook chooses, so the only thing tying it to a
    repository is where it was read from."""
    s = _fleet(tmp_path)
    _obj(s, "guidance:cart-rules", {"root": str(tmp_path / "repos" / "cart")})
    _obj(s, "guidance:shop-rules", {"root": str(tmp_path / "repos" / "shop")})
    s.commit()

    r = _run(s, "cart", "cart-worker")

    assert r["guidance"] == 1
    assert s.get("guidance:cart-rules") is None
    assert s.get("guidance:shop-rules") is not None


def test_guidance_above_a_surviving_service_stays(tmp_path):
    """Forgetting one service of a repository does not remove rules the repository
    declares, because the repository is still there."""
    s = _fleet(tmp_path)
    _obj(s, "guidance:cart-rules", {"root": str(tmp_path / "repos" / "cart")})
    s.commit()

    r = _run(s, "cart")

    assert r["guidance"] == 0
    assert s.get("guidance:cart-rules") is not None


def test_an_unknown_name_is_reported_and_nothing_is_removed(tmp_path):
    s = _fleet(tmp_path)
    p = plan(s, ["cart", "nosuchthing"])

    assert p.unknown == ["nosuchthing"]
    assert [t.slug for t in p.targets] == ["cart"]
    assert s.get("service:cart") is not None, "planning must not write"


def test_the_build_is_restamped(tmp_path):
    """`build_id` identifies this exact build. An index that changed without changing it
    would share an id with a different index, which is what the field exists to prevent."""
    s = _fleet(tmp_path)
    s.set_meta("build_id", "sentinel")   # build ids are second-granular, so pin it
    s.commit()

    _run(s, "cart")

    assert s.meta()["build_id"] != "sentinel"


def test_a_name_that_was_a_slug_falls_through_to_the_repository(tmp_path):
    """Once the `cart` service is gone, `cart` still names the checkout its worker came
    from, so the same word means something wider than it did a moment ago. That is why
    `mind1 forget` prints what it matched and asks before writing."""
    s = _fleet(tmp_path)
    _run(s, "cart")

    p = plan(s, ["cart"])

    assert p.unknown == []
    assert [t.slug for t in p.targets] == ["cart-worker"]
    assert p.repos == ["cart"]


def test_forgetting_a_name_that_is_gone_entirely_is_not_an_error(tmp_path):
    s = _fleet(tmp_path)
    _run(s, "shop")

    p = plan(s, ["shop"])

    assert p.unknown == ["shop"] and not p


def test_the_plan_is_empty_when_there_is_nothing_to_do(tmp_path):
    s = SqliteStore(str(tmp_path / "x.db"))
    s.commit()

    assert not plan(s, [])
