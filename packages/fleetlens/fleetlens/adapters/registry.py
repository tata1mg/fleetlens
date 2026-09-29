"""Adapter registry — run every applicable adapter over a repo and emit interfaces.json.

Add an adapter by importing it and appending to ADAPTERS. Ids are made unique per repo
(a `-2`, `-3` suffix on collision) so downstream references stay stable.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .base import Interface, SkippedSite
from .messaging import MessagingAdapter
from .python_web import PythonWebAdapter
from .ruby_web import RailsRouteAdapter
from .ts_web import TSWebAdapter

ADAPTERS = [PythonWebAdapter(), TSWebAdapter(), RailsRouteAdapter(), MessagingAdapter()]


def discover_interfaces(repo: Path, skipped: list[SkippedSite] | None = None) -> list[Interface]:
    repo = Path(repo)
    seen: set[tuple[str, str]] = set()
    out: list[Interface] = []
    for adapter in ADAPTERS:
        if not adapter.applies(repo):
            continue
        for iface in adapter.discover(repo, skipped):
            key = (iface.method, iface.path)
            if key in seen:
                continue
            seen.add(key)
            out.append(iface)
    return out


def _to_dict(iface: Interface, uid: str) -> dict:
    return {"id": uid, "name": iface.name, "type": iface.type, "method": iface.method,
            "path": iface.path, "handler": iface.handler, "summary": iface.summary,
            "framework": iface.framework, "evidence": iface.evidence}


def build_interfaces(repo: Path, slug: str) -> dict:
    """Discover interfaces and write <repo>/.context/interfaces.json (+ skipped.json: the
    sites adapters saw but could not resolve). Returns a summary."""
    repo = Path(repo)
    skipped: list[SkippedSite] = []
    interfaces = discover_interfaces(repo, skipped)
    records, used = [], {}
    for iface in interfaces:
        uid = iface.id
        if uid in used:
            used[uid] += 1
            uid = f"{uid}-{used[iface.id]}"
        else:
            used[uid] = 1
        records.append(_to_dict(iface, uid))
    out = repo / ".context"
    out.mkdir(parents=True, exist_ok=True)
    (out / "interfaces.json").write_text(
        json.dumps({"schema": "interfaces/v1", "slug": slug, "interfaces": records},
                   indent=2) + "\n")
    (out / "skipped.json").write_text(
        json.dumps({"schema": "skipped/v1", "slug": slug,
                    "sites": [asdict(sk) for sk in skipped]}, indent=2) + "\n")
    return {"interfaces": len(records), "skipped": len(skipped)}
