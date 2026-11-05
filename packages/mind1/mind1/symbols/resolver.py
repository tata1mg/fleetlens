"""Evidence → symbol → hash resolver — the join that drives symbol-level invalidation.

Interfaces and contracts each record `evidence`: `path:line`, `path:lo-hi`, or bare
`path` references to the code they were built from. Given the symbol index from the last
run (old) and this run (new), decide whether an artifact is stale: resolve its evidence
to symbol ids **at the old coordinates** (where the evidence line numbers are valid),
then compare those symbols' `body_hash` in the new index by their stable id
(`path::qualname`, unaffected by line shifts).

An artifact is stale iff any evidence symbol was removed/renamed or its body changed. If
evidence can't be resolved to any symbol (file gone, or a bare path with no parsed
symbols), we conservatively treat it as stale.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

# "<path>", "<path>:<line>", or "<path>:<lo>-<hi>". Path may itself contain no ':'.
_EV_RE = re.compile(r"^(?P<path>[^:]+?)(?::(?P<lo>\d+)(?:-(?P<hi>\d+))?)?$")


def parse_evidence(ev: str) -> Optional[tuple[str, Optional[int], Optional[int]]]:
    """('path', lo, hi) with lo/hi None for a whole-file ref. None if unparseable."""
    m = _EV_RE.match(ev.strip())
    if not m:
        return None
    lo = int(m["lo"]) if m["lo"] else None
    hi = int(m["hi"]) if m["hi"] else lo
    return m["path"], lo, hi


class EvidenceResolver:
    def __init__(self, old_index: Optional[dict], new_index: dict):
        self._old = (old_index or {}).get("symbols", {})
        self._new = new_index.get("symbols", {})
        # path -> [(line_start, line_end, symbol_id)] from the OLD index (evidence coords)
        self._by_path: dict[str, list[tuple[int, int, str]]] = {}
        for sid, s in self._old.items():
            self._by_path.setdefault(s["path"], []).append(
                (s["line_start"], s["line_end"], sid)
            )

    @property
    def has_baseline(self) -> bool:
        return bool(self._old)

    def symbol_ids_for(self, evidence_refs: Iterable[str]) -> set[str]:
        """Symbol ids (in the old index) that the evidence refers to."""
        ids: set[str] = set()
        for ref in evidence_refs:
            parsed = parse_evidence(ref)
            if parsed is None:
                continue
            path, lo, hi = parsed
            rows = self._by_path.get(path)
            if not rows:
                continue
            if lo is None:                          # whole-file ref → every symbol in it
                ids.update(sid for _, _, sid in rows)
            else:                                   # overlap [lo,hi] with [start,end]
                ids.update(sid for start, end, sid in rows if not (hi < start or lo > end))
        return ids

    def is_stale(self, evidence_refs: list[str]) -> bool:
        """True if the artifact built from this evidence must be regenerated."""
        if not self.has_baseline:
            return True                             # first run — nothing to compare against
        ids = self.symbol_ids_for(evidence_refs)
        if not ids:
            return True                             # unresolvable → regenerate to be safe
        for sid in ids:
            new = self._new.get(sid)
            if new is None:                         # removed / renamed
                return True
            if new["body_hash"] != self._old[sid]["body_hash"]:
                return True
        return False
