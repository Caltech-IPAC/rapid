"""Stub-tier tests for `pipeline.gc.plans.GCPlanRepository` (fix round 2).

Three things wave B's brief names, tested here without a live database:

  * `recompute` refuses on any plan not in `COMPUTED` state, checked via a
    CAS `RETURNING` BEFORE any `gc_plan_items` row is touched — matching
    `approve`/`begin_execution`'s own shape. Before this fix, the item
    walk ran unconditionally and only the FINAL `UPDATE gc_plans` silently
    no-op'd on a wrong-state plan, leaving items mutated with no signal.
  * `preview_recompute` — new — computes the same survive/exclude
    partition `recompute` would apply, read-only, so `gc-recompute-plan`'s
    dry run can report the real anti-join answer instead of
    `len(inventory.objects)`.
  * `approve` no longer accepts a `reason` parameter it used to silently
    drop (the SQL never wrote it anywhere `gc_plans` has a column for).

No live PostgreSQL: a fake connection/cursor stands in for `self._conn`,
matching the exact protocol `GCPlanRepository._query` uses
(`with self._conn.cursor() as cur: cur.execute(...); cur.description;
cur.fetchall()`). The fake answers SCRIPTED results per call and records
every statement issued, so a test can assert BOTH that the guard refused
AND that it refused before doing any of the item-mutating work — a fake
that could not distinguish "zero UPDATEs issued" from "some UPDATEs
issued" could not catch the original defect (the guard was checked, just
too late), matching this repo's "doubles must be able to refuse" rule.
"""

import unittest

from pipeline.gc.plans import GCPlanRepository
from pipeline.gc.references import PlanRefused


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.description = None
        self._result = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self._conn.calls.append((" ".join(sql.split()), params))
        script = self._conn.script
        if not script:
            raise AssertionError(
                "no more scripted results; unexpected statement: %s"
                % sql)
        outcome = script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        rows, has_description = outcome
        self._result = rows
        self.description = [("col",)] if has_description else None

    def fetchall(self):
        return self._result


class _FakeConn:
    """`script` is a list of `(rows, has_description)` pairs (or exception
    instances), consumed one per `cur.execute()` call, in order — the
    caller sets up exactly the sequence of SQL responses a real
    PostgreSQL session would give for the scenario under test.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def cursor(self):
        return _FakeCursor(self)


class RecomputeStateGuardTests(unittest.TestCase):
    """The CAS guard: claimed FIRST, before any item is read or written."""

    def test_recompute_refuses_a_plan_not_in_computed_state(self):
        conn = _FakeConn([
            ([(True,)], True),   # schema_present probe
            ([], True),          # the state UPDATE ... RETURNING: no match
        ])
        repo = GCPlanRepository(conn)
        with self.assertRaises(PlanRefused):
            repo.recompute(7, surviving_keys=set(), inventory=_Inventory(),
                           recomputed_by="op")
        # THE GUARD RAN AND NOTHING ELSE DID: exactly two statements
        # (the schema probe — which mentions `gc_plan_items` only as part
        # of its `to_regclass` existence check, not a query against the
        # table — and the state-claiming UPDATE). The property the
        # original defect violated: no SELECT/UPDATE FROM gc_plan_items
        # was ever issued once the claim failed.
        self.assertEqual(len(conn.calls), 2)
        for sql, _ in conn.calls:
            self.assertNotIn("FROM gc_plan_items", sql, (
                "recompute read gc_plan_items after its own state claim "
                "was refused — the exact defect this guard exists to close"))
            self.assertNotIn("UPDATE gc_plan_items", sql, (
                "recompute wrote gc_plan_items after its own state claim "
                "was refused — the exact defect this guard exists to close"))

    def test_recompute_proceeds_when_the_claim_succeeds(self):
        conn = _FakeConn([
            ([(True,)], True),                          # schema probe
            ([(7,)], True),                              # state UPDATE claims
            ([(1, "b", "k1", "v1", "cls", "pending")], True),  # pending items
            (1, False),                                  # item UPDATE (excluded)
        ])
        repo = GCPlanRepository(conn)
        excluded = repo.recompute(
            7, surviving_keys=set(), inventory=_Inventory(),
            recomputed_by="op")
        self.assertEqual(excluded, 1)
        # The state-claiming UPDATE happens BEFORE the item SELECT.
        statements = [sql for sql, _ in conn.calls]
        state_update_idx = next(
            i for i, s in enumerate(statements) if "UPDATE gc_plans" in s)
        item_select_idx = next(
            i for i, s in enumerate(statements)
            if s.startswith("SELECT item_id"))
        self.assertLess(state_update_idx, item_select_idx, (
            "the plan-state claim must be attempted before any "
            "gc_plan_items row is read"))

    def test_a_surviving_item_is_not_excluded(self):
        conn = _FakeConn([
            ([(True,)], True),
            ([(7,)], True),
            ([(1, "b", "k1", "v1", "cls", "pending")], True),
            # no UPDATE scripted: none should be issued for a surviving item
        ])
        repo = GCPlanRepository(conn)
        excluded = repo.recompute(
            7, surviving_keys={("b", "k1", "v1")}, inventory=_Inventory(),
            recomputed_by="op")
        self.assertEqual(excluded, 0)
        self.assertEqual(len(conn.calls), 3, (
            "a surviving item must not be UPDATEd at all"))


class PreviewRecomputeTests(unittest.TestCase):
    """The new read-only dry-run computation: no state claim, no mutation."""

    def test_preview_reports_the_real_survive_exclude_split(self):
        conn = _FakeConn([
            ([(True,)], True),  # schema probe
            ([("b", "k1", "v1"), ("b", "k2", "v2"), ("b", "k3", "v3")], True),
        ])
        repo = GCPlanRepository(conn)
        surviving, excluded, total = repo.preview_recompute(
            7, surviving_keys={("b", "k1", "v1"), ("b", "k3", "v3")})
        self.assertEqual((surviving, excluded, total), (2, 1, 3))

    def test_preview_issues_no_update_at_all(self):
        """The defect this closes: the OLD dry run reported
        `len(inventory.objects)` — the file on disk, not this plan's own
        pending items — and touched nothing. The new one still touches
        nothing (it is a dry run), but now answers the right question.
        """
        conn = _FakeConn([
            ([(True,)], True),
            ([("b", "k1", "v1")], True),
        ])
        repo = GCPlanRepository(conn)
        repo.preview_recompute(7, surviving_keys=set())
        for sql, _ in conn.calls:
            self.assertNotIn("UPDATE", sql.upper(), (
                "preview_recompute must never write; the CLI's dry run "
                "depends on this to be a true dry run"))

    def test_preview_with_zero_pending_items(self):
        conn = _FakeConn([
            ([(True,)], True),
            ([], True),
        ])
        repo = GCPlanRepository(conn)
        self.assertEqual(
            repo.preview_recompute(7, surviving_keys=set()), (0, 0, 0))


class ApproveNoLongerAcceptsAReasonTests(unittest.TestCase):
    """`approve()` dropped the `reason` kwarg it used to silently discard —
    a `TypeError` on an old-shaped call site is the FEATURE here, not a
    regression: it turns a silent no-op into a loud, immediate failure at
    every call site that still thinks the parameter does something.
    """

    def test_approve_no_longer_takes_a_reason_kwarg(self):
        conn = _FakeConn([
            ([(True,)], True),
            ([(7, "computed-by", "approved-by")], True),
        ])
        repo = GCPlanRepository(conn)
        with self.assertRaises(TypeError):
            repo.approve(7, approved_by="op", reason="ignored anyway")

    def test_approve_without_reason_still_works(self):
        conn = _FakeConn([
            ([(True,)], True),
            ([(7, "same-actor", "same-actor")], True),
        ])
        repo = GCPlanRepository(conn)
        result = repo.approve(7, approved_by="same-actor")
        self.assertEqual(result["approved_by"], "same-actor")
        self.assertTrue(result["self_approved"])


class _Inventory:
    """The two attributes `recompute` reads off its `inventory` argument."""

    inventory_id = "inv-2"
    taken_at = "2026-08-14T00:00:00Z"


if __name__ == "__main__":
    unittest.main()
