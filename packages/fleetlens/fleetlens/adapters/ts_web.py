"""TypeScript and JavaScript interface adapter — Express routes (`app.get('/x', handler)`).

Anchors on `<router>.<verb>('/path', ...)` call expressions where the first argument is a
literal path, or an array of them (`app.get(['/a', '/b'], h)` serves both). Handler name is
the second argument when it's a named function.

Only receivers that are an Express app or router count; see `express_receivers`. A UI
codebase holds as many `api.get('/x', config)` requests as it does routes, and reading
every receiver as a router reported each of those as an endpoint.

Known limits: Express only (Nest's decorator routing is a follow-up); router mount
prefixes (`app.use('/p', router)`) are not composed; non-literal paths are skipped. Honest
gaps the LLM tier or a richer adapter can fill later.
"""
from __future__ import annotations

from itertools import chain
from pathlib import Path

from ._ts import (
    _HTTP_VERBS,
    express_receivers,
    iter_calls,
    iter_js_files,
    iter_ts_files,
    parse_file,
)
from .base import Interface, InterfaceAdapter, SkippedSite, snippet_of


def _iter_sources(repo: Path):
    return chain(iter_ts_files(repo), iter_js_files(repo))


def _imports_express(repo: Path) -> bool:
    for p in _iter_sources(repo):
        try:
            b = p.read_bytes()
        except OSError:
            continue
        if b'"express"' in b or b"'express'" in b:
            return True
    return False


class TSWebAdapter(InterfaceAdapter):
    name = "ts-web"

    def applies(self, repo: Path) -> bool:
        return _imports_express(Path(repo))

    def discover(self, repo: Path, skipped=None) -> list[Interface]:
        repo = Path(repo)
        out: list[Interface] = []
        for p in _iter_sources(repo):
            root = parse_file(p)
            routers = express_receivers(root)
            if not routers:
                continue
            rel = p.relative_to(repo).as_posix()
            lines = None
            for call in iter_calls(p, root):
                # a route: router.verb('/path', handler) with a path-like literal first arg
                if call.verb not in _HTTP_VERBS or call.obj not in routers:
                    continue
                paths = call.arg0_list or ([call.arg0] if call.arg0 else [])
                if not paths or not all(x.startswith("/") for x in paths):
                    # router.verb(<expr>, handler): a route whose path we can't read — record it
                    if skipped is not None and call.arg0_expr and call.arg1_is_handler:
                        if lines is None:
                            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                        skipped.append(SkippedSite(
                            kind="interface", reason="non-literal-path", file=rel, line=call.line,
                            expr=call.arg0_expr, snippet=snippet_of(lines, call.line, call.end_line),
                            names=call.arg0_names, method=call.verb.upper()))
                    continue
                for path in paths:
                    out.append(Interface(
                        method=call.verb.upper(), path=path, type="rest",
                        handler=call.arg1_name, summary=None, framework="express",
                        evidence=[f"{rel}:{call.line}"]))
        return out
