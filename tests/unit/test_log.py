"""Tests for rapidpipe.log: line shape, stream defaults, and handlers.

No database, no S3: plain logging.LogRecord plumbing.
"""

from __future__ import annotations

import io
import logging
import re

import pytest

from rapidpipe import log as rapidpipe_log

_LINE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z (DEBUG|INFO|WARNING|ERROR|CRITICAL) "
    r"run=(?P<run>\S+) attempt=(?P<attempt>\S+) stage=(?P<stage>\S+) unit=(?P<unit>\S+) "
    r"(?P<logger>\S+) (?P<message>.*)$"
)


@pytest.fixture(autouse=True)
def _reset_rapidpipe_logger():
    # configure_root() is idempotent (it replaces its handler), but a test
    # that leaves the "rapidpipe" logger's level changed could leak into
    # another; restore both after every test.
    root = logging.getLogger(rapidpipe_log.ROOT_LOGGER_NAME)
    real_root = logging.getLogger()
    saved = (list(root.handlers), root.level, root.propagate,
              list(real_root.handlers), real_root.level)
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
        if getattr(handler, "_rapidpipe_owned", False):
            handler.close()
    for handler in list(real_root.handlers):
        if getattr(handler, "_rapidpipe_owned", False):
            real_root.removeHandler(handler)
            handler.close()
    root.handlers, root.level, root.propagate = saved[0], saved[1], saved[2]
    real_root.handlers, real_root.level = saved[3], saved[4]


def test_resolve_level_env_var_overrides_default(monkeypatch):
    monkeypatch.setenv("RAPIDPIPE_LOG_LEVEL", "DEBUG")
    assert rapidpipe_log.resolve_level(None, logging.WARNING) == logging.DEBUG
    monkeypatch.delenv("RAPIDPIPE_LOG_LEVEL")
    assert rapidpipe_log.resolve_level(None, logging.WARNING) == logging.WARNING


def test_resolve_level_explicit_argument_wins_over_env(monkeypatch):
    monkeypatch.setenv("RAPIDPIPE_LOG_LEVEL", "DEBUG")
    assert rapidpipe_log.resolve_level(logging.ERROR, logging.WARNING) == logging.ERROR


def test_resolve_level_rejects_an_unknown_name():
    with pytest.raises(ValueError):
        rapidpipe_log.resolve_level("not-a-level", logging.INFO)


def test_configure_root_defaults_to_warning_and_stderr(monkeypatch):
    monkeypatch.delenv("RAPIDPIPE_LOG_LEVEL", raising=False)
    stream = io.StringIO()
    rapidpipe_log.configure_root(stream=stream)
    real_root = logging.getLogger()
    assert real_root.level == logging.WARNING

    # A third-party library's own logger still gets the formatted line via configure_root's handler
    # on the real root logger.
    library_logger = logging.getLogger("some_third_party_library")
    library_logger.warning("a library warning")
    line = stream.getvalue().strip()
    match = _LINE_RE.match(line)
    assert match is not None, line
    assert match.group("message") == "a library warning"


def test_configure_root_writes_rapidpipe_info_and_drops_a_librarys(monkeypatch):
    monkeypatch.delenv("RAPIDPIPE_LOG_LEVEL", raising=False)
    own = logging.getLogger(rapidpipe_log.ROOT_LOGGER_NAME)
    own.handlers, own.propagate = [], True     # as in a fresh CLI process; the fixture restores it
    stream = io.StringIO()
    rapidpipe_log.configure_root(stream=stream)
    logging.getLogger("rapidpipe.cli").info("producing")
    logging.getLogger("some_third_party_library").info("chatter")
    messages = [_LINE_RE.match(line).group("message") for line in stream.getvalue().splitlines()]
    assert messages == ["producing"]


def test_configure_root_level_overridden_by_env(monkeypatch):
    monkeypatch.setenv("RAPIDPIPE_LOG_LEVEL", "ERROR")
    rapidpipe_log.configure_root()
    assert logging.getLogger().level == logging.ERROR
    assert logging.getLogger(rapidpipe_log.ROOT_LOGGER_NAME).level == logging.ERROR
