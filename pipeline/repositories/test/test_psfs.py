"""The PSF registration repository over a fake connection: which stored
function each write calls, with which arguments, and that promotion reports
the row's vbest rather than the request. No database."""

import unittest

from pipeline.repositories.errors import RepositoryQueryFailed
from pipeline.repositories.psfs import PsfRepository, PsfRow


class _Cursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=()):
        self._conn.statements.append((statement, tuple(params)))
        if self._conn.fail_on and self._conn.fail_on in statement:
            raise RuntimeError("boom")
        self._row = self._conn.rows.pop(0) if self._conn.rows else None

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, rows=(), fail_on=None):
        self.rows = list(rows)
        self.fail_on = fail_on
        self.statements = []
        self.rollbacks = 0
        self.commits = 0

    def cursor(self):
        return _Cursor(self)

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        self.commits += 1


class AddTests(unittest.TestCase):

    def test_calls_addpsf_with_the_row_and_returns_its_key(self):
        conn = _Connection(rows=[(42, 3)])
        repo = PsfRepository(conn)
        row = repo.add(8, 7, "s3://b/g/psfs/x.fits", "d41d8cd9", status=1)
        self.assertEqual(row, PsfRow(42, 3))
        statement, params = conn.statements[0]
        self.assertIn("addPSF(", statement)
        self.assertEqual(params, (8, 7, "s3://b/g/psfs/x.fits", "d41d8cd9", 1,
                                  None, None))
        self.assertEqual(conn.commits, 0)

    def test_attempt_identity_is_threaded_when_given(self):
        conn = _Connection(rows=[(1, 1)])
        PsfRepository(conn).add(8, 7, "f", "c", 1, attempt_id=99, record_sequence=2)
        self.assertEqual(conn.statements[0][1][-2:], (99, 2))

    def test_no_row_is_a_failure_not_none(self):
        conn = _Connection(rows=[None])
        with self.assertRaises(RepositoryQueryFailed):
            PsfRepository(conn).add(8, 7, "f", "c")

    def test_a_driver_error_rolls_back_and_is_retyped(self):
        conn = _Connection(fail_on="addPSF")
        with self.assertRaises(RepositoryQueryFailed):
            PsfRepository(conn).add(8, 7, "f", "c")
        self.assertEqual(conn.rollbacks, 1)


class PromoteTests(unittest.TestCase):

    def test_reports_the_vbest_the_row_holds(self):
        # updatePSF returns void; the read-back says 1.
        conn = _Connection(rows=[None, (1,)])
        self.assertEqual(PsfRepository(conn).promote(42, "f", "c", 1, 3), 1)
        self.assertIn("updatePSF(", conn.statements[0][0])
        self.assertEqual(conn.statements[0][1][:5], (42, "f", "c", 1, 3))
        self.assertIn("select vbest", conn.statements[1][0])

    def test_a_pinned_incumbent_leaves_the_row_at_zero_and_says_so(self):
        conn = _Connection(rows=[None, (0,)])
        self.assertEqual(PsfRepository(conn).promote(42, "f", "c", 1, 3), 0)


if __name__ == "__main__":
    unittest.main()
