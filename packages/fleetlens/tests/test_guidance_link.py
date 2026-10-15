"""Joining authored guidance to services: the content is a claim, the link is evidence."""
from __future__ import annotations

from fleetlens.adapters.deps import declared_dependencies
from fleetlens.guidance.link import applies, governing, link
from fleetlens.store.models import KnowledgeObject
from fleetlens.store.sqlite import SqliteStore


def _svc(slug, deps):
    return KnowledgeObject("service", slug, slug, None, "unknown", "static", "index",
                           None, None, {"dependencies": deps})


def _rule(gid, *, applies_to=None, forbids=None, status="mandatory"):
    return KnowledgeObject("guidance", gid, gid.replace("-", " ").title(), "body",
                           "2026-10-09", "authored", "authored", None, gid,
                           {"status": status, "scope": "all",
                            "applies_to": applies_to or {}, "forbids": forbids or {},
                            "reviewed": "2026-10-09", "content_hash": "x"})


def _store(services, rules):
    s = SqliteStore(":memory:")
    for o in list(services) + list(rules):
        s.upsert_object(o)
    s.commit()
    return s


def test_a_rule_naming_no_dependency_applies_everywhere():
    """How a rule about layout or naming is written, and the sensible default."""
    assert applies(_rule("layout"), _svc("a", []))


def test_a_rule_applies_only_where_its_dependency_is_declared():
    rule = _rule("redis", applies_to={"dependencies": ["cache-wrapper"]})
    assert applies(rule, _svc("a", ["cache-wrapper", "httpx"]))
    assert not applies(rule, _svc("b", ["httpx"]))


def test_matching_ignores_case():
    rule = _rule("r", applies_to={"dependencies": ["Cache-Wrapper"]})
    assert applies(rule, _svc("a", ["cache-wrapper"]))


def test_edges_carry_why_they_exist():
    store = _store([_svc("a", ["cache-wrapper", "commonutils"])],
                   [_rule("redis", applies_to={"dependencies": ["cache-wrapper"]})])
    link(store)
    rules = governing(store, "service:a")
    assert len(rules) == 1
    assert rules[0]["because"] == ["cache-wrapper"]


def test_a_rule_applying_to_everything_says_so_rather_than_matching_nothing():
    store = _store([_svc("a", ["httpx"])], [_rule("layout")])
    link(store)
    assert governing(store, "service:a")[0]["because"] == "applies to every service"


def test_mandatory_rules_sort_before_the_rest():
    store = _store([_svc("a", [])],
                   [_rule("z-must", status="mandatory"),
                    _rule("a-maybe", status="recommended")])
    link(store)
    assert [r["id"] for r in governing(store, "service:a")] == ["z-must", "a-maybe"]


def test_forbids_is_kept_but_never_reported_as_a_violation():
    """The block documents the rule for a reader. A declared dependency is the wrong signal
    to accuse a service from: the common violation reaches the package transitively and
    declares nothing, so a report built on manifests misses it silently."""
    store = _store([_svc("a", ["cache-wrapper", "redis"])],
                   [_rule("redis", applies_to={"dependencies": ["cache-wrapper"]},
                          forbids={"dependencies": ["redis"]})])
    r = link(store)
    assert "violations" not in r
    rels = {e.relationship for e in store.edges_batch(["service:a"], direction="out")}
    assert rels == {"governed_by"}
    assert store.get("guidance:redis").payload["forbids"]["dependencies"] == ["redis"]


def test_relinking_replaces_only_its_own_edges():
    """A re-link must not disturb the call graph or the service mesh."""
    from fleetlens.store.models import Relationship

    store = _store([_svc("a", ["cache-wrapper"])],
                   [_rule("redis", applies_to={"dependencies": ["cache-wrapper"]})])
    store.replace_edges("resolver", ["service:a"],
                        [Relationship("service:a", "calls", "service:a", "resolver", {})])
    store.commit()
    link(store)
    link(store)
    kept = store._conn.execute(
        "SELECT count(*) FROM relationships WHERE source='resolver'").fetchone()[0]
    mine = store._conn.execute(
        "SELECT count(*) FROM relationships WHERE source='guidance_link'").fetchone()[0]
    assert kept == 1
    assert mine == 1


def test_a_service_with_no_dependencies_still_gets_fleet_wide_rules():
    store = _store([_svc("a", [])], [_rule("layout"),
                                     _rule("redis", applies_to={"dependencies": ["x"]})])
    link(store)
    assert [r["id"] for r in governing(store, "service:a")] == ["layout"]


# --- dependency extraction ----------------------------------------------------------


def test_declared_dependencies_reads_each_ecosystem(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "svc"\ndependencies = [\n'
        '    "uvicorn[standard]~=0.35.0",\n    "httpx==0.28.1",\n    "vortex @ git+ssh://x/vortex.git@1.0",\n]\n'
        '[tool.ruff]\nline-length = 100\n[tool.pytest.ini_options]\naddopts = "-q"\n')
    deps = declared_dependencies(tmp_path)
    assert "httpx" in deps and "vortex" in deps and "uvicorn" in deps
    # Tool configuration is not a dependency, however much it looks like one.
    assert "line-length" not in deps and "addopts" not in deps


def test_a_bracket_inside_a_requirement_does_not_truncate_the_list(tmp_path):
    """`uvicorn[standard]` ended a non-greedy match and silently dropped the rest."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["uvicorn[standard]~=0.35", "last-one==1.0"]\n')
    assert "last-one" in declared_dependencies(tmp_path)


def test_a_bare_vcs_url_names_its_package(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "git+ssh://git@host/org/torpedo.git@3.7.3\n"
        "https://host/x/pkg.zip#egg=named-thing\n"
        "httpx==0.28.1\n")
    deps = declared_dependencies(tmp_path)
    assert "torpedo" in deps and "named-thing" in deps and "httpx" in deps
    assert "git" not in deps


def test_names_are_normalised_to_one_spelling(tmp_path):
    (tmp_path / "requirements.txt").write_text("Cache_Wrapper==1.0\n")
    assert declared_dependencies(tmp_path) == ["cache-wrapper"]


def test_scoped_npm_names_keep_their_scope(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"dependencies": {"@aws-sdk/client-s3": "^3", "ioredis": "^5"}}')
    deps = declared_dependencies(tmp_path)
    assert "@aws-sdk/client-s3" in deps and "ioredis" in deps


def test_poetry_and_cargo_tables_are_read(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry.dependencies]\npython = "^3.11"\nredis = "^5"\n'
        '[tool.poetry.group.dev.dependencies]\npytest = "^8"\n')
    deps = declared_dependencies(tmp_path)
    assert "redis" in deps and "pytest" in deps


def test_no_manifest_is_not_an_error(tmp_path):
    assert declared_dependencies(tmp_path) == []


# --- language scoping -----------------------------------------------------------------


def _svc_lang(slug, deps, langs):
    return KnowledgeObject("service", slug, slug, None, "unknown", "static", "index",
                           None, None, {"dependencies": deps, "languages": langs})


def test_a_rule_scoped_to_a_language_skips_other_languages():
    """"New Python services use X" named no dependency, so it matched the whole fleet."""
    rule = _rule("framework", applies_to={"languages": ["python"]})
    assert applies(rule, _svc_lang("api", [], ["python"]))
    assert not applies(rule, _svc_lang("web", [], ["javascript"]))


def test_language_and_dependency_both_have_to_hold():
    rule = _rule("r", applies_to={"dependencies": ["some-lib"], "languages": ["python"]})
    assert applies(rule, _svc_lang("a", ["some-lib"], ["python"]))
    assert not applies(rule, _svc_lang("b", ["some-lib"], ["javascript"]))
    assert not applies(rule, _svc_lang("c", ["other"], ["python"]))


def test_a_polyglot_service_matches_on_any_of_its_languages():
    rule = _rule("r", applies_to={"languages": ["python"]})
    assert applies(rule, _svc_lang("mono", [], ["typescript", "python"]))


def test_the_declared_language_is_used_when_none_were_observed():
    """An index built before languages were recorded still matches."""
    svc = KnowledgeObject("service", "old", "old", None, "u", "static", "index", None, None,
                          {"dependencies": [], "language": "python"})
    assert applies(_rule("r", applies_to={"languages": ["python"]}), svc)


def test_auto_is_not_a_language():
    svc = KnowledgeObject("service", "old", "old", None, "u", "static", "index", None, None,
                          {"dependencies": [], "language": "auto"})
    assert not applies(_rule("r", applies_to={"languages": ["python"]}), svc)


def test_the_matched_language_is_recorded_as_evidence():
    store = _store([_svc_lang("a", [], ["python"])],
                   [_rule("r", applies_to={"languages": ["python"]})])
    link(store)
    assert governing(store, "service:a")[0]["because"] == ["python"]


# --- observed languages ---------------------------------------------------------------


def test_languages_are_counted_from_files(tmp_path):
    from fleetlens.adapters.languages import observed_languages

    (tmp_path / "pkg").mkdir()
    for i in range(30):
        (tmp_path / "pkg" / f"m{i}.py").write_text("x = 1\n")
    assert observed_languages(tmp_path) == ["python"]


def test_a_stray_config_file_is_not_a_language(tmp_path):
    """One postcss.config.mjs must not make a Python service a JavaScript one."""
    from fleetlens.adapters.languages import observed_languages

    for i in range(60):
        (tmp_path / f"m{i}.py").write_text("x = 1\n")
    (tmp_path / "postcss.config.mjs").write_text("export default {}\n")
    assert observed_languages(tmp_path) == ["python"]


def test_a_single_file_never_counts_however_small_the_service(tmp_path):
    """In a 14-file service one app.js clears any percentage floor."""
    from fleetlens.adapters.languages import observed_languages

    for i in range(13):
        (tmp_path / f"m{i}.py").write_text("x = 1\n")
    (tmp_path / "app.js").write_text("console.log(1)\n")
    assert observed_languages(tmp_path) == ["python"]


def test_a_real_second_language_is_kept(tmp_path):
    from fleetlens.adapters.languages import observed_languages

    for i in range(40):
        (tmp_path / f"m{i}.py").write_text("x = 1\n")
    for i in range(25):
        (tmp_path / f"c{i}.ts").write_text("export const x = 1\n")
    assert observed_languages(tmp_path) == ["python", "typescript"]


def test_dependency_trees_do_not_vote(tmp_path):
    from fleetlens.adapters.languages import observed_languages

    (tmp_path / "node_modules" / "left-pad").mkdir(parents=True)
    for i in range(500):
        (tmp_path / "node_modules" / "left-pad" / f"i{i}.js").write_text("module.exports=1\n")
    for i in range(30):
        (tmp_path / f"m{i}.py").write_text("x = 1\n")
    assert observed_languages(tmp_path) == ["python"]


def test_an_empty_tree_has_no_language(tmp_path):
    from fleetlens.adapters.languages import observed_languages

    assert observed_languages(tmp_path) == []


# --- "I cannot tell" is not "nothing applies" -----------------------------------------


def test_a_service_indexed_before_deps_and_languages_is_not_matchable():
    """The silent failure: every rule needs one or the other, so a stale service matches
    nothing, and an empty list reads exactly like a clean bill of health."""
    from fleetlens.guidance.link import matchable

    stale = KnowledgeObject("service", "old", "old", None, "u", "static", "index", None,
                            None, {"symbol_count": 100, "interface_count": 34})
    assert not matchable(stale)
    assert matchable(_svc("a", ["cache-wrapper"]))
    assert matchable(_svc_lang("b", [], ["python"]))


def test_auto_alone_does_not_make_a_service_matchable():
    from fleetlens.guidance.link import matchable

    svc = KnowledgeObject("service", "x", "x", None, "u", "static", "index", None, None,
                          {"language": "auto"})
    assert not matchable(svc)


# --- relinking when either side changes ------------------------------------------------


def test_indexing_a_service_after_the_rulebook_still_links_it(tmp_path):
    """The ordering trap: link() used to run only when rules were ingested."""
    from fleetlens.guidance import ingest_roots
    from fleetlens.indexing import _relink

    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "r.md").write_text(
        "---\nkind: guidance\nid: redis\ntitle: Redis\n"
        "applies_to:\n  dependencies: [cache-wrapper]\n---\n\nbody\n")

    store = SqliteStore(":memory:")
    ingest_roots(store, [rules])                 # rules first, no services yet
    store.upsert_object(_svc("later", ["cache-wrapper"]))
    store.commit()
    assert governing(store, "service:later") == []

    _relink(store)
    assert [r["id"] for r in governing(store, "service:later")] == ["redis"]


def test_relink_is_a_no_op_without_guidance():
    from fleetlens.indexing import _relink

    store = SqliteStore(":memory:")
    store.upsert_object(_svc("a", ["x"]))
    store.commit()
    _relink(store)
    assert store.edges_batch(["service:a"], direction="out") == []
