"""
File:    logging_setup.py

One logging configuration for the operational layer.

The payload is print()-only today — 2,126 calls, zero `logging` — and the
proposal replaces that with "one configuration in the runtime module: UTC
timestamps, level, job/attempt identifiers on every line via adapter, stream
handler to stdout (safety net) plus per-stage file handlers (bundle members)".
This module is that configuration.

**Why stdout and not stderr for the stream handler.** Batch delivers the
container's output to the CloudWatch safety stream; the observability design
calls that stream a safety net over durable S3 artifacts. Both streams are
delivered, but stdout is where the pipeline's narrative belongs, and keeping
stderr clear means a Python traceback on the way out is visually distinct from
the log the job was writing when it died.

**Why an adapter and not a Filter.** Every line needs the job and attempt
identifiers, and a `LoggerAdapter` binds them to a logger reference the caller
holds. A `Filter` would have to reach for ambient state to find them, which is
exactly the global-mutable-context pattern that makes a forked worker log
another worker's identifiers.

**Identifiers on every line, including before the attempt row is resolved.**
`configure` is called at startup, before ownership resolution, so `attempt_id`
is unknown at that moment. It is rendered as `-` rather than omitted, so the
field structure of every line is identical and a log parser never has to cope
with a missing column. `bind_attempt` returns a new adapter once the id is
known — it does not mutate the old one, so a line emitted earlier keeps
telling the truth about what was known when it was written.
"""

import logging
import os
import sys
import time
from typing import Any

# UTC everywhere. The observability design requires "UTC RFC 3339 with
# offsets"; `logging`'s own asctime is local by default, which is how a log
# ends up with two timezones in it after someone changes an instance's
# TZ.
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

LOG_FORMAT = (
    "%(asctime)s.%(msecs)03dZ %(levelname)s "
    "job=%(job_id)s attempt=%(attempt_id)s "
    "%(name)s %(message)s"
)

DEFAULT_LEVEL = logging.INFO

# The root logger name for the operational layer. Every module's
# `logging.getLogger(__name__)` under `pipeline.` and `observability.` is a
# descendant, so one handler set serves all of them.
ROOT_LOGGER_NAME = "rapid"


class _UTCFormatter(logging.Formatter):
    """A formatter whose `asctime` is UTC, whatever the host's timezone."""

    converter = time.gmtime


class _IdentityFilter(logging.Filter):
    """Guarantees `job_id` and `attempt_id` exist on every record.

    The adapter supplies them for code that logs through it. This filter is
    the backstop for everything else — a library logging through a plain
    `logging.getLogger(__name__)`, or a third-party module — because the
    format string names those fields and a record lacking them would raise
    inside `logging` and be swallowed by its error handling. The result would
    be silently missing log lines, which is the worst possible failure mode
    for the safety stream.
    """

    def __init__(self, job_id: str = "-", attempt_id: str = "-"):
        super().__init__()
        self.job_id = job_id
        self.attempt_id = attempt_id

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "job_id"):
            record.job_id = self.job_id
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

    def bind_attempt(self, attempt_id: Any) -> "RuntimeLogger":
        """Return a new adapter that also knows the attempt id.

        A new object, never a mutation: lines already emitted recorded what
        was known at the time, and rewriting the adapter in place would make
        the identity of a line depend on when someone read it.
        """
        extra = dict(self.extra or {})
        extra["attempt_id"] = attempt_id
        return RuntimeLogger(self.logger, extra)

    def bind(self, **fields: Any) -> "RuntimeLogger":
        extra = dict(self.extra or {})
        extra.update(fields)
        return RuntimeLogger(self.logger, extra)


def configure(job_id: str = "-", attempt_id: Any = "-",
              level: int | str = DEFAULT_LEVEL,
              stream: Any = None) -> RuntimeLogger:
    """Configure the operational layer's logging and return its adapter.

    Idempotent: calling it twice replaces the handler set rather than adding a
    second one, so a re-entrant startup path cannot produce doubled lines.
    That is a real failure mode — duplicate log lines in the safety stream
    read as duplicate work.

    `level` accepts the usual ints or the string names, so it can come
    straight from the parameter tree without a lookup table at the call site.
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
        # Close only what we own. A handler installed by someone else and
        # handed to us would be closed out from under them.
        if getattr(handler, "_rapid_owned", False):
            handler.close()

    root.setLevel(level)
    # Do NOT propagate to the real root logger: if an application has also
    # called `logging.basicConfig`, propagation writes every line twice.
    root.propagate = False

    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(_UTCFormatter(LOG_FORMAT, datefmt=_DATE_FORMAT))
    handler.addFilter(_IdentityFilter(job_id=str(job_id),
                                      attempt_id=str(attempt_id)))
    handler._rapid_owned = True
    root.addHandler(handler)

    return RuntimeLogger(root, {"job_id": str(job_id),
                                "attempt_id": str(attempt_id)})


def get_logger(name: str, adapter: RuntimeLogger | None = None) -> Any:
    """Get a module logger under the operational root.

    With an adapter, returns a child adapter carrying the same identifiers —
    the normal path for a module that was handed the runtime's logger. Without
    one, returns the plain logger, which still gets identifiers from the
    handler's filter (as `-` if nothing bound them).
    """
    logger = logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")
    if adapter is None:
        return logger
    return RuntimeLogger(logger, dict(adapter.extra or {}))


class StageFileHandler:
    """A per-stage file handler, attached for the duration of one stage.

    The bundle members the proposal calls for: each stage's log lines are
    captured to their own file inside the bundle staging directory, in
    addition to flowing to stdout. Used as a context manager by `run_stage`,
    so the handler is always removed — a leaked handler would keep writing a
    finished stage's file with a later stage's lines.

    The file is opened at entry and closed at exit even if the stage raises;
    the log of a failed stage is the log that matters most.
    """

    def __init__(self, path: str, level: int | None = None,
                 logger_name: str = ROOT_LOGGER_NAME):
        self.path = path
        self.level = level
        self._logger = logging.getLogger(logger_name)
        self._handler: logging.Handler | None = None

    def __enter__(self) -> "StageFileHandler":
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        handler = logging.FileHandler(self.path, encoding="utf-8")
        handler.setFormatter(_UTCFormatter(LOG_FORMAT, datefmt=_DATE_FORMAT))
        handler.addFilter(_IdentityFilter())
        if self.level is not None:
            handler.setLevel(self.level)
        handler._rapid_owned = True
        self._logger.addHandler(handler)
        self._handler = handler
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._handler is not None:
            self._logger.removeHandler(self._handler)
            self._handler.close()
            self._handler = None
        return False
