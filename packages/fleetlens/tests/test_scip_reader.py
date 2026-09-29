"""SCIP wire reader — decode only the fields the call graph needs, both range encodings.

The tests build minimal SCIP buffers by hand (a few-line protobuf encoder) so they run
without `scip-python` installed.
"""
from __future__ import annotations

from fleetlens.callgraph.scip_reader import ROLE_DEFINITION, read_documents


# --- tiny protobuf encoder (only what these tests emit) --------------------
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _ld(field: int, payload: bytes) -> bytes:  # length-delimited (wire 2)
    return _tag(field, 2) + _varint(len(payload)) + payload


def _vint(field: int, n: int) -> bytes:  # varint (wire 0)
    return _tag(field, 0) + _varint(n)


def _occurrence(symbol: str, roles: int, start_line: int, *, typed: bool = False) -> bytes:
    body = bytearray()
    if typed:
        single = _vint(1, start_line) + _vint(2, 0) + _vint(3, 5)  # line, start_ch, end_ch
        body += _ld(8, bytes(single))  # single_line_range
    else:
        packed = _varint(start_line) + _varint(0) + _varint(5)  # [line, startCh, endCh]
        body += _ld(1, bytes(packed))  # deprecated repeated int32 range
    body += _ld(2, symbol.encode())
    body += _vint(3, roles)
    return bytes(body)


def _document(path: str, occs: list[bytes]) -> bytes:
    body = _ld(1, path.encode())
    for o in occs:
        body += _ld(2, o)
    return body


def _index(docs: list[bytes]) -> bytes:
    return b"".join(_ld(2, d) for d in docs)


def test_reads_documents_and_roles():
    idx = _index([
        _document("a.py", [
            _occurrence("scip foo", ROLE_DEFINITION, 10),
            _occurrence("scip bar", 0, 20),  # reference (no Definition bit)
        ]),
    ])
    docs = list(read_documents(idx))
    assert len(docs) == 1
    d = docs[0]
    assert d.relative_path == "a.py"
    assert [(o.symbol, o.is_definition, o.start_line) for o in d.occurrences] == [
        ("scip foo", True, 10),
        ("scip bar", False, 20),
    ]


def test_typed_single_line_range_encoding():
    idx = _index([_document("b.py", [_occurrence("scip baz", 0, 42, typed=True)])])
    (d,) = list(read_documents(idx))
    assert d.occurrences[0].start_line == 42
    assert d.occurrences[0].symbol == "scip baz"


def test_skips_occurrence_without_symbol_or_range():
    # an occurrence with only roles set (no symbol) is dropped
    bogus = _vint(3, ROLE_DEFINITION)
    idx = _index([_document("c.py", [bogus, _occurrence("scip ok", 0, 1)])])
    (d,) = list(read_documents(idx))
    assert [o.symbol for o in d.occurrences] == ["scip ok"]
