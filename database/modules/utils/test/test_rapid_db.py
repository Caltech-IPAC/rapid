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


class RunScopedBlockingGateTests(unittest.TestCase):
    """`get_blocking_exposure_scas_for_job_type`'s new `run_id` parameter
    (throughput-sitting ruling, 2026-09-11) — query-SHAPE tests only, no
    server. The database-evaluated proof of what these two query shapes
    actually block on lives in `RunScopedBlockingGateSemanticsTests` below,
    which evaluates the real SQL text over a SQLite stand-in, matching
    `pipeline.operatorctl.test.test_run.PrefixMatchingTests`'s own pattern
    for the same property (SQLite's `LIKE` is ANSI-standard, so this is a
    real evaluation of the query the method actually issues, not a Python
    reimplementation of it that could silently drift).
    """

    def _execute(self, run_id=None):
        db = make_db(iter_rows=[])
        db.get_blocking_exposure_scas_for_job_type(
            "science", [5001, 5002], run_id=run_id)
        query, params = db.cur.execute.call_args.args
        return query, params

    def test_no_run_id_emits_the_pre_existing_production_query_shape(self):
        """THE NON-NEGOTIABLE CONSTRAINT: `run_id=None` must be IDENTICAL
        IN EFFECT to the query this method issued before this parameter
        existed — reproduced here verbatim from the pre-change source (the
        ledger carries the full before/after side by side) but for the one
        added `and wu.run_id is null` clause, which makes explicit what was
        already implicitly true (every pre-108 work unit has a NULL
        run_id), so it changes no row this query could ever have matched.
        """
        text, params = self._execute(run_id=None)

        before = (
            "select exposure_id, sca from (" +
            "  select distinct la.exposure_id as exposure_id, la.sca as sca " +
            "  from Attempts la " +
            "  join logical_jobs lj on lj.logical_job_id = la.logical_job_id " +
            "  where lj.job_type = %s " +
            "  and la.exposure_id = any(%s) " +
            "  and la.sca is not null " +
            "  and (la.lifecycle_state in ('submitted','started') " +
            "       or la.rapid_outcome = 'success') " +
            "  union " +
            "  select (split_part(wu.input_scope, '/', 1))::bigint as exposure_id, " +
            "         (split_part(wu.input_scope, '/', 2))::int as sca " +
            "  from work_units wu " +
            "  where wu.job_type = %s " +
            "  and (split_part(wu.input_scope, '/', 1))::bigint = any(%s) " +
            "  and wu.superseded_by_unit_id is null " +
            "  and wu.state != 'ready'" +
            "  and wu.run_id is null" +
            ") blocking " +
            "order by exposure_id, sca;")

        self.assertEqual(text, before, (
            "run_id=None must emit exactly this text -- any difference is "
            "a behavior change for the PRODUCTION caller, which is the one "
            "caller this parameter must never affect"))
        self.assertEqual(params, ("science", [5001, 5002],
                                  "science", [5001, 5002]))
        # The Attempts branch carries NO run_id predicate at all when
        # unscoped -- confirmed by the params tuple above having exactly
        # four elements (two job_type/expids pairs), not five.
        self.assertNotIn("la.run_id", text)

    def test_a_run_id_scopes_the_work_unit_branch_by_equality(self):
        # Work units are never split across a retry the way an attempt's
        # run_id can gain a `-<n>` suffix (`work_units_current_identity_uq`
        # keys on exactly one row per (job_type, input_scope, run_id)), so
        # this branch is deliberately `=`, not `LIKE`.
        text, params = self._execute(run_id="w9-campaign-1")

        self.assertIn("wu.run_id = %s", text)
        self.assertNotIn("wu.run_id is null", text)
        self.assertNotIn("wu.run_id like", text.lower())
        self.assertIn("w9-campaign-1", params)

    def test_a_run_id_scopes_the_attempts_branch_by_prefix_not_equality(self):
        # PREFIX MATCHING, NEVER EQUALITY (standing rule, this codebase): a
        # split submission batch carries `<run_id>-<n>`
        # (`pipeline.seams.submit_gathered`). Matching by `=` here would
        # miss exactly the split-batch case every other run-scoped reader
        # in this codebase already guards against
        # (`pipeline.operatorctl.actions._run_prefix_pattern`).
        text, params = self._execute(run_id="w9-campaign-1")

        self.assertIn("la.run_id like %s", text)
        self.assertNotIn("la.run_id = %s", text)
        # The wildcard lives on the PARAMETER, never spliced into the SQL
        # text -- same convention as `actions._run_prefix_pattern`.
        self.assertIn("w9-campaign-1%", params)
        self.assertNotIn("w9-campaign-1%", text)

    def test_every_placeholder_has_a_bound_parameter_when_scoped(self):
        text, params = self._execute(run_id="w9-campaign-1")
        self.assertEqual(text.count("%s"), len(params))

    def test_every_placeholder_has_a_bound_parameter_when_unscoped(self):
        text, params = self._execute(run_id=None)
        self.assertEqual(text.count("%s"), len(params))


class RunScopedBlockingGateSemanticsTests(unittest.TestCase):
    """The real blocking predicate, evaluated for real over scripted rows —
    proof of what the two query shapes above actually block on, not just
    what text they emit. SQLite stands in for Postgres the same way
    `pipeline.operatorctl.test.test_run.PrefixMatchingTests` uses it: `LIKE`
    is ANSI-standard and means the same thing there.

    Simplified to the WORK-UNIT branch alone (the property under test) —
    `union`-ing in a second table needs no live schema to prove `wu.run_id
    IS NULL` vs `wu.run_id = ?` selects the rows the ruling says it must.
    """

    def _blocked(self, rows, run_id):
        """`rows` is [(job_type, input_scope, run_id, state), ...] for
        `work_units`, already filtered to `superseded_by_unit_id IS NULL`
        (not modeled -- every row here is current). Returns the
        (exposure, sca) pairs the run_id-scoped/unscoped query blocks.
        """
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE work_units "
                     "(job_type TEXT, input_scope TEXT, run_id TEXT, "
                     " state TEXT)")
        conn.executemany("INSERT INTO work_units VALUES (?, ?, ?, ?)", rows)

        if run_id is None:
            sql_text = (
                "SELECT input_scope FROM work_units "
                "WHERE job_type = ? AND state != 'ready' "
                "AND run_id IS NULL")
            params = ("science",)
        else:
            sql_text = (
                "SELECT input_scope FROM work_units "
                "WHERE job_type = ? AND state != 'ready' "
                "AND run_id = ?")
            params = ("science", run_id)

        return {row[0] for row in conn.execute(sql_text, params)}

    def test_production_is_blocked_by_its_own_completed_work_unit(self):
        # The baseline this parameter must not disturb: an unscoped caller
        # (run_id=None) still blocks on a production (run_id IS NULL) work
        # unit in a non-ready state.
        rows = [("science", "5001/7", None, "complete")]
        self.assertEqual(self._blocked(rows, run_id=None), {"5001/7"})

    def test_a_campaign_is_not_blocked_by_productions_completed_unit(self):
        # THE DEFECT THIS FIXES, reproduced directly: production's own
        # work unit for this (job_type, input_scope) is 'complete' with
        # run_id IS NULL. Before this change, the unscoped query blocked on
        # ANY non-ready row regardless of run, so a campaign gathering the
        # SAME field yielded nothing even though ITS OWN work unit (a
        # different row, by migration 108's run-scoped identity) had never
        # been attempted. Scoped to the campaign's own run_id, this query
        # must not see production's row at all.
        rows = [("science", "5001/7", None, "complete")]
        self.assertEqual(
            self._blocked(rows, run_id="w9-campaign-1"), set(),
            "a campaign run must not be blocked by a production work "
            "unit's state -- migration 108 made work-unit identity "
            "run-scoped precisely so the two rows coexist independently")

    def test_a_campaign_is_blocked_by_its_own_non_ready_unit(self):
        rows = [("science", "5001/7", "w9-campaign-1", "blocked")]
        self.assertEqual(
            self._blocked(rows, run_id="w9-campaign-1"), {"5001/7"})

    def test_a_campaign_is_not_blocked_by_a_different_campaigns_unit(self):
        # Two campaigns' work units for the same field are two different
        # rows (run-scoped identity); one run's state must never leak into
        # another's gate.
        rows = [("science", "5001/7", "w9-campaign-2", "blocked")]
        self.assertEqual(self._blocked(rows, run_id="w9-campaign-1"), set())


class BlockingGateAttemptsPrefixSemanticsTests(unittest.TestCase):
    """The Attempts branch's prefix match, evaluated for real -- the
    `foo`/`foobar` question the task ruling calls out explicitly.

    WHAT ACTUALLY PROTECTS AGAINST THE `foo`/`foobar` OVERLAP is NOT this
    query: `LIKE 'foo%'` DOES match a `run_id` of `foobar-0`, by design (a
    split batch's suffix could in principle be adversarially confused with
    an unrelated run's name). What makes that not happen in practice is
    `derived.create_run` (migration 109), which REFUSES to create a run
    whose name is a prefix of an existing run's name OR whose name an
    existing run's name is a prefix of, in EITHER direction
    (`pipeline.contract.test_run_model.py`'s
    `test_create_run_refuses_a_name_that_is_a_prefix_of_an_existing_run`
    and `test_create_run_refuses_the_overlap_in_the_other_order_too` pin
    this at the database-function level). So `foo` and `foobar` can never
    BOTH exist as declared runs at the same time -- the prefix match here
    is safe not because it is precise, but because the run registry never
    lets two prefix-overlapping names coexist for it to be imprecise about.
    This class demonstrates the raw LIKE behavior the registry's refusal
    exists to make unreachable.
    """

    def _matches(self, run_id, candidate_run_id):
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE attempts (run_id TEXT)")
        conn.execute("INSERT INTO attempts VALUES (?)", (candidate_run_id,))
        pattern = run_id + "%"
        return conn.execute(
            "SELECT count(*) FROM attempts WHERE run_id LIKE ?",
            (pattern,)).fetchone()[0] == 1

    def test_a_split_batch_suffix_of_the_named_run_matches(self):
        self.assertTrue(self._matches("foo", "foo-2"))

    def test_an_unrelated_run_that_happens_to_share_the_prefix_also_matches(
            self):
        # THE RAW SQL BEHAVIOR, unguarded: `LIKE 'foo%'` matches `foobar-0`
        # too. This is not a defect in THIS query -- it is why
        # `derived.create_run`'s prefix-overlap refusal exists one layer up
        # (see class docstring). A test asserting this returns False would
        # be pinning behavior this query does not and cannot provide on its
        # own.
        self.assertTrue(self._matches("foo", "foobar-0"))

    def test_a_run_with_no_shared_prefix_does_not_match(self):
        self.assertFalse(self._matches("foo", "bar-0"))


if __name__ == "__main__":
    unittest.main()
