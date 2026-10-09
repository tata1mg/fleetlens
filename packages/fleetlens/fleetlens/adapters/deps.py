"""Declared dependencies, read from whichever manifest an ecosystem uses.

Every ecosystem writes its dependencies down in one well-known file, and the names in it come
from a global package namespace rather than one organisation's habits: `redis` means the same
package in every repository that declares it. That makes a manifest a better signal than an
import site, and a portable one.

A manifest is also the only honest place to look. Scanning import statements under-reports
badly in some ecosystems: Ruby code almost never requires a gem explicitly because Bundler
does it, so a repository with 1855 files can import nothing while its Gemfile names four
drivers.

Only the names are read. Versions, extras and markers are discarded, because what this feeds
is "does this service use X", not dependency resolution.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

#: manifest name -> how a dependency name is written in it. Each pattern's first group is the
#: package name and nothing else.
_PATTERNS: dict[str, re.Pattern] = {
    "Gemfile": re.compile(r"^\s*gem\s+['\"]([\w\-]+)['\"]", re.M),
    "go.mod": re.compile(r"^\s+([\w.\-/]+)\s+v\d", re.M),
    "requirements.txt": None,       # line-oriented, may be a bare URL
    "Pipfile": re.compile(r"^\s*['\"]?([A-Za-z][\w\-.]*)['\"]?\s*=", re.M),
    "pom.xml": re.compile(r"<artifactId>([\w\-.]+)</artifactId>"),
    "pyproject.toml": None,         # TOML sections, handled separately
    "Cargo.toml": None,
    "composer.json": None,          # JSON, handled separately
    "package.json": None,
}
_JSON_MANIFESTS = ("package.json", "composer.json")
#: Globs that find the rest of the Python convention: requirements-dev.txt, requirements/*.txt.
_GLOBS = ("requirements*.txt", "requirements/*.txt")
#: pyproject keys that look like dependency entries to the pattern above but are not packages.
#: TOML tables whose contents are dependencies, and how each spells them.
#:
#: Section-aware rather than a regex over the whole file, because `[tool.ruff.lint]` and
#: `[tool.pytest.ini_options]` are full of `key = value` lines that look exactly like a
#: dependency entry. Reading the whole file turned `addopts`, `asyncio-mode` and `line-length`
#: into packages this service supposedly depends on.
#:
#: Two shapes exist and both are common:
#:   requirement  the value is a list of strings — `dependencies = ["httpx>=0.27"]`
#:   key          the table key is the package — `[tool.poetry.dependencies]` / httpx = "*"
_TOML_SECTIONS: dict[str, str] = {
    "project": "requirement",                        # only its `dependencies` key
    "project.optional-dependencies": "requirement",
    "dependency-groups": "requirement",
    "tool.poetry.dependencies": "key",
    "tool.poetry.dev-dependencies": "key",
    "tool.uv.sources": "key",
    "dependencies": "key",                           # Cargo
    "dev-dependencies": "key",
    "build-dependencies": "key",
}
_POETRY_GROUP = re.compile(r"\Atool\.poetry\.group\.[\w\-]+\.dependencies\Z")
_TOML_HEADER = re.compile(r"^\s*\[+\s*([^\]]+?)\s*\]+\s*$", re.M)
#: A PEP 508 requirement string: the name is everything before version, extras, marker or url.
_REQUIREMENT = re.compile(r"\A([A-Za-z][\w\-.]*)")
#: A requirement written as a bare VCS or archive URL, with no name in front of it. Common in
#: older requirements.txt files, and the lines that carry them are usually the internal
#: packages, so dropping them loses exactly what is most worth knowing.
_URL_LINE = re.compile(r"\A(?:[a-z0-9+.\-]+\+)?(?:https?|ssh|git|file)://|\A(?:git|hg|svn|bzr)\+")
_EGG = re.compile(r"[#&]egg=([A-Za-z][\w\-.]*)")


def _names_from_json(text: str) -> list[str]:
    try:
        doc = json.loads(text)
    except ValueError:
        return []
    if not isinstance(doc, dict):
        return []
    out: list[str] = []
    for block in ("dependencies", "devDependencies", "peerDependencies",
                  "require", "require-dev"):
        section = doc.get(block)
        if isinstance(section, dict):
            out += list(section)
    return out


def _array_after(body: str, key: str) -> str:
    """The text inside the TOML array assigned to `key`, brackets balanced.

    Not a regex. A requirement string may itself contain brackets, and `uvicorn[standard]`
    ended a non-greedy match four entries into a sixteen-entry list, which silently dropped
    most of a service's dependencies. Quotes are tracked so a bracket inside one does not
    count.
    """
    m = re.search(rf"^\s*{re.escape(key)}\s*=\s*\[", body, re.M)
    if not m:
        return ""
    depth, quote, start = 1, "", m.end()
    for i in range(start, len(body)):
        ch = body[i]
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return body[start:i]
    return body[start:]


def _toml_sections(text: str):
    """(section name, body) for each table in a TOML file, in order."""
    marks = list(_TOML_HEADER.finditer(text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        yield m.group(1).strip(), text[m.end():end]


def _requirement_names(body: str) -> list[str]:
    """Package names out of every quoted string inside list values in `body`."""
    return [m.group(1) for s in re.findall(r"['\"]([^'\"]+)['\"]", body)
            for m in [_REQUIREMENT.match(s.strip())] if m]


def _names_from_toml(text: str) -> list[str]:
    """Dependency names from a pyproject.toml or Cargo.toml, by table."""
    out: list[str] = []
    for name, body in _toml_sections(text):
        kind = _TOML_SECTIONS.get(name)
        if kind is None and _POETRY_GROUP.match(name):
            kind = "key"
        if kind is None:
            continue
        if name == "project":
            # Only this table's `dependencies` array; its siblings are metadata, and
            # `authors = [{name = "..."}]` would otherwise contribute a package.
            out += _requirement_names(_array_after(body, "dependencies"))
            continue
        if kind == "requirement":
            out += _requirement_names(body)
            continue
        for line in body.splitlines():
            key, sep, _ = line.strip().partition("=")
            key = key.strip().strip("'\"")
            if sep and key and not key.startswith("#"):
                out.append(key)
    return out


def _url_requirement_name(line: str) -> str:
    """The package a bare URL requirement installs.

    `#egg=` names it outright when present. Otherwise the repository is the best available
    answer: `git+ssh://git@host/org/torpedo.git@3.7.3` installs `torpedo`. Without this the
    line matches on its scheme and the service appears to depend on a package called `git`.
    """
    egg = _EGG.search(line)
    if egg:
        return egg.group(1)
    url = line.split("#", 1)[0].split(";", 1)[0].strip()
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    tail = tail.split("@", 1)[0]                  # strip a @tag or @branch ref
    for suffix in (".git", ".zip", ".tar.gz", ".tgz", ".whl"):
        if tail.endswith(suffix):
            tail = tail[: -len(suffix)]
            break
    return tail


def _requirements_names(text: str) -> list[str]:
    """Names from a requirements.txt, including the lines that are only a URL."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        if _URL_LINE.match(line):
            name = _url_requirement_name(line)
            if name:
                out.append(name)
            continue
        m = _REQUIREMENT.match(line)
        if m:
            out.append(m.group(1))
    return out


def _normalise(name: str) -> str:
    """One spelling per package, so a rule can name it once.

    Python treats `-` and `_` as the same character in a distribution name and matches
    case-insensitively; Go and Java use paths and coordinates whose last segment is the name
    anyone would write. Scoped npm names (`@scope/pkg`) keep their scope, because the scope is
    part of the identity.
    """
    name = name.strip().lower()
    if name.startswith("@"):
        return name
    if "/" in name:                       # go module path, composer vendor/package
        name = name.rsplit("/", 1)[-1]
    return name.replace("_", "-")


def declared_dependencies(root: Path) -> list[str]:
    """Every package this repository declares, normalised and deduplicated.

    Reads only the repository root. A manifest further down belongs to a vendored copy or a
    sub-package, and attributing its dependencies to this service would be wrong.
    """
    found: set[str] = set()
    files: list[Path] = []
    for name in _PATTERNS:
        p = root / name
        if p.is_file():
            files.append(p)
    for pattern in _GLOBS:
        files += [p for p in root.glob(pattern) if p.is_file()]

    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if path.name in _JSON_MANIFESTS:
            names = _names_from_json(text)
        elif path.name in ("pyproject.toml", "Cargo.toml"):
            names = _names_from_toml(text)
        elif _PATTERNS.get(path.name) is None:
            names = _requirements_names(text)          # requirements.txt and its variants
        else:
            names = [m.group(1) for m in _PATTERNS[path.name].finditer(text)]
        for raw in names:
            norm = _normalise(raw)
            if norm and not norm.startswith("-"):
                found.add(norm)
    return sorted(found)
