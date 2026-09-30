"""The shared pruned walk.

Indexing cost used to scale with whatever happened to be in the working tree rather than
with the size of the project, because `rglob` descends everywhere and the skip list was
applied to its results. These pin the pruning that replaced it.
"""
from __future__ import annotations

from fleetlens.adapters._walk import COMMON_SKIP, iter_files


def test_dependency_trees_are_not_entered(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("x = 1\n")

    # the shapes that actually appear: both virtualenv spellings and a node tree
    for junk in ("venv", ".venv", "node_modules", "__pycache__", ".git"):
        d = tmp_path / junk / "deep" / "deeper"
        d.mkdir(parents=True)
        (d / "buried.py").write_text("x = 1\n")

    found = [p.name for p in iter_files(tmp_path, (".py",))]
    assert found == ["main.py"]


def test_bare_venv_is_skipped_like_dotvenv(tmp_path):
    """`python -m venv venv` is the default spelling, and two adapters listed only the
    dotted form, so the common case was the one that leaked."""
    assert "venv" in COMMON_SKIP and ".venv" in COMMON_SKIP


def test_only_requested_suffixes_come_back(tmp_path):
    for name in ("a.py", "b.ts", "c.tsx", "d.rb", "e.md"):
        (tmp_path / name).write_text("")
    assert {p.name for p in iter_files(tmp_path, (".ts", ".tsx"))} == {"b.ts", "c.tsx"}


def test_order_is_stable(tmp_path):
    """Interface ids are derived from discovery order, so two runs over one tree must not
    disagree about it."""
    for d in ("z", "a", "m"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "f.py").write_text("")
    (tmp_path / "top.py").write_text("")
    once = [str(p) for p in iter_files(tmp_path, (".py",))]
    assert once == [str(p) for p in iter_files(tmp_path, (".py",))]
    assert len(once) == 4


def test_a_symlink_loop_does_not_hang(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("x = 1\n")
    (tmp_path / "app" / "loop").symlink_to(tmp_path)      # points back at the root
    assert [p.name for p in iter_files(tmp_path, (".py",))] == ["main.py"]


def test_an_unreadable_directory_does_not_abort_the_walk(tmp_path):
    (tmp_path / "ok.py").write_text("x = 1\n")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "hidden.py").write_text("x = 1\n")
    locked.chmod(0o000)
    try:
        assert [p.name for p in iter_files(tmp_path, (".py",))] == ["ok.py"]
    finally:
        locked.chmod(0o755)
