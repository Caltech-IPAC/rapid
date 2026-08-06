"""The borrowed-connection path through `RAPIDDB` (round-3 finding #8).

`RAPIDDB` was built for scripts: every one of its thirty-two write methods
ends with `self.conn.commit()`, so each call is its own transaction and the row
is durable when the call returns. Product registration needs the opposite —
several calls that land together or not at all, in the same transaction as the
registered-watermark write the consumer does on ITS connection. Two connections
cannot be one transaction, so the class had to learn to borrow.

**Why a separate module from `test_rapid_db.py`.** That suite imports psycopg2
and `psycopg2.sql` for real, because what it asserts is that hostile values
reach the driver as parameters rather than as SQL text — stubbing the driver
there would test the stub. Nothing here needs a driver at all: the borrowed
connection is supplied by the caller, which is the entire point. So this
installs the smallest stand-in that makes the import boundary crossable and
runs anywhere, off-image included. Same pattern and same reasoning as
`pipeline/test/test_vpo_phases.py`.
"""

import sys
import types
import unittest


def _stub(name):
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def _install_third_party_stubs():
    if "psycopg2" not in sys.modules:
        psycopg2 = _stub("psycopg2")
        # A PACKAGE, not a module: `rapid_db` does `import psycopg2.sql as sql`,
        # which fails with "not a package" against a bare module stub.
        psycopg2.__path__ = []
        psycopg2.connect = lambda *_a, **_k: None
        psycopg2.DatabaseError = Exception
        psycopg2.OperationalError = Exception
        psycopg2.InterfaceError = Exception
        extensions = _stub("psycopg2.extensions")
        extensions.ISOLATION_LEVEL_AUTOCOMMIT = 0
        sql = _stub("psycopg2.sql")
        sql.Identifier = lambda *a: None
        sql.SQL = lambda *a: None
        psycopg2.extensions = extensions
        psycopg2.sql = sql


_install_third_party_stubs()

from database.modules.utils.rapid_db import BorrowedConnection, RAPIDDB


class FakeCursor:
    def __init__(self, owner):
        self.owner = owner

    def execute(self, statement, params=None):
        self.owner.statements.append((statement, params))

    def fetchone(self):
        return (1, 1)

    def __iter__(self):
        return iter([])

    def close(self):
        self.owner.closed_cursors += 1


class FakeConnection:
    """The connection a caller lends. Counts what it was actually asked to do."""

    def __init__(self):
        self.statements = []
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.closed_cursors = 0
        self.isolation_level = 1

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closes += 1

    def set_isolation_level(self, level):
        self.isolation_level = level


class BorrowedConnectionTests(unittest.TestCase):
    """The wrapper that stops a borrower from ending its lender's transaction."""

    def test_commit_is_swallowed_and_counted(self):
        # THE DEFECT, at its narrowest. The thirty-two `self.conn.commit()`
        # sites are what made the product rows durable before the watermark was
        # even attempted. On a borrowed connection they must reach nothing.
        real = FakeConnection()
        borrowed = BorrowedConnection(real)

        borrowed.commit()
        borrowed.commit()

        self.assertEqual(0, real.commits,
                         "a borrowed connection committed its lender's "
                         "transaction; the unit of work is split again")
        self.assertEqual(2, borrowed.commits_suppressed)

    def test_rollback_is_swallowed_and_counted(self):
        # Equally not the borrower's to call: these methods report through
        # `exit_code` rather than by raising, so a rollback here would discard
        # the lender's work silently.
        real = FakeConnection()
        borrowed = BorrowedConnection(real)

        borrowed.rollback()

        self.assertEqual(0, real.rollbacks)
        self.assertEqual(1, borrowed.rollbacks_suppressed)

    def test_close_is_swallowed(self):
        real = FakeConnection()
        BorrowedConnection(real).close()
        self.assertEqual(0, real.closes,
                         "a borrowed connection was closed out from under the "
                         "caller still using it")

    def test_everything_else_passes_through(self):
        # The wrapper must be the real connection in every respect except the
        # transaction boundary — `vacuum_analyze_table` reads and writes
        # `isolation_level`, and the query methods all take cursors from it.
        real = FakeConnection()
        borrowed = BorrowedConnection(real)

        cur = borrowed.cursor()
        cur.execute("select 1", None)
        borrowed.set_isolation_level(0)

        self.assertEqual([("select 1", None)], real.statements)
        self.assertEqual(0, borrowed.isolation_level)


class BorrowingHandleTests(unittest.TestCase):
    """`RAPIDDB(conn=...)`: no connect, no commits, and above all no exit()."""

    def test_the_handle_uses_the_connection_it_was_given(self):
        real = FakeConnection()
        dbh = RAPIDDB(conn=real)

        self.assertIs(real, dbh.conn._conn)
        self.assertFalse(dbh.owns_connection)
        self.assertEqual(0, dbh.exit_code)

    def test_the_classmethod_is_the_same_thing_by_a_clearer_name(self):
        real = FakeConnection()
        self.assertIs(real, RAPIDDB.borrowing(real).conn._conn)

    def test_a_write_method_does_not_commit_on_a_borrowed_connection(self):
        # End to end through a real method. `add_refimage` ends with
        # `if self.exit_code == 0: self.conn.commit()` — unchanged, and
        # correctly so, because the wrapper is what makes it a no-op. The row
        # becomes durable when the CALLER's transaction commits, not here.
        real = FakeConnection()
        dbh = RAPIDDB.borrowing(real)

        dbh.add_refimage(1, 2, 3, 4, 5, 0, 1, "s3://b/ref.fits", "cksum",
                         42, 1)

        self.assertEqual(0, dbh.exit_code)
        self.assertEqual(1, len(real.statements),
                         "the insert did not reach the borrowed connection")
        self.assertEqual(0, real.commits,
                         "add_refimage committed on a borrowed connection: "
                         "the product row is durable before the watermark, "
                         "which is exactly the defect")
        self.assertEqual(1, dbh.conn.commits_suppressed)

    def test_the_attempt_identity_is_the_last_two_parameters(self):
        # The stored function declares them last and defaulted (migration 018),
        # so they must arrive last. Anywhere else and every legacy argument
        # after them shifts by two.
        real = FakeConnection()
        dbh = RAPIDDB.borrowing(real)

        dbh.add_refimage(1, 2, 3, 4, 5, 0, 1, "s3://b/ref.fits", "cksum",
                         42, 7)

        _statement, params = real.statements[0]
        self.assertEqual((42, 7), params[-2:])

    def test_omitting_the_identity_sends_nulls_and_nothing_else_changes(self):
        # Optional means optional. The stored function defaults them, so a
        # legacy caller behaves exactly as before: mint a version, insert.
        real = FakeConnection()
        dbh = RAPIDDB.borrowing(real)

        dbh.add_refimage(1, 2, 3, 4, 5, 0, 1, "s3://b/ref.fits", "cksum")

        _statement, params = real.statements[0]
        self.assertEqual((None, None), params[-2:])

    def test_add_diffimage_carries_the_identity_last_too(self):
        real = FakeConnection()
        dbh = RAPIDDB.borrowing(real)
        corners = [float(n) for n in range(10)]

        dbh.add_diffimage(1, 2, 3, 0, 0, *corners, 1, "s3://b/diff.fits",
                          "cksum", 99, 4)

        _statement, params = real.statements[0]
        self.assertEqual((99, 4), params[-2:])
        self.assertEqual(0, real.commits)

    def test_the_placeholder_count_matches_the_parameter_count(self):
        # The two new `cast(%s as ...)` placeholders and the two new params
        # have to arrive together; one without the other is a driver error at
        # the first live call and nowhere earlier.
        real = FakeConnection()
        dbh = RAPIDDB.borrowing(real)
        corners = [float(n) for n in range(10)]

        dbh.add_refimage(1, 2, 3, 4, 5, 0, 1, "f", "c", 1, 1)
        dbh.add_diffimage(1, 2, 3, 0, 0, *corners, 1, "f", "c", 1, 1)

        for statement, params in real.statements:
            self.assertEqual(statement.count("%s"), len(params),
                             f"placeholder/parameter mismatch in {statement}")

    def test_closing_a_borrowed_handle_leaves_the_connection_open(self):
        # The lender is still using it and will close it when its own block
        # ends. The cursor IS closed — that one the handle opened.
        real = FakeConnection()
        dbh = RAPIDDB.borrowing(real)

        dbh.close()

        self.assertEqual(0, real.closes)
        self.assertEqual(1, real.closed_cursors)

    def test_the_borrowed_path_never_reaches_the_env_var_exit(self):
        # `RAPIDDB.__init__` calls `exit(64)` straight out of library code when
        # DBSERVER/DBPORT/DBNAME/DBUSER/DBPASS is missing, which takes the whole
        # process down. A caller that already holds a working connection has
        # already proved the configuration is fine and must never be at risk of
        # that. The environment is cleared here to prove the branch is taken
        # before any of those reads — an unguarded path would SystemExit.
        import os
        from unittest import mock

        real = FakeConnection()
        with mock.patch.dict(os.environ, {}, clear=True):
            dbh = RAPIDDB.borrowing(real)

        self.assertIs(real, dbh.conn._conn)


if __name__ == "__main__":
    unittest.main()
