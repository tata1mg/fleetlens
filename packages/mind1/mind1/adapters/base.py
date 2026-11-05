"""Interface adapter contract — the pluggable producer boundary for interface discovery.

An adapter inspects a repo and returns the interfaces it *exposes* (REST routes today; gRPC
/ events later). Adapters are deterministic and source-only. Add a framework by adding an
adapter and registering it — the core never changes.
"""
from __future__ import annotations

import ast
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Interface:
    """One exposed interface (an endpoint)."""

    method: str                       # GET | POST | ... (or "" for non-REST)
    path: str                         # "/orders/{id}"
    type: str = "rest"                # rest | grpc | event
    handler: Optional[str] = None     # handler function name (best-effort)
    summary: Optional[str] = None     # from docstring, if any
    framework: Optional[str] = None
    evidence: list[str] = field(default_factory=list)   # ["path:line", ...]

    @property
    def id(self) -> str:
        """Deterministic, stable id: `<method>-<slug(path)>` (REST verb, or PUBLISH/CONSUME
        for events); `<type>-<slug>` when there is no method."""
        if self.method:
            return f"{self.method.lower()}-{_slug(self.path)}"
        return f"{self.type}-{_slug(self.path)}"

    @property
    def name(self) -> str:
        return f"{self.method} {self.path}".strip()


@dataclass
class SkippedSite:
    """A place a deterministic adapter saw a route/call but could not resolve its path.

    Recorded, never guessed. The optional LLM gap-filler works only from these, so it never
    re-reads a repo at large: it gets the snippet plus the source of the symbols `names`
    reference, proposes a value, and must ground it against repo literals.
    """

    kind: str                          # interface | outbound
    reason: str                        # non-literal-path | non-literal-url
    file: str                          # repo-relative
    line: int
    expr: str                          # source text of the unresolved argument
    snippet: str                       # surrounding source lines
    names: list[str] = field(default_factory=list)   # identifiers used in `expr`
    method: Optional[str] = None       # HTTP method when it *was* literal

    @property
    def evidence(self) -> str:
        return f"{self.file}:{self.line}"


def _slug(path: str) -> str:
    """`/events/{id:int}` -> `events-id`; `/` -> `root`. Path params normalized to names."""
    p = re.sub(r"[<{]([A-Za-z_][A-Za-z0-9_]*)(:[^>}]*)?[>}]", r"\1", path)  # {id:int}/<id:int> -> id
    p = re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", r"\1", p)                       # :id -> id
    p = re.sub(r"[^A-Za-z0-9]+", "-", p).strip("-").lower()
    return p or "root"


class InterfaceAdapter(ABC):
    name: str = "adapter"

    @abstractmethod
    def applies(self, repo: Path) -> bool:
        """Cheap check: does this adapter's framework appear in the repo?"""

    @abstractmethod
    def discover(self, repo: Path, skipped: Optional[list[SkippedSite]] = None) -> list[Interface]:
        """Return the interfaces this repo exposes. Sites the adapter saw but could not
        resolve are appended to `skipped` when given."""


def names_in(node: ast.AST) -> list[str]:
    """Identifiers an expression references, in source order, de-duplicated."""
    out: list[str] = []
    for sub in ast.walk(node):
        n = sub.id if isinstance(sub, ast.Name) else sub.attr if isinstance(sub, ast.Attribute) else None
        if n and n not in out:
            out.append(n)
    return out


def snippet_of(lines: list[str], start: int, end: int, pad: int = 2) -> str:
    """1-based inclusive line range with `pad` lines of context, as source text."""
    lo, hi = max(1, start - pad), min(len(lines), end + pad)
    return "\n".join(lines[lo - 1:hi])
