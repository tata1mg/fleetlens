"""Adapter for Python web frameworks.

FastAPI, Flask, Sanic and Starlette are recognised by name, and anything that registers
routes the way they do is read as well. A module qualifies either by importing a known
framework or by showing the idiom: a function decorated with an HTTP verb carrying a
URL-shaped literal. An import allow-list on its own only ever recognises the frameworks
somebody has already added to it and returns nothing at all for the rest, which is how a
gateway with 1190 endpoints behind a house framework indexed as a service with no API.

Registration styles, because real services use all of them:

  * decorators    `@router.get("/path")`, `@bp.route("/path", methods=[...])`
  * bare verbs    `@get("/path")`, `@post(path="/path")`, where the framework exports its
                  verbs as module-level functions rather than methods on an app object
  * keyword path  `@app.get(path="/path")`, which FastAPI and Flask both accept
  * imperative    `bp.add_route(handler, "/path")`, `app.add_url_rule("/path", ...)`,
                  `router.add_api_route("/path", handler, methods=[...])`

A bare verb is a weaker signal than an attribute, since `get` and `post` are ordinary
function names: it counts once the module has shown the idiom somewhere unambiguous.

Prefixes declared on the router or blueprint in the same module are composed in, including
Sanic's `version=4`, which contributes a `/v4` segment rather than a literal prefix.

Coverage of framework idioms can never be complete, so the adapter does not pretend
otherwise. Anything in a framework file that carries a URL-shaped literal and that none of
the above claimed is recorded as an `unrecognised-route-registration` skipped site. That
keeps the failure mode visible: a route fleetlens cannot parse is counted and can be
resolved by the LLM tier, rather than quietly missing. `fl doctor <repo>` reports the split.

Prefixes are resolved across the repository, because a router is nearly always mounted in
a different file from the one declaring its routes; see `_mounts.py`.

Known limits: a mount built at runtime is not followed, and non-literal paths are recorded,
not guessed.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from ._mounts import MountGraph, _module_of, own_prefix
from ._pysrc import read_and_parse
from ._walk import iter_files
from .base import Interface, InterfaceAdapter, SkippedSite, names_in, snippet_of

_VERBS = {"get", "post", "put", "patch", "delete", "options", "head"}
_ROUTE_ATTRS = {"route", "api_route"}
#: Imperative registration, one name per framework we support: Sanic's `add_route`,
#: Flask's `add_url_rule`, FastAPI/Starlette's `add_api_route` and `add_route`. These take
#: the handler first and the path second, which is why the decorator walk never saw them.
_ADD_ROUTE_ATTRS = {"add_route", "add_url_rule", "add_api_route", "add_websocket_route"}
#: Keywords a framework uses for the route path when it is not passed positionally.
#: FastAPI and Flask accept both forms for the same call, so this is not an exotic spelling:
#: `@app.get(path="/x")` is ordinary FastAPI that a positional-only reader misses.
_PATH_KWARGS = {"path", "rule", "uri", "url", "route"}
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


def _path_nodes(call: ast.Call):
    """Argument nodes that could carry the route path, most likely first.

    Positional arguments come first because every framework accepts the path there, then
    the keywords frameworks name it with. Callers take the first one that is a string
    literal; the first one of any kind is what gets reported when none of them is.
    """
    yield from call.args
    for kw in call.keywords:
        if kw.arg in _PATH_KWARGS:
            yield kw.value


def _literal_path(call: ast.Call):
    """The node holding the route's path as a string literal, or None."""
    return next((n for n in _path_nodes(call) if _str(n) is not None), None)


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
        prefix = own_prefix(node.value)
        if prefix:
            out[node.targets[0].id] = prefix
    return out


#: A string that denotes a URL path. Requires a leading slash and a named first segment,
#: so a bare "/" does not qualify. A one-letter separator like "/d/" does, which is why
#: _NOT_REGISTRATION_ATTRS below carries the weight of rejecting arguments to str.split.
#:
#: The tail admits regex metacharacters because route parameters are frequently typed with
#: one: Sanic and Starlette write `{sku_id:\d+}`, Flask writes `<regex("[0-9]+"):x>`. A
#: class that stopped at word characters read those as not-a-path and dropped the route.
_PATH_LIKE = re.compile(r"^/[\w\-.{}<>:*]+[\w\-./{}<>:*\\+()\[\]|^$?!,'\"]*$")
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


def _prefixes_of(receiver: str, prefixes: dict, mounts, module: str) -> list:
    """Every URL prefix the router `receiver` serves under, so one per real path.

    The graph knows where a router is mounted across the whole repository; `prefixes`
    knows only what this one module declared. The graph wins whenever it has an answer,
    because a prefix declared here is already folded into what it computed. A router it
    has never seen, or one mounted in a way it cannot follow, keeps the module-local
    answer, which is what this did before the graph existed.
    """
    if not receiver:
        return [""]
    if mounts is not None:
        resolved = mounts.prefixes_for(module, receiver)
        if resolved:
            return resolved
    return [prefixes.get(receiver, "")]


#: A dotted Python name with no slash in it: `app.services.orders.fetch_order`. Not a URL,
#: however much it looks like one to a reader of string literals.
_DOTTED_NAME = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)+$")


def route_path(literal: str):
    """The URL path a route decorator's literal denotes, or None if it denotes something
    else.

    Frameworks accept a path written without its leading slash and supply one. Sanic says
    so in as many words -- "Fix case where the user did not prefix the URL with a /" -- and
    does it before composing any prefix; Flask joins on the slash; FastAPI requires it.
    Concatenating a prefix with the literal as written therefore reported
    `/v4prescriptions/status` for a route the service serves at `/v4/prescriptions/status`.

    The None case is `mock.patch`, which shares its name with the HTTP verb: a decorator
    `@patch("app.orders.fetch")` was being reported as a PATCH endpoint, and normalising
    the slash would have promoted it to `/app.orders.fetch`.
    """
    if _DOTTED_NAME.match(literal):
        return None
    return literal if literal.startswith("/") else "/" + literal


_VERSION_SEGMENT = re.compile(r"^/v[\w.]+(?=/|$)")


def _reversion(prefix: str, override: str) -> str:
    """`prefix` with its version segment replaced by the one a route declared.

    A route's own `version=` replaces its blueprint's rather than adding to it, so
    `/v4/merchant` under `@route(..., version=5)` is `/v5/merchant`, not `/v5/v4/merchant`.
    """
    if not override:
        return prefix
    return _VERSION_SEGMENT.sub(override, prefix, count=1) \
        if _VERSION_SEGMENT.match(prefix) else override + prefix


def join_path(prefix: str, path: str) -> str:
    """A router's prefix and a route's path as one path, with exactly one slash between."""
    return (prefix.rstrip("/") + path) if prefix else path


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
                    src: str, lines: list, skipped, mounts=None, module: str = "") -> list:
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

    path = route_path(_str(path_arg))
    if path is None:
        return []
    handler = next((_handler_name(a) for a in call.args if a is not path_arg), "")
    for kw in call.keywords:                      # Flask: add_url_rule(rule, endpoint, view_func=)
        if kw.arg in ("view_func", "endpoint", "handler") and not handler:
            handler = _handler_name(kw.value)
    receiver = call.func.value.id if isinstance(call.func.value, ast.Name) else ""
    methods = _methods_kwarg(call) or ["GET"]
    return [Interface(method=m, path=join_path(prefix, path), type="rest",
                      handler=handler or None, summary=None, framework=framework,
                      evidence=[f"{rel}:{call.lineno}"])
            for prefix in _prefixes_of(receiver, prefixes, mounts, module)
            for m in methods]


def _methods_kwarg(call: ast.Call):
    for kw in call.keywords:
        if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
            return [str(e.value).upper() for e in kw.value.elts if isinstance(e, ast.Constant)]
    return None


def _dec_name(dec: ast.Call) -> str:
    """The name a decorator was called by: `post` for `@post(...)` and for `@app.post(...)`.

    Both spellings are in use. A framework that exposes its verbs as module-level functions
    is decorated with a bare name, one that hangs them off an app or router object with an
    attribute, and the registration means the same thing either way.
    """
    if isinstance(dec.func, ast.Attribute):
        return dec.func.attr
    if isinstance(dec.func, ast.Name):
        return dec.func.id
    return ""


def _bare_verb_routes(tree: ast.Module) -> bool:
    """Whether this module decorates functions with bare verb names carrying URL literals.

    `@post("/x")` and `@post(path="/x")` are route registrations; `@post(data=payload)` on
    some unrelated helper is not, and the name alone cannot tell them apart. One decorator
    in the module that is unambiguous settles it for the rest, which is what lets a sibling
    route whose path is computed be recorded as unresolved instead of vanishing.
    """
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name)):
                continue
            if dec.func.id.lower() not in _VERBS and dec.func.id not in _ROUTE_ATTRS:
                continue
            n = _literal_path(dec)
            if n is not None and looks_like_a_path(_str(n)):
                return True
    return False


def _has_route_idiom(tree: ast.Module) -> bool:
    """Whether this module registers routes, judged by what it does rather than what it
    imports.

    An import allow-list only ever recognises the frameworks someone has already added to
    it, and silently returns nothing for the rest: no interfaces, and not even an unresolved
    count, because the file is never examined. House frameworks and smaller libraries are
    the common case for that, and the failure is invisible to the person indexing.

    A decorated function whose decorator is named after an HTTP verb and carries a
    URL-shaped literal is the idiom itself, which is what is worth matching.
    """
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            if _dec_name(dec).lower() not in _VERBS and _dec_name(dec) not in _ROUTE_ATTRS:
                continue
            n = _literal_path(dec)
            if n is not None and looks_like_a_path(_str(n)):
                return True
    return False


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
               skipped: list[SkippedSite] | None = None, mounts=None,
               module: str = "") -> list[Interface]:
    prefixes = _prefixes(tree)
    bare_ok = _bare_verb_routes(tree)
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
            out.extend(_from_add_route(node, prefixes, framework, rel, src, lines,
                                       skipped, mounts, module))
            continue
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            attr = _dec_name(dec)
            bare = isinstance(dec.func, ast.Name)
            if attr.lower() in _VERBS:
                attr = attr.lower()
            if attr not in _VERBS and attr not in _ROUTE_ATTRS:
                continue
            # A bare `@post(...)` is a weaker signal than `@router.post(...)`: the name
            # alone does not say a framework is involved. It counts once the module has
            # shown the idiom somewhere unambiguous, so a sibling route with a computed
            # path is still recorded rather than dropped.
            if bare and not bare_ok:
                continue
            node_ = _literal_path(dec)
            claimed.add(dec.lineno)
            if node_ is None:
                if skipped is not None:
                    literal = [attr.upper()] if attr in _VERBS else _methods_kwarg(dec)
                    report = next(iter(_path_nodes(dec)), None)
                    skipped.append(SkippedSite(
                        kind="interface", reason="non-literal-path", file=rel, line=dec.lineno,
                        expr=(report is not None
                              and ast.get_source_segment(src, report) or ""),
                        snippet=snippet_of(lines, dec.lineno, node.lineno),
                        names=names_in(dec),
                        method=literal[0] if literal and len(literal) == 1 else None))
                continue
            path = route_path(_str(node_))
            if path is None:
                continue              # a mock target, not a route
            # Sanic lets a route carry its own `version=`, which replaces the one its
            # blueprint declared. Reading only the blueprint's put a v5 endpoint at the v4
            # path, where it collided with the real v4 route and was lost.
            override = own_prefix(dec)
            receiver = ("" if bare
                        else dec.func.value.id if isinstance(dec.func.value, ast.Name)
                        else "")
            methods = ([attr.upper()] if attr in _VERBS
                       else _methods_kwarg(dec) or ["GET"])
            summary = (ast.get_docstring(node) or "").split("\n")[0].strip() or None
            for prefix in _prefixes_of(receiver, prefixes, mounts, module):
                full = join_path(_reversion(prefix, override), path)
                for m in methods:
                    out.append(Interface(
                        method=m, path=full, type="rest", handler=node.name,
                        summary=summary, framework=framework,
                        evidence=[f"{rel}:{node.lineno}"],
                    ))
    if skipped is not None:
        skipped.extend(unclaimed_route_candidates(tree, claimed, rel, src, lines))
    return out


class PythonWebAdapter(InterfaceAdapter):
    name = "python-web"

    def applies(self, repo: Path) -> bool:
        for p in _iter_py(repo):
            _, tree = read_and_parse(p)
            if tree is not None and (_framework_of(tree) or _has_route_idiom(tree)):
                return True
        return False

    def discover(self, repo: Path, skipped: list[SkippedSite] | None = None) -> list[Interface]:
        repo = Path(repo)
        # Built once for the repository: a route's real path depends on where its router
        # is mounted, which is nearly always a different file from the one declaring it.
        mounts = MountGraph(repo)
        out: list[Interface] = []
        for p in _iter_py(repo):
            errors: list = []
            src, tree = read_and_parse(p, errors)
            if tree is None:
                # A file nobody can parse is skipped, not fatal -- a repo may hold a Python
                # 2 module or a deliberately broken fixture. But skipping it in silence is
                # how a service serving 149 endpoints indexed 5: one route file used syntax
                # a current interpreter rejects, and the absence looked like an answer.
                if skipped is not None and errors:
                    skipped.append(SkippedSite(
                        kind="interface", reason="unparseable-file",
                        file=p.relative_to(repo).as_posix(), line=0,
                        expr=errors[0], snippet="", names=[], method=None))
                continue
            fw = _framework_of(tree)
            # A module with no recognised framework import is still read when it registers
            # routes the way frameworks do. Without this the file is never opened, so a
            # house framework yields no interfaces and no unresolved count either, which
            # reads as a service with no API rather than as a gap.
            if fw is None and not _has_route_idiom(tree):
                continue
            out += _routes_in(tree, p.relative_to(repo).as_posix(), fw, src, skipped,
                              mounts, _module_of(p, repo)[0])
        return out
