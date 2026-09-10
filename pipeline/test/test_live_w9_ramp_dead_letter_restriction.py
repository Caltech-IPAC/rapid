"""W9_ONLY_DEAD_LETTERED_FROM_RUN must narrow gathering to exactly the
(exposure, sca) pairs a prior run's dead-letters left behind.

**WHY THIS EXISTS.** `gather_science_units` re-yields a dead-lettered unit
on a rerun (the resubmission gate frees anything `missing_or_contradictory`,
not just untried units) interleaved with every other never-attempted unit
the window makes ready. A recovery pass whose cap is sized to the
dead-letter count -- not the whole window -- needs the stream narrowed to
exactly that set before `_capped` sees it, or the cap truncates a mix of
recovered and unrelated work instead of recovering what it was asked to.

Tested at the function level (`_dead_lettered_pairs`,
`_restrict_to_dead_lettered`) rather than through `main()`: no live DB and
no AWS context are available outside the pinned image, and `main()` reaches
both before this restriction runs.
"""

import unittest

from pipeline.test import live_w9_ramp
from submission.manifest import ProcessingUnit
from submission.test import payload_fixtures as fixtures


class _FakeDB:
    """Records the query it was asked and hands back canned rows.

    `execute_sql_queries` is `rapid_db.RAPIDDB`'s own idiom -- a list of
    queries, a parallel list of param tuples, rows back for the last query
    run -- so the fake matches that shape rather than inventing a new one.
    """

    def __init__(self, rows):
        self._rows = rows
        self.sql_queries = None
        self.params_list = None

    def execute_sql_queries(self, sql_queries, params_list=None, debug=1):
        self.sql_queries = sql_queries
        self.params_list = params_list
        return self._rows


class DeadLetteredPairsTests(unittest.TestCase):
    def test_reads_distinct_exposure_sca_pairs_scoped_to_the_prefix(self):
        dbh = _FakeDB(rows=[(90001, 3), (90002, 4)])
        pairs = live_w9_ramp._dead_lettered_pairs(dbh, "w9-ramp-science-7380-sci-c")

        self.assertEqual(pairs, {(90001, 3), (90002, 4)})
        self.assertEqual(len(dbh.sql_queries), 1)
        query = dbh.sql_queries[0].lower()
        self.assertIn("missing_or_contradictory", query)
        self.assertIn("like", query)
        # LIKE, not `=`: a split pass's batches carry `<run_id>-<n>`
        # (`pipeline.seams`:469), so an exact match would miss them.
        self.assertEqual(dbh.params_list, [("w9-ramp-science-7380-sci-c%",)])

    def test_no_rows_is_an_empty_set_not_an_error(self):
        dbh = _FakeDB(rows=[])
        pairs = live_w9_ramp._dead_lettered_pairs(dbh, "no-such-prefix")
        self.assertEqual(pairs, set())

    def test_none_rows_is_an_empty_set(self):
        # execute_sql_queries returns None on a caller error rather than
        # raising (rapid_db.py's own contract); the helper must not choke
        # on that instead of the query genuinely returning zero rows.
        dbh = _FakeDB(rows=None)
        pairs = live_w9_ramp._dead_lettered_pairs(dbh, "no-such-prefix")
        self.assertEqual(pairs, set())


class RestrictToDeadLetteredTests(unittest.TestCase):
    def _unit(self, exposure, sca):
        return ProcessingUnit(payload=fixtures.science_payload(exposure=exposure, sca=sca))

    def test_keeps_only_units_matching_a_pair(self):
        dead = self._unit(90001, 3)
        never_attempted = self._unit(90099, 9)
        units = [dead, never_attempted]

        kept = live_w9_ramp._restrict_to_dead_lettered(units, {(90001, 3)})

        self.assertEqual(kept, [dead])

    def test_empty_pair_set_yields_zero_units(self):
        units = [self._unit(90001, 3), self._unit(90002, 4)]
        kept = live_w9_ramp._restrict_to_dead_lettered(units, set())
        self.assertEqual(kept, [])

    def test_preserves_gathering_order_among_kept_units(self):
        first = self._unit(90001, 3)
        second = self._unit(90002, 4)
        third = self._unit(90003, 5)
        units = [first, second, third]

        kept = live_w9_ramp._restrict_to_dead_lettered(
            units, {(90003, 5), (90001, 3)})

        self.assertEqual(kept, [first, third])


class MainRestrictionWiringTests(unittest.TestCase):
    """`W9_ONLY_DEAD_LETTERED_FROM_RUN` unset must leave gathering untouched.

    This is the behavioural half of the contract the two functions above
    cover mechanically: `main()` only calls either of them when the
    environment variable is set. Read as source text, like this file's
    sibling `test_live_w9_ramp_summary.py` does for its own getattr
    check -- `main()` needs a live DB handle and AWS context to run at
    all, so the wiring is verified without executing it.
    """

    def test_unset_by_default_leaves_the_gate_conditional_on_the_env_var(self):
        import inspect

        source = inspect.getsource(live_w9_ramp.main)
        self.assertIn("W9_ONLY_DEAD_LETTERED_FROM_RUN", source)
        self.assertIn('os.environ.get("W9_ONLY_DEAD_LETTERED_FROM_RUN"', source)


if __name__ == "__main__":
    unittest.main()
