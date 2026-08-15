"""
File:    test_rapid_db_connect.py

Tests for the one sanctioned connection path to the RAPID operations database.

Nothing here opens a connection, reads a secret, or touches AWS. The module
under test exposes the seams that make that possible — ``connect`` takes
``connect_fn`` and ``sleep``, credentials come through a single wrapper that is
patched in this module's namespace, and operational configuration is read via
``os.getenv`` — so every assertion below is about behaviour, not about a live
server being reachable.

What is actually asserted is the part that breaks: the exact kwargs handed to
the driver, the backoff delays a retry really sleeps, which exception class
survives a failed rollback, that a parameter never reaches the statement text,
and — the load-bearing one for this module's reason to exist — that
``ConnectionExecutor`` has no ``exit_code`` member to check.
"""

import os
import unittest
from unittest import mock

import psycopg2
from psycopg2 import sql

from database.modules.utils.rapid_db_connect import (
    DEFAULT_BACKOFF_CAP_S,
    LANE_SESSION,
    LANE_TRANSACTION,
    ConnectionExecutor,
    Credentials,
    DBCredentialError,
    DBError,
    DBUnavailable,
    Endpoint,
    connect,
    connection,
    qualified_identifier,
    resolve_credentials,
    select_columns,
    transaction,
)

MODULE = "database.modules.utils.rapid_db_connect"

# A complete operational environment. Individual tests remove one key to prove
# each is genuinely required rather than defaulted somewhere.
GOOD_ENV = {
    "DBSERVER": "pooler.internal",
    "DBPORT": "6432",
    "DBNAME": "rapidopsdb",
}


def env(**overrides):
    """GOOD_ENV with keys overridden; a None value means "absent"."""
    values = dict(GOOD_ENV)
    values.update(overrides)
    return {k: v for k, v in values.items() if v is not None}


def patch_env(**overrides):
    """Replace the whole environment, so a stray real DBSERVER cannot leak in."""
    return mock.patch.dict(os.environ, env(**overrides), clear=True)


def patch_credentials(user="rapid_rw", password="s3cret"):
    # get_db_credentials is imported INTO the module under test, so the name
    # that matters is the one in this module's namespace, not rapid_db's.
    return mock.patch(f"{MODULE}.get_db_credentials",
                      return_value=(user, password))


class ResolveCredentialsTests(unittest.TestCase):
    """The raising wrapper over rapid_db's (None, None)-returning lookup."""

    def test_returns_the_pair_on_success(self):
        with patch_credentials("rapid_rw", "s3cret"):
            self.assertEqual(resolve_credentials(), ("rapid_rw", "s3cret"))

    def test_none_pair_with_a_secret_id_names_the_secret(self):
        # The swallow this module exists to convert: a printed failure and a
        # (None, None) return becomes a raise that says which secret failed.
        with mock.patch(f"{MODULE}.get_db_credentials", return_value=(None, None)), \
                mock.patch.dict(os.environ,
                                {"RAPID_DB_SECRET_ID": "rapid/db/ops"},
                                clear=True):
            with self.assertRaises(DBCredentialError) as caught:
                resolve_credentials()
        self.assertIn("rapid/db/ops", str(caught.exception))

    def test_none_pair_without_a_secret_id_names_the_fallback(self):
        with mock.patch(f"{MODULE}.get_db_credentials", return_value=(None, None)), \
                mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(DBCredentialError) as caught:
                resolve_credentials()
        message = str(caught.exception)
        self.assertIn("RAPID_DB_SECRET_ID", message)
        self.assertIn("DBUSER", message)

    def test_a_missing_password_alone_is_still_a_failure(self):
        with mock.patch(f"{MODULE}.get_db_credentials",
                        return_value=("rapid_rw", None)), \
                mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(DBCredentialError):
                resolve_credentials()

    def test_an_exception_from_the_lookup_is_re_raised_as_our_type(self):
        boom = RuntimeError("secretsmanager unreachable")
        with mock.patch(f"{MODULE}.get_db_credentials", side_effect=boom):
            with self.assertRaises(DBCredentialError) as caught:
                resolve_credentials()
        # Chained, so the underlying cause is not lost in translation.
        self.assertIs(caught.exception.__cause__, boom)
        self.assertIn("secretsmanager unreachable", str(caught.exception))


class ErrorTaxonomyTests(unittest.TestCase):
    """error_category is what the attempt record's allowlist is keyed on."""

    def test_categories(self):
        self.assertEqual(DBError.error_category, "db_error")
        self.assertEqual(DBUnavailable.error_category, "db_unavailable")
        self.assertEqual(DBCredentialError.error_category, "config_invalid")

    def test_every_error_is_a_dberror(self):
        self.assertTrue(issubclass(DBUnavailable, DBError))
        self.assertTrue(issubclass(DBCredentialError, DBError))


class ConnectArgumentValidationTests(unittest.TestCase):
    """Arguments are rejected before any credential or environment lookup."""

    def test_application_name_is_required(self):
        for missing in ("", None):
            with self.subTest(application_name=missing):
                with self.assertRaises(ValueError):
                    connect(missing, connect_fn=mock.MagicMock())

    def test_an_unknown_lane_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            connect("registration", lane="readonly",
                    connect_fn=mock.MagicMock())
        self.assertIn("readonly", str(caught.exception))

    def test_both_lanes_are_accepted(self):
        for lane in (LANE_TRANSACTION, LANE_SESSION):
            with self.subTest(lane=lane):
                connect_fn = mock.MagicMock()
                with patch_env(), patch_credentials():
                    conn = connect("registration", lane=lane,
                                   connect_fn=connect_fn)
                self.assertIs(conn, connect_fn.return_value)

    def test_attempts_below_one_is_rejected(self):
        for attempts in (0, -1):
            with self.subTest(attempts=attempts):
                with self.assertRaises(ValueError):
                    connect("registration", attempts=attempts,
                            connect_fn=mock.MagicMock())

    def test_validation_happens_before_the_driver_is_called(self):
        connect_fn = mock.MagicMock()
        with self.assertRaises(ValueError):
            connect("", connect_fn=connect_fn)
        connect_fn.assert_not_called()


class ConnectEnvironmentTests(unittest.TestCase):
    """Operational configuration comes from the parameter tree, never a default."""

    def test_each_required_variable_is_required(self):
        for name in ("DBSERVER", "DBPORT", "DBNAME"):
            for value in (None, ""):
                with self.subTest(variable=name, value=value):
                    connect_fn = mock.MagicMock()
                    with patch_env(**{name: value}), patch_credentials():
                        with self.assertRaises(DBCredentialError) as caught:
                            connect("registration", connect_fn=connect_fn)
                    self.assertIn(name, str(caught.exception))
                    connect_fn.assert_not_called()


class ConnectSuccessTests(unittest.TestCase):
    def test_exact_connection_kwargs_are_passed_through(self):
        connect_fn = mock.MagicMock()
        with patch_env(), patch_credentials("rapid_rw", "s3cret"):
            connect("registration", lane=LANE_TRANSACTION,
                    connect_timeout=17, connect_fn=connect_fn)
        connect_fn.assert_called_once_with(
            host="pooler.internal",
            port="6432",
            dbname="rapidopsdb",
            user="rapid_rw",
            password="s3cret",
            connect_timeout=17,
            application_name="registration[transaction]",
        )

    def test_application_name_carries_the_lane(self):
        connect_fn = mock.MagicMock()
        with patch_env(), patch_credentials():
            connect("reconciler", lane=LANE_SESSION, connect_fn=connect_fn)
        self.assertEqual(connect_fn.call_args.kwargs["application_name"],
                         "reconciler[session]")

    def test_application_name_is_truncated_to_63_bytes(self):
        # PostgreSQL truncates at NAMEDATALEN-1 server-side and would silently
        # drop the lane suffix; this asserts the trim happens where it is
        # visible. The name below is long enough that the "[transaction]"
        # suffix is itself cut mid-word — the case worth pinning.
        long_name = "a" * 60
        connect_fn = mock.MagicMock()
        with patch_env(), patch_credentials():
            connect(long_name, lane=LANE_TRANSACTION, connect_fn=connect_fn)
        composed = connect_fn.call_args.kwargs["application_name"]
        self.assertEqual(len(composed), 63)
        self.assertEqual(composed, ("a" * 60 + "[transaction]")[:63])
        self.assertEqual(composed, "a" * 60 + "[tr")

    def test_a_name_that_just_fits_is_not_truncated(self):
        name = "b" * 50  # 50 + len("[transaction]") == 63
        connect_fn = mock.MagicMock()
        with patch_env(), patch_credentials():
            connect(name, lane=LANE_TRANSACTION, connect_fn=connect_fn)
        composed = connect_fn.call_args.kwargs["application_name"]
        self.assertEqual(composed, name + "[transaction]")
        self.assertEqual(len(composed), 63)

    def test_autocommit_is_turned_off_on_the_returned_connection(self):
        # The opposite of rapid_db.py's per-call autocommit, and the reason a
        # multi-statement unit of work through this module is atomic.
        connect_fn = mock.MagicMock()
        with patch_env(), patch_credentials():
            conn = connect("registration", connect_fn=connect_fn)
        self.assertIs(conn, connect_fn.return_value)
        self.assertIs(conn.autocommit, False)

    def test_no_sleep_on_a_first_attempt_success(self):
        sleep = mock.MagicMock()
        with patch_env(), patch_credentials():
            connect("registration", connect_fn=mock.MagicMock(), sleep=sleep)
        sleep.assert_not_called()


class ConnectRetryTests(unittest.TestCase):
    """Retry covers OperationalError only, and is bounded."""

    def test_transient_failure_then_success_returns_the_connection(self):
        good = mock.MagicMock(name="conn")
        connect_fn = mock.MagicMock(side_effect=[
            psycopg2.OperationalError("connection refused"),
            good,
        ])
        sleep = mock.MagicMock()
        with patch_env(), patch_credentials():
            conn = connect("registration", connect_fn=connect_fn, sleep=sleep)
        self.assertIs(conn, good)
        self.assertEqual(connect_fn.call_count, 2)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.5])

    def test_backoff_doubles_and_is_capped(self):
        # Eight attempts, all failing, so the delay sequence runs past the cap:
        # 0.5, 1, 2, 4, 8, then held at 8.0 rather than growing to 16.
        connect_fn = mock.MagicMock(
            side_effect=psycopg2.OperationalError("connection refused"))
        sleep = mock.MagicMock()
        with patch_env(), patch_credentials():
            with self.assertRaises(DBUnavailable):
                connect("registration", attempts=8, connect_fn=connect_fn,
                        sleep=sleep)
        delays = [c.args[0] for c in sleep.call_args_list]
        # One sleep fewer than attempts: the final failure raises, it does not wait.
        self.assertEqual(delays, [0.5, 1.0, 2.0, 4.0, 8.0, 8.0, 8.0])
        self.assertTrue(all(d <= DEFAULT_BACKOFF_CAP_S for d in delays))

    def test_exhaustion_raises_dbunavailable_after_exactly_attempts_calls(self):
        last = psycopg2.OperationalError("the last one")
        connect_fn = mock.MagicMock(side_effect=[
            psycopg2.OperationalError("first"),
            psycopg2.OperationalError("second"),
            last,
        ])
        sleep = mock.MagicMock()
        with patch_env(), patch_credentials():
            with self.assertRaises(DBUnavailable) as caught:
                connect("registration", attempts=3, connect_fn=connect_fn,
                        sleep=sleep)
        self.assertEqual(connect_fn.call_count, 3)
        self.assertIs(caught.exception.__cause__, last)
        self.assertEqual(sleep.call_count, 2)

    def test_a_single_attempt_does_not_retry_or_sleep(self):
        connect_fn = mock.MagicMock(
            side_effect=psycopg2.OperationalError("connection refused"))
        sleep = mock.MagicMock()
        with patch_env(), patch_credentials():
            with self.assertRaises(DBUnavailable):
                connect("registration", attempts=1, connect_fn=connect_fn,
                        sleep=sleep)
        self.assertEqual(connect_fn.call_count, 1)
        sleep.assert_not_called()

    def test_the_failure_message_names_the_target(self):
        connect_fn = mock.MagicMock(
            side_effect=psycopg2.OperationalError("connection refused"))
        with patch_env(), patch_credentials("rapid_rw", "s3cret"):
            with self.assertRaises(DBUnavailable) as caught:
                connect("registration", attempts=1, connect_fn=connect_fn,
                        sleep=mock.MagicMock())
        message = str(caught.exception)
        self.assertIn("pooler.internal", message)
        self.assertIn("rapidopsdb", message)
        # The password must never appear in an error a log will capture.
        self.assertNotIn("s3cret", message)

    def test_a_non_operational_error_propagates_immediately(self):
        # Repeating a statement the server already rejected changes nothing, so
        # ProgrammingError is neither retried nor wrapped.
        boom = psycopg2.ProgrammingError("syntax error")
        connect_fn = mock.MagicMock(side_effect=boom)
        sleep = mock.MagicMock()
        with patch_env(), patch_credentials():
            with self.assertRaises(psycopg2.ProgrammingError) as caught:
                connect("registration", connect_fn=connect_fn, sleep=sleep)
        self.assertIs(caught.exception, boom)
        self.assertEqual(connect_fn.call_count, 1)
        sleep.assert_not_called()

    def test_a_credential_failure_is_not_retried(self):
        connect_fn = mock.MagicMock()
        with patch_env(), mock.patch(f"{MODULE}.get_db_credentials",
                                     return_value=(None, None)):
            with self.assertRaises(DBCredentialError):
                connect("registration", connect_fn=connect_fn)
        connect_fn.assert_not_called()


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.conn = mock.MagicMock(name="conn")
        self.cur = self.conn.cursor.return_value

    def test_clean_exit_commits_and_closes_the_cursor(self):
        with transaction(self.conn) as cur:
            self.assertIs(cur, self.cur)
        self.conn.commit.assert_called_once_with()
        self.conn.rollback.assert_not_called()
        self.cur.close.assert_called_once_with()

    def test_an_exception_rolls_back_closes_and_re_raises(self):
        boom = ValueError("unit of work failed")
        with self.assertRaises(ValueError) as caught:
            with transaction(self.conn):
                raise boom
        self.assertIs(caught.exception, boom)
        self.conn.rollback.assert_called_once_with()
        self.conn.commit.assert_not_called()
        self.cur.close.assert_called_once_with()

    def test_a_failing_rollback_does_not_mask_the_original_exception(self):
        # The reason this wrapper exists rather than psycopg2's `with conn:` —
        # the caller must see what actually went wrong, not the cleanup error.
        self.conn.rollback.side_effect = psycopg2.OperationalError(
            "server closed the connection")
        boom = ValueError("unit of work failed")
        with self.assertRaises(ValueError) as caught:
            with transaction(self.conn):
                raise boom
        self.assertIs(caught.exception, boom)
        self.cur.close.assert_called_once_with()

    def test_the_cursor_is_closed_even_when_commit_fails(self):
        self.conn.commit.side_effect = psycopg2.OperationalError("commit failed")
        with self.assertRaises(psycopg2.OperationalError):
            with transaction(self.conn):
                pass
        self.cur.close.assert_called_once_with()


class ConnectionContextManagerTests(unittest.TestCase):
    def test_the_connection_is_closed_on_a_normal_exit(self):
        connect_fn = mock.MagicMock()
        conn = connect_fn.return_value
        with patch_env(), patch_credentials():
            with connection("registration", connect_fn=connect_fn) as opened:
                self.assertIs(opened, conn)
                conn.close.assert_not_called()
        conn.close.assert_called_once_with()

    def test_the_connection_is_closed_when_the_block_raises(self):
        # A leaked server connection on the pooled path costs a pool slot.
        connect_fn = mock.MagicMock()
        conn = connect_fn.return_value
        boom = ValueError("work failed")
        with patch_env(), patch_credentials():
            with self.assertRaises(ValueError) as caught:
                with connection("registration", connect_fn=connect_fn):
                    raise boom
        self.assertIs(caught.exception, boom)
        conn.close.assert_called_once_with()

    def test_a_failing_close_is_logged_not_raised(self):
        connect_fn = mock.MagicMock()
        conn = connect_fn.return_value
        conn.close.side_effect = psycopg2.OperationalError("already closed")
        with patch_env(), patch_credentials():
            with connection("registration", connect_fn=connect_fn):
                pass

    def test_kwargs_reach_connect(self):
        connect_fn = mock.MagicMock()
        with patch_env(), patch_credentials():
            with connection("registration", lane=LANE_SESSION,
                            connect_fn=connect_fn):
                pass
        self.assertEqual(connect_fn.call_args.kwargs["application_name"],
                         "registration[session]")


class QualifiedIdentifierTests(unittest.TestCase):
    """Structural assertions, not rendered SQL.

    ``Composable.as_string`` needs a real connection or cursor to reach libpq's
    quoting, so rendering here would mean either a live database or a mock
    context whose behaviour we would be asserting instead of psycopg2's. The
    robust offline check is the composed object's structure: that each part is
    an ``sql.Identifier`` holding the name VERBATIM (unparsed, unsplit) and that
    the join is a literal dot. An Identifier is quoted by construction at render
    time, so "arrived intact as an Identifier" is precisely the safety property.
    """

    def test_no_parts_is_rejected(self):
        with self.assertRaises(ValueError):
            qualified_identifier()

    def test_a_single_part_is_one_identifier(self):
        composed = qualified_identifier("attempts")
        self.assertIsInstance(composed, sql.Composed)
        self.assertEqual([s for s in composed.seq if isinstance(s, sql.Identifier)],
                         [sql.Identifier("attempts")])

    def test_parts_are_joined_by_a_literal_dot(self):
        composed = qualified_identifier("rapid", "attempts")
        identifiers = [s for s in composed.seq if isinstance(s, sql.Identifier)]
        separators = [s for s in composed.seq if isinstance(s, sql.SQL)]
        self.assertEqual(identifiers,
                         [sql.Identifier("rapid"), sql.Identifier("attempts")])
        self.assertEqual([s.string for s in separators], ["."])

    def test_three_parts_compose(self):
        composed = qualified_identifier("db", "rapid", "attempts")
        identifiers = [s.strings[0] for s in composed.seq
                       if isinstance(s, sql.Identifier)]
        self.assertEqual(identifiers, ["db", "rapid", "attempts"])

    def test_a_dot_inside_a_name_stays_inside_one_identifier(self):
        # Not split into two identifiers: "weird.name" is ONE object name, so it
        # renders as "weird.name" (quoted), never as weird.name (qualified).
        composed = qualified_identifier("weird.name")
        identifiers = [s for s in composed.seq if isinstance(s, sql.Identifier)]
        self.assertEqual(len(identifiers), 1)
        self.assertEqual(identifiers[0].strings, ("weird.name",))

    def test_a_double_quote_inside_a_name_is_carried_as_an_identifier(self):
        # An Identifier doubles embedded quotes at render time; what matters
        # offline is that the hostile name never became raw SQL text.
        hostile = 'evil"; DROP TABLE attempts; --'
        composed = qualified_identifier(hostile)
        identifiers = [s for s in composed.seq if isinstance(s, sql.Identifier)]
        self.assertEqual(identifiers[0].strings, (hostile,))
        literal_sql = "".join(s.string for s in composed.seq
                              if isinstance(s, sql.SQL))
        self.assertNotIn("DROP TABLE", literal_sql)


class SelectColumnsTests(unittest.TestCase):
    @staticmethod
    def literal_text(composed):
        """Concatenate only the sql.SQL (literal) fragments, recursively.

        Anything appearing here is text the database will parse. Identifiers and
        placeholders deliberately do not contribute, which is what makes
        "no value is in here" an assertable property.
        """
        out = []

        def walk(node):
            if isinstance(node, sql.Composed):
                for child in node.seq:
                    walk(child)
            elif isinstance(node, sql.SQL):
                out.append(node.string)

        walk(composed)
        return "".join(out)

    @staticmethod
    def identifier_names(composed):
        names = []

        def walk(node):
            if isinstance(node, sql.Composed):
                for child in node.seq:
                    walk(child)
            elif isinstance(node, sql.Identifier):
                names.extend(node.strings)

        walk(composed)
        return names

    def test_empty_columns_is_rejected(self):
        for columns in ([], (), None):
            with self.subTest(columns=columns):
                with self.assertRaises(ValueError):
                    select_columns("attempts", columns)

    def test_columns_and_table_travel_as_identifiers(self):
        stmt = select_columns("attempts", ["id", "run_id"])
        self.assertEqual(self.identifier_names(stmt),
                         ["id", "run_id", "attempts"])
        text = self.literal_text(stmt)
        self.assertIn("SELECT", text)
        self.assertIn("FROM", text)

    def test_a_tuple_table_is_schema_qualified(self):
        stmt = select_columns(("rapid", "attempts"), ["id"])
        self.assertEqual(self.identifier_names(stmt), ["id", "rapid", "attempts"])

    def test_a_list_table_is_schema_qualified(self):
        stmt = select_columns(["rapid", "attempts"], ["id"])
        self.assertEqual(self.identifier_names(stmt), ["id", "rapid", "attempts"])

    def test_no_predicate_means_no_placeholder(self):
        stmt = select_columns("attempts", ["id"])
        self.assertNotIn("%s", self.literal_text(stmt))
        self.assertNotIn("WHERE", self.literal_text(stmt))

    def test_a_predicate_emits_a_placeholder_never_a_value(self):
        # The parameterization guarantee at the builder: the where COLUMN is an
        # identifier and the where VALUE has no representation here at all.
        stmt = select_columns("attempts", ["id"], where_column="run_id")
        text = self.literal_text(stmt)
        self.assertIn("WHERE", text)
        self.assertIn("%s", text)
        self.assertIn("run_id", self.identifier_names(stmt))

    def test_a_hostile_column_name_stays_an_identifier(self):
        stmt = select_columns("attempts", ['id"; DROP TABLE attempts; --'])
        self.assertNotIn("DROP TABLE", self.literal_text(stmt))
        self.assertIn('id"; DROP TABLE attempts; --',
                      self.identifier_names(stmt))


class ConnectionExecutorTests(unittest.TestCase):
    def setUp(self):
        self.conn = mock.MagicMock(name="conn")
        self.cur = self.conn.cursor.return_value

    def test_has_no_exit_code_member(self):
        # The contract this class exists to replace: callers raise and catch,
        # they never read a flag off the object.
        executor = ConnectionExecutor(self.conn)
        self.assertFalse(hasattr(executor, "exit_code"))

    def test_is_callable_and_delegates_to_execute(self):
        executor = ConnectionExecutor(self.conn)
        with mock.patch.object(executor, "execute",
                               return_value=[(1,)]) as execute:
            result = executor("SELECT 1", (7,))
        execute.assert_called_once_with("SELECT 1", (7,))
        self.assertEqual(result, [(1,)])

    def test_params_are_passed_separately_to_the_cursor(self):
        # The parameterization guarantee: two arguments, never one interpolated
        # string. This is the assertion that would fail if anyone "simplified"
        # execute() into a format call.
        executor = ConnectionExecutor(self.conn)
        params = ("'; DROP TABLE attempts; --",)
        executor.execute("SELECT * FROM attempts WHERE run_id = %s", params)
        self.cur.execute.assert_called_once_with(
            "SELECT * FROM attempts WHERE run_id = %s", params)

    def test_a_composable_statement_is_passed_through_untouched(self):
        executor = ConnectionExecutor(self.conn)
        stmt = select_columns("attempts", ["id"], where_column="run_id")
        executor.execute(stmt, ("run-1",))
        self.assertIs(self.cur.execute.call_args.args[0], stmt)

    def test_rows_are_returned_when_the_statement_has_a_description(self):
        self.cur.description = [("id",)]
        self.cur.fetchall.return_value = [(1,), (2,)]
        executor = ConnectionExecutor(self.conn)
        self.assertEqual(executor.execute("SELECT id FROM attempts", None),
                         [(1,), (2,)])

    def test_the_rowcount_is_returned_when_there_is_no_result_set(self):
        # An UPDATE has no description; fetchall would raise on a real cursor,
        # so that branch must not be taken at all.
        #
        # Amended by W2 (charge 4, docs/source/dev/attempt_writer_review.rst):
        # this used to assert None. Returning None is what let a lifecycle
        # transition matching zero rows look exactly like a successful one —
        # the writer had nothing to check. The contract is now: rows for a
        # statement with a result set, cursor.rowcount for one without.
        self.cur.description = None
        self.cur.rowcount = 1
        executor = ConnectionExecutor(self.conn)
        self.assertEqual(executor.execute("UPDATE attempts SET x = 1", None), 1)
        self.cur.fetchall.assert_not_called()

    def test_a_zero_rowcount_is_reported_as_zero(self):
        # The case the whole change exists for: an UPDATE that matched
        # nothing must be distinguishable from one that matched a row.
        self.cur.description = None
        self.cur.rowcount = 0
        executor = ConnectionExecutor(self.conn)
        self.assertEqual(executor.execute("UPDATE attempts SET x = 1", None), 0)
        self.cur.fetchall.assert_not_called()

    def test_commits_after_a_successful_statement_by_default(self):
        self.cur.description = None
        ConnectionExecutor(self.conn).execute("UPDATE attempts SET x = 1", None)
        self.conn.commit.assert_called_once_with()
        self.cur.close.assert_called_once_with()

    def test_does_not_commit_when_autocommit_each_is_false(self):
        # The caller is inside transaction() and owns the commit.
        self.cur.description = None
        ConnectionExecutor(self.conn, autocommit_each=False).execute(
            "UPDATE attempts SET x = 1", None)
        self.conn.commit.assert_not_called()
        self.cur.close.assert_called_once_with()

    def test_an_error_rolls_back_closes_and_re_raises(self):
        boom = psycopg2.ProgrammingError("relation does not exist")
        self.cur.execute.side_effect = boom
        executor = ConnectionExecutor(self.conn)
        with self.assertRaises(psycopg2.ProgrammingError) as caught:
            executor.execute("SELECT 1", None)
        self.assertIs(caught.exception, boom)
        self.conn.rollback.assert_called_once_with()
        self.conn.commit.assert_not_called()
        self.cur.close.assert_called_once_with()

    def test_an_error_does_not_roll_back_when_the_caller_owns_the_transaction(self):
        boom = psycopg2.ProgrammingError("relation does not exist")
        self.cur.execute.side_effect = boom
        executor = ConnectionExecutor(self.conn, autocommit_each=False)
        with self.assertRaises(psycopg2.ProgrammingError):
            executor.execute("SELECT 1", None)
        self.conn.rollback.assert_not_called()
        self.cur.close.assert_called_once_with()

    def test_a_failing_rollback_does_not_mask_the_original_error(self):
        self.cur.execute.side_effect = psycopg2.ProgrammingError("bad sql")
        self.conn.rollback.side_effect = psycopg2.OperationalError("gone")
        executor = ConnectionExecutor(self.conn)
        with self.assertRaises(psycopg2.ProgrammingError):
            executor.execute("SELECT 1", None)
        self.cur.close.assert_called_once_with()

    def test_the_cursor_is_closed_when_fetchall_fails(self):
        self.cur.description = [("id",)]
        self.cur.fetchall.side_effect = psycopg2.OperationalError("gone")
        executor = ConnectionExecutor(self.conn)
        with self.assertRaises(psycopg2.OperationalError):
            executor.execute("SELECT id FROM attempts", None)
        self.cur.close.assert_called_once_with()

    def test_the_cursor_is_closed_when_the_commit_fails(self):
        self.cur.description = None
        self.conn.commit.side_effect = psycopg2.OperationalError("gone")
        executor = ConnectionExecutor(self.conn)
        with self.assertRaises(psycopg2.OperationalError):
            executor.execute("UPDATE attempts SET x = 1", None)
        self.cur.close.assert_called_once_with()


class ExplicitInterfaceTests(unittest.TestCase):
    """The parameter interface that retired the environment writes (O1).

    The environment policy's rule is that no process writes the environment
    for a downstream reader. Before this interface, a caller holding the
    endpoint — the payload entrypoint with the parameter tree in hand, the
    reconciler with a credential resolved under its own role — could only
    reach this module by writing `os.environ` and letting `connect` read it
    back. These pin that the passed values are used, that they beat the
    environment, and that a credential passed this way never lands in it.
    """

    def test_an_explicit_endpoint_is_used_and_no_env_is_read(self):
        connect_fn = mock.MagicMock()
        # A completely EMPTY environment: the boundary read would raise
        # DBCredentialError on DBSERVER, so reaching connect_fn at all
        # proves the passed endpoint was used.
        with mock.patch.dict(os.environ, {}, clear=True):
            connect("payload", connect_fn=connect_fn,
                    endpoint=Endpoint("db.internal", "6432", "rapidopsdb"),
                    credentials=Credentials("rapid_rw", "s3cret"))
        kwargs = connect_fn.call_args.kwargs
        self.assertEqual(kwargs["host"], "db.internal")
        self.assertEqual(kwargs["port"], "6432")
        self.assertEqual(kwargs["dbname"], "rapidopsdb")
        self.assertEqual(kwargs["user"], "rapid_rw")
        self.assertEqual(kwargs["password"], "s3cret")

    def test_explicit_values_beat_the_environment(self):
        connect_fn = mock.MagicMock()
        with patch_env(), patch_credentials("env_user", "env_pass"):
            connect("payload", connect_fn=connect_fn,
                    endpoint=Endpoint("passed.internal", "5432", "passeddb"),
                    credentials=Credentials("passed_user", "passed_pass"))
        kwargs = connect_fn.call_args.kwargs
        self.assertEqual(kwargs["host"], "passed.internal")
        self.assertEqual(kwargs["user"], "passed_user")

    def test_a_passed_credential_never_enters_the_environment(self):
        # The deciding case: the reconciler used to write DBUSER/DBPASS so
        # this module could read them, which put a plaintext password in the
        # environment of everything the service execs.
        connect_fn = mock.MagicMock()
        with mock.patch.dict(os.environ, {}, clear=True):
            connect("reconciler", connect_fn=connect_fn,
                    endpoint=Endpoint("db.internal", "6432", "rapidopsdb"),
                    credentials=Credentials("rapid_rw", "s3cret"))
            self.assertNotIn("DBPASS", os.environ)
            self.assertNotIn("DBUSER", os.environ)
            self.assertNotIn("DBSERVER", os.environ)

    def test_the_environment_is_still_the_boundary_fallback(self):
        # A plain script with no tree passes nothing and still connects.
        connect_fn = mock.MagicMock()
        with patch_env(), patch_credentials("rapid_rw", "s3cret"):
            connect("script", connect_fn=connect_fn)
        self.assertEqual(connect_fn.call_args.kwargs["host"],
                         "pooler.internal")

    def test_each_half_falls_back_independently(self):
        # An explicit endpoint with no credential still reads the credential
        # at the boundary, which is what the reconciler does when an
        # operator has supplied DBUSER/DBPASS themselves.
        connect_fn = mock.MagicMock()
        with patch_env(), patch_credentials("env_user", "env_pass"):
            connect("mixed", connect_fn=connect_fn,
                    endpoint=Endpoint("passed.internal", "5432", "passeddb"))
        kwargs = connect_fn.call_args.kwargs
        self.assertEqual(kwargs["host"], "passed.internal")
        self.assertEqual(kwargs["user"], "env_user")

    def test_an_incomplete_endpoint_is_refused_not_half_read(self):
        for missing in ("host", "port", "dbname"):
            with self.subTest(missing=missing):
                fields = {"host": "h", "port": "1", "dbname": "d"}
                fields[missing] = None
                with self.assertRaises(DBCredentialError) as caught:
                    Endpoint(**fields)
                self.assertIn(missing, str(caught.exception))

    def test_an_incomplete_credential_is_refused(self):
        with self.assertRaises(DBCredentialError):
            Credentials("rapid_rw", "")
        with self.assertRaises(DBCredentialError):
            Credentials("", "s3cret")

    def test_the_ordinary_constructor_still_refuses_a_missing_password(self):
        """The half that must NOT be relaxed by `for_pgpass` existing.

        Both other callers of this class — `service_kernel.py` and
        `entrypoints/job.py` — build it from a Secrets Manager payload,
        where an absent password means the secret came back malformed.
        That has to fail here, at construction, rather than reach libpq
        and return as an authentication failure that names the server
        instead of the secret.
        """
        with self.assertRaises(DBCredentialError):
            Credentials("rapid_rw", None)

    def test_a_pgpass_credential_carries_none_not_an_empty_string(self):
        """`None` is what makes libpq consult `~/.pgpass`; `""` is a
        supplied password that happens to be empty, which is sent as-is
        and rejected. The distinction is the whole fix, so it is asserted
        on the value rather than on mere falsiness.
        """
        creds = Credentials.for_pgpass("brusholme")
        self.assertEqual(creds.user, "brusholme")
        self.assertIsNone(creds.password)
        self.assertNotEqual(creds.password, "")

    def test_a_pgpass_credential_still_requires_a_user(self):
        # Without one libpq matches no ~/.pgpass line and falls back to
        # the OS user — connecting as an identity nobody chose.
        with self.assertRaises(DBCredentialError):
            Credentials.for_pgpass("")

    def test_connect_hands_a_pgpass_credential_to_libpq_unchanged(self):
        """The end-to-end direction: the credential must survive
        `connect()`'s own re-wrap and arrive at the driver as `None`.

        This is where the fix would silently regress. `connect()`
        re-wraps its `credentials` argument through `Credentials(...)`
        so a caller passing a bare tuple still meets the completeness
        check — and that re-wrap re-runs the very password check
        `for_pgpass` was built to bypass. A `Credentials` instance is
        therefore taken as it stands, and this test proves it by
        asserting on the kwargs the driver actually received.
        """
        connect_fn = mock.MagicMock()
        connect("payload", connect_fn=connect_fn,
                endpoint=("h", "6432", "rapid"),
                credentials=Credentials.for_pgpass("brusholme"))
        kwargs = connect_fn.call_args.kwargs
        self.assertEqual(kwargs["user"], "brusholme")
        self.assertIsNone(kwargs["password"])

    def test_the_credential_repr_does_not_print_the_password(self):
        # Anything that reprs a structure holding one — a log line, a
        # traceback frame — would otherwise print it.
        rendered = repr(Credentials("rapid_rw", "hunter2"))
        self.assertNotIn("hunter2", rendered)
        self.assertIn("rapid_rw", rendered)

    def test_a_bare_tuple_endpoint_is_validated_not_unpacked_blindly(self):
        with self.assertRaises(DBCredentialError):
            connect("payload", connect_fn=mock.MagicMock(),
                    endpoint=("host-only", "6432", ""),
                    credentials=Credentials("u", "p"))


if __name__ == "__main__":
    unittest.main()
