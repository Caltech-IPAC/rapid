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


class OverlapExclusionClauseTests(unittest.TestCase):
    """Round-4 finding #3: what each exclusion branch actually emits.

    THE PLACEHOLDER COUNT IS THE PROPERTY. The open branch used to emit
    `a.rid is not %s` and bind the string 'null' through it, so PostgreSQL
    parsed `a.rid IS NOT 'null'` and rejected the whole query — the reference
    stage gathered nothing and reported exit_code 67. Historically it parsed
    only because the value was substituted literally; the parameterization
    sweep changed that silently, which is why nothing here caught it.

    These assert the SHAPE cheaply and without a server. What the server makes
    of the text is proven by `submission/test/live_fixe_overlap_sql.py`,
    against a real PostgreSQL, because a mocked cursor accepts any string at
    all — including one that cannot parse.
    """

    #: TEN values: the tile CENTRE (ra0/dec0) followed by the four corners.
    #: The method's signature spells them out individually, and it is the
    #: centre that the cone search is anchored on.
    CORNERS = (10.0, 20.0,
               10.01, 20.01, 10.01, 19.99, 9.99, 19.99, 9.99, 20.01)

    def _execute(self, rid):
        db = make_db(iter_rows=[])
        db.get_overlapping_l2files(rid, 1, 999999.9, *self.CORNERS,
                                   radius_of_initial_cone_search=0.18)
        query, params = db.cur.execute.call_args.args
        return literal_text(query), params

    def test_the_open_branch_emits_no_exclusion_clause_at_all(self):
        text, _ = self._execute(None)

        # Neither spelling of the predicate. "Exclude nothing" is the ABSENCE
        # of a clause, not a clause that happens to be universally true.
        self.assertNotIn("is not", text.lower())
        self.assertNotIn("a.rid !=", text)

    def test_the_open_branch_binds_no_rid_parameter(self):
        """The bug in one assertion.

        A placeholder with no clause to read it, or a clause with no value
        bound to it, is a query that cannot execute. The count of parameters
        must match the placeholders the text actually carries.
        """
        text, params = self._execute(None)

        self.assertEqual(text.count("%s"), len(params))

    def test_the_exclusion_branch_emits_a_bound_inequality(self):
        text, params = self._execute(9002)

        self.assertIn("a.rid != %s", text)
        self.assertEqual(text.count("%s"), len(params))
        # The rid travels as a PARAMETER, never as query text.
        self.assertEqual(params[-1], 9002)
        self.assertNotIn("9002", text)

    def test_the_string_sentinel_is_no_longer_a_special_case(self):
        """'null' is now just a value, and an integer column will refuse it.

        Kept as a regression guard: if the branch is ever keyed on the string
        again, this stops emitting the exclusion clause and fails.
        """
        text, params = self._execute("null")

        self.assertIn("a.rid != %s", text)
        self.assertEqual(params[-1], "null")
        self.assertNotIn("is not", text.lower())


class IncompleteCatalogLoadQueryTests(unittest.TestCase):
    """`get_scas_with_incomplete_catalog_load_for_processing_date` — the
    durable-state predicate crossmatch gathering gates on directly (co-design
    ruling 1). Query-shape only, for the same reason
    `AlertEmissionCatalogLoadClauseTests` is: no live database in this build.
    """

    def _execute(self):
        db = make_db(iter_rows=())
        db.get_scas_with_incomplete_catalog_load_for_processing_date(
            "20260808")
        query, params = db.cur.execute.call_args.args
        return query, params

    def test_scoped_to_one_processing_date_not_per_field(self):
        from submission.routes import JOB_TYPE_CATALOG_LOAD

        text, params = self._execute()

        self.assertIn(JOB_TYPE_CATALOG_LOAD, params)
        self.assertIn("la.processing_date = cast(%s as date)", text)
        # No field/`d.field` reference anywhere: coverage is per-date, per
        # the handle method's own docstring on why a per-field subset would
        # be wrong (crossMatchSources.py reads every SCA of the date).
        self.assertNotIn("field", text)

    def test_every_placeholder_has_a_bound_parameter(self):
        text, params = self._execute()
        self.assertEqual(text.count("%s"), len(params))


class AlertEmissionCatalogLoadClauseTests(unittest.TestCase):
    """`get_attempts_awaiting_alert_emission` carries the ruled catalog-load
    clause (integration review 2026-08, composite ruling 1: "the ruled
    catalog-load clause is missing from the implemented alert predicate").

    THIS IS A QUERY-SHAPE TEST, NOT A BEHAVIORAL ONE. The predicate is
    entirely server-side SQL with no Python-level branching `rapid_db.py`'s
    thin wrapper could exercise without a live database — the stub-refusal
    principle applies to the SQL text itself here: a stub that only ever
    returned "eligible" could not distinguish "the clause is present and
    evaluates true" from "the clause was never written". These tests assert
    the EXISTS clause and its parameter are actually emitted with the right
    shape; full behavioral verification (a promoted attempt whose catalog
    load has not completed is excluded; completed, it is included) needs a
    live probe against real `attempts`/`logical_jobs` rows, which this build
    does not have access to and reports as owed.
    """

    def _execute(self, **kwargs):
        db = make_db(iter_rows=())
        db.get_attempts_awaiting_alert_emission("rel-1", **kwargs)
        query, params = db.cur.execute.call_args.args
        return query, params

    def test_the_catalog_load_exists_clause_is_present(self):
        text, params = self._execute()

        self.assertIn("logical_jobs", text)
        self.assertIn("job_type = %s", text)
        self.assertIn("la.processing_date = d.created::date", text)
        self.assertIn("la.lifecycle_state = 'terminal_after_start'", text)
        self.assertIn("la.rapid_outcome = 'success'", text)

    def test_the_job_type_parameter_is_catalog_load_not_query_text(self):
        from submission.routes import JOB_TYPE_CATALOG_LOAD

        text, params = self._execute()

        self.assertIn(JOB_TYPE_CATALOG_LOAD, params)
        self.assertNotIn(JOB_TYPE_CATALOG_LOAD, text)

    def test_the_emission_exclusion_covers_the_three_stored_states(self):
        # Migration 037's state model (co-design ruling 3): watermark_seed
        # and emitted always exclude; a claim excludes only while fresh.
        text, params = self._execute()

        self.assertIn("e.state in ('watermark_seed', 'emitted')", text)
        self.assertIn("e.state = 'claimed'", text)
        self.assertIn("e.claimed_at >= now() - interval '1 hour'", text)

    def test_every_placeholder_has_a_bound_parameter(self):
        text, params = self._execute()
        self.assertEqual(text.count("%s"), len(params))

    def test_a_limit_appends_its_own_placeholder_last(self):
        text, params = self._execute(limit=5)
        self.assertTrue(text.rstrip(";").endswith("limit %s"))
        self.assertEqual(params[-1], 5)

    def test_the_pending_attempt_gate_is_present(self):
        # THE RESUBMISSION GATE (mission mock, live 2026-08-09): a subject
        # with an alert-production attempt in flight is not re-gathered —
        # without it every accumulator cut re-submitted every not-yet-
        # claimed subject (57, then 94, children for 36 subjects, observed
        # live). Only pending blocks: emitted is the watermark anti-join's
        # job, and a failed attempt frees the subject (retry path).
        from submission.routes import JOB_TYPE_ALERT_PRODUCTION

        text, params = self._execute()

        self.assertIn("ap.lifecycle_state in ('submitted', 'started')", text)
        self.assertIn("ap.exposure_id = a.exposure_id", text)
        self.assertIn("ap.sca = a.sca", text)
        self.assertIn(JOB_TYPE_ALERT_PRODUCTION, params)
        self.assertNotIn(JOB_TYPE_ALERT_PRODUCTION, text)


if __name__ == "__main__":
    unittest.main()
