"""Shared tree-sitter helpers for the Ruby adapters.

Ruby's route and client APIs are ordinary method calls, so everything here works on
`call` nodes: the method name, its positional string/symbol arguments, its keyword
arguments, and the block it may carry.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator, Optional

from ._walk import COMMON_SKIP, iter_files

# Python virtualenv names are here because this adapter runs against every repo, not
# only Ruby ones, and a checked-in venv is thousands of files it would otherwise walk.
_SKIP = {".git", "vendor", "node_modules", "tmp", "log", "coverage", "public",
         ".bundle", "spec", "test", "__pycache__", ".context", ".claude",
         ".venv", "venv", "env"}


def iter_rb_files(repo: Path) -> Iterator[Path]:
    yield from iter_files(repo, (".rb",), skip=COMMON_SKIP | _SKIP)


def parser():
    from tree_sitter_language_pack import get_parser
    return get_parser("ruby")


def parse(path: Path):
    try:
        return parser().parse(path.read_bytes()), path.read_bytes()
    except Exception:  # noqa: BLE001 - a bad parse must never abort the sweep
        return None, b""


def text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def call_name(node, src: bytes) -> str:
    """`foo.bar(1)` -> "bar"; `bar(1)` -> "bar"."""
    m = node.child_by_field_name("method")
    return text(m, src) if m is not None else ""


def receiver(node, src: bytes) -> str:
    r = node.child_by_field_name("receiver")
    return text(r, src) if r is not None else ""


def _arg_nodes(node):
    args = node.child_by_field_name("arguments")
    return [c for c in args.children if c.type not in ("(", ")", ",")] if args else []


def literal(node, src: bytes) -> Optional[str]:
    """A string or symbol literal's value; None for anything computed."""
    if node is None:
        return None
    if node.type == "string":
        return "".join(text(c, src) for c in node.children if c.type == "string_content")
    if node.type in ("simple_symbol", "symbol"):
        return text(node, src).lstrip(":").strip("'\"")
    if node.type == "hash_key_symbol":
        return text(node, src)
    return None


def positional(node, src: bytes) -> list:
    """Positional arguments only, keyword pairs excluded."""
    return [a for a in _arg_nodes(node) if a.type not in ("pair", "hash")]


def first_literal(node, src: bytes) -> Optional[str]:
    for a in positional(node, src):
        v = literal(a, src)
        if v is not None:
            return v
    return None


def kwargs(node, src: bytes) -> dict:
    """Keyword arguments as {name: raw_text}, flattening a trailing hash."""
    out: dict = {}
    pairs = []
    for a in _arg_nodes(node):
        if a.type == "pair":
            pairs.append(a)
        elif a.type == "hash":
            pairs.extend(c for c in a.children if c.type == "pair")
    for p in pairs:
        k, v = p.child_by_field_name("key"), p.child_by_field_name("value")
        if k is None or v is None:
            continue
        out[(literal(k, src) or text(k, src)).rstrip(":")] = text(v, src)
    return out


def symbol_list(raw: str) -> list:
    """`[:index, :show]` -> ["index", "show"]; `:index` -> ["index"]."""
    return [t.strip().lstrip(":").strip("'\"[] ")
            for t in (raw or "").strip("[]").split(",") if t.strip()]


def block_of(node):
    for c in node.children:
        if c.type in ("block", "do_block"):
            return c
    return None


def walk_calls(node) -> Iterator:
    """Every call node in this subtree, outermost first."""
    if node.type in ("call", "method_call", "command", "command_call"):
        yield node
    for c in node.children:
        yield from walk_calls(c)
