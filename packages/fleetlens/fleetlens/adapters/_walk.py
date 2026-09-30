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
from fnmatch import fnmatch
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


#: Files never read, whatever a repo says. A deny list is the wrong shape for secrets: the
#: cost of missing one is unbounded, so these hold even if a `.contextignore` omits them.
#: SECURITY.md promises exactly this, which until now it did not do.
ALWAYS_EXCLUDE = (
    ".env", ".env.*", "*.env",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "*secret*", "*credential*", "*password*", "*.crt.key",
    ".npmrc", ".pypirc", ".netrc", ".htpasswd",
    "*.sqlite", "*.sqlite3", "*.db",
)


def load_contextignore(repo: Path) -> tuple:
    """Patterns from a repo's `.contextignore`, plus the ones that always apply.

    One pattern per line, `#` comments, gitignore-style globs matched against the path
    relative to the repo root and against the bare filename. A repo can add to the excluded
    set; it cannot remove anything from ALWAYS_EXCLUDE.
    """
    extra: list = []
    f = Path(repo) / ".contextignore"
    if f.is_file():
        try:
            for raw in f.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if line and not line.startswith("#"):
                    extra.append(line.rstrip("/"))
        except OSError:
            pass
    return ALWAYS_EXCLUDE + tuple(extra)


def _excluded(rel: str, name: str, patterns: tuple) -> bool:
    return any(fnmatch(name, pat) or fnmatch(rel, pat) or fnmatch(rel, f"{pat}/*")
               for pat in patterns)


def iter_files(repo: Path, suffixes: tuple, *, skip: frozenset = COMMON_SKIP,
               patterns: tuple = None) -> Iterator[Path]:
    """Every file under `repo` with one of `suffixes`, skipping pruned directories.

    Yields in sorted order so a run over the same tree produces the same output twice,
    which matters because interface ids are derived from discovery order.
    """
    repo = Path(repo)
    patterns = load_contextignore(repo) if patterns is None else patterns
    stack = [repo]
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
            rel = p.relative_to(repo).as_posix()
            if p.is_dir():
                if p.name not in skip and not _excluded(rel, p.name, patterns):
                    dirs.append(p)
            elif p.name.endswith(suffixes) and not _excluded(rel, p.name, patterns):
                yield p
        stack.extend(reversed(dirs))      # keep the sorted order on a LIFO stack
