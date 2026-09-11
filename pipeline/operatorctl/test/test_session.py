"""Stub-tier tests for the agent operator identity change in `session.py`.

The defect this closes: `operator_session()` hard-coded `SET ROLE
rapid_operator` with no way for an unattended agent run to assume a
distinct, non-human operate tier, and `break_glass_role()` unconditionally
restored `rapid_operator` on exit regardless of which tier the session had
actually assumed — a latent privilege-widening bug waiting for a second
tier to exist. `RAPID_OPERATOR_TIER` (`"human"`, the default, or `"agent"`)
now selects which role `operator_session()` assumes; the role actually
assumed is threaded back through the `_ASSUMED_ROLES` side table (keyed
by `id(conn)` — see `session.py` for why it is a side table and not an
attribute on the connection) so `break_glass_role()` can restore the
RIGHT role on exit, and — the security property that must not regress — refuse outright when called from a
session that assumed the agent tier, because the agent tier is never
granted `rapid_break_glass` membership and this module must not depend on
Postgres alone to enforce that.

No live database: `_FakeConn`/`_FakeCursor` script exactly the statements
this module issues and record every one, so a test can assert on the EXACT
SQL text (`stub-blind-testing`'s house rule — a double that could not
distinguish `SET ROLE rapid_operator` from `SET ROLE rapid_agent_operator`
could not have caught a regression that assumed the wrong tier).
`_FakeConn.failing_statements` is the fake's ability to say no: it raises
on a scripted `SET ROLE`, the same way a real membership failure would, so
the failure path is genuinely exercised rather than assumed unreachable.
"""

import sys
import types
import unittest
import unittest.mock

# `database.modules.utils.rapid_db_connect` imports `psycopg2` at module
# scope (`import psycopg2`, `import psycopg2.extensions`, `from psycopg2
# import sql`) purely to build a real connection this stub-tier suite never
# calls — `operator_session()` is always invoked with `connect_fn` stubbed
# out below. Matches the identical stub pattern in this package's own
# `test_batch.py` and in `pipeline/reconciler/test/test_main.py`.
if "psycopg2" not in sys.modules:
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        stub = types.ModuleType("psycopg2")
        stub.Error = type("Error", (Exception,), {})
        extensions_stub = types.ModuleType("psycopg2.extensions")
        sql_stub = types.ModuleType("psycopg2.sql")
        stub.extensions = extensions_stub
        stub.sql = sql_stub
        sys.modules["psycopg2"] = stub
        sys.modules["psycopg2.extensions"] = extensions_stub
        sys.modules["psycopg2.sql"] = sql_stub

from pipeline.operatorctl.session import (_ASSUMED_ROLES,
                                          AGENT_OPERATOR_ROLE,
                                          OPERATOR_ROLE,
                                          BREAK_GLASS_ROLE,
                                          SUBMISSION_ROLES,
                                          OperatorSessionError,
                                          break_glass_role,
                                          operator_credentials,
                                          operator_session,
                                          submission_role)
from database.modules.utils.rapid_db_connect import (Credentials,
                                                     DBCredentialError)


class _FakeCursor:
    """Records every `execute()` call; raises for statements the test
    scripts to fail, so a `SET ROLE` refusal is a genuine exception path
    and not merely assumed reachable.
    """

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self._conn.calls.append(normalized)
        for prefix, exc in self._conn.failing_statements:
            if normalized.startswith(prefix):
                raise exc


class _FakeConn:
    """`failing_statements` is a list of `(sql_prefix, exception)` pairs;
    any `execute()` call whose normalized SQL starts with `sql_prefix`
    raises `exception` instead of succeeding — the one seam this suite
    uses to model a `SET ROLE` a login is not a member of.
    """

    def __init__(self, failing_statements=()):
        self.failing_statements = list(failing_statements)
        self.calls = []
        self.rolled_back = 0
        self.closed = False

    def cursor(self):
        return _FakeCursor(self)

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed = True


def _set_role_calls(conn):
    return [c for c in conn.calls if c.startswith("SET ROLE")]


class _SlottedConn:
    """A connection double that REFUSES new attributes, exactly as a real
    `psycopg2.extensions.connection` does.

    This is the double `_FakeConn` above cannot be: `_FakeConn` is an
    ordinary Python object with a `__dict__`, so `conn.rapid_operator_role
    = role` would have succeeded silently against it — which is exactly
    why the original `AttributeError: 'psycopg2.extensions.connection'
    object has no attribute 'rapid_operator_role' and no __dict__ for
    setting new attributes` went undetected by this suite until the
    operate tier was first driven against a real database (2026-08-15).
    `__slots__ = ()` reproduces the C type's refusal: any attempt to set
    an attribute not already named in `__slots__` raises `AttributeError`,
    the same failure mode psycopg2's C extension produces for having no
    `__dict__` at all.
    """

    __slots__ = ("failing_statements", "calls", "rolled_back", "closed")

    def __init__(self, failing_statements=()):
        self.failing_statements = list(failing_statements)
        self.calls = []
        self.rolled_back = 0
        self.closed = False

    def cursor(self):
        return _FakeCursor(self)

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed = True


class OperatorCredentialsTests(unittest.TestCase):
    """Unchanged by this fix — pinned so a future edit to the tier logic
    cannot quietly touch the human credential path these tests were told
    is out of scope.
    """

    def test_pguser_and_pgpassword_resolve_to_credentials(self):
        env = {"PGUSER": "brusholme", "PGPASSWORD": "s3cr3t"}
        with unittest.mock.patch.dict("os.environ", env, clear=True):
            creds = operator_credentials()
        self.assertEqual(creds, Credentials("brusholme", "s3cr3t"))

    def test_missing_pgpassword_yields_a_pgpass_credential(self):
        """The contradiction the previous version of this test PINNED.

        It used to assert `DBCredentialError`, and its docstring recorded
        why: `session.py` documented an unset `PGPASSWORD` falling
        through to `~/.pgpass`, while the `Credentials(user, "")` it
        actually built was refused by `Credentials.__new__`'s falsy-
        password check. The documented path was unreachable, and this
        test held that state in place rather than fixing it.

        Both halves now agree. An unset `PGPASSWORD` resolves to a
        credential carrying `password is None` — NOT `""` — because
        libpq consults `~/.pgpass` only when no password is supplied.
        Asserting `is None` specifically is the point of the test: an
        empty string would satisfy "falsy" and still not reach pgpass.
        """
        env = {"PGUSER": "brusholme"}
        with unittest.mock.patch.dict("os.environ", env, clear=True):
            creds = operator_credentials()
        self.assertEqual(creds.user, "brusholme")
        self.assertIsNone(creds.password)

    def test_an_empty_pgpassword_is_treated_as_unset(self):
        """`PGPASSWORD=` exported empty is a person who set no password,
        not a person whose password is the empty string — no server
        accepts the latter, so reading it literally would turn a common
        shell accident into an authentication failure whose message
        names the wrong cause.
        """
        env = {"PGUSER": "brusholme", "PGPASSWORD": ""}
        with unittest.mock.patch.dict("os.environ", env, clear=True):
            creds = operator_credentials()
        self.assertIsNone(creds.password)

    def test_falls_back_to_user_env_when_pguser_unset(self):
        env = {"USER": "brusholme", "PGPASSWORD": "s3cr3t"}
        with unittest.mock.patch.dict("os.environ", env, clear=True):
            creds = operator_credentials()
        self.assertEqual(creds.user, "brusholme")

    def test_no_login_role_available_raises_db_credential_error(self):
        with unittest.mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(DBCredentialError):
                operator_credentials()


class OperatorSessionTierSelectionTests(unittest.TestCase):
    """Which role `operator_session()` assumes, driven by
    `RAPID_OPERATOR_TIER`, asserted on the EXACT SQL text executed.
    """

    def _run(self, env):
        conn = _FakeConn()
        # A non-empty password: `Credentials.__new__` (031:154-157,
        # unrelated to this change) rejects an empty one outright, so
        # these tier-selection tests use a placeholder password to stay
        # clear of that pre-existing behaviour rather than testing it.
        creds = Credentials("agent_sims_fix_2026_08", "placeholder")

        def connect_fn(application_name, lane=None, credentials=None):
            return conn

        assumed_role_in_block = None
        with unittest.mock.patch.dict("os.environ", env, clear=True):
            with operator_session(credentials=creds,
                                  connect_fn=connect_fn) as yielded:
                self.assertIs(yielded, conn)
                # `_ASSUMED_ROLES` is popped in `operator_session()`'s
                # `finally`, so the entry only exists while the block is
                # open — read it here, not after the `with` exits.
                assumed_role_in_block = _ASSUMED_ROLES.get(id(conn))
        return conn, assumed_role_in_block

    def test_no_env_var_set_assumes_rapid_operator_exactly_as_today(self):
        conn, assumed_role = self._run({})
        self.assertEqual(_set_role_calls(conn),
                         ["SET ROLE " + OPERATOR_ROLE])
        self.assertEqual(assumed_role, OPERATOR_ROLE)

    def test_tier_human_explicitly_assumes_rapid_operator(self):
        conn, assumed_role = self._run({"RAPID_OPERATOR_TIER": "human"})
        self.assertEqual(_set_role_calls(conn),
                         ["SET ROLE " + OPERATOR_ROLE])
        self.assertEqual(assumed_role, OPERATOR_ROLE)

    def test_tier_agent_assumes_rapid_agent_operator(self):
        conn, assumed_role = self._run({"RAPID_OPERATOR_TIER": "agent"})
        self.assertEqual(_set_role_calls(conn),
                         ["SET ROLE " + AGENT_OPERATOR_ROLE])
        self.assertEqual(assumed_role, AGENT_OPERATOR_ROLE)

    def test_the_default_path_is_byte_for_byte_unchanged(self):
        """Same SQL text for unset and for `human` — the two must be
        indistinguishable, which is the "byte-for-byte" requirement.
        """
        default_conn, _ = self._run({})
        human_conn, _ = self._run({"RAPID_OPERATOR_TIER": "human"})
        self.assertEqual(_set_role_calls(default_conn),
                         _set_role_calls(human_conn))

    def test_invalid_tier_value_raises_and_never_connects_or_sets_role(
            self):
        connected = []

        def connect_fn(application_name, lane=None, credentials=None):
            connected.append(True)
            return _FakeConn()

        with unittest.mock.patch.dict(
                "os.environ", {"RAPID_OPERATOR_TIER": "superuser"},
                clear=True):
            with self.assertRaises(OperatorSessionError) as ctx:
                with operator_session(credentials=Credentials("x", "placeholder"),
                                      connect_fn=connect_fn):
                    self.fail("the body of the with-block must not run")

        self.assertIn("RAPID_OPERATOR_TIER", str(ctx.exception))
        self.assertIn("superuser", str(ctx.exception))
        self.assertEqual(connected, [],
                         "an invalid tier must be rejected before any "
                         "connection is opened")

    def test_failed_set_role_raises_operator_session_error_naming_the_role(
            self):
        conn = _FakeConn(failing_statements=[
            ("SET ROLE " + AGENT_OPERATOR_ROLE,
             RuntimeError("permission denied to set role"))])

        def connect_fn(application_name, lane=None, credentials=None):
            return conn

        with unittest.mock.patch.dict(
                "os.environ", {"RAPID_OPERATOR_TIER": "agent"}, clear=True):
            with self.assertRaises(OperatorSessionError) as ctx:
                with operator_session(credentials=Credentials("x", "placeholder"),
                                      connect_fn=connect_fn):
                    self.fail("the body of the with-block must not run")

        self.assertIn(AGENT_OPERATOR_ROLE, str(ctx.exception))
        self.assertEqual(conn.rolled_back, 1)
        self.assertTrue(conn.closed, "the connection must still be closed "
                        "on a failed role assumption")

    def test_failed_set_role_on_human_tier_names_rapid_operator(self):
        conn = _FakeConn(failing_statements=[
            ("SET ROLE " + OPERATOR_ROLE,
             RuntimeError("permission denied to set role"))])

        def connect_fn(application_name, lane=None, credentials=None):
            return conn

        with unittest.mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(OperatorSessionError) as ctx:
                with operator_session(credentials=Credentials("x", "placeholder"),
                                      connect_fn=connect_fn):
                    self.fail("the body of the with-block must not run")

        self.assertIn(OPERATOR_ROLE, str(ctx.exception))
        self.assertNotIn(AGENT_OPERATOR_ROLE, str(ctx.exception))


class BreakGlassRoleTests(unittest.TestCase):
    """Break-glass stays reachable ONLY from the human tier — a security
    property this module must enforce itself, not merely inherit from
    whatever the database happens to grant.
    """

    def test_break_glass_from_the_agent_tier_is_refused(self):
        conn = _FakeConn()
        _ASSUMED_ROLES[id(conn)] = AGENT_OPERATOR_ROLE
        self.addCleanup(_ASSUMED_ROLES.pop, id(conn), None)

        with self.assertRaises(OperatorSessionError) as ctx:
            with break_glass_role(conn):
                self.fail("the body of the with-block must not run")

        self.assertIn(AGENT_OPERATOR_ROLE, str(ctx.exception))
        # The refusal must happen BEFORE any SET ROLE is attempted — this
        # module's own logic is the gate, not a database error surfacing
        # after the fact.
        self.assertEqual(_set_role_calls(conn), [])

    def test_break_glass_from_the_human_tier_still_works(self):
        conn = _FakeConn()
        _ASSUMED_ROLES[id(conn)] = OPERATOR_ROLE
        self.addCleanup(_ASSUMED_ROLES.pop, id(conn), None)

        with break_glass_role(conn) as bg_conn:
            self.assertIs(bg_conn, conn)

        self.assertEqual(_set_role_calls(conn),
                         ["SET ROLE " + BREAK_GLASS_ROLE,
                          "SET ROLE " + OPERATOR_ROLE])

    def test_break_glass_restores_the_human_role_on_exit_not_a_constant(
            self):
        """Pins the fix itself: restoring `OPERATOR_ROLE` unconditionally
        would be a latent privilege-widening bug the moment a second tier
        existed, even though today it happens to be the only tier
        break-glass is reachable from.
        """
        conn = _FakeConn()
        _ASSUMED_ROLES[id(conn)] = OPERATOR_ROLE
        self.addCleanup(_ASSUMED_ROLES.pop, id(conn), None)

        with break_glass_role(conn):
            pass

        self.assertEqual(conn.calls[-1], "SET ROLE " + OPERATOR_ROLE)

    def test_a_session_missing_the_role_marker_is_treated_as_human(self):
        """A connection that never went through `operator_session()` has
        no entry in `_ASSUMED_ROLES` at all. Defaulting that to the human
        tier keeps any caller that constructs a connection some other way
        working exactly as before, rather than refusing on a marker it
        never had reason to set.
        """
        conn = _FakeConn()
        self.assertNotIn(id(conn), _ASSUMED_ROLES)

        with break_glass_role(conn):
            pass

        self.assertEqual(_set_role_calls(conn),
                         ["SET ROLE " + BREAK_GLASS_ROLE,
                          "SET ROLE " + OPERATOR_ROLE])

    def test_break_glass_set_role_failure_raises_naming_break_glass_role(
            self):
        conn = _FakeConn(failing_statements=[
            ("SET ROLE " + BREAK_GLASS_ROLE,
             RuntimeError("permission denied to set role"))])
        _ASSUMED_ROLES[id(conn)] = OPERATOR_ROLE
        self.addCleanup(_ASSUMED_ROLES.pop, id(conn), None)

        with self.assertRaises(OperatorSessionError) as ctx:
            with break_glass_role(conn):
                self.fail("the body of the with-block must not run")

        self.assertIn(BREAK_GLASS_ROLE, str(ctx.exception))
        self.assertEqual(conn.rolled_back, 1)


class OperatorSessionAgainstAnAttributeRefusingConnectionTests(
        unittest.TestCase):
    """Regression test for the live defect (2026-08-15):
    `AttributeError: 'psycopg2.extensions.connection' object has no
    attribute 'rapid_operator_role' and no __dict__ for setting new
    attributes`.

    Every other test in this module uses `_FakeConn`, an ordinary Python
    object that accepts any attribute silently — a double that CANNOT
    refuse what the real `psycopg2.extensions.connection` C type forbids,
    so it could not have caught `operator_session()` setting an attribute
    directly on the connection. `_SlottedConn` closes that gap: its
    `__slots__ = ()`-style refusal of unknown attributes reproduces the
    real type's behaviour, so this test would fail with the exact
    `AttributeError` above if the fix regressed back to
    `conn.rapid_operator_role = role`.
    """

    def test_operator_session_completes_against_an_attribute_refusing_conn(
            self):
        conn = _SlottedConn()

        def connect_fn(application_name, lane=None, credentials=None):
            return conn

        with unittest.mock.patch.dict("os.environ", {}, clear=True):
            with operator_session(
                    credentials=Credentials("x", "placeholder"),
                    connect_fn=connect_fn) as yielded:
                self.assertIs(yielded, conn)
                # The tier must be recorded in the side table, not on the
                # connection — `conn` has no `__dict__` to record it in.
                self.assertEqual(_ASSUMED_ROLES.get(id(conn)),
                                 OPERATOR_ROLE)

        self.assertEqual(_set_role_calls(conn),
                         ["SET ROLE " + OPERATOR_ROLE])
        # The entry must not outlive the session, or a later object
        # reusing this freed `id()` could inherit a stale tier.
        self.assertNotIn(id(conn), _ASSUMED_ROLES)

    def test_break_glass_completes_against_an_attribute_refusing_conn(self):
        conn = _SlottedConn()
        _ASSUMED_ROLES[id(conn)] = OPERATOR_ROLE
        self.addCleanup(_ASSUMED_ROLES.pop, id(conn), None)

        with break_glass_role(conn) as bg_conn:
            self.assertIs(bg_conn, conn)

        self.assertEqual(_set_role_calls(conn),
                         ["SET ROLE " + BREAK_GLASS_ROLE,
                          "SET ROLE " + OPERATOR_ROLE])


class SubmissionRoleTests(unittest.TestCase):
    """Stub-tier tests for `submission_role()` — the identity-fix ruling
    (2026-09-11) that lets `run start --apply` perform the pipeline's own
    submission INSERT without widening `rapid_operator` itself. See
    `submission_role()`'s own docstring in `session.py` for the full
    reasoning; this suite pins the three properties that must not
    regress: the role actually switches (a different `current_user` while
    inside the block), the operate tier is restored on exit — not a
    hard-coded constant — and a login that can assume none of
    `SUBMISSION_ROLES` raises `OperatorSessionError` naming every role it
    tried, having issued each attempt through a SAVEPOINT so a rejected
    first guess cannot poison the second.
    """

    def test_the_first_assumable_role_is_used_and_current_user_switches(
            self):
        conn = _FakeConn()
        _ASSUMED_ROLES[id(conn)] = OPERATOR_ROLE
        self.addCleanup(_ASSUMED_ROLES.pop, id(conn), None)

        with submission_role(conn) as yielded:
            self.assertIs(yielded, conn)
            self.assertEqual(_set_role_calls(conn),
                             ["SET ROLE " + SUBMISSION_ROLES[0]])

        # Restored to the tier `operator_session()` actually recorded —
        # not a constant — on exit.
        self.assertEqual(_set_role_calls(conn),
                         ["SET ROLE " + SUBMISSION_ROLES[0],
                          "SET ROLE " + OPERATOR_ROLE])

    def test_restores_the_agent_tier_not_a_hard_coded_human_role(self):
        """The restore must come from `_ASSUMED_ROLES`, never a constant
        — an agent-tier session that widens for submission must narrow
        back to the agent tier, not silently promote itself to human.
        """
        conn = _FakeConn()
        _ASSUMED_ROLES[id(conn)] = AGENT_OPERATOR_ROLE
        self.addCleanup(_ASSUMED_ROLES.pop, id(conn), None)

        with submission_role(conn):
            pass

        self.assertEqual(_set_role_calls(conn)[-1],
                         "SET ROLE " + AGENT_OPERATOR_ROLE)

    def test_a_session_missing_the_role_marker_is_treated_as_human(self):
        """Same default `break_glass_role()` uses for a connection that
        never went through `operator_session()` at all.
        """
        conn = _FakeConn()
        self.assertNotIn(id(conn), _ASSUMED_ROLES)

        with submission_role(conn):
            pass

        self.assertEqual(_set_role_calls(conn)[-1],
                         "SET ROLE " + OPERATOR_ROLE)

    def test_falls_through_to_the_second_role_when_the_first_is_refused(
            self):
        """Models a login that is a member of `rapid_orchestrator` but
        not `rapid_admin` — the mirror image of the human login this
        ruling was measured against, which is a member of `rapid_admin`
        but not `rapid_orchestrator`. Neither name may be hard-coded.
        """
        conn = _FakeConn(failing_statements=[
            ("SET ROLE " + SUBMISSION_ROLES[0], Exception("not a member"))])
        _ASSUMED_ROLES[id(conn)] = OPERATOR_ROLE
        self.addCleanup(_ASSUMED_ROLES.pop, id(conn), None)

        with submission_role(conn):
            calls = _set_role_calls(conn)
            self.assertEqual(calls,
                             ["SET ROLE " + SUBMISSION_ROLES[0],
                              "SET ROLE " + SUBMISSION_ROLES[1]])

        # The failed first attempt must not have poisoned the connection:
        # no bare `conn.rollback()` was needed to recover from it, because
        # the attempt was wrapped in its own SAVEPOINT.
        self.assertEqual(conn.rolled_back, 0)
        rollback_calls = [c for c in conn.calls
                          if c.startswith("ROLLBACK TO SAVEPOINT")]
        self.assertEqual(len(rollback_calls), 1)

    def test_no_assumable_role_raises_naming_every_role_tried(self):
        conn = _FakeConn(failing_statements=[
            (sql, Exception("not a member")) for sql in
            ("SET ROLE " + r for r in SUBMISSION_ROLES)])
        _ASSUMED_ROLES[id(conn)] = OPERATOR_ROLE
        self.addCleanup(_ASSUMED_ROLES.pop, id(conn), None)

        with self.assertRaises(OperatorSessionError) as ctx:
            with submission_role(conn):
                self.fail("must not yield when no role can be assumed")

        message = str(ctx.exception)
        for role in SUBMISSION_ROLES:
            self.assertIn(role, message)
        # The failure must not have left the connection in an aborted
        # transaction state that a bare `conn.rollback()` would be needed
        # to clear — each attempt is its own SAVEPOINT.
        self.assertEqual(conn.rolled_back, 0)

    def test_a_failing_body_keeps_its_own_exception_when_the_restore_cannot_run(
            self):
        """The restore must never replace the caller's real failure.

        FOUND BY A DEFECT REVIEW AND CONFIRMED AGAINST THE LIVE DATABASE
        (2026-09-11). If the body raises something that aborts the
        transaction — any database error — PostgreSQL then refuses every
        statement on that connection with `InFailedSqlTransaction`. The
        restore in `finally` is such a statement, so a bare `SET ROLE`
        there raises from inside `finally` and REPLACES the exception the
        caller actually needs to see. Measured: a divide-by-zero in the
        body followed by the restore produced exactly that substitution.

        Swallowing the restore's own failure is safe because an aborted
        transaction has already discarded the widening `SET ROLE` —
        `current_user` falls back to the bare login, narrower than either
        role — so the session cannot be left wide in the one case the
        restore cannot run.
        """
        body_error = RuntimeError("the failure the caller must see")
        restore_error = Exception(
            "current transaction is aborted, commands ignored until end "
            "of transaction block")
        conn = _FakeConn(failing_statements=[
            ("SET ROLE " + OPERATOR_ROLE, restore_error)])
        _ASSUMED_ROLES[id(conn)] = OPERATOR_ROLE
        self.addCleanup(_ASSUMED_ROLES.pop, id(conn), None)

        with self.assertRaises(RuntimeError) as ctx:
            with submission_role(conn):
                raise body_error

        # The BODY's exception, not the restore's — this is the whole point.
        self.assertIs(ctx.exception, body_error)

    def test_does_not_widen_the_operate_tier_itself(self):
        """Regression guard for the ruling's central constraint: the fix
        must not be a wider grant on `rapid_operator`/`rapid_agent_
        operator` — it must be a scoped, temporary switch. This is not
        directly testable against a fake connection (no live grants
        here), so it is pinned structurally: `operator_session()` must
        still assume exactly `OPERATOR_ROLE` with no other statement
        alongside it, i.e. `submission_role()` is a distinct, separate
        assumption layered on top rather than a change to what
        `operator_session()` itself assumes.
        """
        conn = _FakeConn()

        def connect_fn(application_name, lane=None, credentials=None):
            return conn

        with unittest.mock.patch.dict("os.environ", {}, clear=True):
            with operator_session(credentials=Credentials("x", "placeholder"),
                                  connect_fn=connect_fn):
                pass

        self.assertEqual(_set_role_calls(conn), ["SET ROLE " + OPERATOR_ROLE])


if __name__ == "__main__":
    unittest.main()
