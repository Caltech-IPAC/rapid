"""
File:    test_logging_setup.py

Tests for the operational layer's one logging configuration.

Every test here supplies its own `io.StringIO` as the `configure()` stream,
so nothing writes to real stdout and nothing depends on run order across
this file. `tearDown` removes whatever handlers the test installed on the
"rapid" logger — a leaked handler from one test would double- or
mis-attribute lines in the next, which is exactly the failure mode the
module itself is designed to prevent in production.
"""

import io
import logging
import os
import tempfile
import time
import unittest

from pipeline.runtime.logging_setup import (
    ROOT_LOGGER_NAME,
    RuntimeLogger,
    StageFileHandler,
    _UTCFormatter,
    configure,
    get_logger,
)


class LoggingTestCase(unittest.TestCase):
    """Common teardown: strip every handler this test attached to "rapid",
    so state never leaks between tests or into other test files."""

    def tearDown(self):
        root = logging.getLogger(ROOT_LOGGER_NAME)
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()


class ConfigureEmitsLinesTests(LoggingTestCase):
    def test_logged_line_reaches_the_supplied_stream(self):
        stream = io.StringIO()
        logger = configure(job_id="job-1", attempt_id="7", stream=stream)
        logger.info("hello world")
        self.assertIn("hello world", stream.getvalue())

    def test_line_carries_utc_timestamp_shape(self):
        stream = io.StringIO()
        logger = configure(stream=stream)
        logger.info("line")
        line = stream.getvalue()
        # %Y-%m-%dT%H:%M:%S.mmmZ — the RFC 3339-shaped UTC stamp the
        # observability design requires.
        self.assertRegex(line, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")

    def test_line_carries_level(self):
        stream = io.StringIO()
        logger = configure(stream=stream)
        logger.warning("careful")
        self.assertIn("WARNING", stream.getvalue())

    def test_line_carries_job_and_attempt_fields(self):
        stream = io.StringIO()
        logger = configure(job_id="job-42", attempt_id="3", stream=stream)
        logger.info("line")
        line = stream.getvalue()
        self.assertIn("job=job-42", line)
        self.assertIn("attempt=3", line)

    def test_default_job_and_attempt_render_as_dash(self):
        stream = io.StringIO()
        logger = configure(stream=stream)
        logger.info("line")
        line = stream.getvalue()
        self.assertIn("job=-", line)
        self.assertIn("attempt=-", line)


class ConfigureIdempotenceTests(LoggingTestCase):
    def test_second_configure_replaces_rather_than_adds_the_handler(self):
        stream = io.StringIO()
        configure(stream=stream)
        configure(stream=stream)
        root = logging.getLogger(ROOT_LOGGER_NAME)
        self.assertEqual(len(root.handlers), 1)

    def test_logging_once_after_two_configures_produces_exactly_one_line(self):
        stream = io.StringIO()
        configure(stream=stream)
        logger = configure(stream=stream)
        logger.info("only once")
        lines = [line for line in stream.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 1)


class ConfigureLevelTests(LoggingTestCase):
    def test_level_accepts_a_string_name(self):
        stream = io.StringIO()
        configure(level="WARNING", stream=stream)
        root = logging.getLogger(ROOT_LOGGER_NAME)
        self.assertEqual(root.level, logging.WARNING)

    def test_level_accepts_an_int(self):
        stream = io.StringIO()
        configure(level=logging.ERROR, stream=stream)
        root = logging.getLogger(ROOT_LOGGER_NAME)
        self.assertEqual(root.level, logging.ERROR)

    def test_level_is_case_insensitive_as_a_string(self):
        stream = io.StringIO()
        configure(level="debug", stream=stream)
        root = logging.getLogger(ROOT_LOGGER_NAME)
        self.assertEqual(root.level, logging.DEBUG)

    def test_invalid_level_name_raises_value_error(self):
        with self.assertRaises(ValueError):
            configure(level="NOT_A_LEVEL", stream=io.StringIO())


class BindAttemptTests(LoggingTestCase):
    def test_bind_attempt_returns_a_new_adapter_object(self):
        stream = io.StringIO()
        logger = configure(job_id="job-1", stream=stream)
        bound = logger.bind_attempt("9")
        self.assertIsNot(bound, logger)
        self.assertIsInstance(bound, RuntimeLogger)

    def test_bound_adapter_logs_the_new_attempt_id(self):
        stream = io.StringIO()
        logger = configure(job_id="job-1", stream=stream)
        bound = logger.bind_attempt("9")
        bound.info("after bind")
        self.assertIn("attempt=9", stream.getvalue())

    def test_original_adapter_still_logs_the_old_attempt_id(self):
        # bind_attempt returns a new object rather than mutating the old one:
        # a line already attributed to "-" must keep telling the truth about
        # what was known when it was written.
        stream = io.StringIO()
        logger = configure(job_id="job-1", attempt_id="-", stream=stream)
        logger.bind_attempt("9")  # discarded
        logger.info("still unbound")
        line = stream.getvalue()
        self.assertIn("attempt=-", line)
        self.assertNotIn("attempt=9", line)


class BindTests(LoggingTestCase):
    def test_bind_adds_arbitrary_fields_to_the_extra_dict(self):
        stream = io.StringIO()
        logger = configure(stream=stream)
        bound = logger.bind(stage="difference")
        self.assertEqual(bound.extra["stage"], "difference")

    def test_bind_preserves_existing_fields(self):
        stream = io.StringIO()
        logger = configure(job_id="job-1", stream=stream)
        bound = logger.bind(stage="difference")
        self.assertEqual(bound.extra["job_id"], "job-1")
        self.assertEqual(bound.extra["stage"], "difference")

    def test_bind_returns_a_new_adapter_not_a_mutation(self):
        stream = io.StringIO()
        logger = configure(stream=stream)
        bound = logger.bind(stage="difference")
        self.assertIsNot(bound, logger)
        self.assertNotIn("stage", logger.extra or {})


class IdentityFilterBackstopTests(LoggingTestCase):
    """A plain `logging.getLogger("rapid.x")` line — not routed through the
    adapter at all — must still format instead of raising, because the
    format string names job_id/attempt_id unconditionally and a missing
    field would make `logging` swallow the record entirely (silently
    missing lines in the safety stream, the worst failure mode here)."""

    def test_plain_logger_line_still_formats_with_dash_fields(self):
        stream = io.StringIO()
        configure(stream=stream)
        plain = logging.getLogger("rapid.something")
        plain.info("from a plain logger")
        line = stream.getvalue()
        self.assertIn("from a plain logger", line)
        self.assertIn("job=-", line)
        self.assertIn("attempt=-", line)


class GetLoggerTests(LoggingTestCase):
    def test_get_logger_without_adapter_returns_a_plain_logger(self):
        logger = get_logger("mymodule")
        self.assertEqual(logger.name, f"{ROOT_LOGGER_NAME}.mymodule")
        self.assertNotIsInstance(logger, RuntimeLogger)

    def test_get_logger_without_adapter_still_emits_dash_fields(self):
        stream = io.StringIO()
        configure(stream=stream)
        logger = get_logger("mymodule")
        logger.info("line")
        line = stream.getvalue()
        self.assertIn("job=-", line)
        self.assertIn("attempt=-", line)

    def test_get_logger_with_adapter_returns_a_child_adapter(self):
        stream = io.StringIO()
        parent = configure(job_id="job-1", attempt_id="3", stream=stream)
        child = get_logger("mymodule", adapter=parent)
        self.assertIsInstance(child, RuntimeLogger)
        self.assertEqual(child.logger.name, f"{ROOT_LOGGER_NAME}.mymodule")

    def test_child_adapter_carries_the_parents_identifiers(self):
        stream = io.StringIO()
        parent = configure(job_id="job-1", attempt_id="3", stream=stream)
        child = get_logger("mymodule", adapter=parent)
        child.info("from child")
        line = stream.getvalue()
        self.assertIn("job=job-1", line)
        self.assertIn("attempt=3", line)

    def test_child_adapter_is_independent_of_the_parent(self):
        stream = io.StringIO()
        parent = configure(job_id="job-1", stream=stream)
        child = get_logger("mymodule", adapter=parent)
        child.extra["job_id"] = "mutated"
        self.assertEqual(parent.extra["job_id"], "job-1")


class StageFileHandlerTests(LoggingTestCase):
    def test_writes_lines_to_the_given_path_while_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "stage.log")
            configure(stream=io.StringIO())
            logger = logging.getLogger(ROOT_LOGGER_NAME)
            with StageFileHandler(path):
                logger.info("inside the stage")
            with open(path, encoding="utf-8") as fh:
                contents = fh.read()
            self.assertIn("inside the stage", contents)

    def test_creates_the_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "dir", "stage.log")
            configure(stream=io.StringIO())
            with StageFileHandler(path):
                pass
            self.assertTrue(os.path.isdir(os.path.dirname(path)))
            self.assertTrue(os.path.isfile(path))

    def test_removes_its_handler_on_normal_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "stage.log")
            configure(stream=io.StringIO())
            logger = logging.getLogger(ROOT_LOGGER_NAME)
            with StageFileHandler(path):
                logger.info("during")
            logger.info("after")
            with open(path, encoding="utf-8") as fh:
                contents = fh.read()
            self.assertIn("during", contents)
            self.assertNotIn("after", contents)

    def test_removes_its_handler_even_when_the_block_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "stage.log")
            configure(stream=io.StringIO())
            logger = logging.getLogger(ROOT_LOGGER_NAME)
            with self.assertRaises(RuntimeError):
                with StageFileHandler(path):
                    logger.info("before the raise")
                    raise RuntimeError("stage blew up")
            logger.info("after the raise")
            with open(path, encoding="utf-8") as fh:
                contents = fh.read()
            self.assertIn("before the raise", contents)
            self.assertNotIn("after the raise", contents)

    def test_handler_is_not_left_on_the_logger_after_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "stage.log")
            configure(stream=io.StringIO())
            logger = logging.getLogger(ROOT_LOGGER_NAME)
            before = len(logger.handlers)
            with StageFileHandler(path):
                self.assertEqual(len(logger.handlers), before + 1)
            self.assertEqual(len(logger.handlers), before)

    def test_file_is_closed_after_exit_so_it_can_be_reopened_elsewhere(self):
        # If the handler leaked its file descriptor, removing the directory
        # (or reopening the same path for exclusive access) would fail on
        # some platforms. TemporaryDirectory's own cleanup exercises this.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "stage.log")
            configure(stream=io.StringIO())
            with StageFileHandler(path):
                pass
            # Reopening for write should not raise if the prior handler
            # released the descriptor.
            with open(path, "a", encoding="utf-8"):
                pass


class UTCFormatterTests(unittest.TestCase):
    """The observability design requires UTC, not host-local time, so a
    changed instance TZ can never put two timezones in one log."""

    def test_formatter_converter_is_time_gmtime(self):
        self.assertIs(_UTCFormatter.converter, time.gmtime)

    def test_formatter_does_not_use_localtime(self):
        self.assertIsNot(_UTCFormatter.converter, time.localtime)


if __name__ == "__main__":
    unittest.main()
