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

Code written against Python 3.4's asyncio uses `async` as an ordinary name, in
`asyncio.async(coro)` and `from asyncio import async`. It has been a keyword since 3.7, so
every module that does this fails to parse, and the route module of such a service usually
does. `ast.parse(feature_version=(3, 6))` used to accept it, but 3.13 dropped that, so a
file that fails is retried once with those names renamed. See `_rename_legacy_async`.
"""
from __future__ import annotations

import ast
import io
import tokenize
import warnings
from pathlib import Path
from typing import Optional

#: Tokens that follow `async` when it is the keyword: `async def`, `async for`, `async with`.
_ASYNC_KEYWORD_NEXT = {"def", "for", "with"}


def _rename_legacy_async(src: str) -> Optional[str]:
    """`src` with every `async` used as a name renamed to `async_`, or None if there is none.

    Only the identifier changes and every line keeps its number, so evidence and snippets
    still point at the right place in the original file.
    """
    try:
        toks = [t for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.NL, tokenize.COMMENT)]
    except (tokenize.TokenError, SyntaxError):
        return None
    hits = [t.start for t, nxt in zip(toks, toks[1:] + [None])
            if t.type == tokenize.NAME and t.string == "async"
            and (nxt is None or nxt.string not in _ASYNC_KEYWORD_NEXT)]
    if not hits:
        return None
    lines = src.splitlines(keepends=True)
    for row, col in reversed(hits):           # right to left keeps earlier columns valid
        line = lines[row - 1]
        lines[row - 1] = line[:col] + "async_" + line[col + len("async"):]
    return "".join(lines)


def _parse_quietly(src: str, path: Path) -> ast.Module:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        warnings.simplefilter("ignore", DeprecationWarning)
        return ast.parse(src, filename=str(path))


def parse(src: str, path: Path, errors: Optional[list] = None) -> Optional[ast.Module]:
    """Parse `src`, or return None if it will not compile.

    A syntax error is not an indexing failure: a repository can hold a Python 2 file, a
    template, or a deliberately broken fixture, and the other few hundred files still have
    interfaces worth finding.
    """
    try:
        return _parse_quietly(src, path)
    except SyntaxError as exc:
        legacy = _rename_legacy_async(src)
        if legacy is not None:
            try:
                return _parse_quietly(legacy, path)
            except (SyntaxError, ValueError, RecursionError):
                pass                          # report the original error, not the retry's
        if errors is not None:
            errors.append(f"SyntaxError: {exc.msg} (line {exc.lineno})" if exc.lineno
                          else f"SyntaxError: {exc.msg}")
        return None
    except (ValueError, RecursionError) as exc:
        # ValueError covers source with null bytes; RecursionError, a deeply nested literal.
        if errors is not None:
            errors.append(f"{type(exc).__name__}: {exc}")
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
