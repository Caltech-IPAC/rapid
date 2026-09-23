"""Running external tools: a thin, injectable wrapper around subprocess.

`dev`'s ``execute_command`` and ``execute_command_in_shell``
(``modules/utils/rapid_pipeline_subs.py``) run a tool with stdout and
stderr merged, print the arguments, the return code and the output, and
return the return code; `dev`'s pipeline never checks it, so a tool that
fails surfaces later as a missing or unreadable output file. The port
keeps that: :meth:`ToolRunner.run` logs and returns the code, and the
caller decides, as `dev` does, whether to look at it (only SFFT's is
looked at).

Tests pass a fake with the same two methods; nothing else in the science
package touches subprocess.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)


class ToolRunner:
    """Runs an external tool in a given working directory."""

    def run(self, args: Sequence[str], *, cwd: Path) -> int:
        """`dev` ``execute_command``: run ``args``, return the exit code."""
        args = [str(a) for a in args]
        logger.info("execute_command: code_to_execute_args = %s", args)
        completed = subprocess.run(
            args, cwd=str(cwd), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        logger.info("returncode = %s", completed.returncode)
        logger.info("code_to_execute_stdout =\n%s", completed.stdout)
        return completed.returncode

    def run_shell(self, command: str, *, cwd: Path) -> int:
        """`dev` ``execute_command_in_shell``: run a bash command string."""
        logger.info("execute_command: bash_command = %s", command)
        completed = subprocess.run(
            command, cwd=str(cwd), shell=True, executable="/bin/bash",
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        logger.info("returncode = %s", completed.returncode)
        logger.info("code_to_execute_stdout =\n%s", completed.stdout)
        return completed.returncode
