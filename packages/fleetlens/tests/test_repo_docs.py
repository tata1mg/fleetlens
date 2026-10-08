"""What a repository claims about itself: README opening + manifest description."""
from __future__ import annotations

import json

from fleetlens.enrich.repo_docs import declared, read_description, read_readme


def test_reads_the_opening_and_stops_at_the_setup_guide(tmp_path):
    (tmp_path / "README.md").write_text(
        "# Orders\n\nHolds the order lifecycle for pharmacy fulfilment.\n\n"
        "## Installation\n\npip install orders\n")
    prose, name = read_readme(tmp_path, 1500)
    assert "pharmacy fulfilment" in prose
    assert "pip install" not in prose
    assert name == "README.md"


def test_stop_headings_match_however_they_are_spelled(tmp_path):
    for heading in ("## Pre-requisites", "## PREREQUISITES", "## 2. Installation",
                    "### Getting  Started", "## Set up"):
        (tmp_path / "README.md").write_text(f"# T\n\nkeepme\n\n{heading}\n\ndropme\n")
        prose, _ = read_readme(tmp_path, 1500)
        assert "keepme" in prose, heading
        assert "dropme" not in prose, heading


def test_badges_and_html_comments_do_not_eat_the_budget(tmp_path):
    (tmp_path / "README.md").write_text(
        "# T\n\n[![build](https://img.shields.io/x)](https://ci)\n"
        "<!-- a note to maintainers -->\n\nthe real description\n")
    prose, _ = read_readme(tmp_path, 1500)
    assert "the real description" in prose
    assert "shields.io" not in prose
    assert "maintainers" not in prose


def test_badge_filter_does_not_backtrack_on_hostile_lines(tmp_path):
    """README text is untrusted; a near-badge line must fail fast, not hang the run."""
    import time
    hostile = "[![" + "]()](![" * 5000 + "\n"
    row = "[![a](https://img.shields.io/a)](https://x) [![b](https://img.shields.io/b)](https://y)\n"
    (tmp_path / "README.md").write_text("# T\n\n" + row + hostile + "\nkept prose\n")
    started = time.monotonic()
    prose, _ = read_readme(tmp_path, 100_000)
    assert time.monotonic() - started < 1.0
    assert "shields.io" not in prose
    assert "kept prose" in prose


def test_a_readme_that_is_only_a_setup_guide_yields_nothing(tmp_path):
    """Better no claim than a misleading one."""
    (tmp_path / "README.md").write_text("## Prerequisites\n\n- python 3.9\n- postgres\n")
    assert read_readme(tmp_path, 1500)[0] == ""


def test_missing_readme_is_not_an_error(tmp_path):
    assert read_readme(tmp_path, 1500) == ("", "")
    assert declared(str(tmp_path), 1500) == ([], "")
    assert declared("/no/such/path", 1500) == ([], "")
    assert declared("", 1500) == ([], "")


def test_description_from_each_ecosystem(tmp_path):
    cases = [
        ("package.json", json.dumps({"description": "node thing"}), "node thing"),
        ("pyproject.toml", 'description = "python thing"\n', "python thing"),
        ("Cargo.toml", 'description = "rust thing"\n', "rust thing"),
        ("pom.xml", "<description>java thing</description>", "java thing"),
        ("x.gemspec", 's.summary = "ruby thing"\n', "ruby thing"),
    ]
    for name, body, expected in cases:
        d = tmp_path / name.replace(".", "_")
        d.mkdir()
        (d / name).write_text(body)
        assert read_description(d)[0] == expected, name


def test_readme_is_labelled_as_a_claim_not_a_fact(tmp_path):
    (tmp_path / "README.md").write_text("# T\n\nwhat it does\n")
    lines, _ = declared(str(tmp_path), 1500)
    head = lines[0].lower()
    assert "may be out of date" in head and "observed surface" in head


def test_editing_the_readme_changes_the_hash_input(tmp_path):
    (tmp_path / "README.md").write_text("# T\n\nfirst\n")
    before = declared(str(tmp_path), 1500)[1]
    (tmp_path / "README.md").write_text("# T\n\nsecond\n")
    assert declared(str(tmp_path), 1500)[1] != before


def test_zero_limit_turns_the_readme_off(tmp_path):
    (tmp_path / "README.md").write_text("# T\n\nwhat it does\n")
    (tmp_path / "package.json").write_text(json.dumps({"description": "kept"}))
    lines, _ = declared(str(tmp_path), 0)
    assert any("kept" in ln for ln in lines)
    assert not any("what it does" in ln for ln in lines)
