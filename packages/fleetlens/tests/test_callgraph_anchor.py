"""handled_by anchor: interface evidence line -> tightest enclosing handler node."""
from __future__ import annotations

from fleetlens.callgraph.anchor import resolve_handlers

_NODES = [
    {"id": "routes.py::C", "path": "routes.py", "kind": "class",
     "line_start": 1, "line_end": 30},
    {"id": "routes.py::C.handler", "path": "routes.py", "kind": "function",
     "line_start": 5, "line_end": 12},
    {"id": "routes.py::other", "path": "routes.py", "kind": "function",
     "line_start": 15, "line_end": 20},
]


def test_resolves_to_enclosing_function_not_class():
    ifaces = [{"id": "get-thing", "evidence": ["routes.py:8"]}]
    assert resolve_handlers(_NODES, ifaces) == [("get-thing", "routes.py::C.handler")]


def test_unresolvable_and_wholefile_evidence_omitted():
    ifaces = [
        {"id": "ping", "evidence": ["README.md:3"]},   # no such node
        {"id": "wholefile", "evidence": ["routes.py"]},  # no line -> can't pinpoint
        {"id": "nolabel"},                               # no evidence
    ]
    assert resolve_handlers(_NODES, ifaces) == []


def test_dedupes_multiple_evidence_in_same_handler():
    ifaces = [{"id": "get-thing", "evidence": ["routes.py:6", "routes.py:9"]}]
    assert resolve_handlers(_NODES, ifaces) == [("get-thing", "routes.py::C.handler")]
