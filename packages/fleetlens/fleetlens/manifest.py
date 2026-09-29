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


@dataclass
class Manifest:
    services: list[ServiceSpec] = field(default_factory=list)
    library_paths: list[str] = field(default_factory=list)
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
    return Manifest(services=services, library_paths=libs, hosts=hosts)


def resolve_services(repo: Path, default_name: Optional[str] = None) -> list[ServiceSpec]:
    """The services to index for a repo: from its manifest, else the 1:1 default."""
    repo = Path(repo)
    manifest = load_manifest(repo)
    if manifest and manifest.services:
        for spec in manifest.services:
            spec.hosts = {**manifest.hosts, **(spec.hosts or {})}
        return manifest.services
    hosts = dict(manifest.hosts) if manifest else {}
    return [ServiceSpec(name=default_name or repo.name, path=".", language="auto", hosts=hosts)]
