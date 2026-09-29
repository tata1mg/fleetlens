"""`fl doctor`: check that this machine can index, and that a given repo can be indexed.

Setup spans several ecosystems (Python for fleetlens, Node for the SCIP indexers, optionally
Ollama for the LLM tier), so a failure usually surfaces far from its cause. The worst example
is `scip-python`, which exits with an unreadable node stack trace when the target repository
has no virtualenv. This reports that directly instead.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

OK, FAIL, WARN, SKIP = "ok", "fail", "warn", "skip"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""


@dataclass
class Report:
    checks: list = field(default_factory=list)

    def add(self, *a, **kw):
        self.checks.append(Check(*a, **kw))

    @property
    def failed(self) -> list:
        return [c for c in self.checks if c.status == FAIL]


def _has(tool: str) -> bool:
    return shutil.which(tool) is not None


def _version(tool: str, *args: str) -> str:
    try:
        r = subprocess.run([tool, *args], capture_output=True, text=True, timeout=20)
        return (r.stdout or r.stderr).strip().splitlines()[0][:60]
    except Exception:  # noqa: BLE001
        return ""


def check_core(rep: Report) -> None:
    v = sys.version_info
    rep.add("python", OK if v >= (3, 10) else FAIL,
            f"{v.major}.{v.minor}.{v.micro}",
            "" if v >= (3, 10) else "fleetlens needs Python 3.10 or newer")

    try:
        import yaml  # noqa: F401
        rep.add("pyyaml", OK, "installed")
    except ImportError:
        rep.add("pyyaml", WARN, "missing",
                "pip install pyyaml   (only needed for fleetlens.yaml manifests)")

    try:
        import tree_sitter_language_pack  # noqa: F401
        rep.add("tree-sitter", OK, "installed")
    except ImportError:
        rep.add("tree-sitter", FAIL, "missing",
                'pip install "fleetlens[callgraph]"   (needed to index symbols)')

    try:
        import mcp  # noqa: F401
        rep.add("mcp", OK, "installed")
    except ImportError:
        rep.add("mcp", WARN, "missing",
                'pip install "fleetlens[server]"   (needed for `fl serve`)')


def check_indexers(rep: Report) -> None:
    for tool, language, required in (("scip-python", "Python", True),
                                     ("scip-typescript", "TypeScript", False),
                                     ("scip-ruby", "Ruby", False)):
        if _has(tool):
            rep.add(tool, OK, f"{language} call graph available")
        elif required:
            rep.add(tool, WARN, f"missing, no {language} call graph",
                    f"npm install -g @sourcegraph/{tool}")
        else:
            note = ("needs a Sorbet setup; routes and queues work without it"
                    if tool == "scip-ruby" else "")
            rep.add(tool, SKIP, f"missing, no {language} call graph. {note}".strip(),
                    f"npm install -g @sourcegraph/{tool}" if tool != "scip-ruby" else
                    "gem install scip-ruby")


def check_ollama(rep: Report, base_url: str = "http://localhost:11434") -> None:
    try:
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=5) as r:
            models = [m.get("name", "") for m in json.loads(r.read()).get("models", [])]
    except (urllib.error.URLError, OSError, ValueError):
        rep.add("ollama", SKIP, "not reachable, LLM tiers unavailable",
                "ollama serve   (only needed for `--fill-gaps` and `fl enrich`)")
        return
    rep.add("ollama", OK, f"{len(models)} model(s) installed")
    if not any("bge" in m or "embed" in m for m in models):
        rep.add("embedding model", SKIP, "none found, semantic discovery unavailable",
                "ollama pull bge-m3")


def check_repo(rep: Report, repo: Path) -> None:
    """Whether this specific repository can be indexed."""
    repo = Path(repo).resolve()
    if not repo.is_dir():
        rep.add("repo", FAIL, f"not a directory: {repo}")
        return

    from .indexing import detect_language
    lang = detect_language(repo)
    rep.add("repo language", OK, f"{repo.name} detected as {lang}")

    if lang == "python":
        # the failure that is hardest to diagnose from scip-python's own output
        venvs = [d for d in ("venv", ".venv", "env", ".env")
                 if (repo / d / "bin" / "python").exists() or (repo / d / "Scripts").exists()]
        if venvs:
            rep.add("repo virtualenv", OK, f"found {venvs[0]}/")
        else:
            rep.add("repo virtualenv", WARN,
                    "none found; scip-python cannot resolve imports and will fail",
                    f"create one in {repo.name} and install its dependencies, "
                    "or index anyway to get interfaces and queues without a call graph")
    elif lang == "ruby":
        if (repo / "config" / "routes.rb").exists():
            n = len(list((repo / "config" / "routes").glob("*.rb"))) if \
                (repo / "config" / "routes").is_dir() else 0
            rep.add("rails routes", OK,
                    f"config/routes.rb found, plus {n} file(s) in config/routes/")
        else:
            rep.add("rails routes", WARN, "no config/routes.rb, no HTTP interfaces will be found")
    elif lang == "typescript":
        if (repo / "tsconfig.json").exists():
            rep.add("tsconfig", OK, "found")
        else:
            rep.add("tsconfig", WARN, "missing; scip-typescript will infer one")

    if (repo / ".contextignore").exists():
        rep.add("contextignore", OK, "repo has its own .contextignore")

    sensitive = [p.name for p in repo.glob(".env*")][:3]
    if sensitive:
        rep.add("secrets", OK,
                f"{', '.join(sensitive)} present and excluded by the default ignore rules")


def run(repo: Optional[Path] = None, base_url: str = "http://localhost:11434") -> Report:
    rep = Report()
    check_core(rep)
    check_indexers(rep)
    check_ollama(rep, base_url)
    if repo is not None:
        check_repo(rep, repo)
    return rep


_MARK = {OK: "ok  ", FAIL: "FAIL", WARN: "warn", SKIP: "--  "}


def render(rep: Report) -> str:
    lines = []
    for c in rep.checks:
        lines.append(f"  [{_MARK[c.status]}] {c.name:<18} {c.detail}")
        if c.fix and c.status in (FAIL, WARN, SKIP):
            lines.append(f"           {c.fix}")
    if rep.failed:
        lines.append("")
        lines.append(f"{len(rep.failed)} blocking problem(s). fleetlens cannot index yet.")
    else:
        lines.append("")
        lines.append("Core checks passed. Anything marked -- is optional.")
    return "\n".join(lines)
