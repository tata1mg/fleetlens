"""Reading and parsing Python source from an indexed repository.

Parsing someone else's code makes their compiler diagnostics yours. Python 3.12 warns about
things like `"\\d"` written outside a raw string, and `ast.parse(src)` with no filename
reports them against `<unknown>`:

    <unknown>:18: SyntaxWarning: invalid escape sequence '\\d'

Across a few hundred repositories that is thousands of lines of output about code the person
running the indexer is not editing and cannot act on from here, drowning the progress and
the summary. fleetlens is not a linter for the repositories it reads.

So warnings are silenced for the duration of the parse, and the filename is passed through
anyway, so that anything which does surface points at a real file instead of `<unknown>`.
"""
from __future__ import annotations

import ast
import warnings
from pathlib import Path
from typing import Optional


def parse(src: str, path: Path) -> Optional[ast.Module]:
    """Parse `src`, or return None if it will not compile.

    A syntax error is not an indexing failure: a repository can hold a Python 2 file, a
    template, or a deliberately broken fixture, and the other few hundred files still have
    interfaces worth finding.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            warnings.simplefilter("ignore", DeprecationWarning)
            return ast.parse(src, filename=str(path))
    except (SyntaxError, ValueError, RecursionError):
        # ValueError covers source with null bytes; RecursionError, a deeply nested literal.
        return None


def read_and_parse(path: Path) -> tuple:
    """(source, tree) for a Python file, or (None, None) if it cannot be used."""
    try:
        src = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    tree = parse(src, path)
    return (src, tree) if tree is not None else (None, None)
