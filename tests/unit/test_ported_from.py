"""The `ported-from` guard: every module under ``rapidpipe/science`` and
``rapidpipe/stages``, plus the two settings tomls that mirror dev's
science .ini, must carry a well-formed ``# ported-from: ...`` header
within its first 5 lines (AGENTS.md), so a new module cannot land
unpinned. ``scripts/science-drift.sh`` reads the same headers to build
its drift report; this test only checks their shape, never git.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCIENCE_DIR = REPO_ROOT / "rapidpipe" / "science"
STAGES_DIR = REPO_ROOT / "rapidpipe" / "stages"
SETTINGS_TOMLS = (
    REPO_ROOT / "rapidpipe" / "settings" / "difference.toml",
    REPO_ROOT / "rapidpipe" / "settings" / "reference.toml",
)

#: "# ported-from: none" or "# ported-from: <path>[, <path>...] @ <8-hex>".
PORTED_FROM_RE = re.compile(
    r"^# ported-from: (none|[A-Za-z0-9_./-]+(, [A-Za-z0-9_./-]+)* @ [0-9a-f]{8})$")


def _guarded_py_files():
    for subdir in (SCIENCE_DIR, STAGES_DIR):
        yield from sorted(subdir.rglob("*.py"))


def _first_lines(path: Path, n: int = 5) -> list[str]:
    with path.open(encoding="utf-8") as fh:
        return [next(fh, "").rstrip("\n") for _ in range(n)]


def _header_line(path: Path) -> str | None:
    for line in _first_lines(path):
        if line.startswith("# ported-from:"):
            return line
    return None


@pytest.mark.parametrize("path", list(_guarded_py_files()), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_every_science_and_stage_module_has_a_ported_from_header(path):
    header = _header_line(path)
    assert header is not None, (
        f"{path.relative_to(REPO_ROOT)} has no '# ported-from:' line in "
        "its first 5 lines")
    assert PORTED_FROM_RE.match(header), (
        f"{path.relative_to(REPO_ROOT)} has a malformed ported-from "
        f"header: {header!r}")


@pytest.mark.parametrize("path", SETTINGS_TOMLS, ids=lambda p: p.name)
def test_each_settings_toml_has_a_ported_from_header(path):
    header = _header_line(path)
    assert header is not None, (
        f"{path.relative_to(REPO_ROOT)} has no '# ported-from:' line in "
        "its first 5 lines")
    assert PORTED_FROM_RE.match(header), (
        f"{path.relative_to(REPO_ROOT)} has a malformed ported-from "
        f"header: {header!r}")


def test_the_guard_covers_every_file_on_disk():
    """A sanity check on the parametrization above: if a future rglob
    change (or a file rename) shrank the collected file list to zero or
    dropped the package's known shape, the parametrized tests above
    would just quietly stop running rather than fail."""
    py_files = list(_guarded_py_files())
    assert len(py_files) >= 50, (
        f"expected at least 50 science/stage modules, found {len(py_files)}; "
        "the file discovery in this test may be broken")
    for path in SETTINGS_TOMLS:
        assert path.is_file(), f"{path} is missing"
