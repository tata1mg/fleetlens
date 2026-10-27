"""Call-graph CLI behaviour."""
from __future__ import annotations

import json

import pytest


def test_indexer_failure_is_one_line_with_detail_on_disk(tmp_path, monkeypatch):
    """scip-python fails with a ~25-line Node stack trace. Inline, once per repo, that
    buries the progress output and the summary under it during a fleet sweep."""
    import subprocess

    from fleetlens.callgraph import cli as cg

    monkeypatch.setattr(cg.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(cg.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a[0], 1, "", "boom: the real cause\n" + "\n".join(f"  at frame {i}" for i in range(24))))

    with pytest.raises(RuntimeError) as err:
        cg._run_indexer("python", tmp_path, tmp_path / "out.scip", "svc")

    msg = str(err.value)
    assert len(msg.splitlines()) == 1                  # one line, not a stack trace
    assert "boom: the real cause" in msg               # and it is the useful line
    log = tmp_path / ".context" / "scip-python-error.log"
    assert log.exists() and "at frame 23" in log.read_text()   # detail kept, not lost


def test_a_stale_symbol_file_is_not_silently_reused(tmp_path, monkeypatch):
    """`.context/_symbols.json` used to be trusted on sight.

    Nothing writes it during a normal index, so it only exists where someone ran
    `fl-symbols` by hand. From then on every index of that repository reported spans
    frozen at that moment while still recording the current commit beside them, which is
    how line numbers end up describing a version of a file nobody can see.
    """
    from fleetlens.callgraph import cli as cg

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("def real_function():\n    return 1\n")
    ctx = tmp_path / ".context"
    ctx.mkdir()
    (ctx / "_symbols.json").write_text(json.dumps({
        "schema": "symbols/v1", "root": tmp_path.name, "file_count": 1, "symbol_count": 1,
        "symbols": {"app/gone.py::a_function_deleted_last_month": {
            "path": "app/gone.py", "lang": "python", "kind": "function",
            "line_start": 999, "line_end": 1001, "body_hash": "deadbeef"}},
    }))

    index = cg._symbol_index(tmp_path, ctx)
    names = set(index.get("symbols") or {})
    assert "app/main.py::real_function" in names, "the repo should have been parsed"
    assert "app/gone.py::a_function_deleted_last_month" not in names

    # Reuse stays possible, but only when a caller asks for it by name and owns the choice.
    reused = cg._symbol_index(tmp_path, ctx, str(ctx / "_symbols.json"))
    assert "app/gone.py::a_function_deleted_last_month" in (reused.get("symbols") or {})


def test_a_failed_run_leaves_no_call_graph_behind(tmp_path, monkeypatch):
    """A failure used to leave the previous run's file in place.

    `_load_service` loads whatever is in `.context/` without consulting the exit status,
    so the last successful build's spans would be loaded into a fresh index and attached
    to today's commit. Leaving nothing is the honest outcome: that repository has no call
    graph this build, which the run already reports.
    """
    from fleetlens.callgraph import cli as cg

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("def f():\n    return 1\n")
    ctx = tmp_path / ".context"
    ctx.mkdir()
    stale = ctx / "callgraph.json"
    stale.write_text('{"schema": "callgraph/v1", "nodes": [{"id": "from-last-month"}]}')

    def boom(*a, **kw):
        raise RuntimeError("scip-python failed (exit 1)")

    monkeypatch.setattr(cg, "_run_indexer", boom)
    rc = cg.main([str(tmp_path), "--language", "python", "--quiet"])

    assert rc == 2
    assert not stale.exists(), "a failed run must not leave last month's spans readable"
