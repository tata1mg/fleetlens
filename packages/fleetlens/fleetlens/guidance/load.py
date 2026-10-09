"""Read a directory of Markdown rules into knowledge objects.

The content lives outside this repository on purpose: it is one organisation's standards, and
fleetlens only supplies the mechanism. A directory is pointed at, one file holds one rule, and
the frontmatter says what the rule applies to.

The frontmatter parser is deliberately a small one rather than a YAML dependency. The fields
here are scalars, flat lists and one nested mapping, which is a grammar worth fifty lines to
avoid adding a parser to a tool whose point is not needing one.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path
from typing import Any

from ..manifest import find_manifest, guidance_roots
from ..store.models import KnowledgeObject

#: Accepted values for `status`. A rule that is not one of these is a typo, and a typo that
#: silently becomes "recommended" is worse than a refused file.
STATUS = ("mandatory", "recommended", "contextual")
#: Accepted values for `scope`. `new_services` is what lets a rule be true about new code while
#: most of the fleet still looks different, which is the normal state during a migration.
SCOPE = ("all", "new_services")

_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.S)
#: Every rule declares what it is. Frontmatter alone is not enough of a marker: Jekyll posts,
#: Hugo content and MkDocs pages all carry it, and a documentation tree sitting beside a
#: rulebook would otherwise be read as rules. A file without this is skipped in silence.
KIND = "guidance"
_ID_OK = re.compile(r"\A[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SUFFIXES = (".md", ".markdown")


class GuidanceError(ValueError):
    """A guidance file that cannot be read as one. Always names the file."""


def _scalar(raw: str) -> Any:
    """A frontmatter value: inline list, quoted string, or bare string."""
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [_scalar(p) for p in inner.split(",") if p.strip()] if inner else []
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def _parse_frontmatter(text: str, where: Path) -> dict:
    """Flat `key: value` pairs, plus one level of nesting by indentation.

    Enough for the shape guidance uses and no more. Anything deeper is rejected loudly rather
    than half-understood, because a rule the index silently misreads is worse than one it
    refuses to load.
    """
    out: dict[str, Any] = {}
    parent: str | None = None
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent and parent is None:
            raise GuidanceError(f"{where}: line {n} is indented under nothing")
        if indent > 4:
            raise GuidanceError(
                f"{where}: line {n} nests deeper than guidance frontmatter allows")
        key, sep, value = line.strip().partition(":")
        if not sep:
            raise GuidanceError(f"{where}: line {n} is not `key: value`")
        key = key.strip()
        if indent:
            out[parent][key] = _scalar(value)        # type: ignore[index]
            continue
        if not value.strip():
            # A bare `key:` opens a nested block. A list written as `- item` lines beneath it
            # is not supported; say so here rather than returning a silently empty mapping.
            out[key] = {}
            parent = key
            continue
        out[key] = _scalar(value)
        parent = None
    return out


def _require(meta: dict, field: str, where: Path) -> str:
    value = meta.get(field)
    if not isinstance(value, str) or not value.strip():
        raise GuidanceError(f"{where}: frontmatter needs a `{field}`")
    return value.strip()


def _block(meta: dict, field: str, where: Path) -> dict:
    raw = meta.get(field) or {}
    if not isinstance(raw, dict):
        raise GuidanceError(f"{where}: `{field}` must be a block of key: [values]")
    out = {}
    for key, value in raw.items():
        out[key] = [value] if isinstance(value, str) else list(value or [])
    return out


def is_guidance(path: Path) -> bool:
    """Whether this file declares itself a rule, read from its frontmatter alone.

    Cheap on purpose: a directory holding rules usually holds other Markdown too, and a
    README is not a malformed rule, it is simply not one.
    """
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return False
    m = _FRONTMATTER.match(head)
    if not m:
        return False
    return bool(re.search(rf"^\s*kind\s*:\s*['\"]?{KIND}['\"]?\s*$", m.group(1), re.M))


def read_file(path: Path) -> KnowledgeObject:
    """One Markdown rule as a knowledge object. Raises GuidanceError on anything malformed."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GuidanceError(f"{path}: {exc}") from exc

    m = _FRONTMATTER.match(text)
    if not m:
        raise GuidanceError(f"{path}: no `---` frontmatter block at the top of the file")
    meta = _parse_frontmatter(m.group(1), path)
    body = text[m.end():].strip()
    if not body:
        raise GuidanceError(f"{path}: frontmatter but no guidance under it")

    kind = str(meta.get("kind", KIND)).strip()
    if kind != KIND:
        raise GuidanceError(f"{path}: kind {kind!r} is not {KIND!r}")
    gid = _require(meta, "id", path)
    if not _ID_OK.match(gid):
        raise GuidanceError(f"{path}: id {gid!r} must be lower-case words joined by hyphens")
    title = _require(meta, "title", path)

    status = str(meta.get("status", "recommended")).strip()
    if status not in STATUS:
        raise GuidanceError(f"{path}: status {status!r} is not one of {', '.join(STATUS)}")
    scope = str(meta.get("scope", "all")).strip()
    if scope not in SCOPE:
        raise GuidanceError(f"{path}: scope {scope!r} is not one of {', '.join(SCOPE)}")

    reviewed = str(meta.get("reviewed", "")).strip()
    if reviewed:
        try:
            date.fromisoformat(reviewed)
        except ValueError as exc:
            raise GuidanceError(f"{path}: reviewed {reviewed!r} is not a YYYY-MM-DD date") from exc

    payload = {
        "status": status,
        "scope": scope,
        "applies_to": _block(meta, "applies_to", path),
        # What a service must NOT declare under this rule. Only meaningful on a mandatory
        # rule, and the match is a package name in a manifest, so a finding is checkable.
        "forbids": _block(meta, "forbids", path),
        "reviewed": reviewed,
        "owner": str(meta.get("owner", "")).strip(),
        "file": path.name,
        # The body's hash, so re-ingesting is free when nothing changed and an edit is caught
        # even when the frontmatter is untouched.
        "content_hash": hashlib.sha1(body.encode()).hexdigest()[:16],
    }
    return KnowledgeObject(
        object_type="guidance",
        object_id=gid,
        name=title,
        summary=body,
        version=reviewed or "unknown",
        # Not `static` and not `llm`. A reader of any response can tell this was written by a
        # person and is therefore a claim, which is the whole reason the type exists.
        source="authored",
        generation_strategy="authored",
        last_generated_at=None,
        # The title and body are what a question like "how should I cache" matches on.
        embed_text=f"{title}\n\n{body}",
        payload=payload,
    )


def load_guidance(directory: str | Path) -> tuple[list[KnowledgeObject], list[str]]:
    """(objects, problems) for every Markdown file under `directory`.

    One bad file does not stop the rest: a typo in one rule should not take the other
    fourteen out of the index. Every problem is returned so the caller can print them all.
    """
    root = Path(directory)
    if not root.is_dir():
        raise GuidanceError(f"{root}: not a directory")

    objs: list[KnowledgeObject] = []
    problems: list[str] = []
    seen: dict[str, Path] = {}
    for path in sorted(p for p in root.rglob("*") if p.suffix.lower() in _SUFFIXES):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        # A README is not a malformed rule. Only a file that claims to be one is held to
        # the rules' standards, so pointing this at a tree of repositories reports what is
        # wrong with the rulebooks and says nothing about the 59 changelogs beside them.
        if not is_guidance(path):
            continue
        try:
            obj = read_file(path)
        except GuidanceError as exc:
            problems.append(str(exc))
            continue
        if obj.object_id in seen:
            problems.append(f"{path}: id {obj.object_id!r} already used by {seen[obj.object_id]}")
            continue
        seen[obj.object_id] = path
        objs.append(obj)
    return objs, problems


def guidance_dirs(path: str | Path) -> list[Path]:
    """The rule directories under `path`, honouring every repo's manifest.

    Three shapes, because all three are reasonable and a fleet usually has the first:

      a directory of repositories   each child's `fleetlens.yaml` says whether it holds
                                    guidance, so services, libraries and rulebooks can share
                                    one parent directory and still be told apart
      one repository                its own manifest decides
      a bare directory of rules     no manifest anywhere, so the path itself is the rulebook

    The declaration is what makes the first case work. Nothing about a folder of Markdown
    says whether it is a rulebook, a documentation site or a changelog archive.
    """
    root = Path(path)
    if not root.is_dir():
        raise GuidanceError(f"{root}: not a directory")

    declared = list(guidance_roots(root))
    if declared:
        return declared

    children: list[Path] = []
    found_manifest = find_manifest(root) is not None
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        if child.name.startswith("."):
            continue
        if find_manifest(child) is not None:
            found_manifest = True
            children += guidance_roots(child)
    if children:
        return children
    # A manifest exists and declares no guidance: that is an answer, not an omission, so do
    # not fall back to scanning. Without one, the caller meant the directory itself.
    return [] if found_manifest else [root]


def ingest(store, directory: str | Path) -> dict:
    """Load every rule under `directory`, replacing whatever guidance was there before."""
    return ingest_roots(store, guidance_dirs(directory), replace=True)


def ingest_roots(store, roots, *, replace: bool) -> dict:
    """Load the rules in `roots` into `store`.

    `replace` withdraws rules that are no longer present, which is right only for a caller
    that has seen every rulebook there is: a fleet sweep, or an explicit ingest of the
    guidance tree. Indexing a single repository has seen one, so it adds and updates without
    withdrawing, because a rule missing from this repo was never this repo's to withdraw.
    """
    roots = [Path(r) for r in roots]
    objs: list[KnowledgeObject] = []
    problems: list[str] = []
    seen_ids: dict[str, str] = {}
    for root in roots:
        if not root.is_dir():
            problems.append(f"{root}: declared as guidance but not a directory")
            continue
        found, trouble = load_guidance(root)
        problems += trouble
        for obj in found:
            if obj.object_id in seen_ids:
                problems.append(f"{root}: id {obj.object_id!r} already defined in "
                                f"{seen_ids[obj.object_id]}")
                continue
            seen_ids[obj.object_id] = str(root)
            objs.append(obj)
    before = {o.id for o in store.list_objects("guidance")}
    for obj in objs:
        store.upsert_object(obj)
    kept = {o.id for o in objs}
    removed = 0
    if replace:
        for stale in sorted(before - kept):
            removed += store.delete_objects_by_id_prefix(stale)
    store.commit()
    # Relink straight away. Guidance that is stored but unlinked answers nothing, and the
    # join is milliseconds, so there is no reason to make it a second command to forget.
    from .link import link
    linked = link(store)
    return {"loaded": len(objs), "removed": removed, "problems": problems,
            "roots": [str(r) for r in roots],
            "ids": sorted(o.object_id for o in objs), **linked}
