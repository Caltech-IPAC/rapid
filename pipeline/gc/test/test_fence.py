"""Stub-tier tests for `pipeline.gc.fence.acquire_fence`'s exception handling.

Fix round 2 (wave B, item 6): `acquire_fence` used to catch every exception
from its `execute` call and fold it into `return False` — identical to the
genuine "someone else holds this fence" outcome (an empty `RETURNING`, no
exception at all). Under a static-grant gap (the role calling this lacking
`INSERT`/`UPDATE` on `gc_fences`), that meant a real `InsufficientPrivilege`
rendered as "fence held", silently disabling all of GC execution under that
role. This file pins the fix: an empty `RETURNING` (no matching row, no
exception) still returns `False`; a raised exception now PROPAGATES.

No live database — `execute` is a fake, matching the shape every caller in
this package already provides (`GCPlanRepository._query`, `Executor.
_acquire_fence`'s lambda, `registration.consumer._fence_conn_executor`): a
bare `execute(sql, params) -> rows`. A double that cannot refuse — that
always returns the same thing regardless of what it is asked — could not
have caught the original defect, so the fakes here are built to raise
exactly the failure modes under test (`stub-blind-testing`'s house rule).
"""

import unittest

from pipeline.gc import fence


class _RecordingExecute:
    """A fake `execute(sql, params)` that answers scripted outcomes.

    `outcomes` is consumed one call at a time, in order — either a list of
    rows to return, or an exception instance/class to raise. Recording every
    call lets a test assert the SQL was even attempted before asserting on
    the outcome, so a test cannot pass by accident on a call that never
    happened.
    """

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = []

    def __call__(self, sql, params=None):
        self.calls.append((sql, params))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException) or (
                isinstance(outcome, type)
                and issubclass(outcome, BaseException)):
            raise outcome
        return outcome


class _InsufficientPrivilege(Exception):
    """Stands in for `psycopg2.errors.InsufficientPrivilege` (SQLSTATE
    42501) without depending on the driver being installed — the stub tier
    runs without psycopg2, and this module's own `except Exception` (before
    the fix) could not tell this apart from any other error anyway, which
    is exactly the property under test.
    """


class AcquireFenceGenuineConflictTests(unittest.TestCase):
    """The one case that legitimately returns `False`: an empty
    `RETURNING`, no exception raised at all — the SQL ran, found a live
    conflicting row, and the `WHERE expires_at < now()` clause on the
    `ON CONFLICT` simply matched nothing.
    """

    def test_empty_returning_is_not_acquired_and_does_not_raise(self):
        execute = _RecordingExecute([[]])
        acquired = fence.acquire_fence(
            execute, bucket="b", object_key="k", holder="h1",
            holder_kind=fence.HOLDER_GC)
        self.assertFalse(acquired)
        self.assertEqual(len(execute.calls), 1)

    def test_a_returned_row_is_acquired(self):
        execute = _RecordingExecute([[(101,)]])
        acquired = fence.acquire_fence(
            execute, bucket="b", object_key="k", holder="h1",
            holder_kind=fence.HOLDER_GC)
        self.assertTrue(acquired)


class AcquireFenceErrorPropagationTests(unittest.TestCase):
    """THE FIX: a database error is no longer folded into `False`.

    Before this round, `except Exception: return False` made this
    indistinguishable from the genuine-conflict case above — both a live
    holder and a permission error came back `False`, and a caller had no
    way to tell "skip this item, someone else has it" from "I could not
    even ask". These pin that a raised exception now propagates, unchanged,
    to the caller.
    """

    def test_insufficient_privilege_propagates_not_false(self):
        execute = _RecordingExecute([_InsufficientPrivilege(
            "permission denied for table gc_fences")])
        with self.assertRaises(_InsufficientPrivilege):
            fence.acquire_fence(
                execute, bucket="b", object_key="k", holder="h1",
                holder_kind=fence.HOLDER_GC)

    def test_an_arbitrary_database_error_also_propagates(self):
        """Not special-cased to one exception type — ANY error from
        `execute` propagates, matching the fixed function's own docstring
        ("permission, connectivity, a broken statement — propagates").
        """
        execute = _RecordingExecute([RuntimeError("connection reset")])
        with self.assertRaises(RuntimeError):
            fence.acquire_fence(
                execute, bucket="b", object_key="k", holder="h1",
                holder_kind=fence.HOLDER_GC)

    def test_the_regression_this_guards_a_permission_error_never_reads_as_held(
            self):
        """The defect restated as its own assertion: a permission error
        must never be observationally equal to `False`. Asserting
        `assertRaises` above already proves this (a raised exception is
        never `False`), but this test names the property directly so a
        future change that reintroduces `except Exception: return False`
        fails here with a message that says why it matters, not just that
        an exception type changed.
        """
        execute = _RecordingExecute([_InsufficientPrivilege("denied")])
        try:
            result = fence.acquire_fence(
                execute, bucket="b", object_key="k", holder="h1",
                holder_kind=fence.HOLDER_GC)
        except _InsufficientPrivilege:
            return  # correct: the caller sees the real failure
        self.fail(
            "acquire_fence swallowed a permission error and returned %r "
            "instead of raising — this is the exact defect that renders "
            "a static-grant gap as 'fence always held'" % (result,))


class ReleaseAndHeldByStillNeverRaiseTests(unittest.TestCase):
    """`release_fence` keeps its own, separate `except Exception: pass` —
    unaffected by this fix and unchanged by it. Pinned here so a future
    change cannot accidentally narrow release's swallow-everything
    behaviour under the assumption this file's fix applies uniformly
    across the module; the two functions have different failure
    tolerances for different, already-documented reasons (a late release
    is best-effort; an acquisition decides whether to proceed at all).
    """

    def test_release_fence_swallows_any_error(self):
        execute = _RecordingExecute([RuntimeError("boom")])
        fence.release_fence(execute, bucket="b", object_key="k",
                            holder="h1")  # must not raise

    def test_held_by_still_propagates_since_it_never_caught_anything(self):
        execute = _RecordingExecute([RuntimeError("boom")])
        with self.assertRaises(RuntimeError):
            fence.held_by(execute, bucket="b", object_key="k")


if __name__ == "__main__":
    unittest.main()
