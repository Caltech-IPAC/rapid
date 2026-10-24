# ported-from: modules/utils/rapid_pipeline_subs.py @ d6aac515
"""Running external tools: a thin, injectable wrapper around subprocess.

`dev`'s ``execute_command`` and ``execute_command_in_shell``
(``modules/utils/rapid_pipeline_subs.py``) run a tool with stdout and
stderr merged, print the arguments, the return code and the output, and
return the return code; `dev`'s pipeline never checks it, so a tool that
fails surfaces later as a missing or unreadable output file, or not at
all when it left a partial one. The port diverges: :meth:`ToolRunner.run`
and :meth:`ToolRunner.run_shell` raise :class:`ToolFailed` on a nonzero
exit unless the caller passes ``check=False``. No stage passes it: SFFT,
the one tool `dev` carries on past (with ZOGY), is the published
differencer since the differencer switch, so its failure fails the job.

Tests pass a fake with the same two methods; nothing else in the science
package touches subprocess.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)

#: How many of a failed tool's last output lines its :class:`ToolFailed` carries.
TAIL_LINES = 20


class ToolFailed(RuntimeError):
    """An external tool exited nonzero: its name, exit code and last output lines."""

    def __init__(self, tool: str, returncode: int, output: str):
        self.tool, self.returncode = tool, returncode
        self.tail = output.splitlines()[-TAIL_LINES:]
        super().__init__(f"{tool} exited with code {returncode}; its last "
                         f"{len(self.tail)} output lines:\n" + "\n".join(self.tail))


def _tool_name(args: Sequence[str]) -> str:
    """The program's name, and the script's when the program is a Python interpreter (ZOGY)."""
    name = Path(args[0]).name
    if name.startswith("python") and len(args) > 1:
        name += " " + Path(args[1]).name
    return name


class ToolRunner:
    """Runs an external tool in a given working directory."""

    def run(self, args: Sequence[str], *, cwd: Path, check: bool = True) -> int:
        """`dev` ``execute_command``: run ``args``, return the exit code; nonzero raises unless ``check`` is false."""
        args = [str(a) for a in args]
        logger.info("execute_command: code_to_execute_args = %s", args)
        completed = subprocess.run(
            args, cwd=str(cwd), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        logger.info("returncode = %s", completed.returncode)
        logger.info("code_to_execute_stdout =\n%s", completed.stdout)
        if check and completed.returncode != 0:
            raise ToolFailed(_tool_name(args), completed.returncode, completed.stdout)
        return completed.returncode

    def run_shell(self, command: str, *, cwd: Path, check: bool = True) -> int:
        """`dev` ``execute_command_in_shell``: run a bash command string, as :meth:`run`."""
        logger.info("execute_command: bash_command = %s", command)
        completed = subprocess.run(
            command, cwd=str(cwd), shell=True, executable="/bin/bash",
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        logger.info("returncode = %s", completed.returncode)
        logger.info("code_to_execute_stdout =\n%s", completed.stdout)
        if check and completed.returncode != 0:
            raise ToolFailed(_tool_name(command.split()), completed.returncode, completed.stdout)
        return completed.returncode
