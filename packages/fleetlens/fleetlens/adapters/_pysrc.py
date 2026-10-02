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

A file that will not parse is still skipped rather than fatal, but the caller can ask why
by passing an `errors` list. Silence was the wrong default: an unreadable route file made a
service look like it had five endpoints when it had a hundred and forty-nine.
"""
from __future__ import annotations

import ast
import warnings
from pathlib import Path
from typing import Optional


def parse(src: str, path: Path, errors: Optional[list] = None) -> Optional[ast.Module]:
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
    except (SyntaxError, ValueError, RecursionError) as exc:
        # ValueError covers source with null bytes; RecursionError, a deeply nested literal.
        if errors is not None:
            line = getattr(exc, "lineno", None)
            errors.append(f"{type(exc).__name__}: {exc.msg if isinstance(exc, SyntaxError) else exc}"
                          + (f" (line {line})" if line else ""))
        return None


def read_and_parse(path: Path, errors: Optional[list] = None) -> tuple:
    """(source, tree) for a Python file, or (None, None) if it cannot be used.

    Pass `errors` to learn *why* a file was unusable. Skipping a file silently is how a
    service with a hundred and forty-nine endpoints indexed five of them: one route file
    held Python that a current interpreter will not parse, and nothing said so.
    """
    try:
        src = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        if errors is not None:
            errors.append(f"OSError: {exc}")
        return None, None
    tree = parse(src, path, errors)
    return (src, tree) if tree is not None else (None, None)
