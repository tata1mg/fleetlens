"""Manifest: default 1:1, monorepo multi-service, sub-path scoping, libraries excluded."""
from __future__ import annotations

import pytest
from fleetlens.manifest import ManifestError, load_manifest, resolve_services


def test_default_is_one_service_per_repo(tmp_path):
    assert load_manifest(tmp_path) is None
    specs = resolve_services(tmp_path, default_name="myrepo")
    assert len(specs) == 1 and specs[0].name == "myrepo" and specs[0].path == "."


def test_yaml_manifest_declares_services(tmp_path):
    (tmp_path / "fleetlens.yaml").write_text(
        "services:\n"
        "  - name: orders\n    path: services/orders\n"
        "  - name: billing\n    path: services/billing\n    language: typescript\n"
        "libraries:\n  - path: packages/shared\n")
    m = load_manifest(tmp_path)
    assert [s.name for s in m.services] == ["orders", "billing"]
    assert m.services[0].path == "services/orders"
    assert m.services[1].language == "typescript"
    assert m.library_paths == ["packages/shared"]


def test_json_manifest_works_without_yaml(tmp_path):
    (tmp_path / "fleetlens.json").write_text(
        '{"services": [{"name": "api", "path": "app"}]}')
    specs = resolve_services(tmp_path)
    assert len(specs) == 1 and specs[0].name == "api" and specs[0].path == "app"


def test_duplicate_and_missing_name_rejected(tmp_path):
    (tmp_path / "fleetlens.json").write_text('{"services":[{"path":"x"}]}')
    with pytest.raises(ManifestError):
        load_manifest(tmp_path)
    (tmp_path / "fleetlens.json").write_text(
        '{"services":[{"name":"a"},{"name":"a"}]}')
    with pytest.raises(ManifestError):
        load_manifest(tmp_path)


def test_monorepo_subpath_scoping(tmp_path):
    """A monorepo manifest yields per-service, sub-path-scoped interfaces + slugs.

    Validated at the adapter/loader level (deterministic) — the SCIP call-graph build is
    exercised separately in the callgraph tests.
    """
    from fleetlens.adapters import registry as iface_registry
    from fleetlens.loaders import interfaces as iface_loader
    from fleetlens.store.sqlite import SqliteStore

    for svc, route in (("orders", "/orders"), ("billing", "/invoices")):
        d = tmp_path / "services" / svc
        d.mkdir(parents=True)
        (d / "app.py").write_text(
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            f"@app.get('{route}')\n"
            "def handler():\n    return {}\n")
    (tmp_path / "fleetlens.yaml").write_text(
        "services:\n  - name: orders\n    path: services/orders\n"
        "  - name: billing\n    path: services/billing\n")

    specs = resolve_services(tmp_path)
    assert {s.name for s in specs} == {"orders", "billing"}

    store = SqliteStore(":memory:")
    for spec in specs:
        root = tmp_path / spec.path
        iface_registry.build_interfaces(root, spec.name)
        iface_loader.load(root / ".context", spec.name, store)

    iface_ids = {o.id for o in store.list_objects("interface")}
    # each service's interface is scoped to ITS OWN sub-path (no bleed across services)
    assert iface_ids == {"interface:orders:get-orders", "interface:billing:get-invoices"}


def test_a_library_repo_is_indexed_for_code_but_is_not_a_service(tmp_path):
    """A shared package is not something another service calls over the network. Indexing
    one as a service invents a node nobody deploys, gives it whatever routes its examples
    and its own health blueprint declare, and corrupts any reading of which services are
    unused. Its code is still worth having: `find_symbol` should reach it.
    """
    from fleetlens.manifest import resolve_services

    (tmp_path / "fleetlens.yaml").write_text('libraries:\n  - path: "."\n')
    specs = resolve_services(tmp_path)

    assert len(specs) == 1
    assert specs[0].library is True
    assert specs[0].path == "."


def test_a_monorepo_can_mix_services_and_libraries(tmp_path):
    from fleetlens.manifest import resolve_services

    (tmp_path / "fleetlens.yaml").write_text(
        "services:\n  - name: orders\n    path: services/orders\n"
        "libraries:\n  - path: packages/shared\n")
    specs = {s.name: s for s in resolve_services(tmp_path)}

    assert specs["orders"].library is False
    shared = next(s for s in specs.values() if s.library)
    assert shared.path == "packages/shared"
