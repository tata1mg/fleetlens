"""Human-authored guidance: loading, validation, and replacing what was there before."""
from __future__ import annotations

import pytest
from fleetlens.guidance import GuidanceError, ingest, load_guidance
from fleetlens.guidance.load import read_file
from fleetlens.store.sqlite import SqliteStore

GOOD = """---
id: cache-access
title: Cache access goes through the shared wrapper
status: mandatory
scope: all
applies_to:
  dependencies: [cache_wrapper, redis_wrapper]
  languages: [python]
reviewed: 2026-10-09
owner: platform
---

Use the shared cache wrapper. Do not import a Redis driver directly.
"""


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_reads_a_rule_into_a_knowledge_object(tmp_path):
    obj = read_file(_write(tmp_path, "cache.md", GOOD))
    assert obj.object_type == "guidance"
    assert obj.id == "guidance:cache-access"
    assert obj.name == "Cache access goes through the shared wrapper"
    assert "shared cache wrapper" in obj.summary
    assert obj.payload["status"] == "mandatory"
    assert obj.payload["applies_to"]["dependencies"] == ["cache_wrapper", "redis_wrapper"]
    assert obj.payload["reviewed"] == "2026-10-09"


def test_authored_content_is_marked_as_authored(tmp_path):
    """The whole point of the type: a reader can tell this is a claim, not an extraction."""
    obj = read_file(_write(tmp_path, "cache.md", GOOD))
    assert obj.source == "authored"
    assert obj.generation_strategy == "authored"
    assert obj.source not in ("static", "llm", "adapter")


def test_title_and_body_are_both_embedded(tmp_path):
    obj = read_file(_write(tmp_path, "cache.md", GOOD))
    assert obj.semantic_indexed
    assert "Cache access" in obj.embed_text and "Redis driver" in obj.embed_text


def test_defaults_when_optional_fields_are_absent(tmp_path):
    obj = read_file(_write(tmp_path, "x.md", "---\nid: x\ntitle: X\n---\n\nbody\n"))
    assert obj.payload["status"] == "recommended"
    assert obj.payload["scope"] == "all"
    assert obj.payload["applies_to"] == {}
    assert obj.version == "unknown"


@pytest.mark.parametrize("text,because", [
    ("no frontmatter at all\n", "no `---`"),
    ("---\ntitle: X\n---\n\nbody\n", "needs a `id`"),
    ("---\nid: x\n---\n\nbody\n", "needs a `title`"),
    ("---\nid: Not Valid\ntitle: X\n---\n\nbody\n", "lower-case words"),
    ("---\nid: x\ntitle: X\nstatus: urgent\n---\n\nbody\n", "not one of"),
    ("---\nid: x\ntitle: X\nscope: someday\n---\n\nbody\n", "not one of"),
    ("---\nid: x\ntitle: X\nreviewed: last tuesday\n---\n\nbody\n", "YYYY-MM-DD"),
    ("---\nid: x\ntitle: X\n---\n\n   \n", "no guidance under it"),
])
def test_malformed_files_are_refused_with_the_reason(tmp_path, text, because):
    """A rule the index misreads is worse than one it refuses to load."""
    with pytest.raises(GuidanceError, match=because):
        read_file(_write(tmp_path, "bad.md", text))


def test_one_bad_file_does_not_take_out_the_rest(tmp_path):
    _write(tmp_path, "good.md", GOOD)
    _write(tmp_path, "bad.md", "no frontmatter\n")
    objs, problems = load_guidance(tmp_path)
    assert [o.object_id for o in objs] == ["cache-access"]
    assert len(problems) == 1 and "bad.md" in problems[0]


def test_duplicate_ids_are_reported_not_silently_merged(tmp_path):
    _write(tmp_path, "a.md", GOOD)
    _write(tmp_path, "b.md", GOOD)
    objs, problems = load_guidance(tmp_path)
    assert len(objs) == 1
    assert any("already used by" in p for p in problems)


def test_dotted_directories_are_skipped(tmp_path):
    (tmp_path / ".git").mkdir()
    _write(tmp_path / ".git", "x.md", GOOD)
    objs, _ = load_guidance(tmp_path)
    assert objs == []


def test_a_missing_directory_says_so(tmp_path):
    with pytest.raises(GuidanceError, match="not a directory"):
        load_guidance(tmp_path / "nope")


def test_ingest_stores_and_is_idempotent(tmp_path):
    _write(tmp_path, "cache.md", GOOD)
    store = SqliteStore(":memory:")
    first = ingest(store, tmp_path)
    second = ingest(store, tmp_path)
    assert first["loaded"] == second["loaded"] == 1
    assert len(store.list_objects("guidance")) == 1


def test_a_withdrawn_rule_stops_being_served(tmp_path):
    """A rule deleted from the directory has been withdrawn; still serving it is worse than
    serving nothing."""
    _write(tmp_path, "cache.md", GOOD)
    _write(tmp_path, "other.md", "---\nid: other\ntitle: Other\n---\n\nbody\n")
    store = SqliteStore(":memory:")
    ingest(store, tmp_path)
    assert len(store.list_objects("guidance")) == 2

    (tmp_path / "other.md").unlink()
    r = ingest(store, tmp_path)
    assert r["removed"] == 1
    assert [o.object_id for o in store.list_objects("guidance")] == ["cache-access"]


def test_editing_a_body_changes_the_content_hash(tmp_path):
    p = _write(tmp_path, "cache.md", GOOD)
    before = read_file(p).payload["content_hash"]
    p.write_text(GOOD.replace("Do not import", "Never import"))
    assert read_file(p).payload["content_hash"] != before


def test_frontmatter_nested_too_deep_is_refused(tmp_path):
    text = "---\nid: x\ntitle: X\napplies_to:\n  dependencies:\n      nested: [a]\n---\n\nb\n"
    with pytest.raises(GuidanceError, match="nests deeper"):
        read_file(_write(tmp_path, "deep.md", text))
