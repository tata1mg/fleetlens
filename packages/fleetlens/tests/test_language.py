"""Language auto-detection + indexer registry (TypeScript wiring)."""
from __future__ import annotations

from fleetlens.callgraph.cli import _INDEXERS
from fleetlens.indexing import detect_language


def test_typescript_indexer_registered():
    assert "typescript" in _INDEXERS and _INDEXERS["typescript"]["tool"] == "scip-typescript"
    assert "python" in _INDEXERS


def test_detect_typescript_by_tsconfig(tmp_path):
    (tmp_path / "tsconfig.json").write_text("{}")
    assert detect_language(tmp_path) == "typescript"


def test_detect_typescript_by_package_json_and_ts(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "index.ts").write_text("export const x = 1;")
    assert detect_language(tmp_path) == "typescript"


def test_detect_python_default(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert detect_language(tmp_path) == "python"
    assert detect_language(tmp_path / "nonexistent" if False else tmp_path) == "python"
