"""Logging for rapidpipe: UTC formatter, run/attempt/stage/unit on every line.

Ported from ``origin/smdc:pipeline/runtime/logging_setup.py`` -- the UTC
formatter, identifiers on every line via an adapter, and the
stdout/per-stage-file handler split are kept; the old attempt schema this
module imported (``job_id`` naming a Batch job, not a rapidpipe run/attempt
pair) is dropped in favour of the run and attempt identifiers ``run_stage``
already carries.

``stage_log_context`` (used by ``rapidpipe.stages.contract.run_stage``)
configures this module's root logger for one stage invocation and returns
a :class:`RuntimeLogger` bound to that invocation's run, attempt, stage
and unit ids. The console handler writes to stderr (a command whose
stdout carries data logs to stderr, the project's Unix convention);
:func:`add_stage_file_handler` adds a second handler writing the same
formatted lines to a per-stage file, used by ``run_stage`` for
``<outputs>/log/<stage>.log``. :func:`configure_root` configures Python's
real root logger the same way, for ``rapidpipe.cli.main``, so a library's
bare ``logging.getLogger(__name__).warning(...)`` gets this module's line
shape instead of Python's last-resort handler (no timestamp, no run
context).
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
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
#: level: ``stage_log_context`` (stages) defaults to ``DEFAULT_LEVEL``
#: (INFO) when unset; ``configure_root`` (the CLI) defaults to WARNING.
#: Either way this variable, when set, wins.
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

    The adapter supplies them for code that logs through it. This filter is
    the backstop for everything else -- a library logging through a plain
    `logging.getLogger(__name__)` -- because the format string names those
    fields and a record lacking them would raise inside `logging` and be
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


class RuntimeLogger(logging.LoggerAdapter):
    """A logger carrying this attempt's identifiers on every line."""

    def process(self, msg: Any, kwargs: Any):
        extra = dict(self.extra or {})
        extra.update(kwargs.get("extra") or {})
        kwargs["extra"] = extra
        return msg, kwargs

    def bind(self, **fields: Any) -> "RuntimeLogger":
        extra = dict(self.extra or {})
        extra.update(fields)
        return RuntimeLogger(self.logger, extra)


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


def configure(run_id: str = "-", attempt_id: Any = "-", stage: str = "-",
              unit_id: Any = "-", level: int | str | None = None,
              stream: Any = None) -> RuntimeLogger:
    """Configure rapidpipe's logging and return its adapter.

    Idempotent: calling it twice replaces the handler set rather than
    adding a second one, so a re-entrant startup path cannot produce
    doubled lines. The console handler defaults to ``sys.stderr`` (a
    command whose stdout carries data logs to stderr; stdout contracts
    and exit codes are unaffected). ``level`` defaults to
    ``RAPIDPIPE_LOG_LEVEL`` if set, else :data:`DEFAULT_LEVEL` (INFO).
    """
    resolved_level = resolve_level(level, DEFAULT_LEVEL)

    root = logging.getLogger(ROOT_LOGGER_NAME)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        if getattr(handler, "_rapidpipe_owned", False):
            handler.close()

    root.setLevel(resolved_level)
    # Do NOT propagate to the real root logger: a caller that has also
    # called `logging.basicConfig` would otherwise see every line twice.
    root.propagate = False

    handler = _make_handler(
        stream=stream if stream is not None else sys.stderr,
        level=resolved_level, run_id=run_id, attempt_id=attempt_id,
        stage=stage, unit_id=unit_id)
    root.addHandler(handler)

    return RuntimeLogger(root, {
        "run_id": str(run_id), "attempt_id": str(attempt_id),
        "stage": str(stage), "unit": str(unit_id),
    })


def configure_root(level: int | str | None = None, stream: Any = None) -> None:
    """Configure Python's real root logger with the same UTC line shape.

    For ``rapidpipe.cli.main``: without this, a third-party library's
    plain ``logging.getLogger(__name__).warning(...)`` (never routed
    through :func:`configure`, which only touches the ``rapidpipe`` logger
    and does not propagate to the real root) falls through to Python's
    last-resort handler -- one line to stderr with no timestamp and no
    run context. ``level`` defaults to ``RAPIDPIPE_LOG_LEVEL`` if set,
    else ``logging.WARNING``. Idempotent, like :func:`configure`.
    """
    resolved_level = resolve_level(level, logging.WARNING)

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_rapidpipe_owned", False):
            root.removeHandler(handler)
            handler.close()

    root.setLevel(resolved_level)
    handler = _make_handler(
        stream=stream if stream is not None else sys.stderr,
        level=resolved_level, run_id="-", attempt_id="-", stage="-", unit_id="-")
    root.addHandler(handler)


def add_stage_file_handler(log_path: Any, *, run_id: str = "-", attempt_id: str = "-",
                            stage: str = "-", unit_id: str = "-",
                            level: int | str | None = None) -> logging.Handler:
    """Attach a file handler at ``log_path``, formatted the same as the
    console handler, to the ``rapidpipe`` logger; return it so the caller
    can remove it (:func:`remove_file_handler`) once the invocation ends.

    Used by ``rapidpipe.stages.contract.run_stage`` for the per-stage log
    file at ``<outputs>/log/<stage>.log``. Creates ``log_path``'s parent
    directory. ``level`` defaults the same way as :func:`configure`.
    """
    resolved_level = resolve_level(level, DEFAULT_LEVEL)
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(str(path), encoding="utf-8")
    handler.setLevel(resolved_level)
    handler.setFormatter(_UTCFormatter(LOG_FORMAT, datefmt=_DATE_FORMAT))
    handler.addFilter(_IdentityFilter(
        run_id=str(run_id), attempt_id=str(attempt_id),
        stage=str(stage), unit_id=str(unit_id)))
    handler._rapidpipe_owned = True
    logging.getLogger(ROOT_LOGGER_NAME).addHandler(handler)
    return handler


def remove_file_handler(handler: logging.Handler) -> None:
    """Detach and close a handler :func:`add_stage_file_handler` returned."""
    logging.getLogger(ROOT_LOGGER_NAME).removeHandler(handler)
    handler.close()


def get_logger(name: str, adapter: RuntimeLogger | None = None) -> Any:
    """Get a module logger under the rapidpipe root.

    With an adapter, returns a child adapter carrying the same identifiers.
    Without one, returns the plain logger, which still gets identifiers
    from the handler's filter (as `-` if nothing bound them).
    """
    logger = logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")
    if adapter is None:
        return logger
    return RuntimeLogger(logger, dict(adapter.extra or {}))


def stage_log_context(stage_name: str, run_id: str, attempt_id: str,
                       unit_id: Any = None) -> RuntimeLogger:
    """Configure logging for one stage invocation; return its logger.

    Used by :func:`rapidpipe.stages.contract.run_stage` at the start of
    every invocation, so every stage logs its start, its exit code and its
    manifest path with the run, attempt, stage and unit ids on every line.
    ``unit_id`` is optional (kept working for a caller with none yet);
    unset, the field prints ``-``.
    """
    resolved_unit = "-" if unit_id is None else unit_id
    adapter = configure(run_id=run_id, attempt_id=attempt_id, stage=stage_name,
                        unit_id=resolved_unit)
    return get_logger(f"stages.{stage_name}", adapter)
