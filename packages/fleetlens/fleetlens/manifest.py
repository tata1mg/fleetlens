"""Service manifest — declare how a repo maps to services (repos != services 1:1).

An optional `fleetlens.yaml` (or `.yml` / `.json`) at a repo root declares the deployable
services in that repo and their source roots:

    services:
      - name: orders
        path: services/orders     # sub-tree to index (monorepo); default "."
        language: auto            # optional; else auto-detected on the sub-path
      - name: billing
        path: services/billing
    libraries:                    # code-only paths; never become mesh services
      - path: packages/shared
    guidance:                     # directories of authored engineering rules
      - path: rules

A repo that is entirely a library declares only `libraries: [{path: "."}]`. Its code is
indexed and its symbols are searchable, but it contributes no service and no interfaces: a
shared package is not something another service calls over the network, and listing it as
one corrupts both the service graph and any "which services are unused" reading of it.

`guidance` names directories of human-authored engineering rules. A repo that holds only
those declares `guidance: [{path: "."}]` and no services: its content is read by
`fl ingest-guidance`, and it contributes no code, no symbols and no service. Declaring it
here is what lets services, libraries and rulebooks sit in one directory and still be told
apart, since nothing about a folder of Markdown says which it is.

No manifest => the zero-config default: the repo is ONE service, slug = folder name.
Deployment topology (which code runs as which deployed service) can't be inferred from
source, so it is declared here rather than guessed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_NAMES = ("fleetlens.yaml", "fleetlens.yml", "fleetlens.json")


@dataclass
class ServiceSpec:
    name: str
    path: str = "."          # source root, relative to the repo
    language: str = "auto"   # "auto" | "python" | "typescript"
    hosts: dict = field(default_factory=dict)   # declared host -> service name
    library: bool = False    # code only: symbols are indexed, but it is not a mesh service


@dataclass
class Manifest:
    services: list[ServiceSpec] = field(default_factory=list)
    library_paths: list[str] = field(default_factory=list)
    #: Sub-paths holding authored guidance. Not code: never indexed, never a service.
    guidance_paths: list[str] = field(default_factory=list)
    # host -> service name, for addresses no generic resolver could map (see adapters.hosts)
    hosts: dict[str, str] = field(default_factory=dict)


class ManifestError(ValueError):
    pass


def _read(path: Path) -> dict:
    if path.suffix == ".json":
        return json.loads(path.read_text()) or {}
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise ManifestError(
            f"{path.name} needs PyYAML — pip install pyyaml (or use fleetlens.json)"
        ) from exc
    return yaml.safe_load(path.read_text()) or {}


def find_manifest(repo: Path) -> Optional[Path]:
    for name in _NAMES:
        p = Path(repo) / name
        if p.exists():
            return p
    return None


def load_manifest(repo: Path) -> Optional[Manifest]:
    path = find_manifest(repo)
    if path is None:
        return None
    raw = _read(path)
    hosts = {str(k).strip().lower(): str(v) for k, v in (raw.get("hosts") or {}).items()}
    services = []
    seen: set[str] = set()
    for entry in raw.get("services", []) or []:
        name = entry.get("name")
        if not name:
            raise ManifestError(f"{path.name}: a service entry is missing 'name'")
        if name in seen:
            raise ManifestError(f"{path.name}: duplicate service name '{name}'")
        seen.add(name)
        services.append(ServiceSpec(name=name, path=str(entry.get("path", ".")),
                                    language=str(entry.get("language", "auto"))))
    libs = [str(e.get("path")) for e in (raw.get("libraries", []) or []) if e.get("path")]
    rules = [str(e.get("path")) for e in (raw.get("guidance", []) or []) if e.get("path")]
    return Manifest(services=services, library_paths=libs, guidance_paths=rules, hosts=hosts)


def guidance_roots(repo: Path) -> list[Path]:
    """Directories of authored rules this repo declares, or [] if it declares none."""
    repo = Path(repo)
    manifest = load_manifest(repo)
    if manifest is None:
        return []
    return [repo if p in (".", "") else repo / p.strip("/") for p in manifest.guidance_paths]


def resolve_services(repo: Path, default_name: Optional[str] = None) -> list[ServiceSpec]:
    """What to index for a repo: from its manifest, else the 1:1 default.

    A library entry yields a spec like any other, marked `library`. Indexing reads its code
    and stores its symbols, but it contributes no service to the mesh and no interfaces,
    because a shared package is not something anything calls over the network. A repo that
    is entirely a library declares `libraries: [{path: "."}]` and no services.
    """
    repo = Path(repo)
    manifest = load_manifest(repo)
    hosts = dict(manifest.hosts) if manifest else {}
    # A repo that declares only guidance holds no code to index. Returning the 1:1 default
    # for it would make a folder of Markdown a service in the mesh.
    if manifest and manifest.guidance_paths and not (manifest.services
                                                     or manifest.library_paths):
        return []
    if manifest and (manifest.services or manifest.library_paths):
        specs = []
        for spec in manifest.services:
            spec.hosts = {**hosts, **(spec.hosts or {})}
            specs.append(spec)
        for path in manifest.library_paths:
            name = repo.name if path in (".", "") else f"{repo.name}/{path.strip('/')}"
            specs.append(ServiceSpec(name=name, path=path or ".", library=True))
        return specs
    return [ServiceSpec(name=default_name or repo.name, path=".", language="auto", hosts=hosts)]
