"""TypeScript outbound-call extraction — axios / fetch / HTTP-client calls.

Catches `<x>.<verb>('url' | `url`)` (axios, http clients) and `fetch('url', {...})`. Keeps
the request path (join key) and, for literal URLs, the host (a corroborating hint).
"""
from __future__ import annotations

from pathlib import Path

from ._ts import _HTTP_VERBS, Call, iter_calls, iter_ts_files
from .base import SkippedSite, snippet_of
from .outbound import _CLIENT_HINTS, OutboundCall, _is_path_like, _url_to_path


def _looks_http(call: Call) -> bool:
    """Bound the noise: `map.get(key)` is not a request. Count it when awaited, given an
    options object, a bare fetch(), or a client-looking receiver."""
    if call.awaited or call.has_object_arg or call.obj is None:
        return True
    return any(h in call.obj.lower() for h in _CLIENT_HINTS) or call.obj.lower() == "axios"


def discover_outbound(repo: Path, skipped: list[SkippedSite] | None = None) -> list[OutboundCall]:
    repo = Path(repo)
    out: list[OutboundCall] = []
    for p in iter_ts_files(repo):
        rel = p.relative_to(repo).as_posix()
        lines = None
        for call in iter_calls(p):
            is_verb = call.verb in _HTTP_VERBS and call.obj is not None
            is_fetch = call.verb == "fetch" and call.obj is None
            if not (is_verb or is_fetch):
                continue
            url = call.arg0
            if not url or not _is_path_like(url):
                # non-string first arg, or a template whose prefix is interpolated
                dynamic = call.arg0_expr or (url and "{}" in url)
                if skipped is not None and dynamic and not call.arg1_is_handler and _looks_http(call):
                    if lines is None:
                        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                    skipped.append(SkippedSite(
                        kind="outbound", reason="non-literal-url", file=rel, line=call.line,
                        expr=call.arg0_expr or f"`{url}`",
                        snippet=snippet_of(lines, call.line, call.end_line),
                        names=call.arg0_names,
                        method="GET" if is_fetch else call.verb.upper()))
                continue
            path, host = _url_to_path(url)
            verb = "GET" if is_fetch else call.verb.upper()  # fetch default method is GET
            out.append(OutboundCall(verb, path, host, f"{rel}:{call.line}"))
    # de-dup identical (verb, path)
    seen, uniq = set(), []
    for c in out:
        key = (c.verb, c.path)
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    return uniq
