"""Joining authored guidance to services: the content is a claim, the link is evidence."""
from __future__ import annotations

from fleetlens.adapters.deps import declared_dependencies
from fleetlens.guidance.link import (
    applies,
    contradicted_by,
    governing,
    link,
    violations,
)
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


def test_a_violation_names_the_packages_that_triggered_it():
    """"Violates the cache rule" is an accusation; "declares redis" is checkable."""
    rule = _rule("redis", applies_to={"dependencies": ["cache-wrapper"]},
                 forbids={"dependencies": ["redis", "aioredis"]})
    assert contradicted_by(rule, _svc("a", ["cache-wrapper", "redis"])) == ["redis"]
    assert contradicted_by(rule, _svc("b", ["cache-wrapper"])) == []


def test_a_rule_that_does_not_govern_a_service_cannot_be_violated_by_it():
    rule = _rule("redis", applies_to={"dependencies": ["cache-wrapper"]},
                 forbids={"dependencies": ["redis"]})
    assert contradicted_by(rule, _svc("b", ["redis"])) == []


def test_only_mandatory_rules_produce_violations():
    forbids = {"dependencies": ["redis"]}
    store = _store([_svc("a", ["redis"])], [_rule("soft", forbids=forbids,
                                                  status="recommended")])
    link(store)
    assert violations(store) == []


def test_violations_report_the_service_rule_and_evidence():
    store = _store([_svc("a", ["cache-wrapper", "redis"]), _svc("b", ["cache-wrapper"])],
                   [_rule("redis-access", applies_to={"dependencies": ["cache-wrapper"]},
                          forbids={"dependencies": ["redis"]})])
    r = link(store)
    assert r["violations"] == 1
    found = violations(store)
    assert len(found) == 1
    assert found[0]["service"] == "a"
    assert found[0]["guidance"] == "redis-access"
    assert found[0]["declares"] == ["redis"]


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
