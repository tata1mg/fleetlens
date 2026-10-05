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


def test_index_info_counts_the_symbols_that_are_there(tmp_path):
    """get_index_info is what a client calls to decide whether to trust the index, so it
    reporting zero symbols for an index full of them is worse than it not existing."""
    from fleetlens.loaders.callgraph import NODE_TYPE as SYMBOL_TYPE
    from fleetlens.store.models import KnowledgeObject
    from fleetlens.store.sqlite import SqliteStore

    store = SqliteStore(str(tmp_path / "f.db"))
    store.upsert_object(KnowledgeObject(
        object_type=SYMBOL_TYPE, object_id="orders:app.main.handler", name="handler",
        summary=None, version="unknown", source="static", generation_strategy="index",
        last_generated_at=None, embed_text=None, payload={}))
    store.commit()

    assert store.index_info()["symbols"] == 1


def test_index_info_is_computed_once_on_a_served_index(tmp_path):
    """Three full scans over half a million rows, for the tool clients are told to call
    first. A read-only store is a snapshot, so the answer cannot change until the file is
    swapped, and `reload_if_changed` is what notices that."""
    from fleetlens.store.models import KnowledgeObject
    from fleetlens.store.sqlite import SqliteStore

    db = tmp_path / "f.db"
    w = SqliteStore(str(db))
    w.upsert_object(KnowledgeObject(
        object_type="service", object_id="orders", name="orders", summary=None,
        version="unknown", source="static", generation_strategy="index",
        last_generated_at=None, embed_text=None, payload={}))
    w.commit()
    w.close()

    r = SqliteStore(str(db), read_only=True)
    first = r.index_info()
    assert first["services"] == 1
    statements = []
    r._conn.set_trace_callback(statements.append)
    assert r.index_info() == first
    r._conn.set_trace_callback(None)
    assert statements == []                 # answered from the cached snapshot

    # a writable store's counts genuinely move, so it is never cached
    w2 = SqliteStore(str(db))
    before = w2.index_info()["services"]
    w2.upsert_object(KnowledgeObject(
        object_type="service", object_id="billing", name="billing", summary=None,
        version="unknown", source="static", generation_strategy="index",
        last_generated_at=None, embed_text=None, payload={}))
    w2.commit()
    assert w2.index_info()["services"] == before + 1


def test_a_served_store_can_be_read_from_several_threads(tmp_path):
    """A sqlite3 connection belongs to the thread that made it, and the server now answers
    on a worker pool. Without a connection per thread every call off the main thread would
    raise, which is a server that works until it is used by two people at once."""
    import threading

    from fleetlens.store.models import KnowledgeObject
    from fleetlens.store.sqlite import SqliteStore

    db = tmp_path / "f.db"
    w = SqliteStore(str(db))
    for n in range(5):
        w.upsert_object(KnowledgeObject(
            object_type="service", object_id=f"s{n}", name=f"s{n}", summary=None,
            version="unknown", source="static", generation_strategy="index",
            last_generated_at=None, embed_text=None, payload={"interface_count": n}))
    w.commit()
    w.close()

    r = SqliteStore(str(db), read_only=True)
    seen, errors = [], []

    def read():
        try:
            seen.append(len(r.service_summaries()))
        except Exception as exc:          # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=read) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert seen == [5] * 8
