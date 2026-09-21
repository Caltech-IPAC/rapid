"""Stub-tier regression test for the in-flight recovery bug in
`pipeline/gc/execute.py`'s `Executor`.

**THE BUG.** `_execute_item` checked `item.status == "in-flight"` FIRST,
before the fence and before `still_referenced`, and dispatched straight to
`_resolve_in_flight` — which re-checked only `self._s3.head_version(...)` and
never consulted `still_referenced` at all. The normal (non-in-flight) branch
correctly re-verifies references INSIDE the fence
(`if still_referenced is not None and still_referenced(item): ...skip...`);
recovery skipped that question entirely. A crash inside the critical section
leaves an item `in-flight`, and the crash window is exactly as long as the
window the normal branch's reference check exists to cover — a foreign
registration can commit a new reference to the object during that window
just as it can during the ordinary critical section, and the shipped
recovery path would still delete it.

**THE FIX UNDER TEST.** `still_referenced` is threaded into
`_resolve_in_flight` and consulted there, with the SAME "skip, record,
don't crash the run" semantics (`DeleteOutcome("skipped-fenced", ...)`) the
normal branch uses for a positive hit — and `still_referenced is None`
(no checker supplied) is handled identically on the recovery path, matching
the two existing contract-tier crash-recovery tests
(`pipeline/contract/test_gc_execution.py::
test_a_crash_inside_the_critical_section_is_resolved_by_rechecking_s3` and
`::test_a_crash_whose_delete_had_already_happened_resolves_as_absent`), which
call `execute(plan_id, commit=conn.commit)` with no `still_referenced`
argument at all.

**NO LIVE DATABASE.** `_FakeConn`/`_FakeCursor` implement exactly the shape
`GCPlanRepository._query` needs (`cursor()` as a context manager,
`execute`/`description`/`fetchall`) over an in-memory `gc_plan_items`-shaped
table, so `Executor._record`/`_mark_in_flight` run against something real
enough to observe — not mocked away — while the S3 side is `StubS3`, copied
from `pipeline/contract/test_gc_execution.py:38-67`'s own double: it CAN
REFUSE (report a missing object, a version that moved, or fail a call), which
is what makes a test built on it prove something (`stub-blind-testing`'s
house rule — a double that cannot fail proves nothing).
"""

import unittest

from pipeline.gc.execute import DeleteOutcome, Executor
from pipeline.gc.plans import PlanItem

BUCKET = "roman-rapid-products"


class StubS3(object):
    """Copied from `pipeline/contract/test_gc_execution.py`'s `StubS3` —
    same shape, same reason: a double that can report a missing object, a
    moved version, or fail a delete, and that records every call so a test
    can assert something was NOT attempted.
    """

    def __init__(self, versions=None, fail_on=()):
        self.versions = dict(versions or {})
        self.fail_on = set(fail_on)
        self.head_calls = []
        self.delete_calls = []

    def head_version(self, bucket, key):
        self.head_calls.append((bucket, key))
        return self.versions.get((bucket, key))

    def delete_version(self, bucket, key, version_id):
        self.delete_calls.append((bucket, key, version_id))
        if key in self.fail_on:
            raise RuntimeError("delete failed for %s" % key)
        current = self.versions.get((bucket, key))
        if current is None:
            return False
        self.versions.pop((bucket, key))
        return True


class _FakeCursor(object):
    def __init__(self, conn):
        self._conn = conn
        self.description = None
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self._conn.calls.append((" ".join(sql.split()), params))
        text = sql
        if "UPDATE gc_plan_items" in text and "SET status = 'in-flight'" in text:
            item_id = params[0]
            self._conn.items[item_id]["status"] = "in-flight"
            self.description = None
        elif "UPDATE gc_plan_items" in text and "SET status = %s" in text:
            status, detail, acted_version, item_id = params
            self._conn.items[item_id].update(
                status=status, status_reason=detail,
                acted_version_id=acted_version)
            self.description = None
        elif "acquire_fence" in text or "gc_fences" in text or \
                "release_fence" in text:
            # THE FENCE ITSELF IS NOT UNDER TEST HERE (that is
            # `pipeline/gc/test/test_fence.py`'s job) — this fake always
            # grants it, so the recovery path under test is reached without
            # a second double's behaviour standing in the way.
            self.description = [("acquired",)]
            self._rows = [(1,)]
        else:
            raise AssertionError("unexpected statement: %s" % sql)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn(object):
    """An in-memory `gc_plan_items` table, keyed by `item_id`.

    `items` is a dict a test seeds directly — no `record_plan` round trip
    needed, since this file's whole subject is `Executor._execute_item`/
    `_resolve_in_flight`, not plan recording (covered elsewhere).
    """

    def __init__(self, items):
        self.items = items
        self.calls = []
        self.committed = 0

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.committed += 1


def make_item(item_id=1, bucket=BUCKET, key="science/r/u/a/x.fits",
             version="v1", status="in-flight"):
    return PlanItem(item_id=item_id, bucket=bucket, object_key=key,
                    version_id=version, object_class="difference_image",
                    status=status, attributed_attempt_id=None)


class RecoveryReferenceCheckTests(unittest.TestCase):
    """s3-recovery-referenced / s3-recovery-clear (case-map-3.txt)."""

    def test_s3_recovery_referenced_object_still_present_and_referenced_is_not_deleted(
            self):
        """s3-recovery-referenced: the crash left the object undeleted (S3
        still reports the planned version), and a foreign in-flight
        submission has become a reference to it during the crash window.
        `still_referenced` must be consulted on the recovery path and the
        delete must NOT be attempted — the exact fix this file exists to
        pin; before it, `_resolve_in_flight` never called
        `still_referenced` at all and would have deleted this object.
        """
        item = make_item()
        conn = _FakeConn({item.item_id: {"status": "in-flight",
                                         "status_reason": None,
                                         "acted_version_id": None}})
        s3 = StubS3(versions={(BUCKET, item.object_key): "v1"})
        executor = Executor(conn, s3, actor="gc")

        outcome = executor._execute_item(
            item, lambda checked: True, conn.commit)

        self.assertEqual(outcome.status, "skipped-fenced")
        self.assertEqual(s3.delete_calls, [],
                         "no delete may be attempted once still_referenced "
                         "reports a hit on the recovery path")
        self.assertTrue(s3.head_calls, "recovery still asks S3, not just "
                                       "the reference check")
        recorded = conn.items[item.item_id]
        self.assertEqual(recorded["status"], "skipped-fenced")

    def test_s3_recovery_clear_object_still_present_and_not_referenced_is_deleted(
            self):
        """s3-recovery-clear: the crash left the object undeleted and
        NOTHING references it — the converse case, so the fix cannot pass
        by refusing everything on the recovery path. Resolves exactly as
        before the fix: re-checked against S3, found still there, deleted.
        """
        item = make_item(item_id=2, key="science/r/u/a/clear.fits")
        conn = _FakeConn({item.item_id: {"status": "in-flight",
                                         "status_reason": None,
                                         "acted_version_id": None}})
        s3 = StubS3(versions={(BUCKET, item.object_key): "v1"})
        executor = Executor(conn, s3, actor="gc")

        outcome = executor._execute_item(
            item, lambda checked: False, conn.commit)

        self.assertEqual(outcome.status, "deleted")
        self.assertEqual(s3.delete_calls, [(BUCKET, item.object_key, "v1")])
        self.assertEqual(conn.items[item.item_id]["status"], "deleted")

    def test_s3_recovery_clear_with_no_checker_behaves_as_before_the_fix(self):
        """`still_referenced=None` (no checker at all — the shape the two
        existing contract-tier crash-recovery tests in
        `pipeline/contract/test_gc_execution.py` call with) must resolve
        exactly as it did before this fix: the fix must not newly REQUIRE a
        checker to be supplied, only consult one when it is.
        """
        item = make_item(item_id=3, key="science/r/u/a/none.fits")
        conn = _FakeConn({item.item_id: {"status": "in-flight",
                                         "status_reason": None,
                                         "acted_version_id": None}})
        s3 = StubS3(versions={(BUCKET, item.object_key): "v1"})
        executor = Executor(conn, s3, actor="gc")

        outcome = executor._execute_item(item, None, conn.commit)

        self.assertEqual(outcome.status, "deleted")
        self.assertEqual(s3.delete_calls, [(BUCKET, item.object_key, "v1")])

    def test_the_already_happened_case_is_unaffected_by_the_fix(self):
        """The OTHER recovery branch — the delete already ran before the
        crash (S3 no longer has the planned version) — never reaches
        `still_referenced` at all, deliberately: there is nothing left to
        protect by re-checking references against bytes already gone.
        Asserted here so a future change cannot make this branch call the
        checker too and silently change its semantics.
        """
        item = make_item(item_id=4, key="science/r/u/a/gone.fits")
        conn = _FakeConn({item.item_id: {"status": "in-flight",
                                         "status_reason": None,
                                         "acted_version_id": None}})
        s3 = StubS3(versions={})   # the delete already ran before the crash

        calls = []

        def spy_still_referenced(checked):
            calls.append(checked)
            return True   # if this were consulted, the item would skip

        executor = Executor(conn, s3, actor="gc")
        outcome = executor._execute_item(item, spy_still_referenced,
                                         conn.commit)

        self.assertEqual(outcome.status, "already-absent")
        self.assertEqual(calls, [], "the already-absent branch must not "
                                    "consult still_referenced at all")
        self.assertEqual(s3.delete_calls, [])

    def test_still_referenced_receives_the_item_itself(self):
        """The callback is handed the SAME `item` `_execute_item` was
        given — the shape `still_referenced_check`'s own `is_referenced`
        closure reads (`item.bucket`, `item.object_key`) — not a bare key
        string or a boolean flag.
        """
        item = make_item(item_id=5, key="science/r/u/a/shape.fits")
        conn = _FakeConn({item.item_id: {"status": "in-flight",
                                         "status_reason": None,
                                         "acted_version_id": None}})
        s3 = StubS3(versions={(BUCKET, item.object_key): "v1"})
        seen = []

        def checker(checked_item):
            seen.append(checked_item)
            return False

        Executor(conn, s3, actor="gc")._execute_item(
            item, checker, conn.commit)

        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0], item)


class DeleteOutcomeShapeTests(unittest.TestCase):
    """The recovery-referenced outcome uses the EXISTING `DeleteOutcome`
    shape, not an invented one — the brief's explicit constraint ("match
    the existing DeleteOutcome/_record conventions exactly — don't invent a
    new outcome shape").
    """

    def test_the_skip_outcome_is_a_plain_deleteoutcome(self):
        item = make_item(item_id=6, key="science/r/u/a/pin.fits")
        conn = _FakeConn({item.item_id: {"status": "in-flight",
                                         "status_reason": None,
                                         "acted_version_id": None}})
        s3 = StubS3(versions={(BUCKET, item.object_key): "v1"})

        outcome = Executor(conn, s3, actor="gc")._execute_item(
            item, lambda checked: True, conn.commit)

        self.assertIsInstance(outcome, DeleteOutcome)
        self.assertEqual(outcome.item_id, item.item_id)
        self.assertEqual(outcome.status, "skipped-fenced")
        self.assertTrue(outcome.detail)


if __name__ == "__main__":
    unittest.main()
