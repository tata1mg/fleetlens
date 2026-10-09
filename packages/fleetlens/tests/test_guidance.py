"""Human-authored guidance: loading, validation, and replacing what was there before."""
from __future__ import annotations

import pytest
from fleetlens.guidance import GuidanceError, ingest, load_guidance
from fleetlens.guidance.load import read_file
from fleetlens.store.sqlite import SqliteStore

GOOD = """---
kind: guidance
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
    obj = read_file(_write(tmp_path, "x.md", "---\nkind: guidance\nid: x\ntitle: X\n---\n\nbody\n"))
    assert obj.payload["status"] == "recommended"
    assert obj.payload["scope"] == "all"
    assert obj.payload["applies_to"] == {}
    assert obj.version == "unknown"


@pytest.mark.parametrize("text,because", [
    ("no frontmatter at all\n", "no `---`"),
    ("---\nkind: guidance\ntitle: X\n---\n\nbody\n", "needs a `id`"),
    ("---\nkind: guidance\nid: x\n---\n\nbody\n", "needs a `title`"),
    ("---\nkind: guidance\nid: Not Valid\ntitle: X\n---\n\nbody\n", "lower-case words"),
    ("---\nkind: guidance\nid: x\ntitle: X\nstatus: urgent\n---\n\nbody\n", "not one of"),
    ("---\nkind: guidance\nid: x\ntitle: X\nscope: someday\n---\n\nbody\n", "not one of"),
    ("---\nkind: guidance\nid: x\ntitle: X\nreviewed: last tuesday\n---\n\nbody\n", "YYYY-MM-DD"),
    ("---\nkind: guidance\nid: x\ntitle: X\n---\n\n   \n", "no guidance under it"),
])
def test_malformed_files_are_refused_with_the_reason(tmp_path, text, because):
    """A rule the index misreads is worse than one it refuses to load."""
    with pytest.raises(GuidanceError, match=because):
        read_file(_write(tmp_path, "bad.md", text))


def test_markdown_that_is_not_guidance_is_passed_over_in_silence(tmp_path):
    """A README is not a malformed rule. It is simply not one."""
    _write(tmp_path, "good.md", GOOD)
    _write(tmp_path, "README.md", "# The project\n\nWhat it does.\n")
    _write(tmp_path, "CHANGELOG.md", "## 1.2.0\n\n- a change\n")
    _write(tmp_path, "post.md", "---\nlayout: post\ntitle: A blog post\n---\n\nhello\n")
    objs, problems = load_guidance(tmp_path)
    assert [o.object_id for o in objs] == ["cache-access"]
    assert problems == []


def test_a_file_claiming_to_be_guidance_is_held_to_the_standard(tmp_path):
    _write(tmp_path, "good.md", GOOD)
    _write(tmp_path, "bad.md", "---\nkind: guidance\ntitle: No id\n---\n\nbody\n")
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
    _write(tmp_path, "other.md", "---\nkind: guidance\nid: other\ntitle: Other\n---\n\nbody\n")
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
    text = "---\nkind: guidance\nid: x\ntitle: X\napplies_to:\n  dependencies:\n      nested: [a]\n---\n\nb\n"
    with pytest.raises(GuidanceError, match="nests deeper"):
        read_file(_write(tmp_path, "deep.md", text))


# --- embedding and the MCP tools -------------------------------------------------------


class FakeEmbedder:
    model = "fake-embed"

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [[float(len(t)), float(t.count("cache"))] for t in texts]


def test_guidance_is_embedded_as_written_not_paraphrased(tmp_path):
    """No LLM: a rule arrives already written by the person who meant it."""
    from fleetlens.guidance import embed_guidance

    _write(tmp_path, "cache.md", GOOD)
    store = SqliteStore(":memory:")
    ingest(store, tmp_path)
    r = embed_guidance(store, FakeEmbedder())
    assert r["embedded"] == 1
    stored = store._conn.execute(
        "SELECT summary FROM enrichments WHERE object_type='guidance'").fetchone()[0]
    assert stored == store.get("guidance:cache-access").summary


def test_embedding_is_gated_on_the_content_hash(tmp_path):
    from fleetlens.guidance import embed_guidance

    p = _write(tmp_path, "cache.md", GOOD)
    store = SqliteStore(":memory:")
    ingest(store, tmp_path)
    emb = FakeEmbedder()
    assert embed_guidance(store, emb)["embedded"] == 1
    assert embed_guidance(store, emb)["skipped"] == 1          # unchanged, no work

    p.write_text(GOOD.replace("Do not import", "Never import"))
    ingest(store, tmp_path)
    assert embed_guidance(store, emb)["embedded"] == 1         # edited, re-embedded


def test_the_server_says_guidance_exists_only_when_it_does(tmp_path):
    from fleetlens.cli import _guidance_instructions
    from fleetlens.server.app import build_context

    db = tmp_path / "x.db"
    store = SqliteStore(str(db))
    store.commit()
    store.close()
    ctx = build_context(str(db))
    assert _guidance_instructions(ctx) == ""

    _write(tmp_path, "cache.md", GOOD)
    ingest(ctx.store, tmp_path)
    said = _guidance_instructions(ctx)
    assert "get_engineering_guidance" in said and "1 rules" in said
    ctx.close()


# --- telling services, libraries and rulebooks apart in one directory ------------------

GUIDANCE_MANIFEST = "guidance:\n  - path: rules\n"


def test_a_tree_of_repositories_is_read_through_their_manifests(tmp_path):
    """The case that matters: services, libraries and rulebooks under one parent."""
    from fleetlens.guidance import guidance_dirs

    book = tmp_path / "standards"
    (book / "rules").mkdir(parents=True)
    (book / "fleetlens.yaml").write_text(GUIDANCE_MANIFEST)
    _write(book / "rules", "cache.md", GOOD)

    svc = tmp_path / "orders"
    svc.mkdir()
    (svc / "README.md").write_text("# orders\n")

    lib = tmp_path / "shared"
    lib.mkdir()
    (lib / "fleetlens.yaml").write_text('libraries:\n  - path: "."\n')
    (lib / "CHANGELOG.md").write_text("## 1.0\n")

    assert guidance_dirs(tmp_path) == [book / "rules"]

    store = SqliteStore(":memory:")
    r = ingest(store, tmp_path)
    assert r["loaded"] == 1
    assert r["problems"] == []


def test_a_repo_that_declares_guidance_is_read_directly(tmp_path):
    from fleetlens.guidance import guidance_dirs

    (tmp_path / "rules").mkdir()
    (tmp_path / "fleetlens.yaml").write_text(GUIDANCE_MANIFEST)
    _write(tmp_path / "rules", "cache.md", GOOD)
    assert guidance_dirs(tmp_path) == [tmp_path / "rules"]


def test_a_bare_directory_of_rules_still_works(tmp_path):
    """No manifest anywhere, so the caller meant this directory."""
    from fleetlens.guidance import guidance_dirs

    _write(tmp_path, "cache.md", GOOD)
    assert guidance_dirs(tmp_path) == [tmp_path]


def test_a_manifest_that_declares_no_guidance_is_an_answer(tmp_path):
    """Not an omission to fall back from: the repo said it holds no rules."""
    from fleetlens.guidance import guidance_dirs

    (tmp_path / "fleetlens.yaml").write_text('libraries:\n  - path: "."\n')
    _write(tmp_path, "cache.md", GOOD)
    assert guidance_dirs(tmp_path) == []


def test_a_guidance_only_repo_is_not_a_service(tmp_path):
    from fleetlens.manifest import resolve_services

    (tmp_path / "fleetlens.yaml").write_text('guidance:\n  - path: "."\n')
    assert resolve_services(tmp_path) == []


def test_a_repo_with_both_still_yields_its_services(tmp_path):
    from fleetlens.manifest import guidance_roots, resolve_services

    (tmp_path / "fleetlens.yaml").write_text(
        'services:\n  - name: orders\n    path: svc\nguidance:\n  - path: rules\n')
    assert [s.name for s in resolve_services(tmp_path)] == ["orders"]
    assert guidance_roots(tmp_path) == [tmp_path / "rules"]


def test_ingest_without_replace_leaves_other_rulebooks_alone(tmp_path):
    """Indexing one repository has seen one rulebook, so it withdraws nothing."""
    from fleetlens.guidance import ingest_roots

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _write(a, "cache.md", GOOD)
    _write(b, "other.md", "---\nkind: guidance\nid: other\ntitle: Other\n---\n\nbody\n")

    store = SqliteStore(":memory:")
    ingest_roots(store, [a], replace=False)
    ingest_roots(store, [b], replace=False)
    assert len(store.list_objects("guidance")) == 2

    r = ingest_roots(store, [a], replace=True)
    assert r["removed"] == 1
    assert [o.object_id for o in store.list_objects("guidance")] == ["cache-access"]


def test_the_same_id_in_two_rulebooks_is_reported(tmp_path):
    from fleetlens.guidance import ingest_roots

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _write(a, "cache.md", GOOD)
    _write(b, "cache.md", GOOD)
    store = SqliteStore(":memory:")
    r = ingest_roots(store, [a, b], replace=True)
    assert r["loaded"] == 1
    assert any("already defined in" in p for p in r["problems"])


def test_index_all_tells_the_three_kinds_apart_in_one_directory(tmp_path):
    """The prod layout: services, libraries and rulebooks under one parent."""
    from fleetlens.indexing import index_all

    svc = tmp_path / "orders"
    (svc / "app").mkdir(parents=True)
    (svc / "app" / "routes.py").write_text(
        'from sanic import Blueprint\nbp = Blueprint("o")\n'
        '@bp.route("/orders")\nasync def h(r):\n    return 1\n')
    (svc / "pyproject.toml").write_text('[project]\ndependencies = ["redis==5.0"]\n')

    lib = tmp_path / "shared"
    lib.mkdir()
    (lib / "fleetlens.yaml").write_text('libraries:\n  - path: "."\n')
    (lib / "helper.py").write_text("def helper():\n    return 1\n")

    book = tmp_path / "standards"
    (book / "rules").mkdir(parents=True)
    (book / "fleetlens.yaml").write_text(GUIDANCE_MANIFEST)
    (book / "README.md").write_text("# Standards\n\nNot a rule.\n")
    _write(book / "rules", "cache.md", GOOD)

    store = SqliteStore(":memory:")
    res = index_all(tmp_path, store)

    assert [o.object_id for o in store.list_objects("service")] == ["orders"]
    assert [o.object_id for o in store.list_objects("guidance")] == ["cache-access"]
    # The rulebook is not a repository that failed to index.
    assert res["considered"] == 2 and res["rulebooks"] == 1 and res["failed"] == []
    # Its README is not a malformed rule.
    assert res["guidance"]["problems"] == []


def test_indexing_one_repo_picks_up_the_rules_it_declares(tmp_path):
    from fleetlens.indexing import index_repo

    (tmp_path / "rules").mkdir()
    (tmp_path / "fleetlens.yaml").write_text(GUIDANCE_MANIFEST)
    _write(tmp_path / "rules", "cache.md", GOOD)

    store = SqliteStore(":memory:")
    out = index_repo(tmp_path, store)
    assert [o.object_id for o in store.list_objects("guidance")] == ["cache-access"]
    assert store.list_objects("service") == []
    assert out[-1]["guidance"]["loaded"] == 1
