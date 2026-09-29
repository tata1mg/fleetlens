"""Shared tree-sitter helpers for the TypeScript adapters.

Route/HTTP calls in TS are `call_expression` nodes whose `function` is a `member_expression`
(`obj.verb`) or an `identifier` (`fetch`). We only need the callee verb, the first string
argument (a path/URL), and the second argument (an inline vs named handler).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

_SKIP = {".git", ".venv", "node_modules", "__pycache__", "dist", "build", ".context",
         "tests", "test", "__tests__"}
_HTTP_VERBS = {"get", "post", "put", "patch", "delete", "options", "head"}


@dataclass
class Call:
    obj: Optional[str]      # receiver identifier (e.g. "app", "axios"), or None for a bare call
    verb: str               # "get" | "post" | ... | function name for bare calls ("fetch")
    arg0: Optional[str]     # first string/template argument as a template ({} for interp)
    arg1_name: Optional[str]  # second arg's identifier name, if it is a bare identifier
    line: int
    # for recording unresolved sites when arg0 is not a string:
    arg0_expr: Optional[str] = None       # source text of the first argument
    arg0_names: list[str] = field(default_factory=list)  # identifiers it references
    arg1_is_handler: bool = False         # second arg is a function or identifier
    has_object_arg: bool = False          # any argument is an object literal (options)
    awaited: bool = False
    end_line: int = 0


def iter_ts_files(repo: Path) -> Iterator[Path]:
    for ext in ("*.ts", "*.tsx"):
        for p in repo.rglob(ext):
            if any(part in _SKIP for part in p.relative_to(repo).parts):
                continue
            if p.name.endswith(".d.ts"):
                continue
            yield p


def _string_value(node) -> Optional[str]:
    """A `string` or `template_string` node -> its text, with ${...} rendered as {}."""
    if node is None:
        return None
    if node.type == "string":
        return "".join(c.text.decode("utf-8", "replace")
                       for c in node.children if c.type == "string_fragment")
    if node.type == "template_string":
        out = []
        for c in node.children:
            if c.type == "string_fragment":
                out.append(c.text.decode("utf-8", "replace"))
            elif c.type == "template_substitution":
                out.append("{}")
        return "".join(out)
    return None


_NAME_TYPES = {"identifier", "property_identifier", "shorthand_property_identifier"}


def _names(node) -> list[str]:
    out: list[str] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in _NAME_TYPES:
            t = n.text.decode("utf-8", "replace")
            if t not in out:
                out.append(t)
        stack.extend(reversed(n.children))
    return out


def _parser(path: Path):
    from tree_sitter_language_pack import get_parser
    return get_parser("tsx" if path.suffix == ".tsx" else "typescript")


def iter_calls(path: Path) -> Iterator[Call]:
    """Yield each call_expression of interest (verb member-calls and bare `fetch`)."""
    try:
        src = path.read_bytes()
        tree = _parser(path).parse(src)
    except Exception:  # noqa: BLE001 - a bad parse must never abort the sweep
        return
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.type != "call_expression":
            continue
        func = node.child_by_field_name("function")
        args = node.child_by_field_name("arguments")
        if func is None or args is None:
            continue
        obj = verb = None
        if func.type == "member_expression":
            o = func.child_by_field_name("object")
            prop = func.child_by_field_name("property")
            if prop is None:
                continue
            verb = prop.text.decode()
            obj = o.text.decode() if o is not None and o.type == "identifier" else None
        elif func.type == "identifier":
            verb = func.text.decode()
        else:
            continue
        arg_nodes = [c for c in args.children if c.type not in ("(", ")", ",")]
        arg0 = _string_value(arg_nodes[0]) if arg_nodes else None
        arg1_name = None
        arg1_is_handler = False
        if len(arg_nodes) > 1:
            arg1_is_handler = arg_nodes[1].type in ("identifier", "arrow_function", "function",
                                                    "function_expression")
            if arg_nodes[1].type == "identifier":
                arg1_name = arg_nodes[1].text.decode()
        arg0_expr = arg0_names = None
        if arg_nodes and arg0 is None:
            arg0_expr = arg_nodes[0].text.decode("utf-8", "replace")
            arg0_names = _names(arg_nodes[0])
        yield Call(obj=obj, verb=verb, arg0=arg0, arg1_name=arg1_name,
                   line=node.start_point[0] + 1,
                   arg0_expr=arg0_expr, arg0_names=arg0_names or [],
                   arg1_is_handler=arg1_is_handler,
                   has_object_arg=any(a.type == "object" for a in arg_nodes),
                   awaited=node.parent is not None and node.parent.type == "await_expression",
                   end_line=node.end_point[0] + 1)
