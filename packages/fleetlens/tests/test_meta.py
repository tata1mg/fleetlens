"""Build metadata: which build an index is and which commits it came from."""
from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess

import pytest
from fleetlens.store.models import KnowledgeObject
from fleetlens.store.sqlite import SCHEMA_VERSION, SqliteStore


def _obj(name):
    return KnowledgeObject(
        object_type="service", object_id=name, name=name, summary=None,
        version="1", source="static", generation_strategy="deterministic",
        last_generated_at=None, embed_text=None, payload={})


def _built(path, shas=None):
    st = SqliteStore(str(path))
    st.upsert_object(_obj("orders"))
    st.stamp_build(shas or {})
    st.commit()
    return st


def _git_repo(path):
    path.mkdir(parents=True)
    (path / "app").mkdir()
    (path / "app" / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/x')\ndef h():\n    return []\n")
    env = ["-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), *env, "commit", "-qm", "init"], check=True)
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


def test_fresh_store_has_meta_table_and_stamp_fields(tmp_path):
    """A build must be identifiable: id, time, schema and tool version all recorded."""
    st = _built(tmp_path / "f.db")
    meta = st.meta()
    assert re.fullmatch(r"\d{8}T\d{6}Z", meta["build_id"])
    assert meta["schema_version"] == str(SCHEMA_VERSION)
    assert meta["fleetlens_version"]
    assert meta["built_at"]
    assert st.get_meta("build_id") == meta["build_id"]


def test_stamp_merges_repo_shas_instead_of_replacing(tmp_path):
    """Re-indexing one repo must not erase the recorded commits of the others."""
    st = _built(tmp_path / "f.db", {"a": "1" * 40})
    st.stamp_build({"b": "2" * 40})
    assert st.repo_shas() == {"a": "1" * 40, "b": "2" * 40}
    st.stamp_build({"a": None})         # a stopped being a checkout: only its entry goes
    assert st.repo_shas() == {"b": "2" * 40}


def test_index_info_adds_build_fields_and_keeps_old_ones(tmp_path):
    """Existing consumers read indexed_at and the counts; they must not move."""
    info = _built(tmp_path / "f.db", {"a": "1" * 40}).index_info()
    assert info["build_id"] and info["schema_version"] == SCHEMA_VERSION
    assert info["indexed_at"] and info["services"] == 1
    assert "repo_shas" not in info      # healthz is unauthenticated and this can be huge


def test_read_only_store_on_a_pre_meta_index_reports_none(tmp_path):
    """A server started on an index built by an older version must answer, not crash."""
    db = tmp_path / "legacy.db"
    st = _built(db)
    st.close()
    raw = sqlite3.connect(db)
    raw.execute("DROP TABLE meta")
    raw.commit()
    raw.close()
    ro = SqliteStore(str(db), read_only=True)
    info = ro.index_info()
    assert info["build_id"] is None and info["schema_version"] is None
    assert info["services"] == 1
    assert ro.repo_shas() == {}


def test_reload_picks_up_a_new_build_id(tmp_path):
    """A swapped-in index must not be reported under the old build's cached id."""
    import os
    db = tmp_path / "f.db"
    _built(db).close()
    ro = SqliteStore(str(db), read_only=True)
    old = ro.index_info()["build_id"]
    new = tmp_path / "new.db"
    st = _built(new)
    st.set_meta("build_id", "20990101T000000Z")
    st.commit()
    st.close()
    os.replace(new, db)
    assert ro.reload_if_changed()
    assert ro.index_info()["build_id"] == "20990101T000000Z" != old


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_index_repo_stamps_the_git_sha(tmp_path, monkeypatch):
    """The point of the SHA: say which source an answer was derived from."""
    from fleetlens import indexing
    monkeypatch.setattr(indexing.cg_cli, "main", lambda *a, **kw: 2)
    sha = _git_repo(tmp_path / "orders")
    st = SqliteStore(str(tmp_path / "f.db"))
    indexing.index_repo(tmp_path / "orders", st, slug="orders")
    assert st.repo_shas() == {"orders": sha}
    assert st.get_meta("build_id")


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_index_all_stamps_once_for_the_sweep(tmp_path, monkeypatch):
    """A sweep is one build; every repo that loaded gets its sha in it."""
    from fleetlens import indexing
    monkeypatch.setattr(indexing.cg_cli, "main", lambda *a, **kw: 2)
    sha = _git_repo(tmp_path / "base" / "orders")
    st = SqliteStore(str(tmp_path / "f.db"))
    indexing.index_all(tmp_path / "base", st)
    assert st.repo_shas() == {"orders": sha}


def test_non_git_directory_is_indexed_without_a_sha(tmp_path, monkeypatch):
    """Indexing must not depend on the source being a git checkout."""
    from fleetlens import indexing
    monkeypatch.setattr(indexing.cg_cli, "main", lambda *a, **kw: 2)
    (tmp_path / "plain" / "app").mkdir(parents=True)
    (tmp_path / "plain" / "app" / "main.py").write_text("x = 1\n")
    assert indexing._git_head(tmp_path / "plain") is None
    st = SqliteStore(str(tmp_path / "f.db"))
    indexing.index_repo(tmp_path / "plain", st, slug="plain")
    assert st.repo_shas() == {}
    assert st.get_meta("build_id")
