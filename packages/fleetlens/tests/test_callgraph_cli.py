"""Call-graph CLI behaviour."""
from __future__ import annotations

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
