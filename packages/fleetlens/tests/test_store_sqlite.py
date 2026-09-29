"""SqliteStore: upsert/read, prefix-delete cascade, batched edges, find_ids."""
from __future__ import annotations

from fleetlens.store.models import KnowledgeObject, Relationship
from fleetlens.store.sqlite import SqliteStore


def _obj(gid: str) -> KnowledgeObject:
    otype, _, local = gid.partition(":")
    return KnowledgeObject(otype, local, local, None, "unknown", "static", "callgraph",
                           None, None, {})


def _store() -> SqliteStore:
    return SqliteStore(":memory:")


def test_upsert_get_and_find():
    s = _store()
    s.upsert_object(_obj("code_symbol:svc:a.py::foo"))
    s.commit()
    assert s.get("code_symbol:svc:a.py::foo") is not None
    assert s.find_ids("foo", "code_symbol") == ["code_symbol:svc:a.py::foo"]
    assert s.find_ids("foo", "interface") == []  # type-scoped


def test_prefix_delete_cascades_edges_and_is_scoped():
    s = _store()
    for gid in ("code_symbol:svc:f", "code_symbol:svc:g", "code_symbol:other:h",
                "interface:svc:get-x"):
        s.upsert_object(_obj(gid))
    s.replace_edges("static", [], [
        Relationship("code_symbol:svc:f", "calls", "code_symbol:svc:g", "static", {}),
        Relationship("interface:svc:get-x", "handled_by", "code_symbol:svc:f", "static", {}),
        Relationship("code_symbol:other:h", "calls", "code_symbol:other:h", "static", {}),
    ])
    s.commit()

    removed = s.delete_objects_by_id_prefix("code_symbol:svc:")
    s.commit()

    assert removed == 2
    # both edges touching a deleted node cascade (from_id AND to_id); 'other' survives
    remaining = {(e.from_id, e.relationship, e.to_id) for e in s.edges_of("code_symbol:other:h")}
    assert remaining == {("code_symbol:other:h", "calls", "code_symbol:other:h")}
    assert s.get("interface:svc:get-x") is not None  # interface node itself survives


def test_prefix_delete_is_literal_not_wildcard():
    s = _store()
    s.upsert_object(_obj("code_symbol:svc:a"))
    s.upsert_object(_obj("codeXsymbol:svc:a"))  # '_' must not match 'X'
    assert s.delete_objects_by_id_prefix("code_symbol:") == 1
    assert s.get("codeXsymbol:svc:a") is not None


def test_edges_batch_direction_and_filter():
    s = _store()
    for gid in ("code_symbol:svc:a", "code_symbol:svc:b", "code_symbol:svc:c"):
        s.upsert_object(_obj(gid))
    s.replace_edges("static", [], [
        Relationship("code_symbol:svc:a", "calls", "code_symbol:svc:b", "static", {}),
        Relationship("code_symbol:svc:b", "calls", "code_symbol:svc:c", "static", {}),
    ])
    out = s.edges_batch(["code_symbol:svc:a", "code_symbol:svc:b"], "calls", "out")
    assert {(e.from_id, e.to_id) for e in out} == {
        ("code_symbol:svc:a", "code_symbol:svc:b"),
        ("code_symbol:svc:b", "code_symbol:svc:c"),
    }
    incoming = s.edges_batch(["code_symbol:svc:c"], "calls", "in")
    assert [(e.from_id, e.to_id) for e in incoming] == [("code_symbol:svc:b", "code_symbol:svc:c")]


def test_list_objects_excludes_stubs():
    s = _store()
    s.upsert_object(_obj("code_symbol:svc:real"))
    s.ensure_stubs(["code_symbol:svc:stubbed"])
    s.commit()
    ids = [o.id for o in s.list_objects("code_symbol")]
    assert ids == ["code_symbol:svc:real"]
    assert len(s.list_objects("code_symbol", include_stubs=True)) == 2
