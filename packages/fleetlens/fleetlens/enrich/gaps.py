"""LLM gap-filler — resolves the sites deterministic adapters recorded but could not parse.

Opt-in, bounded, grounded. It never scans a repo at large: the only inputs are the
`skipped` sites each service node carries (a route decorator or HTTP call whose path was
not a literal). For each site it:
  1. gathers one-hop context — the source of symbols the unresolved expression references,
     looked up in the already-indexed call graph (generic; no per-repo rules),
  2. asks the LLM for a strict JSON answer (resolved path, or "unresolvable: comes from X"),
  3. GROUNDS the answer: the proposed path must exist as a string literal somewhere in the
     repo's source, or it is rejected. This is what keeps a small local model honest.

Accepted answers land as `source="llm"` objects and never overwrite static ones. A site is
processed once per (content hash); re-indexing resets that, since the evidence changed.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Optional

from ..adapters.base import Interface
from ..store.base import ContextStore, KnowledgeStore, RelationshipStore
from ..store.models import KnowledgeObject, Relationship
from .providers import LLMProvider

_VERBS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}
_SYS = ("You are a static-analysis assistant. Given a call site and the definitions it "
        "references, trace each expression to its concrete value. Reply with a single JSON "
        "object and nothing else.")
_MAX_TOKENS = 120
_MAX_CTX_SYMBOLS = 4
_MAX_CTX_LINES = 40
_SRC_EXT = (".py", ".ts", ".js", ".tsx", ".yaml", ".yml", ".json", ".toml", ".ini", ".env")
_SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", ".context"}


def _hash(site: dict) -> str:
    return hashlib.sha1("|".join(str(site.get(k, "")) for k in
                                 ("kind", "file", "line", "expr", "snippet")).encode()).hexdigest()[:16]


def _read_lines(root: Path, rel: str, start: int, end: int) -> str:
    try:
        lines = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    end = min(end, start + _MAX_CTX_LINES - 1)
    return "\n".join(lines[max(0, start - 1):end])


def _context(knowledge: KnowledgeStore, slug: str, root: Path, names: list[str]) -> str:
    """Source of up to N symbols in this service whose name matches an identifier the
    unresolved expression uses. Classes/modules first: a `Model.uri()` call is usually
    answered by the class body, not the accessor."""
    prefix = f"code_symbol:{slug}:"
    picked: list[KnowledgeObject] = []
    seen: set[str] = set()
    for n in names:
        if len(n) < 3:
            continue
        for sid in knowledge.find_ids(n, "code_symbol", limit=25):
            if not sid.startswith(prefix) or sid in seen:
                continue
            obj = knowledge.get(sid)
            if not obj or obj.payload.get("qualname", "").rsplit(".", 1)[-1] != n:
                continue
            seen.add(sid)
            picked.append(obj)
    picked.sort(key=lambda o: 0 if o.payload.get("kind") in ("class", "module") else 1)
    blocks = []
    for obj in picked[:_MAX_CTX_SYMBOLS]:
        p = obj.payload
        src = _read_lines(root, p["path"], int(p.get("line_start") or 1), int(p.get("line_end") or 1))
        if src:
            blocks.append(f"# {p['path']}:{p.get('line_start')} ({p.get('kind')} {p.get('qualname')})\n{src}")
    return "\n\n".join(blocks) or _text_context(root, names)


def _iter_source(root: Path):
    for p in root.rglob("*"):
        if p.suffix in _SRC_EXT and not any(part in _SKIP for part in p.parts):
            yield p


def _text_context(root: Path, names: list[str], pad: int = 6) -> str:
    """Fallback when the call graph has no symbol for a name (no indexer for the language,
    or a plain constant): the lines that define it, found textually. Language-agnostic."""
    wanted = [n for n in names if len(n) >= 3]
    if not wanted:
        return ""
    defn = re.compile(r"^\s*(?:export\s+)?(?:const|let|var|def|class|val|static\s+final\s+\w+)?\s*"
                      r"(?:%s)\s*[=:(]" % "|".join(re.escape(n) for n in wanted))
    blocks: list[str] = []
    for p in _iter_source(root):
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            if defn.match(line):
                rel = p.relative_to(root).as_posix()
                blocks.append(f"# {rel}:{i + 1}\n" + "\n".join(lines[i:i + pad + 1]))
                if len(blocks) >= _MAX_CTX_SYMBOLS:
                    return "\n\n".join(blocks)
    return "\n\n".join(blocks)


def _event_prompt(site: dict, context: str) -> str:
    known = f"\nThe direction is already known: {site['method']}" if site.get("method") else ""
    return (f"Below is a messaging call (queue/topic publish or consume) whose channel name is "
            f"not a string literal. Trace it to the concrete queue/topic name, or to the "
            f"configuration key it is read from.\n"
            f"Site: {site['file']}:{site['line']}\nExpression: {site['expr']}{known}\n\n"
            f"Code at the site:\n```\n{site['snippet']}\n```\n\n"
            f"Definitions the expression references:\n```\n{context or '(none found)'}\n```\n\n"
            "Reply with exactly one of:\n"
            '{"resolved": true, "direction": "PUBLISH|CONSUME", "channel": "<literal name>"}\n'
            '{"resolved": true, "direction": "PUBLISH|CONSUME", "config_key": "<DOTTED.KEY.PATH>"}\n'
            '{"resolved": false, "reason": "<where the value comes from>"}')


def _prompt(site: dict, context: str) -> str:
    if site["kind"] == "event":
        return _event_prompt(site, context)
    what = ("a route registration" if site["kind"] == "interface" else "an outbound HTTP call")
    known = f"\nThe HTTP method is already known: {site['method']}" if site.get("method") else ""
    return (f"Below is {what} whose path is given by an expression rather than a string. "
            f"Trace the expression through the definitions to the concrete path.\n"
            f"Site: {site['file']}:{site['line']}\nExpression: {site['expr']}{known}\n\n"
            f"Code at the site:\n```\n{site['snippet']}\n```\n\n"
            f"Definitions the expression references:\n```\n{context or '(none found)'}\n```\n\n"
            "Rules:\n"
            "- A class attribute or constant reached through an accessor (e.g. `Model.uri()` "
            "returning `cls._uri`) counts as concrete: report its string value.\n"
            "- An enum member like `HTTPMethod.POST` means the verb POST.\n"
            "- If the path is built from configuration, environment variables, or code not "
            "shown, it is NOT resolvable; say where it comes from.\n\n"
            "Reply with exactly one of:\n"
            '{"resolved": true, "method": "<VERB>", "path": "</concrete/path>", "host": "<host or null>"}\n'
            '{"resolved": false, "reason": "<where the value comes from>"}')


def _parse(text: str) -> Optional[dict]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _grounded(root: Path, path: str, context: str) -> bool:
    """A proposed path counts only if it exists as a string literal in the repo."""
    needles = (f'"{path}"', f"'{path}'")
    if any(n in context for n in needles):
        return True
    for p in root.rglob("*"):
        if p.suffix not in _SRC_EXT or any(part in _SKIP for part in p.parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(n in text for n in needles):
            return True
    return False


def _handler_for(symbols: list[KnowledgeObject], file: str, line: int) -> Optional[KnowledgeObject]:
    """Tightest indexed function/method whose span contains the site line — the same anchor
    the static loader uses for `handled_by`, over the symbols already in the store."""
    best = None
    for o in symbols:
        p = o.payload
        if p.get("path") != file or p.get("kind") not in ("function", "method"):
            continue
        lo, hi = int(p.get("line_start") or 0), int(p.get("line_end") or 0)
        if lo <= line <= hi and (best is None or hi - lo < best[0]):
            best = (hi - lo, o)
    return best[1] if best else None


def _ground_channel(root: Path, ans: dict, context: str) -> Optional[str]:
    """Event answers ground two ways: a literal channel present in the repo, or a config key
    that resolves to a value in the repo's config files (the value is then the channel)."""
    from ..adapters.messaging import config_channels
    channel = str(ans.get("channel") or "").strip()
    if channel and _grounded(root, channel, context):
        return channel
    key = str(ans.get("config_key") or "").strip().strip(".")
    if key:
        for ch in config_channels(root):
            if ch["key"] == key or ch["key"].endswith("." + key):
                return ch["value"]
    return None


def _event_object(slug: str, direction: str, channel: str, site: dict, chash: str) -> KnowledgeObject:
    iface = Interface(method=direction, path=channel, type="event")
    return KnowledgeObject(
        object_type="interface", object_id=f"{slug}:{iface.id}", name=iface.name,
        summary=None, version="unknown", source="llm", generation_strategy="gap-fill",
        last_generated_at=None, embed_text=None,
        payload={"type": "event", "method": direction, "path": channel, "handler": None,
                 "framework": "messaging", "evidence": [f"{site['file']}:{site['line']}"],
                 "confidence": "llm-grounded", "gap": chash})


def _iface_object(slug: str, method: str, path: str, site: dict, chash: str,
                  handler: Optional[KnowledgeObject]) -> KnowledgeObject:
    iface = Interface(method=method, path=path)
    return KnowledgeObject(
        object_type="interface", object_id=f"{slug}:{iface.id}", name=iface.name,
        summary=None, version="unknown", source="llm", generation_strategy="gap-fill",
        last_generated_at=None, embed_text=None,
        payload={"type": "rest", "method": method, "path": path,
                 "handler": handler.name if handler else None,
                 "framework": None, "evidence": [f"{site['file']}:{site['line']}"],
                 "confidence": "llm-grounded", "gap": chash})


def fill_gaps(store, llm: LLMProvider, *, only_slug: Optional[str] = None) -> dict:
    """Resolve skipped sites for every service (or one). `store` implements
    KnowledgeStore + ContextStore. Returns counts."""
    knowledge: KnowledgeStore = store
    ctx: ContextStore = store
    rels: RelationshipStore = store
    stats = {"sites": 0, "resolved": 0, "unresolved": 0, "rejected": 0, "skipped": 0,
             "interfaces": 0, "outbound": 0, "handled_by": 0}

    for svc in knowledge.list_objects("service"):
        slug = svc.object_id
        if only_slug and slug != only_slug:
            continue
        sites = svc.payload.get("skipped") or []
        if not sites or not svc.payload.get("root"):
            continue
        root = Path(svc.payload["root"])
        done: dict = dict(svc.payload.get("gaps") or {})
        static_keys = {(o.payload.get("method"), o.payload.get("path"))
                       for o in knowledge.list_objects("interface") if o.id.startswith(f"interface:{slug}:")}
        outbound = list(svc.payload.get("outbound") or [])
        symbols: Optional[list[KnowledgeObject]] = None  # loaded lazily, once per service
        touched = False

        for site in sites:
            stats["sites"] += 1
            chash = _hash(site)
            if chash in done:
                stats["skipped"] += 1
                continue
            context = _context(knowledge, slug, root, site.get("names") or [])
            raw = llm.complete(_prompt(site, context), system=_SYS, max_tokens=_MAX_TOKENS)
            ans = _parse(raw) or {}
            record = {"kind": site["kind"], "site": f"{site['file']}:{site['line']}", "expr": site.get("expr")}
            touched = True

            if not ans.get("resolved"):
                record.update(status="unresolved", reason=str(ans.get("reason") or raw)[:200])
                stats["unresolved"] += 1
                done[chash] = record
                continue

            if site["kind"] == "event":
                direction = str(site.get("method") or ans.get("direction") or "").upper()
                channel = _ground_channel(root, ans, context)
                if direction not in ("PUBLISH", "CONSUME") or not channel:
                    record.update(status="rejected", proposed=f"{direction} {ans.get('channel') or ans.get('config_key')}",
                                  reason="channel not found as a literal or config value in repo")
                    stats["rejected"] += 1
                else:
                    record.update(status="resolved", method=direction, path=channel)
                    stats["resolved"] += 1
                    if (direction, channel) not in static_keys:
                        ctx.upsert_object(_event_object(slug, direction, channel, site, chash))
                        static_keys.add((direction, channel))
                        stats["interfaces"] += 1
                        svc.payload["interface_count"] = int(svc.payload.get("interface_count") or 0) + 1
                done[chash] = record
                continue

            path = str(ans.get("path") or "").strip()
            method = str(site.get("method") or ans.get("method") or "").upper()
            if not path.startswith("/") or method not in _VERBS or not _grounded(root, path, context):
                record.update(status="rejected", proposed=f"{method} {path}",
                              reason="path not found as a literal in repo" if path.startswith("/") else "malformed")
                stats["rejected"] += 1
                done[chash] = record
                continue

            record.update(status="resolved", method=method, path=path)
            stats["resolved"] += 1
            if site["kind"] == "interface":
                if (method, path) not in static_keys:
                    if symbols is None:
                        symbols = [o for o in knowledge.list_objects("code_symbol")
                                   if o.id.startswith(f"code_symbol:{slug}:")]
                    handler = _handler_for(symbols, site["file"], int(site["line"]))
                    iface = _iface_object(slug, method, path, site, chash, handler)
                    ctx.upsert_object(iface)
                    if handler:
                        rels.replace_edges("llm", [], [Relationship(iface.id, "handled_by", handler.id, "llm", {})])
                        record["handler"] = handler.id
                        stats["handled_by"] += 1
                    static_keys.add((method, path))
                    stats["interfaces"] += 1
                    svc.payload["interface_count"] = int(svc.payload.get("interface_count") or 0) + 1
            else:
                if not any(o.get("verb") == method and o.get("path") == path for o in outbound):
                    outbound.append({"verb": method, "path": path, "host": ans.get("host") or None,
                                     "evidence": f"{site['file']}:{site['line']}", "source": "llm"})
                    stats["outbound"] += 1
            done[chash] = record

        if touched:
            svc.payload["gaps"] = done
            svc.payload["outbound"] = outbound
            ctx.upsert_object(svc)
    ctx.commit()
    return stats
