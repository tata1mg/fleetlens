"""What a repository says about itself: its README's opening and its manifest description.

Everything else fleetlens indexes is derived from source and cannot go stale, because it is
re-extracted every run. These two are the opposite: a human wrote them and nothing forces
them to stay true. They are still worth reading, because they are the only place the
*purpose* of a service is written down. Endpoints say what a service exposes; they never say
why it exists, which domain it owns, or what it was split out of.

So the rule is read them, label them as claims, and let the caller place them next to the
observed surface rather than mixed into it. A model reconciling a stale README against live
routes is a model doing something it is good at. A model handed both as equal facts is not.

Deliberately generic. Every ecosystem puts a one-line description in its manifest and an
opening paragraph in its README, and neither is one organisation's convention.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

#: Headings that mark the end of the part worth reading. A README's first section says what
#: the thing is; everything from "Installation" onward says how to operate it, which is not
#: what a summary needs and is most of the bytes.
_STOP_WORDS = {
    "installation", "install", "getting started", "quick start", "quickstart", "setup",
    "set up", "usage", "how to use", "requirements", "prerequisites", "pre requisites",
    "development", "developing", "building", "build", "running", "run", "run locally",
    "configuration", "config", "testing", "tests", "deployment", "deploy", "contributing",
    "contribution", "license", "licence", "changelog", "table of contents", "toc",
    "authors", "credits", "acknowledgments", "acknowledgements", "api reference",
}
_HEADING = re.compile(r"^(#{1,6})\s*(.+?)\s*#*$")


def _is_stop_heading(line: str) -> bool:
    """Whether a Markdown heading marks the end of the part worth reading.

    Matched on a normalised form rather than the literal text, because the same heading is
    written `Pre-requisites`, `PREREQUISITES` and `Pre Requisites` across repositories and
    a literal list catches only the spelling you thought of. Numbering is dropped too, so
    `## 2. Installation` stops as readily as `## Installation`.
    """
    m = _HEADING.match(line)
    if not m:
        return False
    text = re.sub(r"[^a-z0-9]+", " ", m.group(2).lower()).strip()
    text = re.sub(r"^\d+\s+", "", text)
    return text in _STOP_WORDS
#: Badge and image-only lines. A row of shields.io links is pure noise at the top of most
#: READMEs and would otherwise eat the character budget before any prose is reached.
#: Each part is a negated class rather than `.*?`, so a part cannot run past its own
#: closing bracket; with `.*?` inside the repeated group, a long run of `[![](`-like text
#: backtracks exponentially, and README text comes from arbitrary repositories.
_BADGE = re.compile(
    r"^\s*(?:\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)\s*)+$"
    r"|^\s*(?:!\[[^\]]*\]\([^)]*\)\s*)+$"
)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_HTML_TAG = re.compile(r"<[^>]+>")
#: rST section underlines, which are punctuation-only lines that read as noise in a prompt.
_RST_RULE = re.compile(r"^\s*[=\-~^\"'`*+#]{3,}\s*$")

_README_NAMES = ("readme.md", "readme.rst", "readme.txt", "readme",
                 "readme.markdown", "readme.adoc")


def _find_readme(root: Path) -> Path | None:
    """The root README, whatever this ecosystem spells it. Case-insensitive, root only."""
    try:
        entries = {p.name.lower(): p for p in root.iterdir() if p.is_file()}
    except OSError:
        return None
    for name in _README_NAMES:
        if name in entries:
            return entries[name]
    # Something like README.es.md, or an unusual extension.
    for low, p in sorted(entries.items()):
        if low.startswith("readme"):
            return p
    return None


def _clean(text: str) -> str:
    text = _HTML_COMMENT.sub(" ", text)
    out = []
    for line in text.splitlines():
        if _is_stop_heading(line):
            break
        if _BADGE.match(line) or _RST_RULE.match(line):
            continue
        line = _HTML_TAG.sub(" ", line)
        # Markdown heading marks and list bullets carry no meaning once the text is prose
        # in a prompt, and the hashes read as structure the model should honour.
        line = re.sub(r"^#{1,6}\s*", "", line)
        out.append(line.rstrip())
    # Collapse the blank-line runs that removing badges and headings leaves behind.
    joined = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
    return joined.strip()


def read_readme(root: Path, limit: int) -> tuple[str, str]:
    """(prose, filename). The README's opening section, cleaned and capped at `limit`.

    Returns empty strings when there is no README or nothing survives cleaning, which is
    common: plenty of repositories have a README that is a title and a build badge.
    """
    p = _find_readme(root)
    if p is None:
        return "", ""
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", ""
    text = _clean(raw)
    if len(text) <= limit:
        return (text, p.name) if text else ("", "")
    # Cut on a paragraph break when one is near the limit, otherwise on a line break, so the
    # prompt never ends halfway through a sentence.
    window = text[:limit]
    cut = window.rfind("\n\n")
    if cut < limit // 2:
        cut = window.rfind("\n")
    return (window[:cut] if cut > limit // 3 else window).strip(), p.name


#: manifest file -> how its one-line description is written. Every ecosystem has one, and it
#: is usually kept current because packaging tools surface it.
_DESC_PATTERNS: dict[str, re.Pattern] = {
    "pyproject.toml": re.compile(r'^\s*description\s*=\s*["\'](.+?)["\']\s*$', re.M),
    "setup.py": re.compile(r'\bdescription\s*=\s*["\'](.+?)["\']'),
    "setup.cfg": re.compile(r'^\s*description\s*=\s*(.+?)\s*$', re.M),
    "Cargo.toml": re.compile(r'^\s*description\s*=\s*["\'](.+?)["\']\s*$', re.M),
    "pom.xml": re.compile(r"<description>\s*(.+?)\s*</description>", re.S),
    "build.gradle": re.compile(r'^\s*description\s*=?\s*["\'](.+?)["\']', re.M),
}
_JSON_MANIFESTS = ("package.json", "composer.json")


def read_description(root: Path) -> tuple[str, str]:
    """(one-line description, filename) from whichever manifest this repository uses."""
    for name in _JSON_MANIFESTS:
        p = root / name
        if not p.exists():
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        desc = (doc.get("description") or "").strip() if isinstance(doc, dict) else ""
        if desc:
            return desc[:400], name
    for name, pat in _DESC_PATTERNS.items():
        p = root / name
        if not p.exists():
            continue
        try:
            m = pat.search(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if m and m.group(1).strip():
            return " ".join(m.group(1).split())[:400], name
    # A gemspec is named after the gem, so it has to be globbed rather than looked up.
    try:
        specs = sorted(root.glob("*.gemspec"))
    except OSError:
        specs = []
    for p in specs:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for key in ("summary", "description"):
            m = re.search(rf'\.{key}\s*=\s*["\'](.+?)["\']', text)
            if m and m.group(1).strip():
                return " ".join(m.group(1).split())[:400], p.name
    return "", ""


def declared(root_str: str, readme_limit: int) -> tuple[list[str], str]:
    """(prompt lines, hash input) for what a repository claims about itself.

    The hash input is returned separately so a README edit re-summarises that one service
    and nothing else. Without it the claim could change while the content hash did not, and
    the summary would never be regenerated.
    """
    if not root_str:
        return [], ""
    root = Path(root_str)
    if not root.is_dir():
        return [], ""
    desc, desc_file = read_description(root)
    prose, readme_file = read_readme(root, readme_limit)
    if not desc and not prose:
        return [], ""
    where = ", ".join(f for f in (desc_file, readme_file) if f)
    lines = [f"Declared purpose, written by hand in {where}. It may be out of date; where "
             f"it disagrees with the observed surface below, the observed surface is right."]
    if desc:
        lines.append(f"  {desc}")
    if prose:
        lines += ["  " + ln for ln in prose.splitlines()]
    return lines, f"{desc}\n{prose}"
