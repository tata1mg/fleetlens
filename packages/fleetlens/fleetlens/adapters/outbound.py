"""Extract a service's OUTBOUND HTTP calls — the consumer side of a dependency edge.

Deterministic, AST-based. Catches the common shapes without needing to know the client
library: `<x>.<verb>("/path" | "http://host/path" | f"...")`, and the `path = "..."` +
`<x>.<verb>(path)` two-step (client wrappers). We keep the request *path* (the join key for
the resolver) and, when the URL is literal, the host (a weak corroborating hint).

Honest limits: paths built by non-trivial code (helpers, string ops beyond f-strings/format)
are missed; a `.get()` on a non-HTTP object could be a false positive (filtered by requiring
a path-like argument). Everything the resolver produces is confidence-tagged.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from ._pysrc import read_and_parse
from ._walk import iter_files
from .base import SkippedSite, names_in, snippet_of

_VERBS = {"get", "post", "put", "patch", "delete", "options", "head"}
_HTTP_KWARGS = {"json", "headers", "params", "data", "timeout", "auth", "cookies"}
_CLIENT_HINTS = ("http", "client", "session", "request", "api")
# `request.args.get("q")` is a query-param read, not an outbound call, but the receiver
# contains "request" so the client heuristic used to accept it. These accessors read the
# INBOUND request and never perform I/O.
_PARAM_ACCESSORS = ("args", "form", "query", "query_args", "params", "json", "headers",
                    "cookies", "match_info", "files")
_SKIP = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", "dist", "build",
         "vendor", ".context", "tests", "test"}


@dataclass
class OutboundCall:
    verb: str
    path: str
    host: str | None
    evidence: str


def _iter_py(repo: Path):
    yield from iter_files(repo, (".py",))


def _template(node) -> str | None:
    """Literal string, "...".format(...), or an f-string -> a template ({} for interp)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format" and isinstance(node.func.value, ast.Constant)):
        return node.func.value.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            parts.append(str(v.value) if isinstance(v, ast.Constant) else "{}")
        return "".join(parts)
    return None


def _path_from_expr(node) -> str | None:
    """Recover the PATH from a URL built around a non-literal host.

    `urljoin(settings.HOST, "/prepare-notification")`, `base + "/orders"` and
    `f"{base}/orders"` all carry a literal path even though the host comes from config. The
    resolver joins on path, so the path alone is enough to resolve the dependency; the host
    stays unknown and the edge is simply not host-corroborated.
    """
    t = _template(node)
    if t is not None:
        # f"{base}/orders" renders as "{}/orders" — drop a leading interpolation
        while t.startswith("{}"):
            t = t[2:]
        return t or None
    if isinstance(node, ast.Call):
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name in ("urljoin", "url_join", "urlunparse") and len(node.args) >= 2:
            return _path_from_expr(node.args[1])
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _path_from_expr(node.right)
    return None


def _url_to_path(url: str) -> tuple[str, str | None]:
    """('/orders/{}', 'order-service') from a full URL, or (path, None) if already a path."""
    if url.startswith(("http://", "https://")):
        rest = url.split("://", 1)[1]
        host, _, tail = rest.partition("/")
        return ("/" + tail if tail else "/"), (host or None)
    return url, None


def _is_path_like(s: str) -> bool:
    return s.startswith("/") or s.startswith("http://") or s.startswith("https://")


def _looks_http(call: ast.Call, awaited: bool) -> bool:
    """Bound the noise: a `.get(x)` on a dict is not a request. Treat it as HTTP only when
    it's awaited, carries request-style kwargs, or the receiver is named like a client."""
    recv = ast.unparse(call.func.value)
    parts = [p.strip("()") for p in recv.split(".")]
    # only `<something request-ish>.<accessor>` — never a bare `session`/`client`
    if (len(parts) >= 2 and parts[-1] in _PARAM_ACCESSORS
            and any(p.lower() in ("request", "req") for p in parts[:-1])):
        return False        # reading the inbound request, not making an outbound one
    if awaited or any(kw.arg in _HTTP_KWARGS for kw in call.keywords):
        return True
    return any(h in recv.lower() for h in _CLIENT_HINTS)


def _calls_in(func: ast.AST, rel: str, src: str = "",
              skipped: list[SkippedSite] | None = None) -> list[OutboundCall]:
    # last literal assigned to a `path`-ish variable in this function
    path_vars: dict[str, str] = {}
    out: list[OutboundCall] = []
    lines = src.splitlines()
    awaited = {id(n.value) for n in ast.walk(func) if isinstance(n, ast.Await)}
    for sub in ast.walk(func):
        if (isinstance(sub, ast.Assign) and len(sub.targets) == 1
                and isinstance(sub.targets[0], ast.Name)):
            t = _template(sub.value)
            if t is not None and _is_path_like(t):
                path_vars[sub.targets[0].id] = t
        if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                and (sub.func.attr in _VERBS or sub.func.attr == "request") and sub.args):
            is_request = sub.func.attr == "request"
            # `.request(method, url)` carries the URL second; only worth recording as a gap
            arg0 = sub.args[1] if is_request and len(sub.args) > 1 else sub.args[0]
            url = None if is_request else _template(arg0)
            if url is None and isinstance(arg0, ast.Name) and not is_request:
                url = path_vars.get(arg0.id)
            if url is None or not _is_path_like(url):
                # host from config, path still literal
                url = _path_from_expr(arg0) or url
            if url is None or not _is_path_like(url):
                if (skipped is not None and _looks_http(sub, id(sub) in awaited)
                        and not isinstance(arg0, (ast.Constant, ast.Starred))):
                    method = None if is_request else sub.func.attr.upper()
                    if is_request:
                        m = sub.args[0]
                        method = m.value.upper() if isinstance(m, ast.Constant) and isinstance(m.value, str) else None
                    skipped.append(SkippedSite(
                        kind="outbound", reason="non-literal-url", file=rel, line=sub.lineno,
                        expr=ast.get_source_segment(src, arg0) or "",
                        snippet=snippet_of(lines, sub.lineno, getattr(sub, "end_lineno", sub.lineno)),
                        names=names_in(arg0) + (names_in(sub.args[0]) if is_request else []),
                        method=method))
                continue
            path, host = _url_to_path(url)
            out.append(OutboundCall(sub.func.attr.upper(), path, host,
                                    f"{rel}:{getattr(sub, 'lineno', 0)}"))
    return out


def discover_outbound(repo: Path, skipped: list[SkippedSite] | None = None) -> list[OutboundCall]:
    repo = Path(repo)
    out: list[OutboundCall] = []
    for p in _iter_py(repo):
        src, tree = read_and_parse(p)
        if tree is None:
            continue
        rel = p.relative_to(repo).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out += _calls_in(node, rel, src, skipped)
    # de-dup identical (verb, path)
    seen, uniq = set(), []
    for c in out:
        key = (c.verb, c.path)
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    return uniq
