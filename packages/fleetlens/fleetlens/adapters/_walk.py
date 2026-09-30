"""One pruned directory walk, shared by every adapter.

`Path.rglob` cannot be pruned: it descends into every directory and leaves the caller to
discard what it did not want. Each adapter then paired it with a skip list applied to the
results, so a repository with a virtualenv or node_modules in the working tree was walked
in full, once per adapter and once per file pattern, to reach a few hundred source files.

That made indexing cost scale with whatever happened to be checked out rather than with the
size of the project. On a service with a 7000-file `venv/` beside 184 source files, the
walking dominated everything else the indexer did.

This descends once and refuses to enter a skipped directory in the first place.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

#: Directories no adapter should ever read. Dependency trees, build output, caches and
#: version-control metadata. Kept in one place because the previous per-adapter copies
#: drifted: two of them listed `.venv` but not `venv`, which is the name `python -m venv`
#: produces by default and therefore the one most likely to be present.
COMMON_SKIP = frozenset({
    ".git", ".hg", ".svn",
    ".venv", "venv", "env", ".env",
    "node_modules", "bower_components", "vendor", ".bundle",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    "dist", "build", ".next", ".nuxt", "target", "coverage",
    ".context", ".idea", ".vscode", ".claude",
})


def iter_files(repo: Path, suffixes: tuple, *, skip: frozenset = COMMON_SKIP) -> Iterator[Path]:
    """Every file under `repo` with one of `suffixes`, skipping pruned directories.

    Yields in sorted order so a run over the same tree produces the same output twice,
    which matters because interface ids are derived from discovery order.
    """
    stack = [Path(repo)]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue                      # unreadable directory is not worth failing over
        dirs = []
        for p in entries:
            if p.is_symlink():
                # A symlink out of the tree, or back into it, turns one walk into many.
                continue
            if p.is_dir():
                if p.name not in skip:
                    dirs.append(p)
            elif p.name.endswith(suffixes):
                yield p
        stack.extend(reversed(dirs))      # keep the sorted order on a LIFO stack
