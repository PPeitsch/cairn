"""The version and the changelog move together: `__version__` is the latest released section of
CHANGELOG.md, and every section has its compare link."""

import re
from pathlib import Path

from cairn import __version__

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
SECTION_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - (\d{4}-\d{2}-\d{2})$", re.M)


def _released() -> list[str]:
    return [m.group(1) for m in SECTION_RE.finditer(CHANGELOG.read_text(encoding="utf-8"))]


def test_version_is_the_latest_release():
    released = _released()
    assert released, "CHANGELOG.md has no released section"
    assert __version__ == released[0], \
        f"__version__ is {__version__} but the latest section of CHANGELOG.md is {released[0]}"


def test_releases_are_in_order_and_linked():
    text = CHANGELOG.read_text(encoding="utf-8")
    released = _released()
    as_tuples = [tuple(map(int, v.split("."))) for v in released]
    assert as_tuples == sorted(as_tuples, reverse=True) and len(set(as_tuples)) == len(as_tuples)
    assert "## [Unreleased]" in text
    for v in ["Unreleased", *released]:
        assert re.search(rf"^\[{re.escape(v)}\]: https://", text, re.M), f"no link for [{v}]"
