"""Shared tree-sitter helpers for the TypeScript adapters.

Route/HTTP calls in TS are `call_expression` nodes whose `function` is a `member_expression`
(`obj.verb`) or an `identifier` (`fetch`). We only need the callee verb, the first string
argument (a path/URL), and the second argument (an inline vs named handler).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from ._walk import COMMON_SKIP, iter_files

# `venv` and `env` as well as `.venv`: a Python service with a TypeScript frontend is
# ordinary, and without the bare names this walks thousands of site-packages files.
_SKIP = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", "dist", "build",
         ".context", "tests", "test", "__tests__"}
_HTTP_VERBS = {"get", "post", "put", "patch", "delete", "options", "head"}
#: Plain JavaScript. A UI server or BFF is as often `.js` as `.ts`, and reading only the
#: latter indexed every one of them as a service with no interfaces and nothing skipped.
JS_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs")


@dataclass
class Call:
    obj: Optional[str]      # receiver identifier (e.g. "app", "axios"), or None for a bare call
    verb: str               # "get" | "post" | ... | function name for bare calls ("fetch")
    arg0: Optional[str]     # first string/template argument as a template ({} for interp)
    arg1_name: Optional[str]  # second arg's identifier name, if it is a bare identifier
    line: int
    arg0_list: Optional[list[str]] = None  # first argument, when an array of string literals
    # for recording unresolved sites when arg0 is not a string:
    arg0_expr: Optional[str] = None       # source text of the first argument
    arg0_names: list[str] = field(default_factory=list)  # identifiers it references
    arg1_is_handler: bool = False         # second arg is a function or identifier
    has_object_arg: bool = False          # any argument is an object literal (options)
    awaited: bool = False
    end_line: int = 0


def iter_ts_files(repo: Path) -> Iterator[Path]:
    for p in iter_files(repo, (".ts", ".tsx"), skip=COMMON_SKIP | _SKIP):
        if p.name.endswith(".d.ts"):
            continue                      # declarations carry no routes or call sites
        yield p


def iter_js_files(repo: Path) -> Iterator[Path]:
    for p in iter_files(repo, JS_SUFFIXES, skip=COMMON_SKIP | _SKIP):
        if ".min." in p.name:
            continue                      # a minified bundle is build output, not source
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
    if path.suffix in JS_SUFFIXES:
        return get_parser("javascript")   # the JavaScript grammar includes JSX
    return get_parser("tsx" if path.suffix == ".tsx" else "typescript")


def parse_file(path: Path):
    """The tree-sitter root node for `path`, or None if it cannot be read or parsed."""
    try:
        return _parser(path).parse(path.read_bytes()).root_node
    except Exception:  # noqa: BLE001 - a bad parse must never abort the sweep
        return None


def _string_list(node) -> Optional[list[str]]:
    """`["/a", "/b"]` -> ["/a", "/b"]; None unless every element is a string literal."""
    if node is None or node.type != "array":
        return None
    elts = [c for c in node.children if c.is_named]
    vals = [_string_value(c) if c.type == "string" else None for c in elts]
    return vals if vals and all(v is not None for v in vals) else None


#: What an Express app or router is called with: `app.use(mw)`, `app.listen(port)`,
#: `router.route("/x")`, `router.param("id", fn)`. A request client is not, which is what
#: separates `router.get("/x", handler)` from `api.get("/x", config)` in the same codebase.
_EXPRESS_ONLY_METHODS = {"use", "listen", "route", "param"}
#: Type names an Express app or router parameter is annotated with in TypeScript.
_EXPRESS_TYPES = {"Express", "Application", "Router"}


def _express_bindings(root) -> set[str]:
    """Local names bound to the express module or its `Router` export."""
    names = {"express", "Router"}
    stack = [root]
    while stack:
        n = stack.pop()
        stack.extend(n.children)
        if n.type == "import_statement":
            src = n.child_by_field_name("source")
            if _string_value(src) != "express":
                continue
            for c in n.children:
                if c.type == "import_clause":
                    for ident in _names(c):
                        names.add(ident)
        elif n.type == "variable_declarator":
            value = n.child_by_field_name("value")
            if value is None or value.type != "call_expression":
                continue
            fn = value.child_by_field_name("function")
            args = value.child_by_field_name("arguments")
            if fn is None or fn.text != b"require" or args is None:
                continue
            first = next((c for c in args.children if c.is_named), None)
            if _string_value(first) == "express":
                names.update(_names(n.child_by_field_name("name")))
    return names


def _is_express_constructor(call, bindings: set[str]) -> bool:
    """`express()`, `Router()`, `express.Router()`, or the same under a local alias."""
    fn = call.child_by_field_name("function")
    if fn is None:
        return False
    if fn.type == "identifier":
        return fn.text.decode() in bindings
    if fn.type == "member_expression":
        prop = fn.child_by_field_name("property")
        return prop is not None and prop.text == b"Router"
    return False


def express_receivers(root) -> set[str]:
    """Identifiers in this file that denote an Express app or router.

    A verb call with a path literal is a route only on one of these. Every receiver used
    to count, which was harmless in a TypeScript server and wrong in a UI codebase, where
    `api.get("/orders", config)` is a request to another service, not an endpoint.

    A name qualifies when it is bound to `express()` or a `Router()`, when it is called
    with a method only an app or router has, or when it is a parameter typed as one. The
    second covers the common `export function addRoutes(app) { app.use(...); app.get(...) }`.
    """
    if root is None:
        return set()
    bindings = _express_bindings(root)
    out: set[str] = set()
    stack = [root]
    while stack:
        n = stack.pop()
        stack.extend(n.children)
        if n.type in ("variable_declarator", "assignment_expression"):
            target = n.child_by_field_name("name") or n.child_by_field_name("left")
            value = n.child_by_field_name("value") or n.child_by_field_name("right")
            if target is not None and target.type == "identifier" and value is not None \
                    and value.type == "call_expression" and _is_express_constructor(value, bindings):
                out.add(target.text.decode())
        elif n.type == "call_expression":
            fn = n.child_by_field_name("function")
            if fn is not None and fn.type == "member_expression":
                obj, prop = fn.child_by_field_name("object"), fn.child_by_field_name("property")
                if obj is not None and obj.type == "identifier" and prop is not None \
                        and prop.text.decode() in _EXPRESS_ONLY_METHODS:
                    out.add(obj.text.decode())
        elif n.type in ("required_parameter", "optional_parameter"):
            pattern, typ = n.child_by_field_name("pattern"), n.child_by_field_name("type")
            if pattern is not None and pattern.type == "identifier" and typ is not None \
                    and _EXPRESS_TYPES & set(_type_names(typ)):
                out.add(pattern.text.decode())
    return out


def _type_names(node) -> list[str]:
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        if n.type == "type_identifier":
            out.append(n.text.decode())
        stack.extend(n.children)
    return out


def iter_calls(path: Path, root=None) -> Iterator[Call]:
    """Yield each call_expression of interest (verb member-calls and bare `fetch`).

    Pass `root` from `parse_file` to reuse a tree already parsed for this file."""
    if root is None:
        root = parse_file(path)
        if root is None:
            return
    stack = [root]
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
                   arg0_list=_string_list(arg_nodes[0]) if arg_nodes else None,
                   arg0_expr=arg0_expr, arg0_names=arg0_names or [],
                   arg1_is_handler=arg1_is_handler,
                   has_object_arg=any(a.type == "object" for a in arg_nodes),
                   awaited=node.parent is not None and node.parent.type == "await_expression",
                   end_line=node.end_point[0] + 1)
