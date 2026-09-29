"""Tests for the language-agnostic symbol indexer (incremental change detection).

Skipped entirely if the optional `symbols` extra (tree-sitter) isn't installed.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_language_pack")

from fleetlens.symbols import build_index, diff_index  # noqa: E402


def _write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")


ROUTES_V1 = '''
    from torpedo import Request

    @app.route("/apps", methods=["POST"])
    async def create_app(request: Request):
        payload = request.json
        return make(payload)

    @app.route("/apps/<app_id>", methods=["GET"])
    async def get_app(request: Request, app_id: str):
        return fetch(app_id)
'''


def test_extracts_symbols_with_ranges_and_hashes(tmp_path):
    _write(tmp_path, "app/routes/app.py", ROUTES_V1)
    idx = build_index(tmp_path)
    syms = idx["symbols"]
    assert "app/routes/app.py::create_app" in syms
    assert "app/routes/app.py::get_app" in syms
    ca = syms["app/routes/app.py::create_app"]
    assert ca["kind"] == "function" and ca["lang"] == "python"
    assert ca["body_hash"] and ca["line_start"] < ca["line_end"]


def test_decorator_change_is_detected(tmp_path):
    # Same body, ONLY the route decorator changes (POST -> PUT). Must change the hash,
    # or an interface change would slip past incremental gating.
    _write(tmp_path, "r.py", ROUTES_V1)
    before = build_index(tmp_path)
    _write(tmp_path, "r.py", ROUTES_V1.replace('methods=["POST"]', 'methods=["PUT"]'))
    after = build_index(tmp_path)
    d = diff_index(before, after)
    assert d["changed"] == ["r.py::create_app"]
    assert d["added"] == [] and d["removed"] == []


def test_editing_one_handler_does_not_touch_its_neighbor(tmp_path):
    _write(tmp_path, "r.py", ROUTES_V1)
    before = build_index(tmp_path)
    # change only create_app's body
    _write(tmp_path, "r.py", ROUTES_V1.replace("return make(payload)",
                                                "return make(payload, extra=True)"))
    after = build_index(tmp_path)
    d = diff_index(before, after)
    assert d["changed"] == ["r.py::create_app"]           # neighbor get_app untouched


def test_add_and_remove(tmp_path):
    _write(tmp_path, "r.py", ROUTES_V1)
    before = build_index(tmp_path)
    _write(tmp_path, "r.py", ROUTES_V1 + '\n    def helper():\n        return 1\n')
    after = build_index(tmp_path)
    d = diff_index(before, after)
    assert d["added"] == ["r.py::helper"] and d["changed"] == [] and d["removed"] == []


def test_line_shift_above_does_not_rehash_below(tmp_path):
    # Adding a symbol above must NOT change the hash of one below (hash is content, not
    # line numbers) — this is why we hash bodies, not diff line ranges.
    _write(tmp_path, "r.py", ROUTES_V1)
    before = build_index(tmp_path)
    h_before = before["symbols"]["r.py::get_app"]["body_hash"]
    _write(tmp_path, "r.py", "\n    def new_top():\n        return 0\n" + ROUTES_V1)
    after = build_index(tmp_path)
    assert after["symbols"]["r.py::get_app"]["body_hash"] == h_before


def test_class_methods_are_qualified(tmp_path):
    _write(tmp_path, "m.py", '''
        class Manager:
            def create(self):
                return 1
            def delete(self):
                return 2
    ''')
    syms = build_index(tmp_path)["symbols"]
    assert "m.py::Manager" in syms
    assert "m.py::Manager.create" in syms and "m.py::Manager.delete" in syms


def test_typescript_arrow_const_handler(tmp_path):
    _write(tmp_path, "h.ts", '''
        export const placeOrder = async (req: Request) => {
          return svc.place(req.body);
        };
        export function ping() { return "ok"; }
    ''')
    syms = build_index(tmp_path)["symbols"]
    assert "h.ts::placeOrder" in syms and syms["h.ts::placeOrder"]["lang"] == "typescript"
    assert "h.ts::ping" in syms


def test_skip_dirs_and_unknown_extensions(tmp_path):
    _write(tmp_path, "app/real.py", "def a():\n    return 1\n")
    _write(tmp_path, "node_modules/x.js", "function b(){return 2;}")
    _write(tmp_path, ".context/_symbols.json", "{}")
    _write(tmp_path, "readme.md", "# not code")
    syms = build_index(tmp_path)["symbols"]
    assert "app/real.py::a" in syms
    assert not any("node_modules" in k for k in syms)
    assert not any(k.endswith(".md") for k in syms)
