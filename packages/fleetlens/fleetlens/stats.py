"""Where indexing time actually goes.

Exists because the answer is not what you would guess. On a Python service the external
SCIP indexer is two thirds of the wall clock and everything fleetlens does itself is the
rest, so an optimisation aimed at the wrong end buys nothing. Reporting the split is how
that stops being a guess.

The collector is deliberately tiny and always safe to pass: it records durations and
nothing else, so a step that raises still gets its time recorded on the way out.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class Stats:
    """Wall-clock time per step, in the order the steps first ran."""

    times: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)
    total: float = 0.0

    @contextmanager
    def step(self, label: str):
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - started
            self.times[label] = self.times.get(label, 0.0) + elapsed
            self.counts[label] = self.counts.get(label, 0) + 1

    @contextmanager
    def overall(self):
        started = time.perf_counter()
        try:
            yield
        finally:
            self.total = time.perf_counter() - started

    def render(self, *, files: int = 0) -> str:
        """A table in running order, not sorted by size.

        Order matters more than ranking here: the steps form a pipeline, and one of them
        overlaps the others, so a list sorted by duration would imply a sequence that does
        not exist.
        """
        if not self.times:
            return "  no steps recorded"
        width = max(len(k) for k in self.times)
        lines = [f"  {'step':<{width}}  {'seconds':>8}  {'share':>6}"]
        lines.append("  " + "-" * (width + 18))
        for label, secs in self.times.items():
            share = f"{secs / self.total * 100:5.1f}%" if self.total else "    -"
            lines.append(f"  {label:<{width}}  {secs:8.2f}  {share}")
        lines.append("  " + "-" * (width + 18))
        lines.append(f"  {'TOTAL':<{width}}  {self.total:8.2f}")
        if files:
            lines.append(f"  {files} source files, {files / self.total:.0f} files/s"
                         if self.total else "")
        # Nested steps overlap the parent that contains them, so the column does not add up
        # to the total and saying so is cheaper than someone rechecking the arithmetic.
        lines.append("  (steps run inside an overlapped stage, so shares exceed 100%)")
        return "\n".join(lines)
