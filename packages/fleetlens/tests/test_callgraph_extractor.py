"""Extractor: SCIP references + tree-sitter spans -> precise caller->callee edges.

Uses the hand-rolled protobuf encoder from test_scip_reader so it needs no indexer.
"""
from __future__ import annotations

from fleetlens.callgraph.extractor import build_callgraph
from fleetlens.callgraph.scip_reader import ROLE_DEFINITION
from test_scip_reader import _document, _index, _occurrence


def _symbol_index() -> dict:
    return {
        "schema": "symbols/v1",
        "root": "demo",
        "symbols": {
            "m.py::C": {"path": "m.py", "lang": "python", "kind": "class",
                        "line_start": 1, "line_end": 10, "body_hash": "x"},
            "m.py::C.foo": {"path": "m.py", "lang": "python", "kind": "function",
                            "line_start": 2, "line_end": 4, "body_hash": "x"},
            "m.py::C.bar": {"path": "m.py", "lang": "python", "kind": "function",
                            "line_start": 5, "line_end": 9, "body_hash": "x"},
        },
    }


def _scip() -> bytes:
    foo, bar = "scip m C.foo", "scip m C.bar"
    return _index([_document("m.py", [
        _occurrence(foo, ROLE_DEFINITION, 1),   # def foo -> 1-based line 2
        _occurrence(bar, ROLE_DEFINITION, 4),   # def bar -> 1-based line 5
        _occurrence(foo, 0, 5),                 # ref foo inside bar (1-based line 6)
        _occurrence("scip ext Thing", 0, 6),    # external ref -> no edge
        _occurrence("local 0", 0, 6),           # local symbol -> ignored
    ])])


def test_edge_resolves_to_precise_callee(tmp_path):
    scip = tmp_path / "demo.scip"
    scip.write_bytes(_scip())
    cg = build_callgraph(scip, _symbol_index(), "demo")

    assert cg["stats"]["nodes"] == 3
    edges = {(e["from"], e["to"]): e["weight"] for e in cg["edges"]}
    # bar calls foo — exactly one internal edge; external + local refs produce none
    assert edges == {("m.py::C.bar", "m.py::C.foo"): 1}


def test_class_is_a_node_but_not_a_call_target(tmp_path):
    # a reference resolving to the class def line must NOT create a call edge
    scip = tmp_path / "demo.scip"
    scip.write_bytes(_index([_document("m.py", [
        _occurrence("scip m C", ROLE_DEFINITION, 0),  # class C def (1-based line 1)
        _occurrence("scip m C.bar", ROLE_DEFINITION, 4),
        _occurrence("scip m C", 0, 5),                # ref to class inside bar
    ])]))
    cg = build_callgraph(scip, _symbol_index(), "demo")
    assert cg["edges"] == []  # class ref is not a call
    assert any(n["kind"] == "class" for n in cg["nodes"])  # but class is still a node


def test_top_level_reference_has_no_caller(tmp_path):
    # a reference not enclosed by any function (import-time) yields no edge
    scip = tmp_path / "demo.scip"
    scip.write_bytes(_index([_document("m.py", [
        _occurrence("scip m C.foo", ROLE_DEFINITION, 1),
        _occurrence("scip m C.foo", 0, 0),  # ref at line 1 (1-based) — top of file
    ])]))
    cg = build_callgraph(scip, _symbol_index(), "demo")
    assert cg["edges"] == []
