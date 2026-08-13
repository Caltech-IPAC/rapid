"""
File:    test_process.py

Tests for `run_tool`, `run_shell`, and the redaction they share.

The design's central claim is "no `check=False`": every failure path — a
nonzero exit, a missing binary, a non-executable target, a timeout — raises
`ToolError` with the same shape rather than returning something a caller
could ignore. That claim is what most of these tests are pinning down: not
just that a failure raises, but that it raises the SAME class with
`error_category="tool_failure"` and the argv/returncode in `details`,
regardless of which of the four paths produced it.

Real subprocesses are used for the straightforward cases (`/bin/echo`,
`/bin/sh -c "exit N"`) since the module's own contract is about real process
behavior. The `_run` injection point is used where the real world can't
produce the case on demand — a `PermissionError`, or a `TimeoutExpired` with
partial output already captured.
"""

import os
import subprocess
import tempfile
import unittest
from unittest import mock

from pipeline.runtime.errors import ToolError
from pipeline.runtime.process import (
    MIRROR_LINE_LIMIT,
    ToolResult,
    redact,
    render_argv,
    run_shell,
    run_tool,
    sensitive_values_from_env,
)


class RecordingLogger:
    """Stands in for a `RuntimeLogger`, recording every call."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def _record(self, level, msg, args):
        self.calls.append((level, (msg % args) if args else msg))

    def info(self, msg, *args, **kwargs):
        self._record("info", msg, args)

    def error(self, msg, *args, **kwargs):
        self._record("error", msg, args)

    def warning(self, msg, *args, **kwargs):
        self._record("warning", msg, args)

    def messages(self, level=None):
        return [m for lv, m in self.calls if level is None or lv == level]


class RunToolSuccessTests(unittest.TestCase):
    def test_returns_tool_result_with_stdout_stderr_duration(self):
        result = run_tool(["/bin/echo", "hello"])
        self.assertIsInstance(result, ToolResult)
        self.assertEqual(result.stdout.strip(), "hello")
        self.assertEqual(result.stderr, "")
        self.assertIsInstance(result.duration_s, float)
        self.assertGreaterEqual(result.duration_s, 0.0)

    def test_argv_is_a_list_of_strings(self):
        # argv elements may arrive as non-strings (an int exit code, e.g.);
        # run_tool coerces every element with str() before use.
        result = run_tool(["/bin/echo", 42])
        self.assertEqual(result.argv, ["/bin/echo", "42"])
        for element in result.argv:
            self.assertIsInstance(element, str)


class RunToolFailureTests(unittest.TestCase):
    def test_nonzero_exit_raises_tool_error_with_category_and_returncode(self):
        with self.assertRaises(ToolError) as ctx:
            run_tool(["/bin/sh", "-c", "exit 3"])
        exc = ctx.exception
        self.assertEqual(exc.error_category, "tool_failure")
        self.assertEqual(exc.details["returncode"], 3)

    def test_missing_binary_raises_tool_error_via_real_filenotfound(self):
        # A genuinely nonexistent binary name, so FileNotFoundError comes from
        # the real subprocess machinery rather than being injected — this is
        # the exact g0001 failure mode (unset PATH) the module exists to fix.
        with self.assertRaises(ToolError) as ctx:
            run_tool(["/definitely/not/a/real/binary-xyz"])
        exc = ctx.exception
        self.assertEqual(exc.error_category, "tool_failure")
        self.assertEqual(exc.details["returncode"], 127)

    def test_missing_binary_raises_tool_error_via_injection(self):
        def fake_run(*a, **k):
            raise FileNotFoundError("no such file")
        with self.assertRaises(ToolError) as ctx:
            run_tool(["some-tool"], _run=fake_run)
        exc = ctx.exception
        self.assertEqual(exc.error_category, "tool_failure")
        self.assertEqual(exc.details["returncode"], 127)

    def test_permission_error_raises_tool_error(self):
        def fake_run(*a, **k):
            raise PermissionError("not executable")
        with self.assertRaises(ToolError) as ctx:
            run_tool(["some-tool"], _run=fake_run)
        exc = ctx.exception
        self.assertEqual(exc.error_category, "tool_failure")
        self.assertEqual(exc.details["returncode"], 126)

    def test_timeout_raises_tool_error(self):
        def fake_run(*a, **k):
            raise subprocess.TimeoutExpired(cmd="some-tool", timeout=1)
        with self.assertRaises(ToolError) as ctx:
            run_tool(["some-tool"], timeout=1, _run=fake_run)
        exc = ctx.exception
        self.assertEqual(exc.error_category, "tool_failure")
        self.assertIsNone(exc.details["returncode"])
        self.assertEqual(exc.details["timeout_s"], 1)

    def test_real_timeout_raises_tool_error(self):
        # A real subprocess.run timeout, not injected, to confirm the actual
        # TimeoutExpired path (not just the shape we constructed by hand).
        with self.assertRaises(ToolError) as ctx:
            run_tool(["/bin/sleep", "2"], timeout=0.2)
        self.assertEqual(ctx.exception.error_category, "tool_failure")


class RunToolArgumentValidationTests(unittest.TestCase):
    def test_string_argv_raises_typeerror(self):
        with self.assertRaises(TypeError):
            run_tool("echo hello")

    def test_bytes_argv_raises_typeerror(self):
        with self.assertRaises(TypeError):
            run_tool(b"echo hello")

    def test_empty_argv_raises_valueerror(self):
        with self.assertRaises(ValueError):
            run_tool([])


class RunToolCaptureFileTests(unittest.TestCase):
    """The capture file is a bundle member — the record that survives the
    container after the log line has scrolled off."""

    def test_capture_file_contains_command_output_and_exit_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture_path = os.path.join(tmp, "stage.out")
            run_tool(["/bin/echo", "capture-me"], capture_path=capture_path)
            with open(capture_path, encoding="utf-8") as handle:
                contents = handle.read()
            self.assertIn("/bin/echo", contents)
            self.assertIn("capture-me", contents)
            self.assertIn("--- exit 0 ---", contents)

    def test_capture_file_records_a_failed_command_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture_path = os.path.join(tmp, "stage.out")
            with self.assertRaises(ToolError):
                run_tool(["/bin/sh", "-c", "echo oops 1>&2; exit 5"],
                         capture_path=capture_path)
            with open(capture_path, encoding="utf-8") as handle:
                contents = handle.read()
            self.assertIn("oops", contents)
            self.assertIn("--- exit 5 ---", contents)

    def test_capture_path_none_writes_nothing_and_does_not_raise(self):
        # capture_path is optional; _append_capture_file is a no-op when it's
        # None.
        run_tool(["/bin/echo", "no capture"], capture_path=None)

    def test_no_spool_file_survives_a_successful_run(self):
        # finding 17: stdout/stderr are redirected to spool files rather than
        # captured in memory. The spool must be cleaned up on every path, or
        # a long-lived worker running many tools would leak temp files
        # exactly as fast as it used to leak memory.
        with tempfile.TemporaryDirectory() as tmp:
            capture_path = os.path.join(tmp, "stage.out")
            run_tool(["/bin/echo", "hi"], capture_path=capture_path)
            leftovers = [n for n in os.listdir(tmp) if n != "stage.out"]
            self.assertEqual(leftovers, [])

    def test_no_spool_file_survives_a_failed_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture_path = os.path.join(tmp, "stage.out")
            with self.assertRaises(ToolError):
                run_tool(["/bin/sh", "-c", "exit 1"], capture_path=capture_path)
            leftovers = [n for n in os.listdir(tmp) if n != "stage.out"]
            self.assertEqual(leftovers, [])

    def test_no_spool_file_survives_a_missing_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture_path = os.path.join(tmp, "stage.out")
            with self.assertRaises(ToolError):
                run_tool(["/definitely/not/a/real/binary-xyz"],
                         capture_path=capture_path)
            leftovers = [n for n in os.listdir(tmp) if n != "stage.out"]
            self.assertEqual(leftovers, [])

    def test_capture_file_holds_output_larger_than_the_captured_text_limit(self):
        # The capture file is streamed from the spool in chunks; it must
        # carry the FULL output even when that output is larger than what
        # ToolResult.stdout keeps in memory, which is the whole point of
        # writing to disk rather than through subprocess.run(capture_output).
        from pipeline.runtime import process as process_module

        oversized = process_module.CAPTURED_TEXT_LIMIT + 1000
        with tempfile.TemporaryDirectory() as tmp:
            capture_path = os.path.join(tmp, "stage.out")
            script = f"head -c {oversized} /dev/zero | tr '\\0' 'a'; echo"
            run_tool(["/bin/sh", "-c", script], capture_path=capture_path)
            with open(capture_path, encoding="utf-8") as handle:
                contents = handle.read()
            self.assertGreaterEqual(contents.count("a"), oversized)


class ToolResultCapturedTextLimitTests(unittest.TestCase):
    """`ToolResult.stdout`/`.stderr` and the `ToolError` tails are bounded to
    CAPTURED_TEXT_LIMIT, read from the tail of the spool file — the fix for
    finding 17's unbounded `capture_output=True` buffering."""

    def test_stdout_is_truncated_past_the_captured_text_limit(self):
        from pipeline.runtime import process as process_module

        oversized = process_module.CAPTURED_TEXT_LIMIT + 1000
        script = f"head -c {oversized} /dev/zero | tr '\\0' 'a'"
        result = run_tool(["/bin/sh", "-c", script])
        self.assertLess(len(result.stdout), oversized)
        self.assertIn("...(truncated)...", result.stdout)

    def test_small_output_is_not_marked_truncated(self):
        result = run_tool(["/bin/echo", "small"])
        self.assertNotIn("...(truncated)...", result.stdout)
        self.assertEqual(result.stdout.strip(), "small")


class RunToolLoggerMirrorTests(unittest.TestCase):
    """Output is mirrored to the logger line by line, bounded by
    MIRROR_LINE_LIMIT so a chatty tool cannot flood the safety stream."""

    def test_output_lines_are_mirrored_to_the_logger(self):
        logger = RecordingLogger()
        run_tool(["/bin/echo", "mirror-me"], logger=logger)
        mirrored = [m for m in logger.messages("info") if "mirror-me" in m]
        self.assertTrue(mirrored)

    def test_large_output_is_truncated_at_the_mirror_line_limit(self):
        logger = RecordingLogger()
        n_lines = MIRROR_LINE_LIMIT + 50
        script = f"for i in $(seq 1 {n_lines}); do echo line$i; done"
        run_tool(["/bin/sh", "-c", script], logger=logger)

        out_lines = [m for m in logger.messages("info") if "[out]" in m]
        # MIRROR_LINE_LIMIT actual lines, plus one summary line noting the
        # overflow count.
        self.assertEqual(len(out_lines), MIRROR_LINE_LIMIT + 1)
        summary = out_lines[-1]
        self.assertIn("more line(s) in the capture file", summary)
        self.assertIn(str(n_lines - MIRROR_LINE_LIMIT), summary)
        # The truncated tail never reached the logger.
        self.assertFalse(any(f"line{n_lines}" in m for m in out_lines))


class RunShellTests(unittest.TestCase):
    def test_success_returns_tool_result(self):
        result = run_shell("echo shell-hello")
        self.assertEqual(result.stdout.strip(), "shell-hello")

    def test_nonzero_exit_raises_tool_error_with_shell_true(self):
        with self.assertRaises(ToolError) as ctx:
            run_shell("exit 4")
        exc = ctx.exception
        self.assertEqual(exc.error_category, "tool_failure")
        self.assertEqual(exc.details["returncode"], 4)
        self.assertIs(exc.details["shell"], True)

    def test_timeout_raises_tool_error(self):
        with self.assertRaises(ToolError) as ctx:
            run_shell("sleep 2", timeout=0.2)
        exc = ctx.exception
        self.assertEqual(exc.error_category, "tool_failure")
        self.assertIs(exc.details["shell"], True)

    def test_injected_timeout_raises_tool_error(self):
        def fake_run(*a, **k):
            raise subprocess.TimeoutExpired(cmd="sleep 5", timeout=1)
        with self.assertRaises(ToolError) as ctx:
            run_shell("sleep 5", timeout=1, _run=fake_run)
        self.assertEqual(ctx.exception.details["timeout_s"], 1)

    def test_list_command_raises_typeerror(self):
        with self.assertRaises(TypeError):
            run_shell(["echo", "hello"])


class RedactTests(unittest.TestCase):
    def test_redacts_sensitive_env_var_value(self):
        with mock.patch.dict(os.environ, {"RAPID_DB_PASSWORD": "supersecret1"}):
            self.assertEqual(redact("the value is supersecret1 here"),
                             "the value is ***REDACTED*** here")

    def test_redacts_substring_named_variable(self):
        # "pass" is a substring pattern, so db_password_file also matches.
        with mock.patch.dict(os.environ, {"db_password_file": "hunter2longvalue"}):
            self.assertEqual(redact("path=hunter2longvalue"),
                             "path=***REDACTED***")

    def test_redacts_aws_access_key_id(self):
        self.assertEqual(redact("key: AKIAABCDEFGHIJKLMNOP end"),
                         "key: ***REDACTED*** end")

    def test_redacts_aws_session_style_asia_key(self):
        self.assertEqual(redact("ASIAABCDEFGHIJKLMNOP"), "***REDACTED***")

    def test_redacts_x_amz_signature_value_only(self):
        text = "https://x?X-Amz-Signature=deadbeef1234&other=1"
        self.assertEqual(
            redact(text),
            "https://x?X-Amz-Signature=***REDACTED***&other=1")

    def test_redacts_x_amz_security_token_value_only(self):
        text = "X-Amz-Security-Token=abcXYZ123&foo=bar"
        self.assertEqual(redact(text), "X-Amz-Security-Token=***REDACTED***&foo=bar")

    def test_redacts_x_amz_credential_value_only(self):
        text = "X-Amz-Credential=AKIDEXAMPLE/20260101/us-east-1/s3/aws4_request"
        self.assertEqual(redact(text), "X-Amz-Credential=***REDACTED***")

    def test_short_values_are_left_alone(self):
        # Values under 6 chars are excluded even if the name looks sensitive:
        # redacting every occurrence of a short string would mangle unrelated
        # output.
        with mock.patch.dict(os.environ, {"RAPID_TOKEN": "ab1"}):
            self.assertEqual(redact("ab1 appears here"), "ab1 appears here")

    def test_non_string_input_returned_unchanged(self):
        self.assertEqual(redact(None), None)
        self.assertEqual(redact(42), 42)
        self.assertEqual(redact([1, 2]), [1, 2])

    def test_empty_string_returned_unchanged(self):
        self.assertEqual(redact(""), "")

    def test_longest_secret_redacted_first_when_substring_of_another(self):
        # If "secretvalue" (short) were redacted before "secretvaluelonger"
        # (long), the long secret would leave a "longer" fragment exposed.
        with mock.patch.dict(os.environ, {
            "RAPID_SHORT_SECRET": "secretvalue",
            "RAPID_LONG_SECRET": "secretvaluelonger",
        }):
            result = redact("here: secretvaluelonger and here: secretvalue")
            self.assertNotIn("secretvaluelonger", result)
            self.assertEqual(
                result, "here: ***REDACTED*** and here: ***REDACTED***")

    def test_extra_values_are_also_redacted(self):
        self.assertEqual(
            redact("carries mysecretvalue1 too", extra_values=["mysecretvalue1"]),
            "carries ***REDACTED*** too")


class SensitiveValuesFromEnvTests(unittest.TestCase):
    def test_picks_up_sensitive_names(self):
        with mock.patch.dict(os.environ, {"RAPID_API_KEY": "abcdef123456"},
                             clear=False):
            values = sensitive_values_from_env()
            self.assertIn("abcdef123456", values)

    def test_skips_short_values(self):
        with mock.patch.dict(os.environ, {"RAPID_SECRET": "ab1"}, clear=False):
            values = sensitive_values_from_env()
            self.assertNotIn("ab1", values)

    def test_ignores_non_sensitive_names(self):
        with mock.patch.dict(os.environ, {"RAPID_WORK_ROOT": "/tmp/rapid"},
                             clear=False):
            values = sensitive_values_from_env()
            self.assertNotIn("/tmp/rapid", values)

    def test_explicit_env_dict_overrides_os_environ(self):
        values = sensitive_values_from_env({"MY_PASSWORD": "explicitvalue1"})
        self.assertIn("explicitvalue1", values)


class RenderArgvTests(unittest.TestCase):
    def test_quotes_arguments_containing_spaces(self):
        rendered = render_argv(["/bin/echo", "hello world"])
        self.assertIn("'hello world'", rendered)

    def test_unquoted_simple_arguments_stay_bare(self):
        rendered = render_argv(["/bin/echo", "plain"])
        self.assertEqual(rendered, "/bin/echo plain")

    def test_render_argv_redacts(self):
        with mock.patch.dict(os.environ, {"RAPID_SECRET": "verysecretvalue"},
                             clear=False):
            rendered = render_argv(["/bin/echo", "verysecretvalue"])
            self.assertNotIn("verysecretvalue", rendered)
            self.assertIn("***REDACTED***", rendered)


if __name__ == "__main__":
    unittest.main()
