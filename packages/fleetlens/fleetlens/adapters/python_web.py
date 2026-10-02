"""Adapter for Python web frameworks: FastAPI, Flask, Sanic, Starlette.

Two registration styles, because real services use both:

  * decorators    `@router.get("/path")`, `@bp.route("/path", methods=[...])`
  * imperative    `bp.add_route(handler, "/path")`, `app.add_url_rule("/path", ...)`,
                  `router.add_api_route("/path", handler, methods=[...])`

Prefixes declared on the router or blueprint in the same module are composed in, including
Sanic's `version=4`, which contributes a `/v4` segment rather than a literal prefix.

Coverage of framework idioms can never be complete, so the adapter does not pretend
otherwise. Anything in a framework file that carries a URL-shaped literal and that none of
the above claimed is recorded as an `unrecognised-route-registration` skipped site. That
keeps the failure mode visible: a route fleetlens cannot parse is counted and can be
resolved by the LLM tier, rather than quietly missing. `fl doctor <repo>` reports the split.

Known limits: only module-local prefixes, so cross-module `include_router(prefix=...)` and
`register_blueprint(url_prefix=)` composition is not followed. Non-literal paths are
recorded, not guessed.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from ._pysrc import read_and_parse
from ._walk import iter_files
from .base import Interface, InterfaceAdapter, SkippedSite, names_in, snippet_of

_VERBS = {"get", "post", "put", "patch", "delete", "options", "head"}
_ROUTE_ATTRS = {"route", "api_route"}
#: Imperative registration, one name per framework we support: Sanic's `add_route`,
#: Flask's `add_url_rule`, FastAPI/Starlette's `add_api_route` and `add_route`. These take
#: the handler first and the path second, which is why the decorator walk never saw them.
_ADD_ROUTE_ATTRS = {"add_route", "add_url_rule", "add_api_route", "add_websocket_route"}
_FRAMEWORKS = {"fastapi": "fastapi", "flask": "flask", "sanic": "sanic",
               "starlette": "starlette"}
_SKIP = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", "dist", "build",
         "vendor", ".context", "tests", "test"}


def _iter_py(repo: Path):
    yield from iter_files(repo, (".py",))


def _str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _prefixes(tree: ast.Module) -> dict:
    """var -> url prefix, from the router or blueprint declared in this module.

    Covers `APIRouter(prefix="/p")`, `Blueprint(name, url_prefix="/p")` and Sanic's
    `Blueprint(name, version=4)`, which contributes a `/v4` segment rather than a literal
    prefix. A prefix written without a leading slash still denotes one.
    """
    out = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call)):
            continue
        prefix, version = "", ""
        for kw in node.value.keywords:
            if not isinstance(kw.value, ast.Constant):
                continue
            if kw.arg in ("prefix", "url_prefix"):
                prefix = str(kw.value.value).strip("/")
            elif kw.arg == "version" and kw.value.value is not None:
                v = str(kw.value.value).strip("/")
                version = v if v.startswith("v") else f"v{v}"
        parts = [p for p in (version, prefix) if p]
        if parts:
            out[node.targets[0].id] = "/" + "/".join(parts)
    return out


#: A string that denotes a URL path rather than a filesystem path or a separator. Requires
#: a named segment, so "/" and "/d/" (arguments to str.split) do not qualify.
_PATH_LIKE = re.compile(r"^/[\w\-.{}<>:*]+[\w\-./{}<>:*]*$")
#: Methods that take a path-shaped string without registering anything. String handling
#: dominates: every false positive in the first run of this detector was a separator passed
#: to split, strip or join.
_NOT_REGISTRATION_ATTRS = {
    "split", "rsplit", "strip", "lstrip", "rstrip", "join", "replace", "partition",
    "rpartition", "startswith", "endswith", "removeprefix", "removesuffix", "count",
    "find", "index", "format", "encode", "decode", "lower", "upper",
}
#: Receivers that take a path-shaped string but are not registering a route. Outbound HTTP
#: clients are the main one; they are discovered separately as outbound calls.
_NOT_REGISTRATION = ("request", "session", "client", "http", "requests", "urllib",
                     "os", "path", "shutil", "open", "logger", "log")


def looks_like_a_path(value: str) -> bool:
    """Whether a string literal denotes a URL path.

    Deliberately narrow. The point is to notice a route we failed to parse, so a false
    positive costs an entry in the unresolved count, while a false negative is the silent
    miss this exists to prevent.
    """
    return bool(value) and len(value) < 200 and bool(_PATH_LIKE.match(value))


def unclaimed_route_candidates(tree: ast.Module, claimed: set, rel: str, src: str,
                               lines: list) -> list:
    """Calls carrying a URL-shaped literal that no adapter recognised.

    The framework-specific matching above will always be incomplete: frameworks add
    registration styles, and new frameworks appear. What must not happen is a route going
    missing without anyone knowing, which is what made four endpoints in a real service
    invisible: not found, and not counted as unresolved either.

    So in a file that imports a web framework, any call holding a path-shaped string that
    the adapters did not claim is recorded as a skipped site. That encodes no framework's
    API, which is the point: an idiom released tomorrow shows up as unresolved rather than
    as absent, and the grounded LLM tier can resolve it from the literal already present.
    """
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or node.lineno in claimed:
            continue
        recv = ""
        if isinstance(node.func, ast.Attribute):
            if node.func.attr in _NOT_REGISTRATION_ATTRS:
                continue
            recv = (node.func.value.id if isinstance(node.func.value, ast.Name) else "").lower()
        elif isinstance(node.func, ast.Name):
            recv = node.func.id.lower()
        if any(x in recv for x in _NOT_REGISTRATION):
            continue
        path = next((_str(a) for a in node.args if _str(a) is not None), None)
        if path is None or not looks_like_a_path(path):
            continue
        out.append(SkippedSite(
            kind="interface", reason="unrecognised-route-registration", file=rel,
            line=node.lineno, expr=(ast.get_source_segment(src, node) or "")[:200],
            snippet=snippet_of(lines, node.lineno, getattr(node, "end_lineno", node.lineno)),
            names=names_in(node), method=None))
    return out


def _handler_name(node) -> str:
    """The handler a route was registered with, however it was referred to."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _handler_name(node.func)
    return ""


def _from_add_route(call: ast.Call, prefixes: dict, framework: str, rel: str,
                    src: str, lines: list, skipped) -> list:
    """Routes from an imperative registration call.

    Argument order differs between frameworks: Sanic and Starlette take the handler first
    and the path second, Flask's `add_url_rule` takes the rule first. Rather than encode
    each signature, take the first string argument as the path and the other positional as
    the handler, which is true of all of them and of anything shaped like them.
    """
    path_arg = next((a for a in call.args if _str(a) is not None), None)
    if path_arg is None:
        # A registration whose path is computed. Recorded rather than dropped: the LLM tier
        # resolves exactly these, and an unrecorded route is one nobody knows is missing.
        if skipped is not None and call.args:
            skipped.append(SkippedSite(
                kind="interface", reason="non-literal-path", file=rel, line=call.lineno,
                expr=ast.get_source_segment(src, call) or "",
                snippet=snippet_of(lines, call.lineno, getattr(call, "end_lineno", call.lineno)),
                names=names_in(call), method=None))
        return []

    path = _str(path_arg)
    handler = next((_handler_name(a) for a in call.args if a is not path_arg), "")
    for kw in call.keywords:                      # Flask: add_url_rule(rule, endpoint, view_func=)
        if kw.arg in ("view_func", "endpoint", "handler") and not handler:
            handler = _handler_name(kw.value)
    receiver = call.func.value.id if isinstance(call.func.value, ast.Name) else ""
    prefix = prefixes.get(receiver, "")
    full = (prefix + path) if prefix else path
    methods = _methods_kwarg(call) or ["GET"]
    return [Interface(method=m, path=full, type="rest", handler=handler or None,
                      summary=None, framework=framework,
                      evidence=[f"{rel}:{call.lineno}"]) for m in methods]


def _methods_kwarg(call: ast.Call):
    for kw in call.keywords:
        if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
            return [str(e.value).upper() for e in kw.value.elts if isinstance(e, ast.Constant)]
    return None


def _framework_of(tree: ast.Module) -> str | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root in _FRAMEWORKS:
                    return _FRAMEWORKS[root]
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root in _FRAMEWORKS:
                return _FRAMEWORKS[root]
    return None


def _routes_in(tree: ast.Module, rel: str, framework: str | None, src: str = "",
               skipped: list[SkippedSite] | None = None) -> list[Interface]:
    prefixes = _prefixes(tree)
    lines = src.splitlines()
    out = []
    claimed: set = set()
    for node in ast.walk(tree):
        # Imperative registration: `router.add_route(handler, "/path", methods=[...])`.
        # The decorator walk below cannot see these, because there is no decorator and the
        # handler is passed as an argument rather than defined underneath.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in _ADD_ROUTE_ATTRS:
            claimed.add(node.lineno)
            out.extend(_from_add_route(node, prefixes, framework, rel, src, lines, skipped))
            continue
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.args):
                continue
            attr = dec.func.attr
            if attr not in _VERBS and attr not in _ROUTE_ATTRS:
                continue
            claimed.add(dec.lineno)
            path = _str(dec.args[0])
            if path is None:
                if skipped is not None:
                    literal = [attr.upper()] if attr in _VERBS else _methods_kwarg(dec)
                    skipped.append(SkippedSite(
                        kind="interface", reason="non-literal-path", file=rel, line=dec.lineno,
                        expr=ast.get_source_segment(src, dec.args[0]) or "",
                        snippet=snippet_of(lines, dec.lineno, node.lineno),
                        names=names_in(dec),
                        method=literal[0] if literal and len(literal) == 1 else None))
                continue
            prefix = prefixes.get(dec.func.value.id, "") if isinstance(dec.func.value, ast.Name) else ""
            full = (prefix + path) if prefix else path
            if attr in _VERBS:
                methods = [attr.upper()]
            else:
                methods = _methods_kwarg(dec) or ["GET"]
            for m in methods:
                out.append(Interface(
                    method=m, path=full, type="rest", handler=node.name,
                    summary=(ast.get_docstring(node) or "").split("\n")[0].strip() or None,
                    framework=framework, evidence=[f"{rel}:{node.lineno}"],
                ))
    if skipped is not None:
        skipped.extend(unclaimed_route_candidates(tree, claimed, rel, src, lines))
    return out


class PythonWebAdapter(InterfaceAdapter):
    name = "python-web"

    def applies(self, repo: Path) -> bool:
        for p in _iter_py(repo):
            _, tree = read_and_parse(p)
            if tree is not None and _framework_of(tree):
                return True
        return False

    def discover(self, repo: Path, skipped: list[SkippedSite] | None = None) -> list[Interface]:
        repo = Path(repo)
        out: list[Interface] = []
        for p in _iter_py(repo):
            src, tree = read_and_parse(p)
            if tree is None:
                continue
            fw = _framework_of(tree)
            if fw is None:
                continue
            out += _routes_in(tree, p.relative_to(repo).as_posix(), fw, src, skipped)
        return out
