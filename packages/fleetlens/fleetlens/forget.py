"""Take a repository back out of an index.

Re-indexing cannot do this. The loaders clear a repo's own rows before re-inserting them,
so indexing replaces what it visits; a repo you stop indexing is never visited, and its
rows stay. Left alone, a decommissioned service keeps answering `list_services` and
`get_service_graph` as a live service, with evidence and a confidence label attached, and
nothing in the response says the row is stale.

The rule this module follows is that forgetting a repository must leave the index in the
state a full rebuild without that repository would have produced. Everything below is a
consequence of it:

* Edges into a forgotten service go, rather than being kept as a record of what used to
  call it. `resolve` matches a caller's observed hosts and paths against indexed services,
  so with the target gone a rebuild would not produce that edge either. They cascade, since
  both ends of `relationships` are declared `ON DELETE CASCADE`.
* Enrichments go, although they are deliberately not foreign-keyed and are ignored at read
  time when their object is missing. A rebuild would not have them.
* A `repo_shas` entry goes only when nothing indexed from that repository survives. One
  repository can declare several services, and `fl forget` on one of them does not mean the
  checkout is gone.
* The build is re-stamped. The index is no longer the one that `build_id` named, and two
  different indexes sharing an id is exactly what that field exists to prevent.

This is not an undo. Nothing records what was removed, and rebuilding stays the way back.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

#: Objects keyed by slug under their own id prefix. The trailing colon matters: without it
#: `service:cart` would also match `service:cart-worker`.
SCOPED = ("interface", "code_symbol")


@dataclass
class Target:
    """One indexed service or library, and what removing it takes with it."""
    slug: str
    kind: str                 # "service" or "library"
    root: str                 # where it was indexed from
    symbols: int = 0
    interfaces: int = 0


@dataclass
class Plan:
    """What `forget` would do, assembled before anything is written."""
    targets: list[Target] = field(default_factory=list)
    #: Directories whose authored guidance goes with the targets.
    roots: list[str] = field(default_factory=list)
    #: `repo_shas` keys to reconsider once the targets are gone.
    repos: list[str] = field(default_factory=list)
    #: Names that match neither a slug nor an indexed repository.
    unknown: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.targets or self.repos)


def _indexed(store) -> list[Target]:
    out = []
    for kind in ("service", "library"):
        for o in store.list_objects(kind):
            payload = o.payload or {}
            out.append(Target(slug=o.id.split(":", 1)[1], kind=kind,
                              root=str(payload.get("root") or ""),
                              symbols=int(payload.get("symbol_count") or 0),
                              interfaces=int(payload.get("interface_count") or 0)))
    return out


def _repo_root(root: str, repo: str) -> str:
    """The ancestor of `root` named `repo`, or "" if there is none.

    `repo_shas` is keyed by the repository's directory name while objects carry an absolute
    path, and nothing in the index joins the two. A service indexed from `repo/services/api`
    has `repo` as an ancestor, so walking up is the right shape of match. It is ambiguous
    only when a directory inside one repository shares a name with another, which is why
    `fl forget` shows what it matched before it writes anything.
    """
    if not root or not repo:
        return ""
    p = Path(root)
    for anc in (p, *p.parents):
        if anc.name == repo:
            return str(anc)
    return ""


def _under(path: str, roots: list[Path]) -> bool:
    if not path:
        return False
    p = Path(path)
    return any(p == r or r in p.parents for r in roots)


def plan(store, names: list[str]) -> Plan:
    """Work out what removing `names` would take with it, touching nothing.

    A name is a service or library slug, or a repository directory name as `get_index_info`
    reports it under `repositories`. Those are different keys and a caller has whichever one
    they were shown, so both are accepted.
    """
    known = _indexed(store)
    by_slug = {t.slug: t for t in known}
    shas = store.repo_shas()
    out, considered = Plan(), set()

    def take(t: Target) -> None:
        if not any(x.slug == t.slug and x.kind == t.kind for x in out.targets):
            out.targets.append(t)

    for name in names:
        # A slug wins over a repository of the same name, because it is the narrower
        # reading and the one that removes less.
        if name in by_slug:
            t = by_slug[name]
            take(t)
            if t.root:
                out.roots.append(t.root)
            considered |= {k for k in shas if _repo_root(t.root, k)}
            continue
        if name in shas:
            considered.add(name)
            for t in known:
                if _repo_root(t.root, name):
                    take(t)
            continue
        out.unknown.append(name)

    # A repository leaves the index only when nothing indexed from it survives. Decided
    # here rather than after the delete so that the plan shown to the caller is the whole
    # truth, including the authored rules that go with a repository that is wholly gone.
    doomed = {(t.kind, t.slug) for t in out.targets}
    survivors = [t for t in known if (t.kind, t.slug) not in doomed]
    for key in sorted(considered):
        if any(_repo_root(t.root, key) for t in survivors):
            continue
        out.repos.append(key)
        out.roots += [r for r in (_repo_root(t.root, key) for t in out.targets) if r]

    out.repos = sorted(set(out.repos))
    out.roots = sorted(set(out.roots))
    return out


def forget(store, p: Plan) -> dict:
    """Execute `p`. One transaction, so an interrupted run cannot half-remove a repository."""
    exact, prefixes = [], []
    for t in p.targets:
        exact.append(f"{t.kind}:{t.slug}")
        prefixes += [f"{kind}:{t.slug}:" for kind in SCOPED]

    # Guidance is keyed by an id its rulebook chooses, so it is identified by where it was
    # read from. Rules declared at the repository root go when the repository does; rules
    # declared above a service that is only part of a repository stay, because the
    # repository does.
    roots = [Path(r) for r in p.roots]
    guidance = [o.id for o in store.list_objects("guidance")
                if _under(str((o.payload or {}).get("root") or ""), roots)]
    exact += guidance

    counts = store.delete_scope(exact, prefixes)

    # The index is no longer the build that `build_id` named. Re-stamp before writing the
    # pruned map, since stamp_build merges into whatever repo_shas currently holds.
    store.stamp_build({})
    shas = store.repo_shas()
    pruned = [k for k in p.repos if shas.pop(k, None) is not None]
    if pruned:
        store.set_meta("repo_shas", json.dumps(shas, sort_keys=True))

    # Guidance is joined to services by what each service declares, so removing a service
    # changes the join even when no rule was touched. Indexing relinks for the same reason.
    if guidance or p.targets:
        try:
            if store.list_objects("guidance"):
                from .guidance import link
                link(store)
        except Exception:    # noqa: BLE001 - a failed relink must not fail a removal
            pass
    store.commit()
    return {"slugs": sorted(t.slug for t in p.targets), "repos": pruned,
            "guidance": len(guidance), **counts}
