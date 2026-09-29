"""TypeScript interface adapter — Express routes (`app.get('/x', handler)`).

Anchors on `<router>.<verb>('/path', ...)` call expressions where the first argument is a
literal path. Handler name is the second argument when it's a named function.

Known limits (v1): Express only (Nest's decorator routing is a follow-up); router mount
prefixes (`app.use('/p', router)`) are not composed; non-literal paths are skipped. Honest
gaps the LLM tier or a richer adapter can fill later.
"""
from __future__ import annotations

from pathlib import Path

from ._ts import _HTTP_VERBS, iter_calls, iter_ts_files
from .base import Interface, InterfaceAdapter, SkippedSite, snippet_of


def _imports_express(repo: Path) -> bool:
    for p in iter_ts_files(repo):
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
        for p in iter_ts_files(repo):
            rel = p.relative_to(repo).as_posix()
            lines = None
            for call in iter_calls(p):
                # a route: obj.verb('/path', handler) with a path-like literal first arg
                if call.verb not in _HTTP_VERBS or call.obj is None:
                    continue
                if not call.arg0 or not call.arg0.startswith("/"):
                    # obj.verb(<expr>, handler): a route whose path we can't read — record it
                    if skipped is not None and call.arg0_expr and call.arg1_is_handler:
                        if lines is None:
                            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                        skipped.append(SkippedSite(
                            kind="interface", reason="non-literal-path", file=rel, line=call.line,
                            expr=call.arg0_expr, snippet=snippet_of(lines, call.line, call.end_line),
                            names=call.arg0_names, method=call.verb.upper()))
                    continue
                out.append(Interface(
                    method=call.verb.upper(), path=call.arg0, type="rest",
                    handler=call.arg1_name, summary=None, framework="express",
                    evidence=[f"{rel}:{call.line}"]))
        return out
