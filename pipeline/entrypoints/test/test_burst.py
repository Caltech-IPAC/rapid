"""
File:    test_burst.py

Tests for the admission-load Batch job: a stub-tier suite with no I/O, no
database, and no network, so it runs on a laptop with plain
`python3 -m pytest`.

Nothing here imports `psycopg2` or opens a socket. `burst.run` takes its
connection factory, its sleep function and its clock as parameters — exactly
the injection-point convention `database.modules.utils.rapid_db_connect.
connect` already uses (`connect_fn=`, `sleep=`) — so every assertion below is
about the SEQUENCE and its CONTRACT (exit codes, the two printed lines' exact
format, which lane and horizon the connect calls use, that the hold and the
gap are honoured), never about a live pooler being reachable.

`run`'s default `connect_fn` is the real
`database.modules.utils.rapid_db_connect.connection` context manager, so
importing `pipeline.entrypoints.burst` does reach `psycopg2` (through
`rapid_db_connect`'s own `import psycopg2` at module scope) — the same import
`test_rapid_db_connect.py` itself takes for granted. `psycopg2` is a pure
Python wheel with no live connection required to import, so this stays within
the stub tier's "no I/O" rule; nothing here calls `psycopg2.connect`.
"""

import contextlib
import io
import unittest
from unittest import mock

from database.modules.utils.rapid_db_connect import (
    LANE_TRANSACTION,
    STARTUP_BACKOFF_CAP_S,
    STARTUP_BACKOFF_INITIAL_S,
    STARTUP_BACKOFF_MULTIPLIER,
    STARTUP_CONNECT_ATTEMPTS,
    STARTUP_HORIZON_S,
    DBUnavailable,
)
from pipeline.entrypoints import burst


def _fake_connect_fn(conn=None, connections=None):
    """A `connection(application_name, **kwargs)`-shaped double.

    Returns a context manager yielding `conn` (a fresh `MagicMock` per call
    unless one is given), recording every call's args/kwargs on itself as
    `.calls` — mirroring how `test_rapid_db_connect.py`'s own
    `ConnectionContextManagerTests` drives `connection()` via a mocked
    `connect_fn`, one level further out: burst's `connect_fn` stands in for
    the whole `connection()` context manager, not for `psycopg2.connect`
    underneath it, because burst never sees the driver directly.
    """
    calls = []

    @contextlib.contextmanager
    def fake(application_name, **kwargs):
        calls.append((application_name, kwargs))
        this_conn = conn if conn is not None else mock.MagicMock(name="conn")
        if connections is not None:
            connections.append(this_conn)
        yield this_conn

    fake.calls = calls
    return fake


def _failing_connect_fn(*, fail_on):
    """A `connect_fn` double that raises `DBUnavailable` on given call numbers
    (1-based) and otherwise behaves like `_fake_connect_fn`."""
    calls = []
    state = {"count": 0}

    @contextlib.contextmanager
    def fake(application_name, **kwargs):
        state["count"] += 1
        calls.append((application_name, kwargs))
        if state["count"] in fail_on:
            raise DBUnavailable(
                f"could not connect to the pooler on call {state['count']}")
        yield mock.MagicMock(name=f"conn{state['count']}")

    fake.calls = calls
    return fake


class HappyPathTests(unittest.TestCase):
    """Both connects succeed: the connection is held and released, twice."""

    def test_exit_code_is_zero(self):
        connect_fn = _fake_connect_fn()
        sleep = mock.MagicMock()
        code = burst.run(300, 200, connect_fn=connect_fn, sleep=sleep)
        self.assertEqual(code, 0)

    def test_two_connects_happen_in_order(self):
        connect_fn = _fake_connect_fn()
        burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock())
        self.assertEqual(len(connect_fn.calls), 2)

    def test_each_connection_runs_select_1_and_is_released(self):
        # "Held and released": the connection is a context manager, so the
        # double's own `__exit__` firing IS the release -- asserted here by
        # checking the cursor's SELECT 1 ran on a connection that the `with`
        # block, not this test, closed out.
        connections = []
        connect_fn = _fake_connect_fn(connections=connections)
        burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock())
        self.assertEqual(len(connections), 2)
        for conn in connections:
            cursor_cm = conn.cursor.return_value
            cursor = cursor_cm.__enter__.return_value
            cursor.execute.assert_called_once_with("SELECT 1")
            # The `with conn.cursor() as cur:` block exited normally, which
            # is what releases the cursor back to the connection.
            cursor_cm.__exit__.assert_called_once()


class FirstConnectFailureTests(unittest.TestCase):
    """DBUnavailable on the FIRST connect exits 70 without a second attempt."""

    def test_exit_code_is_70(self):
        connect_fn = _failing_connect_fn(fail_on={1})
        code = burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock())
        self.assertEqual(code, burst.EXIT_UNRECORDABLE)
        self.assertEqual(code, 70)

    def test_no_second_connect_is_attempted(self):
        connect_fn = _failing_connect_fn(fail_on={1})
        burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock())
        self.assertEqual(len(connect_fn.calls), 1)

    def test_neither_gap_line_is_printed(self):
        # The failure happens before the hold, so the contract lines --
        # which are printed only after the FIRST connection's hold completes
        # -- must not appear at all.
        connect_fn = _failing_connect_fn(fail_on={1})
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock())
        self.assertEqual(buf.getvalue(), "")


class SecondConnectFailureTests(unittest.TestCase):
    """DBUnavailable on the SECOND connect exits 70, after the first succeeded."""

    def test_exit_code_is_70(self):
        connect_fn = _failing_connect_fn(fail_on={2})
        code = burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock())
        self.assertEqual(code, burst.EXIT_UNRECORDABLE)

    def test_both_connects_were_attempted(self):
        connect_fn = _failing_connect_fn(fail_on={2})
        burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock())
        self.assertEqual(len(connect_fn.calls), 2)

    def test_both_contract_lines_still_printed_before_the_failure(self):
        # The second connect fails only once the reconnect is attempted,
        # which is AFTER both lines this contract requires are printed.
        connect_fn = _failing_connect_fn(fail_on={2})
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock())
        lines = [line for line in buf.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("burst: gap entered "))
        self.assertTrue(lines[1].startswith("burst: reconnecting "))


class UnexpectedErrorTests(unittest.TestCase):
    """Any OTHER exception also exits 70 (matching job.py's own last resort)."""

    def test_a_non_dbunavailable_exception_also_exits_70(self):
        @contextlib.contextmanager
        def boom(application_name, **kwargs):
            raise RuntimeError("something else entirely")
            yield  # pragma: no cover - unreachable, makes this a generator

        code = burst.run(300, 200, connect_fn=boom, sleep=mock.MagicMock())
        self.assertEqual(code, 70)


class ContractLineFormatTests(unittest.TestCase):
    """The two printed lines are an acceptance script's parse target."""

    def test_lines_are_printed_in_order_with_the_exact_prefixes(self):
        connect_fn = _fake_connect_fn()
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock())
        lines = [line for line in buf.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0][:len("burst: gap entered ")],
                         "burst: gap entered ")
        self.assertEqual(lines[1][:len("burst: reconnecting ")],
                         "burst: reconnecting ")

    def test_timestamp_is_iso8601_utc_with_a_bare_z_suffix(self):
        import datetime
        import re

        fixed = datetime.datetime(2026, 9, 14, 1, 23, 45)
        clock = mock.MagicMock(return_value=fixed)
        connect_fn = _fake_connect_fn()
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock(),
                      clock=clock)
        lines = [line for line in buf.getvalue().splitlines() if line]
        pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
        gap_ts = lines[0][len("burst: gap entered "):]
        reconnect_ts = lines[1][len("burst: reconnecting "):]
        self.assertRegex(gap_ts, pattern)
        self.assertRegex(reconnect_ts, pattern)
        self.assertEqual(gap_ts, "2026-09-14T01:23:45Z")
        self.assertEqual(reconnect_ts, "2026-09-14T01:23:45Z")

    def test_no_stray_output_between_or_around_the_two_lines(self):
        # Guards the "exactly" in the contract: nothing from this sequence
        # goes to stdout except the two lines, in order.
        connect_fn = _fake_connect_fn()
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock())
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 2)


class HoldAndGapTests(unittest.TestCase):
    """The hold and the gap are the two configurable sleeps, in order."""

    def test_hold_then_gap_are_slept_in_order_with_the_given_durations(self):
        sleep = mock.MagicMock()
        connect_fn = _fake_connect_fn()
        burst.run(hold_s=417, gap_s=63, connect_fn=connect_fn, sleep=sleep)
        self.assertEqual([c.args[0] for c in sleep.call_args_list],
                         [417, 63])

    def test_a_zero_hold_and_gap_still_runs_the_full_sequence(self):
        sleep = mock.MagicMock()
        connect_fn = _fake_connect_fn()
        code = burst.run(hold_s=0, gap_s=0, connect_fn=connect_fn, sleep=sleep)
        self.assertEqual(code, 0)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0, 0])

    def test_defaults_are_300_and_200_when_the_environment_is_unset(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                burst._env_seconds("RAPID_BURST_HOLD_S", burst.DEFAULT_HOLD_S),
                300.0)
            self.assertEqual(
                burst._env_seconds("RAPID_BURST_GAP_S", burst.DEFAULT_GAP_S),
                200.0)

    def test_environment_overrides_are_read_as_seconds(self):
        with mock.patch.dict("os.environ",
                             {"RAPID_BURST_HOLD_S": "45",
                              "RAPID_BURST_GAP_S": "12.5"}, clear=True):
            self.assertEqual(
                burst._env_seconds("RAPID_BURST_HOLD_S", burst.DEFAULT_HOLD_S),
                45.0)
            self.assertEqual(
                burst._env_seconds("RAPID_BURST_GAP_S", burst.DEFAULT_GAP_S),
                12.5)

    def test_a_non_numeric_override_raises(self):
        with mock.patch.dict("os.environ",
                             {"RAPID_BURST_HOLD_S": "soon"}, clear=True):
            with self.assertRaises(ValueError):
                burst._env_seconds("RAPID_BURST_HOLD_S", burst.DEFAULT_HOLD_S)

    def test_a_negative_override_raises(self):
        with mock.patch.dict("os.environ",
                             {"RAPID_BURST_GAP_S": "-1"}, clear=True):
            with self.assertRaises(ValueError):
                burst._env_seconds("RAPID_BURST_GAP_S", burst.DEFAULT_GAP_S)

    def test_main_exits_70_on_a_bad_environment_value_without_connecting(self):
        with mock.patch.dict("os.environ",
                             {"RAPID_BURST_HOLD_S": "not-a-number"},
                             clear=True):
            with mock.patch.object(burst, "run") as run_mock:
                code = burst.main([])
        self.assertEqual(code, burst.EXIT_UNRECORDABLE)
        run_mock.assert_not_called()


class ConnectPolicyTests(unittest.TestCase):
    """The connect calls use the transaction lane and the STARTUP horizon.

    This is the load-bearing assertion for burst's whole reason to exist: it
    has to retry on the SAME policy `job.py`'s own first connection uses, or
    it proves something about a different, easier-to-satisfy door.
    """

    def test_both_connects_use_the_transaction_lane(self):
        connect_fn = _fake_connect_fn()
        burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock())
        for _application_name, kwargs in connect_fn.calls:
            self.assertEqual(kwargs["lane"], LANE_TRANSACTION)

    def test_both_connects_use_the_startup_horizon_and_backoff_sizing(self):
        connect_fn = _fake_connect_fn()
        burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock())
        self.assertEqual(len(connect_fn.calls), 2)
        for _application_name, kwargs in connect_fn.calls:
            self.assertEqual(kwargs["horizon"], STARTUP_HORIZON_S)
            self.assertEqual(kwargs["attempts"], STARTUP_CONNECT_ATTEMPTS)
            self.assertEqual(kwargs["backoff_initial"],
                             STARTUP_BACKOFF_INITIAL_S)
            self.assertEqual(kwargs["backoff_multiplier"],
                             STARTUP_BACKOFF_MULTIPLIER)
            self.assertEqual(kwargs["backoff_cap"], STARTUP_BACKOFF_CAP_S)
            self.assertIs(kwargs["jitter"], True)

    def test_both_connects_carry_an_application_name(self):
        # Required by `connect()` itself (pooler-side attribution); burst
        # names itself once and uses the same name both times, since both
        # connections play the identical role in the sequence.
        connect_fn = _fake_connect_fn()
        burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock())
        names = [application_name for application_name, _kwargs
                in connect_fn.calls]
        self.assertEqual(names, ["rapid-burst", "rapid-burst"])


if __name__ == "__main__":
    unittest.main()
