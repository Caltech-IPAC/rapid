"""Logging for rapidpipe: UTC formatter, run/attempt identifiers on every line.

Ported from ``origin/smdc:pipeline/runtime/logging_setup.py`` -- the UTC
formatter, run/attempt identifiers on every line via an adapter, and the
stdout/per-stage-file handler split are kept; the old attempt schema this
module imported (``job_id`` naming a Batch job, not a rapidpipe run/attempt
pair) is dropped in favour of the run and attempt identifiers ``run_stage``
already carries.

``stage_log_context`` (used by ``rapidpipe.stages.contract.run_stage``)
configures this module's root logger for one stage invocation and returns
a :class:`RuntimeLogger` bound to that invocation's run and attempt ids.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any

# UTC everywhere, per the ported module's rationale: `logging`'s own
# `asctime` is local by default, which is how a log ends up with two
# timezones in it after someone changes an instance's TZ.
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

LOG_FORMAT = (
    "%(asctime)s.%(msecs)03dZ %(levelname)s "
    "run=%(run_id)s attempt=%(attempt_id)s "
    "%(name)s %(message)s"
)

DEFAULT_LEVEL = logging.INFO

#: The root logger name for rapidpipe. Every module's
#: `logging.getLogger(__name__)` under `rapidpipe.` is a descendant, so one
#: handler set serves all of them.
ROOT_LOGGER_NAME = "rapidpipe"


class _UTCFormatter(logging.Formatter):
    """A formatter whose `asctime` is UTC, whatever the host's timezone."""

    converter = time.gmtime


class _IdentityFilter(logging.Filter):
    """Guarantees `run_id` and `attempt_id` exist on every record.

    The adapter supplies them for code that logs through it. This filter is
    the backstop for everything else -- a library logging through a plain
    `logging.getLogger(__name__)` -- because the format string names those
    fields and a record lacking them would raise inside `logging` and be
    swallowed by its error handling, which would silently drop log lines.
    """

    def __init__(self, run_id: str = "-", attempt_id: str = "-"):
        super().__init__()
        self.run_id = run_id
        self.attempt_id = attempt_id

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "run_id"):
            record.run_id = self.run_id
        if not hasattr(record, "attempt_id"):
            record.attempt_id = self.attempt_id
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


def configure(run_id: str = "-", attempt_id: Any = "-",
              level: int | str = DEFAULT_LEVEL,
              stream: Any = None) -> RuntimeLogger:
    """Configure rapidpipe's logging and return its adapter.

    Idempotent: calling it twice replaces the handler set rather than
    adding a second one, so a re-entrant startup path cannot produce
    doubled lines.
    """
    if isinstance(level, str):
        resolved = logging.getLevelName(level.upper())
        if not isinstance(resolved, int):
            raise ValueError(
                f"{level!r} is not a logging level; expected one of "
                "DEBUG, INFO, WARNING, ERROR, CRITICAL")
        level = resolved

    root = logging.getLogger(ROOT_LOGGER_NAME)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        if getattr(handler, "_rapidpipe_owned", False):
            handler.close()

    root.setLevel(level)
    # Do NOT propagate to the real root logger: a caller that has also
    # called `logging.basicConfig` would otherwise see every line twice.
    root.propagate = False

    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(_UTCFormatter(LOG_FORMAT, datefmt=_DATE_FORMAT))
    handler.addFilter(_IdentityFilter(run_id=str(run_id), attempt_id=str(attempt_id)))
    handler._rapidpipe_owned = True
    root.addHandler(handler)

    return RuntimeLogger(root, {"run_id": str(run_id), "attempt_id": str(attempt_id)})


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


def stage_log_context(stage_name: str, run_id: str, attempt_id: str) -> RuntimeLogger:
    """Configure logging for one stage invocation; return its logger.

    Used by :func:`rapidpipe.stages.contract.run_stage` at the start of
    every invocation, so every stage logs its start, its exit code and its
    manifest path with the run and attempt ids on every line.
    """
    adapter = configure(run_id=run_id, attempt_id=attempt_id)
    return get_logger(f"stages.{stage_name}", adapter)
