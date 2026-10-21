"""Pin lifecycle: snapshots, server-controlled expiry, and the collector."""
from __future__ import annotations

import json
import logging
import os
import sqlite3

import pytest
from fleetlens import pins as pins_mod
from fleetlens.pins import PinError, PinPolicy, PinStore
from fleetlens.store.models import KnowledgeObject
from fleetlens.store.sqlite import SqliteStore

DAY = 86400


def _obj(name):
    return KnowledgeObject(
        object_type="service", object_id=name, name=name, summary=None,
        version="1", source="static", generation_strategy="deterministic",
        last_generated_at=None, embed_text=None, payload={})


class _Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def _live(tmp_path, stamped=True):
    db = tmp_path / "fleet.db"
    st = SqliteStore(str(db))
    st.upsert_object(_obj("orders"))
    if stamped:
        st.stamp_build({})
    st.commit()
    return db, st


def _store(tmp_path, clock=None, **kw):
    policy = PinPolicy(tmp_path / "pins", **kw)
    return PinStore(policy, clock or _Clock())


def test_create_makes_a_readable_independent_snapshot(tmp_path):
    """The pin must hold the data, as a separate file a server could open on its own."""
    db, live = _live(tmp_path)
    ps = _store(tmp_path)
    pin = ps.create(str(db), "eval-1", "baseline", "me")
    snap = tmp_path / "pins" / "eval-1.db"
    assert pin["build_id"] == live.get_meta("build_id")
    assert snap.stat().st_ino != db.stat().st_ino
    ro = SqliteStore(str(snap), read_only=True)
    assert [o.name for o in ro.list_objects("service")] == ["orders"]


def test_later_writes_to_the_source_do_not_reach_the_snapshot(tmp_path):
    """Guards the hardlink mistake: fl index writes in place and would edit the pin."""
    db, live = _live(tmp_path)
    ps = _store(tmp_path)
    ps.create(str(db), "p", "", "")
    live.upsert_object(_obj("payments"))
    live.commit()
    ro = SqliteStore(str(tmp_path / "pins" / "p.db"), read_only=True)
    assert [o.name for o in ro.list_objects("service")] == ["orders"]


def test_snapshot_taken_during_an_open_write_transaction_is_valid(tmp_path):
    """VACUUM INTO must see a consistent committed state, never the half-written one."""
    db, live = _live(tmp_path)
    live.upsert_object(_obj("uncommitted"))        # not committed
    ps = _store(tmp_path)
    ps.create(str(db), "p", "", "")
    con = sqlite3.connect(tmp_path / "pins" / "p.db")
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    names = {r[0] for r in con.execute("SELECT name FROM knowledge_objects")}
    assert names == {"orders"}


def test_duplicate_alias_is_a_conflict(tmp_path):
    db, _ = _live(tmp_path)
    ps = _store(tmp_path)
    ps.create(str(db), "p", "", "")
    with pytest.raises(PinError) as e:
        ps.create(str(db), "p", "", "")
    assert e.value.status == 409


@pytest.mark.parametrize("alias", ["../x", "../../etc/passwd", "a/b", "A b", "", ".hidden",
                                   "x" * 64, "a\\b", "..", None, 5])
def test_bad_aliases_are_rejected_and_write_nothing(tmp_path, alias):
    """The alias is a file name; no value may be able to leave the pins directory."""
    db, _ = _live(tmp_path)
    ps = _store(tmp_path)
    with pytest.raises(PinError) as e:
        ps.create(str(db), alias, "", "")
    assert e.value.status == 400
    assert not list((tmp_path / "pins").glob("*"))
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["fleet.db"]


def test_alias_boundary_lengths(tmp_path):
    db, _ = _live(tmp_path)
    ps = _store(tmp_path)
    ps.create(str(db), "a" * 63, "", "")
    with pytest.raises(PinError):
        ps.create(str(db), "a" * 64, "", "")


def test_source_without_a_build_id_is_refused(tmp_path):
    """An unstamped index cannot say what the pin holds."""
    db, _ = _live(tmp_path, stamped=False)
    ps = _store(tmp_path)
    with pytest.raises(PinError) as e:
        ps.create(str(db), "p", "", "")
    assert e.value.status == 409 and "Reindex" in e.value.message
    assert not list((tmp_path / "pins").glob("*"))


def test_default_expiry_is_thirty_days_and_env_changes_it(tmp_path, monkeypatch):
    db, _ = _live(tmp_path)
    clock = _Clock()
    pin = _store(tmp_path, clock).create(str(db), "a", "", "")
    assert pin["days_left"] == 30
    monkeypatch.setenv("FLEETLENS_RETAIN_PINNED_DAYS", "7")
    policy = PinPolicy.from_env(tmp_path / "p2")
    assert policy.retain_days == 7
    assert PinStore(policy, clock).create(str(db), "b", "", "")["days_left"] == 7


def test_reason_and_owner_are_truncated(tmp_path):
    db, _ = _live(tmp_path)
    pin = _store(tmp_path).create(str(db), "a", "r" * 900, "o" * 300)
    assert len(pin["reason"]) == 500 and len(pin["owner"]) == 100


@pytest.mark.parametrize("days", [0, -1, 11, True, "3", 2.5, None])
def test_extend_rejects_out_of_range_values(tmp_path, days):
    db, _ = _live(tmp_path)
    ps = _store(tmp_path)
    ps.create(str(db), "p", "", "")
    with pytest.raises(PinError) as e:
        ps.extend("p", days)
    assert e.value.status == 422 and "10" in e.value.message


def test_extend_adds_to_the_existing_expiry_within_the_cap(tmp_path):
    db, _ = _live(tmp_path)
    ps = _store(tmp_path)
    before = ps.create(str(db), "p", "", "")
    assert ps.extend("p", 1)["days_left"] == before["days_left"] + 1
    assert ps.extend("p", 10)["days_left"] == before["days_left"] + 11
    with pytest.raises(PinError) as e:
        ps.extend("nope", 1)
    assert e.value.status == 404


def test_extend_cap_follows_the_setting(tmp_path, monkeypatch):
    db, _ = _live(tmp_path)
    monkeypatch.setenv("FLEETLENS_PIN_EXTEND_MAX_DAYS", "3")
    ps = PinStore(PinPolicy.from_env(tmp_path / "pins"), _Clock())
    ps.create(str(db), "p", "", "")
    ps.extend("p", 3)
    with pytest.raises(PinError) as e:
        ps.extend("p", 4)
    assert "3" in e.value.message


@pytest.mark.parametrize("name,value", [("FLEETLENS_RETAIN_PINNED_DAYS", "abc"),
                                        ("FLEETLENS_RETAIN_PINNED_DAYS", "0"),
                                        ("FLEETLENS_PIN_EXTEND_MAX_DAYS", "-2")])
def test_bad_env_values_fail_at_start_naming_the_variable(tmp_path, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit) as e:
        PinPolicy.from_env(tmp_path)
    assert name in str(e.value)


def test_delete_removes_both_files_and_404s_when_absent(tmp_path):
    db, _ = _live(tmp_path)
    ps = _store(tmp_path)
    ps.create(str(db), "p", "", "")
    ps.delete("p")
    assert not list((tmp_path / "pins").glob("p.*"))
    with pytest.raises(PinError) as e:
        ps.delete("p")
    assert e.value.status == 404


def test_list_is_sorted_by_creation(tmp_path):
    db, _ = _live(tmp_path)
    clock = _Clock()
    ps = _store(tmp_path, clock)
    ps.create(str(db), "b", "", "")
    clock.now += 60
    ps.create(str(db), "a", "", "")
    assert [p["alias"] for p in ps.list()] == ["b", "a"]


def test_collect_removes_only_expired_pins_and_logs_each(tmp_path, caplog):
    """The collector may delete exactly what has expired, and must say so."""
    db, _ = _live(tmp_path)
    clock = _Clock()
    ps = _store(tmp_path, clock)
    ps.create(str(db), "old", "", "")
    clock.now += 20 * DAY
    ps.create(str(db), "new", "", "")
    clock.now += 10 * DAY - 1           # old expires in 1 second: still alive
    assert ps.collect() == []
    clock.now += 1                      # exactly at expiry: gone
    with caplog.at_level(logging.INFO, logger=pins_mod.__name__):
        assert ps.collect() == ["old"]
    assert [p["alias"] for p in ps.list()] == ["new"]
    assert (tmp_path / "pins" / "new.db").exists()
    assert not (tmp_path / "pins" / "old.db").exists()
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(msgs) == 1 and "old" in msgs[0] and "build" in msgs[0]


def test_collect_leaves_a_corrupt_sidecar_alone(tmp_path, caplog):
    """Unreadable must never be treated as expired."""
    db, _ = _live(tmp_path)
    clock = _Clock()
    ps = _store(tmp_path, clock)
    ps.create(str(db), "p", "", "")
    (tmp_path / "pins" / "p.json").write_text("{not json")
    clock.now += 365 * DAY
    with caplog.at_level(logging.WARNING, logger=pins_mod.__name__):
        assert ps.collect() == []
    assert (tmp_path / "pins" / "p.db").exists()
    assert any("unreadable" in r.getMessage() for r in caplog.records)


def test_collect_removes_old_orphans_but_not_fresh_ones(tmp_path):
    clock = _Clock(now=1_800_000_000.0)
    ps = _store(tmp_path, clock)
    d = tmp_path / "pins"
    d.mkdir()
    for name in ("orphan.db", "fresh.db", "half.db.tmp"):
        (d / name).write_bytes(b"x")
    os.utime(d / "orphan.db", (clock.now - 7200, clock.now - 7200))
    os.utime(d / "half.db.tmp", (clock.now - 7200, clock.now - 7200))
    os.utime(d / "fresh.db", (clock.now - 60, clock.now - 60))
    ps.collect()
    assert sorted(p.name for p in d.iterdir()) == ["fresh.db"]


def test_collect_keeps_the_snapshot_of_a_dotted_alias(tmp_path):
    """An alias with a dot must still be matched to its own sidecar."""
    db, _ = _live(tmp_path)
    clock = _Clock()
    ps = _store(tmp_path, clock)
    ps.create(str(db), "v1.2", "", "")
    clock.now += 2 * DAY
    os.utime(tmp_path / "pins" / "v1.2.db", (clock.now - 7200, clock.now - 7200))
    assert ps.collect() == []
    assert (tmp_path / "pins" / "v1.2.db").exists()


def test_failed_create_leaves_no_tmp_file(tmp_path, monkeypatch):
    db, _ = _live(tmp_path)
    ps = _store(tmp_path)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(pins_mod.os, "replace", boom)
    with pytest.raises(OSError):
        ps.create(str(db), "p", "", "")
    assert not list((tmp_path / "pins").glob("*"))


def test_old_sqlite_is_a_clear_error(tmp_path, monkeypatch):
    db, _ = _live(tmp_path)
    monkeypatch.setattr(pins_mod.sqlite3, "sqlite_version_info", (3, 26, 0))
    with pytest.raises(PinError) as e:
        _store(tmp_path).create(str(db), "p", "", "")
    assert e.value.status == 501


def test_sidecar_carries_the_documented_fields(tmp_path):
    db, _ = _live(tmp_path)
    _store(tmp_path).create(str(db), "p", "why", "me")
    rec = json.loads((tmp_path / "pins" / "p.json").read_text())
    assert set(rec) == {"alias", "build_id", "reason", "owner", "created_at", "expires_at"}
