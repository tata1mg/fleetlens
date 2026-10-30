"""Every install command in the docs must pin the version this tree actually is.

The docs tell people to install from a git tag. Those pins are moved by hand at release
time, in several files, and a pin left behind sends readers to an older build while the
page around it describes the new one. That is invisible in review: a stale tag looks
exactly like a current one.

Version and pins move together in the release commit, so between releases they agree and
this test is quiet. It speaks up for the release that forgets a file.
"""
from __future__ import annotations

import re
from pathlib import Path

from fleetlens import __version__

ROOT = Path(__file__).resolve().parents[3]

#: `@v1.2.3` in a github.com/tata1mg/fleetlens install URL.
PIN = re.compile(r"fleetlens\.git@v(\d+\.\d+\.\d+)")

#: The changelog records what past releases pinned, so its old tags are correct history.
SKIP = {"CHANGELOG.md"}


def _docs() -> list[Path]:
    seen = {p for p in ROOT.glob("*.md")}
    seen |= set((ROOT / "docs").glob("*.md"))
    seen |= set((ROOT / "packages" / "fleetlens").glob("*.md"))
    return sorted(p for p in seen if p.name not in SKIP)


def test_every_documented_install_pins_this_version():
    stale = []
    for path in _docs():
        for found in PIN.findall(path.read_text()):
            if found != __version__:
                stale.append(f"{path.relative_to(ROOT)}: @v{found}")
    assert not stale, (
        f"install pins disagree with __version__ ({__version__}):\n  "
        + "\n  ".join(stale)
        + "\n\nmove them in the release commit, or correct __version__.")


def test_the_pins_exist_at_all():
    """Guards the test above, which passes trivially if the regex stops matching."""
    found = {p.relative_to(ROOT).as_posix() for p in _docs() if PIN.search(p.read_text())}
    assert len(found) >= 3, f"expected the documented install commands, found {found}"
