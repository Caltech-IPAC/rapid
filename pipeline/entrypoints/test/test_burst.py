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

`run`'s default `inputs_fn` is the real `burst.database_inputs`, which reads
the pipeline parameter tree via SSM and then Secrets Manager — both real AWS
calls. Every `burst.run(...)` call below therefore passes `inputs_fn=`
explicitly (`_fake_inputs_fn()`), the same way every call already passes
`connect_fn=` and `sleep=` rather than relying on defaults that reach out.
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
    Credentials,
    DBCredentialError,
    DBUnavailable,
    Endpoint,
)
from pipeline.entrypoints import burst

#: A plausible, complete parameter-tree response: exactly the four keys
#: `database_inputs` requires, nothing else. Individual tests copy and
#: mutate this rather than restating all four keys each time.
_COMPLETE_PARAMETERS = {
    "db/server": "rapid-db.internal",
    "db/port": "5432",
    "db/name": "rapid",
    "db/secret-id": "rapid/db/rapid-burst",
}


def _fake_inputs_fn(endpoint=None, credentials=None):
    """A `database_inputs()`-shaped double: takes nothing, returns a fixed
    `(Endpoint, Credentials)` pair — the real types `database_inputs` builds,
    not bare tuples, so a test asserting on `.host` or `.user` exercises the
    actual shape `_connect_kwargs` forwards.

    A `mock.MagicMock(return_value=...)` rather than a plain function so
    tests can read `.call_count` and `.assert_called_once()` directly, the
    same double shape `test_job.py` uses for its own injected callables.
    """
    endpoint = endpoint or Endpoint(host="rapid-db.internal", port="5432",
                                    dbname="rapid")
    credentials = credentials or Credentials("rapid_burst", "s3cr3t")
    return mock.MagicMock(return_value=(endpoint, credentials))


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
        code = burst.run(300, 200, connect_fn=connect_fn, sleep=sleep,
                         inputs_fn=_fake_inputs_fn())
        self.assertEqual(code, 0)

    def test_two_connects_happen_in_order(self):
        connect_fn = _fake_connect_fn()
        burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock(),
                 inputs_fn=_fake_inputs_fn())
        self.assertEqual(len(connect_fn.calls), 2)

    def test_each_connection_runs_select_1_and_is_released(self):
        # "Held and released": the connection is a context manager, so the
        # double's own `__exit__` firing IS the release -- asserted here by
        # checking the cursor's SELECT 1 ran on a connection that the `with`
        # block, not this test, closed out.
        connections = []
        connect_fn = _fake_connect_fn(connections=connections)
        burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock(),
                 inputs_fn=_fake_inputs_fn())
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
        code = burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock(),
                         inputs_fn=_fake_inputs_fn())
        self.assertEqual(code, burst.EXIT_UNRECORDABLE)
        self.assertEqual(code, 70)

    def test_no_second_connect_is_attempted(self):
        connect_fn = _failing_connect_fn(fail_on={1})
        burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock(),
                 inputs_fn=_fake_inputs_fn())
        self.assertEqual(len(connect_fn.calls), 1)

    def test_neither_gap_line_is_printed(self):
        # The failure happens before the hold, so the contract lines --
        # which are printed only after the FIRST connection's hold completes
        # -- must not appear at all.
        connect_fn = _failing_connect_fn(fail_on={1})
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock(),
                 inputs_fn=_fake_inputs_fn())
        self.assertEqual(buf.getvalue(), "")


class SecondConnectFailureTests(unittest.TestCase):
    """DBUnavailable on the SECOND connect exits 70, after the first succeeded."""

    def test_exit_code_is_70(self):
        connect_fn = _failing_connect_fn(fail_on={2})
        code = burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock(),
                         inputs_fn=_fake_inputs_fn())
        self.assertEqual(code, burst.EXIT_UNRECORDABLE)

    def test_both_connects_were_attempted(self):
        connect_fn = _failing_connect_fn(fail_on={2})
        burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock(),
                 inputs_fn=_fake_inputs_fn())
        self.assertEqual(len(connect_fn.calls), 2)

    def test_both_contract_lines_still_printed_before_the_failure(self):
        # The second connect fails only once the reconnect is attempted,
        # which is AFTER both lines this contract requires are printed.
        connect_fn = _failing_connect_fn(fail_on={2})
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock(),
                 inputs_fn=_fake_inputs_fn())
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

        code = burst.run(300, 200, connect_fn=boom, sleep=mock.MagicMock(),
                         inputs_fn=_fake_inputs_fn())
        self.assertEqual(code, 70)


class ContractLineFormatTests(unittest.TestCase):
    """The two printed lines are an acceptance script's parse target."""

    def test_lines_are_printed_in_order_with_the_exact_prefixes(self):
        connect_fn = _fake_connect_fn()
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock(),
                 inputs_fn=_fake_inputs_fn())
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
                      clock=clock, inputs_fn=_fake_inputs_fn())
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
            burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock(),
                 inputs_fn=_fake_inputs_fn())
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 2)


class HoldAndGapTests(unittest.TestCase):
    """The hold and the gap are the two configurable sleeps, in order."""

    def test_hold_then_gap_are_slept_in_order_with_the_given_durations(self):
        sleep = mock.MagicMock()
        connect_fn = _fake_connect_fn()
        burst.run(hold_s=417, gap_s=63, connect_fn=connect_fn, sleep=sleep,
                 inputs_fn=_fake_inputs_fn())
        self.assertEqual([c.args[0] for c in sleep.call_args_list],
                         [417, 63])

    def test_a_zero_hold_and_gap_still_runs_the_full_sequence(self):
        sleep = mock.MagicMock()
        connect_fn = _fake_connect_fn()
        code = burst.run(hold_s=0, gap_s=0, connect_fn=connect_fn, sleep=sleep,
                         inputs_fn=_fake_inputs_fn())
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
        burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock(),
                 inputs_fn=_fake_inputs_fn())
        for _application_name, kwargs in connect_fn.calls:
            self.assertEqual(kwargs["lane"], LANE_TRANSACTION)

    def test_both_connects_use_the_startup_horizon_and_backoff_sizing(self):
        connect_fn = _fake_connect_fn()
        burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock(),
                 inputs_fn=_fake_inputs_fn())
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
        burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock(),
                 inputs_fn=_fake_inputs_fn())
        names = [application_name for application_name, _kwargs
                in connect_fn.calls]
        self.assertEqual(names, ["rapid-burst", "rapid-burst"])


class InputsReachBothConnectsTests(unittest.TestCase):
    """The endpoint and credentials `inputs_fn` returns are what both
    connects actually receive -- not a value `connect()` fell back to
    reading from the process environment.

    This is the regression surface directly: the shipped defect was
    `connect()` silently reading `DBSERVER`/`DBPORT`/`DBNAME` because
    nothing was ever passed in. Asserting the exact objects arrive as the
    `endpoint=`/`credentials=` kwargs is the only way to tell "the right
    value got there" apart from "no value was passed and it happened not to
    matter in this fake".
    """

    def test_both_connects_receive_the_resolved_endpoint_and_credentials(self):
        endpoint = Endpoint(host="pooler.example", port="6432", dbname="rapid")
        credentials = Credentials("burst_role", "hunter2")
        inputs_fn = _fake_inputs_fn(endpoint=endpoint, credentials=credentials)
        connect_fn = _fake_connect_fn()
        burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock(),
                 inputs_fn=inputs_fn)
        self.assertEqual(len(connect_fn.calls), 2)
        for _application_name, kwargs in connect_fn.calls:
            self.assertIs(kwargs["endpoint"], endpoint)
            self.assertIs(kwargs["credentials"], credentials)


class InputsFnCalledOnceTests(unittest.TestCase):
    """`inputs_fn` is resolved ONCE for the whole run, not once per connect.

    The reconnect after the gap is the half that has to survive a pooler
    outage (module docstring, `run`'s own comment above `inputs_fn =
    inputs_fn or database_inputs`): if the reconnect re-resolved the tree
    and the secret, it would reintroduce a dependency on SSM and Secrets
    Manager being reachable at exactly the moment it is trying to prove the
    DATABASE door survives an outage -- a second, unrelated way to fail
    that has nothing to do with the pooler. So this asserts both the call
    count and that the SAME resolved objects were reused, not merely that
    they happened to be equal.
    """

    def test_inputs_fn_is_called_exactly_once(self):
        inputs_fn = _fake_inputs_fn()
        connect_fn = _fake_connect_fn()
        burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock(),
                 inputs_fn=inputs_fn)
        inputs_fn.assert_called_once()

    def test_the_second_connect_reuses_the_first_connects_objects(self):
        inputs_fn = _fake_inputs_fn()
        connect_fn = _fake_connect_fn()
        burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock(),
                 inputs_fn=inputs_fn)
        self.assertEqual(len(connect_fn.calls), 2)
        (_name1, kwargs1), (_name2, kwargs2) = connect_fn.calls
        # Identity, not equality: the point is that the SAME resolution
        # travelled to both connects, not that a second, independent
        # resolution happened to produce an equal-looking pair.
        self.assertIs(kwargs1["endpoint"], kwargs2["endpoint"])
        self.assertIs(kwargs1["credentials"], kwargs2["credentials"])


class DatabaseInputsMissingKeysTests(unittest.TestCase):
    """A parameter tree missing any of the four `db/*` keys fails loudly,
    naming exactly what is missing, rather than letting `connect()` fall
    back to reading the environment -- the fallback that killed 531
    children on the live 3,000-child run this fix answers.

    `fetch_parameters` is imported LOCALLY inside `database_inputs` (its own
    docstring notes this), so patching the attribute on
    `submission.startup` -- the module the local import resolves against at
    call time -- reaches it; patching `pipeline.entrypoints.burst.
    fetch_parameters` would not, because that name is never bound at module
    scope here.
    """

    def test_missing_keys_are_named_in_the_raised_error(self):
        incomplete = dict(_COMPLETE_PARAMETERS)
        del incomplete["db/server"]
        del incomplete["db/secret-id"]
        with mock.patch("submission.startup.fetch_parameters",
                        return_value=incomplete):
            # DBCredentialError specifically, not any exception: the
            # module has a type for a configuration fault where the
            # database itself may be perfectly healthy, and it carries
            # the `config_invalid` category with it. A bare RuntimeError
            # would still exit 70 through run()'s last-resort handler,
            # but the log line an operator reads first would not say
            # which kind of failure this was.
            with self.assertRaises(DBCredentialError) as ctx:
                burst.database_inputs()
        message = str(ctx.exception)
        self.assertEqual(ctx.exception.error_category, "config_invalid")
        self.assertIn("db/server", message)
        self.assertIn("db/secret-id", message)
        # The two keys that WERE present must not be misreported as missing.
        self.assertNotIn("db/port,", message)
        self.assertNotIn("db/name,", message)

    def test_run_exits_70_when_inputs_fn_raises(self):
        # `run()` itself never talks to SSM; this proves the OUTER contract
        # -- whatever reason `inputs_fn` fails for (a missing key here, a
        # network error in production), `run` treats it as the same
        # unrecordable case a failed connect gets, because there is still no
        # attempt row to record a categorized failure into.
        def broken_inputs_fn():
            raise RuntimeError(
                "the pipeline parameter tree does not carry the database "
                "endpoint; missing: db/server, db/secret-id")

        connect_fn = _fake_connect_fn()
        code = burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock(),
                         inputs_fn=broken_inputs_fn)
        self.assertEqual(code, burst.EXIT_UNRECORDABLE)
        self.assertEqual(code, 70)
        # And the connect side was never reached at all -- a broken
        # resolution fails before anything tries to open a socket.
        self.assertEqual(len(connect_fn.calls), 0)


class NoEnvironmentFallbackRegressionTests(unittest.TestCase):
    """Pins the actual shipped defect: `run()` calling `connect_fn` without
    both `endpoint=` and `credentials=` kwargs, which let `connect()` fall
    back to reading `DBSERVER`/`DBPORT`/`DBNAME` from the process
    environment and killed 531 of 3,000 children with `DBCredentialError`
    before any of them reached the pooler.

    Deliberately does NOT assert on the values passed (that is
    `InputsReachBothConnectsTests` above) -- it asserts on their PRESENCE,
    so that if a future change reintroduces a code path where `connect_fn`
    is ever invoked with one or both of these kwargs absent (or `None`,
    which `_connect_kwargs` treats identically to absent -- see its `if
    endpoint is not None` / `if credentials is not None` guards), this test
    fails even if that path happens to be exercised by a fake that would
    otherwise silently succeed the way `connect()`'s real environment
    fallback did.
    """

    def test_every_connect_call_carries_both_kwargs_and_neither_is_none(self):
        connect_fn = _fake_connect_fn()
        burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock(),
                 inputs_fn=_fake_inputs_fn())
        self.assertGreaterEqual(len(connect_fn.calls), 1)
        for _application_name, kwargs in connect_fn.calls:
            self.assertIn("endpoint", kwargs)
            self.assertIn("credentials", kwargs)
            self.assertIsNotNone(kwargs["endpoint"])
            self.assertIsNotNone(kwargs["credentials"])

    def test_the_reconnect_specifically_still_carries_both_kwargs(self):
        # The defect was live in BOTH connects (same `kwargs` dict, built
        # once), but the reconnect is the one this job exists to prove
        # survives an outage, so it gets its own explicit check rather than
        # relying only on the loop above.
        connect_fn = _fake_connect_fn()
        burst.run(300, 200, connect_fn=connect_fn, sleep=mock.MagicMock(),
                 inputs_fn=_fake_inputs_fn())
        self.assertEqual(len(connect_fn.calls), 2)
        _name, reconnect_kwargs = connect_fn.calls[1]
        self.assertIn("endpoint", reconnect_kwargs)
        self.assertIn("credentials", reconnect_kwargs)


if __name__ == "__main__":
    unittest.main()
