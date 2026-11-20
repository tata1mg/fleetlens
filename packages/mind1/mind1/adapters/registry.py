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
    """Every interface in the repo, with one path reported once.

    Only one route can serve a path, so a repeated (method, path) collapses either way.
    What matters is which kind of repeat it was. Two adapters describing the same endpoint
    is ordinary and silent. Two routes from the *same* adapter landing on one path means
    the repo declared two endpoints and we read them as one, which happens when a path was
    composed wrongly -- a per-route `version=` we ignored, a prefix we failed to apply.

    That second kind used to be dropped without a trace, so a service lost an endpoint
    nobody could discover was missing. It is now recorded as a skipped site, and the
    surviving interface carries both source locations.
    """
    repo = Path(repo)
    seen: dict[tuple[str, str], tuple[str, Interface]] = {}
    out: list[Interface] = []
    for adapter in ADAPTERS:
        if not adapter.applies(repo):
            continue
        for iface in adapter.discover(repo, skipped):
            key = (iface.method, iface.path)
            prev = seen.get(key)
            if prev is None:
                seen[key] = (adapter.name, iface)
                out.append(iface)
                continue
            owner, kept = prev
            if owner != adapter.name:
                continue                       # one endpoint, two adapters: expected
            for e in iface.evidence:           # keep the trail to both declarations
                if e not in kept.evidence:
                    kept.evidence.append(e)
            if skipped is not None:
                file, _, line = (iface.evidence[0] if iface.evidence else "").rpartition(":")
                skipped.append(SkippedSite(
                    kind="interface", reason="duplicate-path", file=file or "?",
                    line=int(line) if line.isdigit() else 0, expr=iface.path,
                    snippet=f"also declared at {kept.evidence[0] if kept.evidence else '?'}",
                    names=[], method=iface.method))
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
