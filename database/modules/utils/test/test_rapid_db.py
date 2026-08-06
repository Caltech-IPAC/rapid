"""
File:    test_rapid_db.py

Regression tests for the W3 SQL-parameterization sweep of ``rapid_db.py``.

Nothing here opens a connection. ``RAPIDDB`` has no injection seam in
``__init__`` (it connects unconditionally), so each test builds an instance
with ``__new__`` and stubs ``conn``/``cur`` directly — the same "assert what
the driver was actually handed" approach as
``test_rapid_db_connect.py::ConnectionExecutorTests``.

What is asserted is the one property this sweep exists to guarantee: a value
containing a single quote reaches ``cursor.execute`` as a separate parameter,
never interpolated into the query text. Before the sweep, every one of these
methods built its query with an f-string or a TEMPLATE_-regex substitution,
so a quote in ``dateobs``/``filename``/a table name would have landed in the
SQL text itself. The methods below are one representative of each converted
pattern (TEMPLATE_ regex, plain f-string with a value, f-string with a
dynamic identifier, and insert-with-identifier-and-values); passing here for
all of them is evidence the pattern, not just one method, was fixed.
"""

import unittest
from unittest import mock

from psycopg2 import sql

from database.modules.utils.rapid_db import RAPIDDB

HOSTILE = "O'Brien's; DROP TABLE l2files; --"


def make_db(fetchone=None, iter_rows=()):
    """A RAPIDDB with __init__ (and its unconditional connect) bypassed.

    ``cur`` is a MagicMock standing in for a psycopg2 cursor: ``execute`` is
    the call under test, ``fetchone``/iteration are primed with whatever the
    method under test needs to read back.
    """
    db = RAPIDDB.__new__(RAPIDDB)
    db.exit_code = 0
    db.conn = mock.MagicMock(name="conn")
    db.cur = mock.MagicMock(name="cur")
    db.cur.fetchone.return_value = fetchone
    db.cur.__iter__.return_value = iter(iter_rows)
    return db


def literal_text(composed):
    """Concatenate only the literal (sql.SQL) fragments of a Composed query.

    Mirrors ``test_rapid_db_connect.py``'s helper of the same name: anything
    gathered here is text the database will parse as SQL. A value that ended
    up in here rather than in the params tuple would be the injection this
    sweep removes.
    """
    if isinstance(composed, str):
        return composed
    out = []

    def walk(node):
        if isinstance(node, sql.Composed):
            for child in node.seq:
                walk(child)
        elif isinstance(node, sql.SQL):
            out.append(node.string)

    walk(composed)
    return "".join(out)


class TemplateRegexPatternRoundTripTests(unittest.TestCase):
    """``add_exposure``: was TEMPLATE_ regex substitution into a stored-function call."""

    def test_hostile_dateobs_is_a_parameter_not_query_text(self):
        db = make_db(fetchone=(101, 3))
        db.add_exposure(HOSTILE, 60000.0, 42, 6, 9, "F184", 100.0, 0, 1)

        query, params = db.cur.execute.call_args.args
        self.assertNotIn(HOSTILE, literal_text(query))
        self.assertIn(HOSTILE, params)
        # The value travels verbatim -- no str()-then-reparse round trip that
        # could itself mangle a quote.
        self.assertEqual(params[0], HOSTILE)

    def test_success_sets_expid_and_fid_from_the_row(self):
        db = make_db(fetchone=(101, 3))
        db.add_exposure("2026-08-06T00:00:00Z", 60000.0, 42, 6, 9, "F184",
                        100.0, 0, 1)
        self.assertEqual(db.expid, 101)
        self.assertEqual(db.fid, 3)
        self.assertEqual(db.exit_code, 0)
        db.conn.commit.assert_called_once_with()

    def test_no_row_returned_sets_exit_code_67_and_does_not_commit(self):
        db = make_db(fetchone=None)
        db.add_exposure("2026-08-06T00:00:00Z", 60000.0, 42, 6, 9, "F184",
                        100.0, 0, 1)
        self.assertEqual(db.exit_code, 67)
        db.conn.commit.assert_not_called()


class FStringValueOnlyPatternRoundTripTests(unittest.TestCase):
    """``update_l2filemeta_hp6``: was a plain f-string with two interpolated values."""

    def test_params_travel_separately_from_the_query_text(self):
        db = make_db(iter_rows=[])
        db.update_l2filemeta_hp6(HOSTILE, 12345)

        query, params = db.cur.execute.call_args.args
        self.assertIsInstance(query, str)
        self.assertNotIn(HOSTILE, query)
        self.assertIn("%s", query)
        self.assertEqual(params, (12345, HOSTILE))

    def test_a_db_error_sets_exit_code_67(self):
        db = make_db()
        db.cur.execute.side_effect = Exception("boom")
        db.update_l2filemeta_hp6("rid-1", 5)
        self.assertEqual(db.exit_code, 67)
        db.conn.commit.assert_not_called()


class DynamicIdentifierValuePatternRoundTripTests(unittest.TestCase):
    """``delete_merge_from_field``: dynamic table name (Identifier) plus a value."""

    def test_hostile_table_name_never_becomes_raw_sql_text(self):
        db = make_db()
        db.cur.rowcount = 1
        db.delete_merge_from_field(HOSTILE, 7)

        query, params = db.cur.execute.call_args.args
        # The table name is carried as an sql.Identifier, quoted at render
        # time by psycopg2 -- it must not appear in the literal SQL text.
        self.assertNotIn(HOSTILE, literal_text(query))
        self.assertEqual(params, (7,))

    def test_the_hostile_name_is_present_as_an_identifier(self):
        db = make_db()
        db.cur.rowcount = 1
        db.delete_merge_from_field(HOSTILE, 7)
        query, _params = db.cur.execute.call_args.args
        identifiers = [part for part in query.seq if isinstance(part, sql.Identifier)]
        self.assertEqual(identifiers, [sql.Identifier(HOSTILE)])

    def test_success_commits_and_returns_none(self):
        db = make_db()
        db.cur.rowcount = 1
        db.delete_merge_from_field("merges_42", 7)
        self.assertEqual(db.exit_code, 0)
        db.conn.commit.assert_called_once_with()


class IdentifierAndValuesInsertPatternRoundTripTests(unittest.TestCase):
    """``add_astro_object_to_field``: dynamic identifier AND multiple values, RETURNING."""

    def test_hostile_table_name_and_value_both_stay_out_of_the_literal_text(self):
        db = make_db(fetchone=(99,))
        db.add_astro_object_to_field(HOSTILE, 10.5, -20.25, HOSTILE, 42, 6, 9)

        query, params = db.cur.execute.call_args.args
        text = literal_text(query)
        self.assertNotIn(HOSTILE, text)
        self.assertIn("RETURNING", text)
        # flux0 (the second HOSTILE argument) travels as a parameter too.
        self.assertEqual(params, (10.5, -20.25, HOSTILE, 42, 6, 9))

    def test_success_returns_the_aid_and_commits(self):
        db = make_db(fetchone=(99,))
        aid = db.add_astro_object_to_field("astroobjects_42", 10.5, -20.25,
                                           100.0, 42, 6, 9)
        self.assertEqual(aid, 99)
        self.assertEqual(db.exit_code, 0)
        db.conn.commit.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
