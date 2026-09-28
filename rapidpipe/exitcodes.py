"""The one exit-code vocabulary for every rapidpipe entrypoint.

:class:`ExitCode` is the single set of process exit codes that the ``rapid``
command line, the ``python -m rapidpipe.release`` hook runner, the selftest
harness and every stage entrypoint return. Values 64 to 75 are the stage
contract's codes (sysexits-derived); 0, 1 and 2 are the operator-facing
outcomes. A stage may return only the subset
:data:`rapidpipe.stages.contract.STAGE_EXIT_CODES`; the command line may
return any member. The ``rapid_docs`` tool page carries the table that
documents this vocabulary, and the stage-contract page carries the stage
subset; they describe this module, they do not define a second list.

:class:`ArgumentParser` is ``argparse.ArgumentParser`` with its parse
failures mapped to :attr:`ExitCode.USAGE` (64) instead of argparse's 2, so
that 2 keeps its one meaning (still running). Subparsers inherit the class
through argparse's ``parser_class=type(self)`` default.

:class:`CommandExit` is the exception a command's code raises to stop the
command with a given exit code and a one-line message; the command-line
tool catches it at the command boundary, prints the message to stderr and
exits with the code.

This module is stdlib-only and imports nothing from ``rapidpipe``, so any
subpackage, science modules included, may import it.
"""

from __future__ import annotations

import argparse
import sys
from enum import IntEnum
from typing import NoReturn

__all__ = ["ArgumentParser", "CommandExit", "ExitCode"]


class ExitCode(IntEnum):
    """Every exit code a rapidpipe process returns, and the caller's action.

    Do not renumber or add to this list without changing the ``rapid_docs``
    tool page's table first.
    """

    SUCCESS = 0
    """Success; for a stage, its manifest is published. Caller's action:
    none."""

    FAILURE = 1
    """A negative outcome, not an error in the tool: a check failed, a run
    or unit failed or was cancelled, two runs differ, a release was refused
    or one of its hooks failed. Caller's action: read the output; do not
    retry blindly."""

    INCOMPLETE = 2
    """Not yet terminal: the run is still running (``rapid run status``
    without ``--watch``). Caller's action: poll again later."""

    USAGE = 64
    """Bad arguments, invalid settings or configuration, missing
    environment, or an unmet precondition; also every argparse parse
    failure. Caller's action: fix the invocation, no retry."""

    INPUT_REJECTED = 65
    """A declared input was absent, corrupt or incompatible once its
    storage was reached. Caller's action: fail, no retry."""

    NOT_IMPLEMENTED = 69
    """Declared, not implemented in this build: the stage has a real
    declaration and validates its arguments, settings and input manifest
    like any other stage, but its science has not been ported yet
    (sysexits' EX_UNAVAILABLE; chosen over 64, which would misreport a
    correct invocation as a usage error, and 70, which calls for
    investigation of something unexpected, when the absence is deliberate
    and known). Caller's action: fail, no retry."""

    STAGE_ERROR = 70
    """Unclassified error (an unhandled exception); stop for investigation.
    Caller's action: fail, no retry."""

    TRANSIENT_FAILURE = 75
    """A recognised temporary dependency failure; repeating the same work
    may succeed. Caller's action: retry within the limit."""


class ArgumentParser(argparse.ArgumentParser):
    """``argparse.ArgumentParser`` whose parse failures exit 64 (USAGE).

    The message shape is argparse's own (usage, then ``prog: error: msg``
    on stderr); only the exit status differs. ``--help`` still exits 0.
    """

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(int(ExitCode.USAGE), f"{self.prog}: error: {message}\n")


class CommandExit(Exception):
    """Stop the command with ``code``, printing ``message`` to stderr."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
