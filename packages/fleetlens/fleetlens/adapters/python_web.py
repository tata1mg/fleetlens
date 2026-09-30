"""Adapter for decorator-based Python web frameworks: FastAPI, Flask, Sanic, Starlette.

They all expose routes the same way — `@<app_or_router>.<verb>("/path")` or
`@<app_or_router>.route("/path", methods=[...])` — so one AST adapter covers them. Anchors
on the route decorator, resolves the path/method from its arguments, and composes a
router/blueprint `prefix` declared in the same module.

Known limits (v1): only module-local prefixes (an APIRouter/Blueprint variable declared in
the same file); cross-module `include_router(prefix=...)` / `register_blueprint(url_prefix=)`
composition is not followed. Non-literal paths are skipped. These are the honest gaps the
LLM tier or a richer adapter can fill later.
"""
from __future__ import annotations

import ast
from pathlib import Path

from ._walk import iter_files
from .base import Interface, InterfaceAdapter, SkippedSite, names_in, snippet_of

_VERBS = {"get", "post", "put", "patch", "delete", "options", "head"}
_ROUTE_ATTRS = {"route", "api_route"}
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
    """var -> url prefix, from `X = APIRouter(prefix="/p")` / `X = Blueprint(n, url_prefix="/p")`."""
    out = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call)):
            for kw in node.value.keywords:
                if kw.arg in ("prefix", "url_prefix") and isinstance(kw.value, ast.Constant):
                    out[node.targets[0].id] = str(kw.value.value).rstrip("/")
    return out


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
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.args):
                continue
            attr = dec.func.attr
            if attr not in _VERBS and attr not in _ROUTE_ATTRS:
                continue
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
    return out


class PythonWebAdapter(InterfaceAdapter):
    name = "python-web"

    def applies(self, repo: Path) -> bool:
        for p in _iter_py(repo):
            try:
                if _framework_of(ast.parse(p.read_bytes())):
                    return True
            except SyntaxError:
                continue
        return False

    def discover(self, repo: Path, skipped: list[SkippedSite] | None = None) -> list[Interface]:
        repo = Path(repo)
        out: list[Interface] = []
        for p in _iter_py(repo):
            try:
                src = p.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(src)
            except SyntaxError:
                continue
            fw = _framework_of(tree)
            if fw is None:
                continue
            out += _routes_in(tree, p.relative_to(repo).as_posix(), fw, src, skipped)
        return out
