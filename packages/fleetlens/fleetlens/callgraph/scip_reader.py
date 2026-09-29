"""Minimal, dependency-free reader for the SCIP index format.

SCIP (https://github.com/sourcegraph/scip) is a protobuf-encoded, language-agnostic
code index emitted by per-language indexers (`scip-python`, `scip-typescript`,
`scip-ruby`). We only need three things out of it to build a call graph:

  * every document's `relative_path`
  * every occurrence's `symbol` id and `symbol_roles` bitset (is this a *definition*
    or a *reference*?)
  * the occurrence's start line

Rather than depend on `protobuf` + a generated `scip_pb2` (a version-fragile pairing
that repeatedly broke tooling built on SCIP), we decode only the handful of fields we
use straight off the protobuf wire. The field numbers below are pinned to the canonical
`scip.proto`; SCIP's schema is append-only, so unknown fields are simply skipped.

Wire format recap: each field is a varint tag `(field_number << 3) | wire_type`.
    wire_type 0 = varint, 1 = 64-bit, 2 = length-delimited, 5 = 32-bit.
Only 0 and 2 appear in the fields we read; the rest are skipped generically.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

# --- scip.proto field numbers (pinned) -------------------------------------
# Index.documents
_INDEX_DOCUMENTS = 2
# Document.relative_path / Document.occurrences
_DOC_RELATIVE_PATH = 1
_DOC_OCCURRENCES = 2
# Occurrence.range (deprecated packed int32) / symbol / symbol_roles
_OCC_RANGE = 1
_OCC_SYMBOL = 2
_OCC_SYMBOL_ROLES = 3
# Occurrence typed ranges (newer indexers may emit these instead of field 1)
_OCC_SINGLE_LINE_RANGE = 8
_OCC_MULTI_LINE_RANGE = 9
# SingleLineRange.line / MultiLineRange.start_line — both are field 1
_RANGE_START_LINE = 1

# SymbolRole bitset
ROLE_DEFINITION = 0x1


@dataclass(frozen=True)
class Occurrence:
    symbol: str
    roles: int
    start_line: int  # 0-based, as encoded in SCIP

    @property
    def is_definition(self) -> bool:
        return bool(self.roles & ROLE_DEFINITION)


@dataclass(frozen=True)
class Document:
    relative_path: str
    occurrences: list[Occurrence]


# --- wire primitives -------------------------------------------------------
def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Return (value, new_pos). Standard base-128 varint."""
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def _iter_fields(buf: bytes) -> Iterator[tuple[int, int, object]]:
    """Yield (field_number, wire_type, payload) for each field in one message.

    payload is an int for varints, or a memoryview/bytes slice for length-delimited
    fields. 64-bit and 32-bit fields are skipped (we never read any).
    """
    pos, n = 0, len(buf)
    while pos < n:
        tag, pos = _read_varint(buf, pos)
        field_no, wire = tag >> 3, tag & 0x7
        if wire == 0:  # varint
            val, pos = _read_varint(buf, pos)
            yield field_no, wire, val
        elif wire == 2:  # length-delimited
            length, pos = _read_varint(buf, pos)
            yield field_no, wire, buf[pos:pos + length]
            pos += length
        elif wire == 1:  # 64-bit
            pos += 8
        elif wire == 5:  # 32-bit
            pos += 4
        else:  # pragma: no cover - groups are obsolete and never appear in SCIP
            raise ValueError(f"unsupported wire type {wire}")


def _range_start_line(buf: bytes) -> int | None:
    """Start line from a SingleLineRange / MultiLineRange message (field 1 in both)."""
    for field_no, _wire, val in _iter_fields(buf):
        if field_no == _RANGE_START_LINE and isinstance(val, int):
            return val
    return None


def _packed_start_line(buf: bytes) -> int | None:
    """First int32 of a packed `repeated int32 range` — the start line."""
    if not buf:
        return None
    val, _ = _read_varint(bytes(buf), 0)
    return val


def _parse_occurrence(buf: bytes) -> Occurrence | None:
    symbol = ""
    roles = 0
    start_line: int | None = None
    for field_no, _wire, val in _iter_fields(buf):
        if field_no == _OCC_SYMBOL and isinstance(val, (bytes, memoryview)):
            symbol = bytes(val).decode("utf-8", "replace")
        elif field_no == _OCC_SYMBOL_ROLES and isinstance(val, int):
            roles = val
        elif field_no == _OCC_RANGE and isinstance(val, (bytes, memoryview)):
            start_line = _packed_start_line(val)
        elif field_no in (_OCC_SINGLE_LINE_RANGE, _OCC_MULTI_LINE_RANGE) and isinstance(
            val, (bytes, memoryview)
        ):
            # typed range takes precedence over the deprecated packed field
            start_line = _range_start_line(bytes(val))
    if not symbol or start_line is None:
        return None
    return Occurrence(symbol=symbol, roles=roles, start_line=start_line)


def _parse_document(buf: bytes) -> Document:
    path = ""
    occs: list[Occurrence] = []
    for field_no, _wire, val in _iter_fields(buf):
        if field_no == _DOC_RELATIVE_PATH and isinstance(val, (bytes, memoryview)):
            path = bytes(val).decode("utf-8", "replace")
        elif field_no == _DOC_OCCURRENCES and isinstance(val, (bytes, memoryview)):
            occ = _parse_occurrence(bytes(val))
            if occ is not None:
                occs.append(occ)
    return Document(relative_path=path, occurrences=occs)


def read_documents(data: bytes) -> Iterator[Document]:
    """Stream the documents of a SCIP index (top-level Index.documents)."""
    for field_no, _wire, val in _iter_fields(data):
        if field_no == _INDEX_DOCUMENTS and isinstance(val, (bytes, memoryview)):
            yield _parse_document(bytes(val))
