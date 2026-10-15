"""Which languages a service is actually written in, counted from its own source files.

Declared language is a single value chosen for the indexer: it picks which SCIP tool to run
and which adapters to use. That is not the same question as "what is this service written
in", and a monorepo answers the second one with more than one value.

Counted from files rather than from the symbol table on purpose. Symbols come from an
external SCIP indexer that fails for its own reasons, and a service whose call graph could
not be built should still know it is Python. The walk is the pruned shared one, so a
virtualenv or a node_modules does not vote.
"""
from __future__ import annotations

from pathlib import Path

from ._walk import iter_files

#: extension -> language name. The names match what the SCIP readers already store on
#: symbols (`python`, `typescript`, `tsx`, `javascript`, `ruby`), so a rule written against
#: one is written against the other.
EXTENSIONS: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".rb": "ruby", ".rake": "ruby",
    ".go": "go",
    ".java": "java", ".kt": "kotlin", ".scala": "scala",
    ".rs": "rust", ".php": "php", ".cs": "csharp",
}
#: A language has to account for this much of the source files to count. Without a floor,
#: one `postcss.config.mjs` in a Python service makes it a JavaScript service too, and a
#: rule scoped to JavaScript then fires on forty repositories that are not.
MIN_SHARE = 0.05
#: ...unless it has at least this many files, which keeps a small but real second language
#: from being rounded away in a large repository.
MIN_FILES = 20
#: ...and never on the strength of one file, whatever the share. In a fourteen-file service
#: a single `app.js` is 7% of the source, which clears any sensible percentage floor while
#: being exactly the evidence that should not count.
MIN_ANY = 2


def language_counts(root: Path) -> dict[str, int]:
    """Source file counts by language, for one service root."""
    counts: dict[str, int] = {}
    for path in iter_files(Path(root), tuple(EXTENSIONS)):
        lang = EXTENSIONS.get(path.suffix.lower())
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    return counts


def observed_languages(root: Path) -> list[str]:
    """The languages a service is meaningfully written in, most files first.

    Thresholded rather than "every extension present", because a config file, a build script
    or a single vendored helper is not a language the service is written in, and a rule
    scoped to that language should not fire on it.
    """
    counts = language_counts(root)
    total = sum(counts.values())
    if not total:
        return []
    keep = [(n, lang) for lang, n in counts.items()
            if n >= MIN_ANY and (n >= MIN_FILES or n / total >= MIN_SHARE)]
    return [lang for _, lang in sorted(keep, key=lambda x: (-x[0], x[1]))]
