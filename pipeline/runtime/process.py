"""
File:    process.py

External command execution: `run_tool`, its one named shell variant, and the
redaction applied to everything they echo.

This module replaces `rapid_pipeline_subs.execute_command` and its five
file-local copies. The audit of record (docs/source/dev/
execute_command_exit_code_audit.rst) found ~60 call sites of which ~12 check
the result, all at the `>= 64` convention; a missing binary raises an uncaught
`FileNotFoundError`; and the file-local copies carry a latent
drop-arguments bug. The design's answer is a checked primitive with no
unchecked variant, which is what this is.

**There is no `check=False`.** Not as a default, not as a keyword. A caller
that genuinely expects a nonzero exit — a probe, a `test`-style command —
catches `ToolError` and reads `exc.returncode`, which is a visible statement
at the call site that the failure was anticipated. That is the whole
difference from a boolean flag: the flag makes "I didn't check" and "I checked
and don't care" look identical in the source, and ~48 call sites are the
evidence for what that costs.

**Missing binary is a failure, not an exception class the caller must know
about.** `FileNotFoundError` from `subprocess` becomes `ToolError` with
`category="tool_failure"`, same as a nonzero exit, because from the pipeline's
point of view they are the same event: the tool did not run. The g0001
incident was exactly this — an unset `PATH` in the container, surfacing as an
uncaught `FileNotFoundError` rather than a classified tool failure.

**Output goes three places.** To the per-stage capture file (a bundle member,
so it survives the container), to the logger (so it reaches the safety stream
live, which is what an automated diagnosis agent can actually query), and into
the returned `ToolResult` (so the caller can parse it). The old per-stage
`.out` files died with the container; these do not.
"""

import contextlib
import os
import re
import shlex
import subprocess
import tempfile
import time
from typing import Any, Iterable, Sequence

from pipeline.runtime.errors import ToolError
from pipeline.runtime.logging_setup import get_logger

_logger = get_logger("process")

# Environment variable names whose VALUES are never echoed. Matched
# case-insensitively as substrings, so `RAPID_DB_PASSWORD` and
# `db_password_file` both match `password`.
#
# The observability policy prohibits credentials and tokens in diagnostics and
# requires known-sensitive values to be redacted before emission. This is the
# "known-sensitive" list; it is deliberately a substring match rather than an
# exact-name allowlist, because the failure mode to avoid is a new variable
# name nobody added to a list.
SENSITIVE_NAME_PATTERNS = (
    "password", "passwd", "secret", "token", "credential", "apikey",
    "api_key", "access_key", "private_key", "session_token", "signature",
    "authorization", "auth_token", "pass",
)

REDACTED = "***REDACTED***"

# Values that look like credentials regardless of the name attached to them:
# AWS access key ids and session tokens, and the query-string form of a
# presigned URL. A presigned S3 URL in a log is a live, usable credential for
# as long as it has not expired, which is the case most easily forgotten.
_VALUE_PATTERNS = (
    # AWS access key id (AKIA/ASIA + 16 uppercase alphanumerics)
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # Presigned URL signature parameters, value only
    re.compile(r"(?i)(X-Amz-Signature=)[0-9a-f]+"),
    re.compile(r"(?i)(X-Amz-Security-Token=)[^\s&\"']+"),
    re.compile(r"(?i)(X-Amz-Credential=)[^\s&\"']+"),
)


def _sensitive_name(name: str) -> bool:
    lowered = name.lower()
    return any(pattern in lowered for pattern in SENSITIVE_NAME_PATTERNS)


def sensitive_values_from_env(env: dict | None = None) -> set:
    """Collect the values of environment variables with sensitive names.

    These are the strings redacted from any echoed text. Reading them from the
    environment rather than a static list is what makes the redaction cover
    the actual secret in play — a password that arrives in `RAPID_DB_PASSWORD`
    is redacted wherever it appears in a tool's output, including in a message
    the tool composed itself and a name-based rule would never have caught.

    Short values are skipped: redacting every occurrence of a two-character
    string would mangle unrelated output into unreadability, and a
    two-character secret is not one.
    """
    source = os.environ if env is None else env
    values = set()
    for name, value in source.items():
        if not value or not isinstance(value, str):
            continue
        if len(value) < 6:
            continue
        if _sensitive_name(name):
            values.add(value)
    return values


def redact(text: Any, extra_values: Iterable[str] = ()) -> Any:
    """Remove known-sensitive values from text before it is echoed.

    Applied to every argv rendering, every captured line, and every serialized
    error message. Returns non-string input unchanged so it can be used
    uniformly by callers handling mixed types.
    """
    if not isinstance(text, str) or not text:
        return text

    values = sensitive_values_from_env() | {v for v in extra_values if v}
    # Longest first: if one secret is a substring of another, redacting the
    # short one first would leave a fragment of the long one visible.
    for value in sorted(values, key=len, reverse=True):
        if len(value) >= 6 and value in text:
            text = text.replace(value, REDACTED)

    for pattern in _VALUE_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


class ToolResult:
    """What a successful command produced.

    Only ever constructed for a command that exited 0 — a failure raises
    `ToolError` instead, so there is no `returncode` to check on this object
    and no way to hold a result that quietly represents a failure.
    """

    __slots__ = ("argv", "stdout", "stderr", "duration_s", "capture_path")

    def __init__(self, argv: Sequence[str], stdout: str, stderr: str,
                 duration_s: float, capture_path: str | None = None):
        self.argv = list(argv)
        self.stdout = stdout
        self.stderr = stderr
        self.duration_s = duration_s
        self.capture_path = capture_path

    def __repr__(self) -> str:
        return (f"ToolResult(argv={self.argv!r}, "
                f"duration_s={self.duration_s:.3f}, "
                f"stdout={len(self.stdout)}B, stderr={len(self.stderr)}B)")


def render_argv(argv: Sequence[str]) -> str:
    """Render an argv list for the log, quoted and redacted.

    `shlex.quote` on each element, so an argument containing a space is
    visibly one argument — the drop-arguments bug in the file-local
    `shell=True` copies came from exactly this ambiguity being invisible in
    the log.
    """
    return redact(" ".join(shlex.quote(str(a)) for a in argv))


def _append_capture_file(capture_path: str | None, argv_text: str,
                         stdout_path: str | None, stderr_path: str | None,
                         returncode: int) -> None:
    """Append one command's full record to the stage capture file.

    Streamed from the stdout/stderr spool files rather than passed as
    in-memory strings — the spool files are exactly what a subprocess run
    with redirected output leaves behind, and copying them in chunks means a
    pathologically noisy tool never requires its full output to exist as one
    Python string.
    """
    if capture_path is None:
        return
    directory = os.path.dirname(capture_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(capture_path, "a", encoding="utf-8") as handle:
        handle.write(f"$ {argv_text}\n")
        _copy_capture_body(stdout_path, handle)
        if stderr_path is not None and os.path.getsize(stderr_path) > 0:
            handle.write("--- stderr ---\n")
            _copy_capture_body(stderr_path, handle)
        handle.write(f"--- exit {returncode} ---\n\n")


def _copy_capture_body(spool_path: str | None, handle: Any) -> None:
    if spool_path is None or os.path.getsize(spool_path) == 0:
        return
    ended_with_newline = True
    with open(spool_path, "r", encoding="utf-8", errors="replace") as source:
        while True:
            chunk = source.read(_COPY_CHUNK_CHARS)
            if not chunk:
                break
            handle.write(chunk)
            ended_with_newline = chunk.endswith("\n")
    if not ended_with_newline:
        handle.write("\n")


# Chunk size for copying a spooled stdout/stderr file into the capture file.
# Bounds how much of a single command's output is in memory at once, same
# purpose as MIRROR_LINE_LIMIT below but for the file-copy path.
_COPY_CHUNK_CHARS = 65536

# How much of a single command's stdout/stderr, read from the tail of its
# spool file, is kept as `ToolResult.stdout`/`.stderr` and is available for
# `ToolError`'s `*_tail` fields. The full text always reaches the capture
# file (streamed from disk, never held whole in memory); this is what a
# caller reading `result.stdout` actually gets for the common case of small
# tool output, and what an exception message quotes for a large one.
CAPTURED_TEXT_LIMIT = 1_000_000


def _read_capture_tail(spool_path: str | None) -> str:
    """Read the last `CAPTURED_TEXT_LIMIT` bytes of a spooled output file.

    A bounded read regardless of how large the file grew — the whole point
    of spooling to disk rather than `capture_output=True` is that a chatty
    tool's output is never required to exist as one in-memory string.
    """
    if spool_path is None:
        return ""
    size = os.path.getsize(spool_path)
    if size == 0:
        return ""
    with open(spool_path, "rb") as handle:
        if size > CAPTURED_TEXT_LIMIT:
            handle.seek(size - CAPTURED_TEXT_LIMIT)
            prefix = "...(truncated)...\n"
        else:
            prefix = ""
        raw = handle.read()
    return prefix + raw.decode("utf-8", errors="replace")


def _mirror_spooled(stdout_path: str | None, stderr_path: str | None,
                    logger: Any) -> None:
    """Mirror spooled output to the logger, line by line, from disk.

    Line by line rather than one blob: the safety stream is queried by line,
    and a 5,000-line tool output delivered as a single record is one
    unsearchable wall. Bounded — a tool that emits a million lines would
    otherwise turn the safety stream into the durable store, which the design
    explicitly refuses. Reading from the spool file rather than an in-memory
    string means only the first MIRROR_LINE_LIMIT lines are ever materialized,
    regardless of how large the tool's actual output was.
    """
    for label, path in (("out", stdout_path), ("err", stderr_path)):
        if path is None or os.path.getsize(path) == 0:
            continue
        shown = 0
        total = 0
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                total += 1
                if shown < MIRROR_LINE_LIMIT:
                    logger.info("  [%s] %s", label, redact(line.rstrip("\n")))
                    shown += 1
        if total > MIRROR_LINE_LIMIT:
            logger.info("  [%s] ... %d more line(s) in the capture file",
                        label, total - MIRROR_LINE_LIMIT)


# How many lines of a single command's stdout/stderr reach the logger. The
# full text always reaches the capture file; this bounds only the live mirror.
MIRROR_LINE_LIMIT = 200


def run_tool(argv: Sequence[str], cwd: str | None = None,
             env: dict | None = None, timeout: float | None = None,
             capture_path: str | None = None, logger: Any = None,
             input_text: str | None = None,
             extra_sensitive: Iterable[str] = (),
             _run=None) -> ToolResult:
    """Run one external command. Raise `ToolError` unless it exits 0.

    `argv` is a list and `shell=False` always: the shell is not involved, so
    a filename containing a space or a semicolon is data rather than syntax.
    The only shell path in this module is `run_shell`, and it is named so a
    reader can find every use of it.

    Raises `ToolError` for: a nonzero exit, a missing binary
    (`FileNotFoundError`), a non-executable target (`PermissionError`), and a
    timeout. All four are the same event from the pipeline's side — the tool
    did not do its job — and all four carry `error_category="tool_failure"`
    with the argv, exit code, and captured output in `details`.

    Stdout and stderr are redirected straight to spool files rather than
    captured via `capture_output=True` — a tool that emits gigabytes of
    output would otherwise force `subprocess` to hold all of it as one
    in-memory string before this function ever sees it. `ToolResult.stdout`/
    `.stderr` and `ToolError`'s `*_tail` fields carry up to
    `CAPTURED_TEXT_LIMIT` characters read back from the spool; the full text
    always reaches the capture file, streamed from disk.

    `_run` is a test injection point for `subprocess.run`; nothing in
    production passes it.
    """
    log = logger if logger is not None else _logger
    run = _run if _run is not None else subprocess.run

    if isinstance(argv, (str, bytes)):
        raise TypeError(
            "run_tool takes an argv LIST, not a string: a string would have "
            "to be split by a shell, and this path deliberately has no shell. "
            "Use run_shell(command) if a shell is genuinely required.")
    argv = [str(a) for a in argv]
    if not argv:
        raise ValueError("run_tool needs at least the program name")

    argv_text = render_argv(argv)
    log.info("run: %s", argv_text)
    started = time.monotonic()

    with _spool_pair(capture_path) as (stdout_path, stderr_path):
        try:
            with open(stdout_path, "wb") as out_f, \
                 open(stderr_path, "wb") as err_f:
                completed = run(argv, cwd=cwd, env=env, timeout=timeout,
                                input=input_text, stdout=out_f, stderr=err_f,
                                text=True, shell=False)
        except FileNotFoundError as exc:
            duration = time.monotonic() - started
            _spill(str(exc), stderr_path)
            _append_capture_file(capture_path, argv_text, stdout_path,
                                 stderr_path, 127)
            log.error("tool not found: %s (%s)", argv[0], exc)
            raise ToolError(
                f"tool not found: {argv[0]!r} — is it installed and on PATH? "
                f"(argv: {argv_text})",
                argv=argv_text, returncode=127, duration_s=duration,
            ) from exc
        except PermissionError as exc:
            duration = time.monotonic() - started
            _spill(str(exc), stderr_path)
            _append_capture_file(capture_path, argv_text, stdout_path,
                                 stderr_path, 126)
            log.error("tool not executable: %s (%s)", argv[0], exc)
            raise ToolError(
                f"tool not executable: {argv[0]!r} ({exc})",
                argv=argv_text, returncode=126, duration_s=duration,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - started
            _spill(exc.stdout, stdout_path)
            _spill(exc.stderr, stderr_path)
            _append_capture_file(capture_path, argv_text, stdout_path,
                                 stderr_path, -1)
            stderr_tail = _read_capture_tail(stderr_path)
            log.error("tool timed out after %ss: %s", timeout, argv_text)
            raise ToolError(
                f"tool timed out after {timeout}s: {argv_text}",
                argv=argv_text, returncode=None, timeout_s=timeout,
                duration_s=duration,
                stderr_tail=redact(_tail(stderr_tail), extra_sensitive),
            ) from exc

        duration = time.monotonic() - started
        _append_capture_file(capture_path, argv_text, stdout_path,
                             stderr_path, completed.returncode)
        _mirror_spooled(stdout_path, stderr_path, log)
        stdout = _read_capture_tail(stdout_path)
        stderr = _read_capture_tail(stderr_path)

        if completed.returncode != 0:
            log.error("tool failed with exit %s: %s", completed.returncode,
                      argv_text)
            raise ToolError(
                f"{argv[0]!r} exited {completed.returncode} (argv: {argv_text})",
                argv=argv_text, returncode=completed.returncode,
                duration_s=duration,
                stdout_tail=redact(_tail(stdout), extra_sensitive),
                stderr_tail=redact(_tail(stderr), extra_sensitive),
            )

        log.info("ok: %s (%.3fs)", argv[0], duration)
        return ToolResult(argv, stdout, stderr, duration, capture_path)


def run_shell(command: str, cwd: str | None = None, env: dict | None = None,
              timeout: float | None = None, capture_path: str | None = None,
              logger: Any = None, extra_sensitive: Iterable[str] = (),
              _run=None) -> ToolResult:
    """Run a command THROUGH A SHELL, with identical checking.

    The one named shell variant. Its checking is exactly `run_tool`'s — the
    same `ToolError` on a nonzero exit, the same capture, the same
    redaction — so choosing the shell never means choosing a weaker contract.
    What differs is only that a shell interprets the string.

    Use it where the shell is the point: `source`-ing a virtualenv activate
    script, a pipeline of tools whose intermediate output should not be
    materialized, a redirection the tool cannot do itself. Everything else
    uses `run_tool`. The proposal names today's only such case — the SFFT
    virtualenv — and records that it folds into the main environment at
    implementation unless a real dependency conflict forces it to stay.

    A caller that reaches for this because the argv is inconvenient to build
    is making the codebase's ~60 unchecked sites' mistake in a new place; the
    named variant exists so that choice is visible in review.
    """
    log = logger if logger is not None else _logger
    run = _run if _run is not None else subprocess.run

    if not isinstance(command, str):
        raise TypeError(
            "run_shell takes a command STRING; use run_tool for an argv list")

    shown = redact(command)
    log.info("run (shell): %s", shown)
    started = time.monotonic()

    with _spool_pair(capture_path) as (stdout_path, stderr_path):
        try:
            with open(stdout_path, "wb") as out_f, \
                 open(stderr_path, "wb") as err_f:
                completed = run(command, cwd=cwd, env=env, timeout=timeout,
                                stdout=out_f, stderr=err_f, text=True,
                                shell=True)
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - started
            _spill(exc.stdout, stdout_path)
            _spill(exc.stderr, stderr_path)
            _append_capture_file(capture_path, shown, stdout_path,
                                 stderr_path, -1)
            stderr_tail = _read_capture_tail(stderr_path)
            log.error("shell command timed out after %ss: %s", timeout, shown)
            raise ToolError(
                f"shell command timed out after {timeout}s: {shown}",
                argv=shown, returncode=None, timeout_s=timeout,
                duration_s=duration, shell=True,
                stderr_tail=redact(_tail(stderr_tail), extra_sensitive),
            ) from exc

        duration = time.monotonic() - started
        _append_capture_file(capture_path, shown, stdout_path, stderr_path,
                             completed.returncode)
        _mirror_spooled(stdout_path, stderr_path, log)
        stdout = _read_capture_tail(stdout_path)
        stderr = _read_capture_tail(stderr_path)

        if completed.returncode != 0:
            # A shell reports "command not found" as exit 127 rather than
            # raising FileNotFoundError, so the missing-binary case arrives
            # here. It is still a tool_failure — same category, same class —
            # which is the point of the two paths having one contract.
            log.error("shell command failed with exit %s: %s",
                      completed.returncode, shown)
            raise ToolError(
                f"shell command exited {completed.returncode}: {shown}",
                argv=shown, returncode=completed.returncode, shell=True,
                duration_s=duration,
                stdout_tail=redact(_tail(stdout), extra_sensitive),
                stderr_tail=redact(_tail(stderr), extra_sensitive),
            )

        log.info("ok (shell) (%.3fs)", duration)
        return ToolResult([command], stdout, stderr, duration, capture_path)


# How much of a failed command's output travels in the exception's details —
# and therefore into the terminal record. The full text is in the capture
# file, which is a bundle member; this is the excerpt that makes the record
# self-explanatory without making it large.
TAIL_CHARS = 4000


def _tail(text: str) -> str:
    if not text:
        return ""
    if len(text) <= TAIL_CHARS:
        return text
    return "...(truncated)...\n" + text[-TAIL_CHARS:]


def _as_text(value: Any) -> str:
    """`TimeoutExpired.stdout` may be bytes even under `text=True`."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


@contextlib.contextmanager
def _spool_pair(capture_path: str | None):
    """Two temp file paths for a command's stdout/stderr, cleaned up on exit.

    Spooled next to the capture file when there is one (same filesystem,
    same lifecycle as the bundle it will join), otherwise in the system temp
    directory — the case tests exercise by calling `run_tool` with no
    `capture_path` at all.
    """
    directory = os.path.dirname(capture_path) if capture_path else None
    if directory:
        os.makedirs(directory, exist_ok=True)
    out_fd, out_path = tempfile.mkstemp(prefix=".run-tool-out-", dir=directory)
    os.close(out_fd)
    err_fd, err_path = tempfile.mkstemp(prefix=".run-tool-err-", dir=directory)
    os.close(err_fd)
    try:
        yield out_path, err_path
    finally:
        for path in (out_path, err_path):
            with contextlib.suppress(OSError):
                os.remove(path)


def _spill(value: Any, spool_path: str) -> None:
    """Write already-in-memory partial output (from an injected exception)
    to its spool file, so the rest of the pipeline can treat every path the
    same way: read the tail back from disk.

    Only reached when something upstream of this module — a test's `_run`
    injection, chiefly — hands `TimeoutExpired` a stdout/stderr value
    directly rather than leaving it on disk the way a real redirected
    subprocess does (confirmed: with `stdout=`/`stderr=` set to real file
    objects rather than `PIPE`, `TimeoutExpired.stdout`/`.stderr` are `None`
    and the partial output is already in the spool file written by the OS).
    """
    if value is None:
        return
    text = _as_text(value)
    if not text:
        return
    with open(spool_path, "a", encoding="utf-8") as handle:
        handle.write(text)
