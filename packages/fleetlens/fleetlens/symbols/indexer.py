"""Language-agnostic symbol index — the deterministic substrate for incremental runs.

Parses a repo with Tree-sitter and emits, for every top-level/nested definition
(function / method / class), a stable record:

    <path>::<qualified_name>  ->  { path, lang, kind, line_start, line_end, body_hash }

`body_hash` is a content hash of the definition's source span, so re-running after a
code change tells us *exactly which symbols changed* (added / removed / body-changed) —
independent of line shifts, comments, or formatting elsewhere in the file. Contract and
interface invalidation is then a join: an artifact regenerates iff one of the symbols it
was built from (its recorded evidence) has a new hash.

Only symbol *definitions* are extracted — the robust, language-agnostic part of
Tree-sitter. No call graph is built here (that needs per-language name resolution and is
deliberately out of scope; see the incremental design notes).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from ..adapters._walk import iter_files

# ext -> Tree-sitter language name (as understood by tree_sitter_language_pack).
_LANG_BY_EXT = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".go": "go",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
    ".rs": "rust",
}

# Per-language node types that denote a definition, and whether they are class-like.
# Anything not listed is ignored. Kept intentionally small; extend per language as needed.
_DEF_NODES = {
    "python":     {"function_definition": "function", "class_definition": "class"},
    "javascript": {"function_declaration": "function", "method_definition": "method",
                   "class_declaration": "class"},
    "typescript": {"function_declaration": "function", "method_definition": "method",
                   "class_declaration": "class", "interface_declaration": "interface",
                   "abstract_class_declaration": "class"},
    "tsx":        {"function_declaration": "function", "method_definition": "method",
                   "class_declaration": "class", "interface_declaration": "interface",
                   "abstract_class_declaration": "class"},
    "go":         {"function_declaration": "function", "method_declaration": "method",
                   "type_declaration": "type"},
    "java":       {"method_declaration": "method", "constructor_declaration": "method",
                   "class_declaration": "class", "interface_declaration": "interface"},
    "ruby":       {"method": "method", "class": "class", "module": "module"},
    "php":        {"function_definition": "function", "method_declaration": "method",
                   "class_declaration": "class"},
    "rust":       {"function_item": "function", "impl_item": "impl", "struct_item": "type"},
}

# Directories never worth parsing.
_MAX_BYTES = 2_000_000  # skip pathologically large / generated files


@dataclass(frozen=True)
class Symbol:
    symbol_id: str
    path: str
    lang: str
    kind: str
    line_start: int
    line_end: int
    body_hash: str


def _body_hash(src: bytes) -> str:
    return hashlib.sha1(src).hexdigest()[:12]


def _name_of(node) -> Optional[str]:
    """Best-effort definition name: the `name` field, else an identifier child."""
    n = node.child_by_field_name("name")
    if n is not None and n.text:
        return n.text.decode("utf-8", "replace")
    for child in node.children:
        if child.type in ("identifier", "type_identifier", "constant", "property_identifier"):
            return child.text.decode("utf-8", "replace")
    return None


def _span_node(node):
    """The node whose source span defines the symbol's identity for hashing.

    A definition's decorators/annotations often carry the contract-relevant bits (an
    `@app.route("/x", methods=["POST"])` decorator IS the route). Tree-sitter puts the
    bare `function_definition` INSIDE a `decorated_definition`, so hashing the bare node
    would miss a decorator edit. When the parent is a decorated wrapper, hash that.
    """
    p = node.parent
    if p is not None and p.type in ("decorated_definition", "decorated_method"):
        return p
    return node


def _iter_defs(node, lang_defs: dict[str, str], stack: tuple[str, ...]):
    """Depth-first walk yielding (span_node, kind, qualified_name) for every definition.

    A JS/TS `const handler = () => {}` is captured via its variable_declarator when the
    value is a function/arrow — this catches FE route handlers written as arrow consts.
    """
    kind = lang_defs.get(node.type)
    name = None
    if kind is not None:
        name = _name_of(node)
    elif node.type == "variable_declarator":
        val = node.child_by_field_name("value")
        if val is not None and val.type in ("arrow_function", "function", "function_expression"):
            kind, name = "function", _name_of(node)

    next_stack = stack
    if kind is not None and name:
        qual = ".".join(stack + (name,))
        yield _span_node(node), kind, qual
        next_stack = stack + (name,)

    for child in node.children:
        yield from _iter_defs(child, lang_defs, next_stack)


def index_file(path: Path, rel: str) -> Iterable[Symbol]:
    lang = _LANG_BY_EXT.get(path.suffix.lower())
    if lang is None:
        return []
    lang_defs = _DEF_NODES.get(lang)
    if not lang_defs:
        return []
    try:
        src = path.read_bytes()
    except OSError:
        return []
    if len(src) > _MAX_BYTES:
        return []
    try:
        from tree_sitter_language_pack import get_parser
        tree = get_parser(lang).parse(src)
    except Exception:  # noqa: BLE001 — a bad parse must never abort the whole index
        return []

    out: list[Symbol] = []
    seen: set[str] = set()
    for node, kind, qual in _iter_defs(tree.root_node, lang_defs, ()):
        sid = f"{rel}::{qual}"
        if sid in seen:  # overloads / same-name siblings — disambiguate by line
            sid = f"{sid}@{node.start_point[0] + 1}"
        seen.add(sid)
        out.append(Symbol(
            symbol_id=sid,
            path=rel,
            lang=lang,
            kind=kind,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            body_hash=_body_hash(node.text),
        ))
    return out


def build_index(repo: Path) -> dict:
    """Parse every supported file under `repo`; return the serializable index dict."""
    repo = repo.resolve()
    symbols: dict[str, dict] = {}
    files = 0
    for path in _walk(repo):
        rel = path.relative_to(repo).as_posix()
        got = list(index_file(path, rel))
        if got:
            files += 1
        for s in got:
            symbols[s.symbol_id] = {
                "path": s.path, "lang": s.lang, "kind": s.kind,
                "line_start": s.line_start, "line_end": s.line_end,
                "body_hash": s.body_hash,
            }
    return {
        "schema": "symbols/v1",
        "root": repo.name,
        "file_count": files,
        "symbol_count": len(symbols),
        # keyed by symbol_id so diffing two indexes is a plain dict comparison
        "symbols": dict(sorted(symbols.items())),
    }


def _walk(repo: Path) -> Iterable[Path]:
    """Source files to extract symbols from, using the walk every adapter shares.

    This used to keep its own skip list and its own `rglob`, which meant a directory a repo
    excluded was honoured for interfaces and ignored for symbols: excluding `examples/` from
    a library dropped four endpoints and left five symbols behind. One walk, one set of
    rules, and the exclusion means the same thing everywhere.
    """
    yield from iter_files(repo, tuple(_LANG_BY_EXT))


def diff_index(old: dict, new: dict) -> dict:
    """Compare two index dicts → {added, removed, changed} lists of symbol_ids.

    `changed` = same id, different body_hash. Deterministic; no parsing.
    """
    o = old.get("symbols", {}) if old else {}
    n = new.get("symbols", {})
    o_ids, n_ids = set(o), set(n)
    changed = [i for i in (o_ids & n_ids) if o[i].get("body_hash") != n[i].get("body_hash")]
    return {
        "added": sorted(n_ids - o_ids),
        "removed": sorted(o_ids - n_ids),
        "changed": sorted(changed),
    }
