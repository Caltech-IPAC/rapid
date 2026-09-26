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
    # configure()/configure_root() are idempotent (they replace the
    # handler set), but a test that leaves the "rapidpipe" logger's level
    # changed could leak into another; restore both after every test.
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


def test_configure_default_stream_is_stderr_not_stdout():
    out = io.StringIO()
    err = io.StringIO()
    # configure()'s own default is sys.stderr; pass explicit streams here
    # only to assert the *un*passed one gets nothing, not to override it.
    logger = rapidpipe_log.stage_log_context("admit", "R1", "A1", unit_id="U1")
    handler = logging.getLogger(rapidpipe_log.ROOT_LOGGER_NAME).handlers[0]
    assert handler.stream is __import__("sys").stderr

    # Rewire the handler's stream to prove nothing an emitted line reaches
    # goes to a stdout-like stream by construction.
    handler.stream = err
    logger.info("hello")
    assert "hello" in err.getvalue()
    assert out.getvalue() == ""


def test_line_shape_has_run_attempt_stage_unit():
    stream = io.StringIO()
    logger = rapidpipe_log.stage_log_context(
        "admit", "R1", "A1", unit_id="e1/SCA07")
    logging.getLogger(rapidpipe_log.ROOT_LOGGER_NAME).handlers[0].stream = stream
    logger.info("stage=admit start")

    line = stream.getvalue().strip()
    match = _LINE_RE.match(line)
    assert match is not None, line
    assert match.group("run") == "R1"
    assert match.group("attempt") == "A1"
    assert match.group("stage") == "admit"
    assert match.group("unit") == "e1/SCA07"
    assert match.group("message") == "stage=admit start"


def test_identity_filter_defaults_are_dashes_for_a_plain_logger():
    stream = io.StringIO()
    rapidpipe_log.configure(stream=stream)  # run/attempt/stage/unit all default "-"
    plain = logging.getLogger("rapidpipe.some.library")
    plain.info("a plain message")

    line = stream.getvalue().strip()
    match = _LINE_RE.match(line)
    assert match is not None, line
    assert (match.group("run"), match.group("attempt"),
            match.group("stage"), match.group("unit")) == ("-", "-", "-", "-")


def test_stage_log_context_unit_id_optional_defaults_to_dash():
    stream = io.StringIO()
    logger = rapidpipe_log.stage_log_context("admit", "R1", "A1")  # no unit_id
    logging.getLogger(rapidpipe_log.ROOT_LOGGER_NAME).handlers[0].stream = stream
    logger.info("x")
    match = _LINE_RE.match(stream.getvalue().strip())
    assert match.group("unit") == "-"


def test_file_handler_writes_the_same_shaped_lines(tmp_path):
    log_path = tmp_path / "log" / "admit.log"
    handler = rapidpipe_log.add_stage_file_handler(
        log_path, run_id="R1", attempt_id="A1", stage="admit", unit_id="U1")
    try:
        logger = rapidpipe_log.get_logger(
            "stages.admit",
            rapidpipe_log.RuntimeLogger(
                logging.getLogger(rapidpipe_log.ROOT_LOGGER_NAME),
                {"run_id": "R1", "attempt_id": "A1", "stage": "admit", "unit": "U1"}))
        logger.info("from the file handler")
    finally:
        rapidpipe_log.remove_file_handler(handler)

    assert log_path.exists()
    contents = log_path.read_text()
    assert "from the file handler" in contents
    match = _LINE_RE.match(contents.strip().splitlines()[-1])
    assert match is not None
    assert match.group("run") == "R1" and match.group("stage") == "admit"


def test_remove_file_handler_detaches_it(tmp_path):
    log_path = tmp_path / "admit.log"
    handler = rapidpipe_log.add_stage_file_handler(log_path)
    root = logging.getLogger(rapidpipe_log.ROOT_LOGGER_NAME)
    assert handler in root.handlers
    rapidpipe_log.remove_file_handler(handler)
    assert handler not in root.handlers


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

    # A third-party library's own logger, never touching rapidpipe.log's
    # adapter, still gets the formatted line via configure_root's handler
    # on the real root logger.
    library_logger = logging.getLogger("some_third_party_library")
    library_logger.warning("a library warning")
    line = stream.getvalue().strip()
    match = _LINE_RE.match(line)
    assert match is not None, line
    assert match.group("message") == "a library warning"


def test_configure_root_level_overridden_by_env(monkeypatch):
    monkeypatch.setenv("RAPIDPIPE_LOG_LEVEL", "ERROR")
    rapidpipe_log.configure_root()
    assert logging.getLogger().level == logging.ERROR
