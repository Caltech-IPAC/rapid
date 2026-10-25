"""Logging for rapidpipe: UTC formatter, run/attempt/stage/unit on every line.

Ported from ``origin/smdc:pipeline/runtime/logging_setup.py`` -- the UTC
formatter and identifiers on every line are kept; the old
attempt schema this module imported (``job_id`` naming a Batch job) is
dropped.

:func:`configure_root` configures Python's real root logger with this
module's line shape, for ``rapidpipe.cli.main``, so a library's bare
``logging.getLogger(__name__).warning(...)`` gets it instead of Python's
last-resort handler (no timestamp, no run context). The console handler
writes to stderr (a command whose stdout carries data logs to stderr, the
project's Unix convention).
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any

# UTC everywhere, per the ported module's rationale: `logging`'s own
# `asctime` is local by default, which is how a log ends up with two
# timezones in it after someone changes an instance's TZ.
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

LOG_FORMAT = (
    "%(asctime)s.%(msecs)03dZ %(levelname)s "
    "run=%(run_id)s attempt=%(attempt_id)s stage=%(stage)s unit=%(unit)s "
    "%(name)s %(message)s"
)

DEFAULT_LEVEL = logging.INFO

#: The environment variable a caller may set to override the default log
#: level: ``configure_root`` (the CLI) defaults to INFO for rapidpipe's own
#: loggers and WARNING for every other library's; this variable, when set,
#: wins for all of them.
LEVEL_ENV_VAR = "RAPIDPIPE_LOG_LEVEL"

#: The root logger name for rapidpipe. Every module's
#: `logging.getLogger(__name__)` under `rapidpipe.` is a descendant, so one
#: handler set serves all of them.
ROOT_LOGGER_NAME = "rapidpipe"


class _UTCFormatter(logging.Formatter):
    """A formatter whose `asctime` is UTC, whatever the host's timezone."""

    converter = time.gmtime


class _IdentityFilter(logging.Filter):
    """Guarantees `run_id`/`attempt_id`/`stage`/`unit` exist on every record.

    Every caller logs through a plain `logging.getLogger(__name__)`, and
    the format string names those fields: a record lacking them would raise inside `logging` and be
    swallowed by its error handling, which would silently drop log lines.
    """

    def __init__(self, run_id: str = "-", attempt_id: str = "-",
                 stage: str = "-", unit_id: str = "-"):
        super().__init__()
        self.run_id = run_id
        self.attempt_id = attempt_id
        self.stage = stage
        self.unit_id = unit_id

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "run_id"):
            record.run_id = self.run_id
        if not hasattr(record, "attempt_id"):
            record.attempt_id = self.attempt_id
        if not hasattr(record, "stage"):
            record.stage = self.stage
        if not hasattr(record, "unit"):
            record.unit = self.unit_id
        return True


def _coerce_level(level: int | str) -> int:
    if isinstance(level, str):
        resolved = logging.getLevelName(level.upper())
        if not isinstance(resolved, int):
            raise ValueError(
                f"{level!r} is not a logging level; expected one of "
                "DEBUG, INFO, WARNING, ERROR, CRITICAL")
        return resolved
    return level


def resolve_level(level: int | str | None, default: int) -> int:
    """The effective level: ``level`` if given, else ``RAPIDPIPE_LOG_LEVEL``
    if set, else ``default``."""
    if level is not None:
        return _coerce_level(level)
    env_value = os.environ.get(LEVEL_ENV_VAR)
    if env_value:
        return _coerce_level(env_value)
    return default


def _make_handler(*, stream: Any, level: int, run_id: str, attempt_id: str,
                   stage: str, unit_id: str) -> logging.Handler:
    handler = logging.StreamHandler(stream)
    handler.setLevel(level)
    handler.setFormatter(_UTCFormatter(LOG_FORMAT, datefmt=_DATE_FORMAT))
    handler.addFilter(_IdentityFilter(
        run_id=str(run_id), attempt_id=str(attempt_id),
        stage=str(stage), unit_id=str(unit_id)))
    handler._rapidpipe_owned = True
    return handler


def configure_root(level: int | str | None = None, stream: Any = None) -> None:
    """Configure Python's real root logger with the same UTC line shape.

    For ``rapidpipe.cli.main``: without this, a third-party library's
    plain ``logging.getLogger(__name__).warning(...)`` falls through to
    Python's last-resort handler -- one line to stderr with no timestamp and no
    run context. ``level`` defaults to ``RAPIDPIPE_LOG_LEVEL`` if set,
    else ``logging.WARNING`` for the root and ``DEFAULT_LEVEL`` (INFO) for
    the ``rapidpipe`` logger, so a Batch job's ``job run`` writes its own
    INFO lines (a packet set produced, acknowledged) to CloudWatch and a
    library's INFO chatter stays out. Idempotent.
    """
    resolved_level = resolve_level(level, logging.WARNING)
    own_level = resolve_level(level, DEFAULT_LEVEL)

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_rapidpipe_owned", False):
            root.removeHandler(handler)
            handler.close()

    root.setLevel(resolved_level)
    logging.getLogger(ROOT_LOGGER_NAME).setLevel(own_level)
    handler = _make_handler(
        stream=stream if stream is not None else sys.stderr,
        level=min(resolved_level, own_level), run_id="-", attempt_id="-", stage="-", unit_id="-")
    root.addHandler(handler)
