"""``ToolRunner``: an external tool's nonzero exit is a named failure unless the caller opts out.

Contracts: a nonzero exit raises ``ToolFailed`` carrying the tool's name and
exit code and only the last ``TAIL_LINES`` lines of its merged output, its
stderr included; ``check=False`` returns the code instead; a zero exit
returns 0; ``run_shell`` behaves the same.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from rapidpipe.science.difference.tools import TAIL_LINES, ToolFailed, ToolRunner


def fake_tool(tmp_path: Path, code: int, lines: int = 3) -> Path:
    """A shell script printing ``lines`` numbered lines, the last to stderr, then exiting ``code``."""
    tool = tmp_path / "faketool"
    tool.write_text("#!/bin/sh\n"
                    f"i=1; while [ $i -lt {lines} ]; do echo \"line $i\"; i=$((i+1)); done\n"
                    f"echo \"line {lines}\" >&2\n"
                    f"exit {code}\n")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return tool


def test_a_nonzero_exit_raises_naming_the_tool_and_its_code(tmp_path):
    with pytest.raises(ToolFailed) as failed:
        ToolRunner().run([fake_tool(tmp_path, 3), "-x"], cwd=tmp_path)
    assert (failed.value.tool, failed.value.returncode) == ("faketool", 3)
    assert "faketool exited with code 3" in str(failed.value)


def test_the_failure_carries_only_the_last_output_lines_stderr_included(tmp_path):
    lines = TAIL_LINES + 5
    with pytest.raises(ToolFailed) as failed:
        ToolRunner().run([fake_tool(tmp_path, 1, lines)], cwd=tmp_path)
    shown = str(failed.value).splitlines()
    assert f"line {lines}" in shown and "line 1" not in shown
    assert len(failed.value.tail) == TAIL_LINES


def test_check_false_returns_the_code(tmp_path):
    assert ToolRunner().run([fake_tool(tmp_path, 2)], cwd=tmp_path, check=False) == 2


def test_a_zero_exit_returns_zero(tmp_path):
    assert ToolRunner().run([fake_tool(tmp_path, 0)], cwd=tmp_path) == 0


def test_run_shell_checks_the_same_way(tmp_path):
    tool = fake_tool(tmp_path, 4)
    with pytest.raises(ToolFailed) as failed:
        ToolRunner().run_shell(f"{tool} -x", cwd=tmp_path)
    assert (failed.value.tool, failed.value.returncode) == ("faketool", 4)
    assert ToolRunner().run_shell(f"{tool} -x", cwd=tmp_path, check=False) == 4
