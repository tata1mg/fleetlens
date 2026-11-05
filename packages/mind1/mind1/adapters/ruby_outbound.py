"""Outbound HTTP calls in Ruby: RestClient, HTTParty, Faraday, Net::HTTP.

Same contract as the Python and TypeScript extractors. A literal path or URL becomes an
outbound call the resolver can join on; anything computed is recorded as a skipped site
rather than guessed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import _ruby as R
from .base import SkippedSite, snippet_of
from .outbound import OutboundCall, _is_path_like, _url_to_path

_VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}
# Receivers that identify an HTTP client rather than an arbitrary object with a .get method.
_CLIENTS = ("restclient", "httparty", "faraday", "net::http", "http", "conn", "client",
            "connection", "api")
_EXCLUDE_RECEIVERS = ("params", "headers", "session", "cookies", "config", "settings",
                      "options", "payload", "hash", "cache", "redis")


def _looks_http(recv: str, method: str) -> bool:
    low = recv.lower()
    if any(x in low for x in _EXCLUDE_RECEIVERS):
        return False
    if any(c in low for c in _CLIENTS):
        return True
    # `RestClient::Request.execute(url: ...)` and similar
    return method in ("execute", "request") and "::" in recv


def discover_outbound(repo: Path, skipped: Optional[list] = None) -> list:
    repo = Path(repo)
    out: list = []
    for path in R.iter_rb_files(repo):
        tree, src = R.parse(path)
        if tree is None:
            continue
        rel = path.relative_to(repo).as_posix()
        lines = None
        for call in R.walk_calls(tree.root_node):
            method = R.call_name(call, src)
            if method not in _VERBS and method not in ("execute", "request", "new"):
                continue
            recv = R.receiver(call, src)
            if not _looks_http(recv, method):
                continue

            url = R.first_literal(call, src)
            if url is None:
                kw = R.kwargs(call, src)
                raw = kw.get("url") or kw.get("uri") or kw.get("path") or ""
                url = raw.strip("'\"") if raw.startswith(("'", '"')) else None

            verb = method.upper() if method in _VERBS else \
                (R.kwargs(call, src).get("method", "get").strip("':\"").upper() or "GET")

            if url and _is_path_like(url):
                p, host = _url_to_path(url)
                out.append(OutboundCall(verb, p, host, f"{rel}:{call.start_point[0] + 1}"))
                continue
            if skipped is not None and method in _VERBS:
                if lines is None:
                    lines = src.decode("utf-8", "replace").splitlines()
                line = call.start_point[0] + 1
                skipped.append(SkippedSite(
                    kind="outbound", reason="non-literal-url", file=rel, line=line,
                    expr=R.text(call, src)[:80],
                    snippet=snippet_of(lines, line, call.end_point[0] + 1),
                    names=[], method=verb))
    seen, uniq = set(), []
    for c in out:
        if (c.verb, c.path) not in seen:
            seen.add((c.verb, c.path))
            uniq.append(c)
    return uniq
