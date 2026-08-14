"""Acceptance criteria 9 and 10 — plan integrity, and execution safety.

**THE STUB S3 CAN REFUSE, FAIL, AND REPORT A CHANGED VERSION.** A double that
cannot fail proves nothing (`~/Vault/knowledge/stub-blind-testing.md`, and the
brief's explicit requirement). `StubS3` below can: return a missing object,
fail a delete, report a version that changed after planning, and count calls
so a test can assert something was NOT attempted.

**THE SQL IS REAL.** Plans, items, statuses, the immutability triggers and the
fence all execute against the scratch schema. The triggers in particular
cannot be tested any other way: they are the backstop that holds even for the
table owner, and a Python fake would assert only that this module believes
they exist.
"""

import datetime

import pytest

from pipeline.contract import fixture
from pipeline.gc.execute import Executor
from pipeline.gc.inventory import Inventory, InventoryObject
from pipeline.gc.plans import (GCPlanRepository, PlanBoundExceeded,
                               candidate_checksum)
from pipeline.gc.references import Candidate, PlanRefused

pytestmark = pytest.mark.contract

BUCKET = "roman-rapid-products"
NOW = datetime.datetime(2026, 8, 12, tzinfo=datetime.timezone.utc)


def require_gc_schema(conn):
    if not fixture.has_table(conn, "gc_plans"):
        pytest.skip("DRAFT 052 is not applied (gc_plans absent)")


class StubS3(object):
    """An S3 double that CAN REFUSE — which is why it is worth having.

    `versions` maps `(bucket, key)` to the current version id, or to None for
    an absent object. `fail_on` names keys whose delete raises. `moved` lets a
    test simulate a version that changed between planning and execution.
    """

    def __init__(self, versions=None, fail_on=(), fail_head_on=()):
        self.versions = dict(versions or {})
        self.fail_on = set(fail_on)
        self.fail_head_on = set(fail_head_on)
        self.head_calls = []
        self.delete_calls = []

    def head_version(self, bucket, key):
        self.head_calls.append((bucket, key))
        if key in self.fail_head_on:
            raise RuntimeError("head refused for %s" % key)
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


def make_object(key, version="v1"):
    return InventoryObject(bucket=BUCKET, key=key, version_id=version,
                           size=1, last_modified=NOW)


def make_inventory(objects, inventory_id="inv"):
    return Inventory(inventory_id=inventory_id, taken_at=NOW,
                     objects=tuple(objects), complete=True)


def record_a_plan(conn, keys=("science/r/u/attempt-0000000001/a.fits",),
                  max_deletions=100, allowlist=("difference_image",),
                  horizon=86400, tag=None):
    """One recorded plan with one item per key."""
    tag = tag or fixture.RUN_TAG
    repo = GCPlanRepository(conn)
    objects = [make_object(key) for key in keys]
    # NO ATTRIBUTED ATTEMPT BY DEFAULT.
    #
    # This used to hardcode `attempt_id=1`, which COLLIDES with whatever
    # attempt 1 happens to be on a shared scratch database — and once the
    # executor began re-verifying discharge inside the fence (N2), that
    # collision made every item in every execution test resolve as
    # `skipped-fenced`, because the real attempt 1 is not discharged. The
    # fixture was asserting against a row it did not create, which is the
    # fixture-honesty rule this tier is built on.
    #
    # `None` means "not attributed", which the recheck correctly treats as
    # nothing to re-verify. The one test that needs the recheck to fire sets
    # an attempt id explicitly and patches the owner lookup with it.
    candidates = [
        Candidate(obj=o, object_class="difference_image", attempt_id=None,
                  canonical_prefix=o.key.rsplit("/", 1)[0])
        for o in objects]
    plan = repo.record_plan(
        candidates=candidates, retained_counts={"not-allowlisted": 3},
        inventory=make_inventory(objects),
        declared_buckets=(BUCKET,), declared_prefixes=("science/",),
        horizon_seconds=horizon,
        horizon_provenance="max(pitr=<unset>, retry=86400)",
        max_deletions=max_deletions, allowlist=allowlist,
        reason="contract test %s" % tag,
        idempotency_key="gc-%s-%s" % (tag, id(keys)),
        computed_by="contract-test")
    conn.commit()
    return repo, plan


# ---------------------------------------------------------------------------
# Criterion 9 — plan integrity, immutability, bounds, transitions, approval.
# ---------------------------------------------------------------------------
def test_the_candidate_checksum_is_recorded_and_verified():
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        repo, plan = record_a_plan(conn)
        assert plan.candidate_checksum.startswith("sha256:")
        # Verified from the RECORDED ITEMS, which is the question that
        # matters: "is this still the plan that was computed".
        assert repo.verify_checksum(plan.plan_id) == plan.candidate_checksum
    finally:
        conn.close()


def test_a_plan_exceeding_its_bound_is_refused_at_computation():
    """Refused at computation, NOT truncated at execution.

    Silent truncation would read as "covered everything" when it did not —
    the same no-false-cleans failure this arc has hit before. The refusal
    happens before any row is written, asserted by counting plans after.
    """
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM gc_plans")
            before = cur.fetchone()[0]
        with pytest.raises(PlanBoundExceeded):
            record_a_plan(conn,
                          keys=("science/r/u/attempt-0000000001/a.fits",
                                "science/r/u/attempt-0000000001/b.fits"),
                          max_deletions=1, tag="bound-" + fixture.RUN_TAG)
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM gc_plans")
            assert cur.fetchone()[0] == before, "no partial plan was written"
    finally:
        conn.close()


def test_a_computed_plans_candidates_cannot_be_rewritten():
    """The trigger backstop, tested UNDER A ROLE THAT CAN REACH IT.

    The connection here is the OWNER, which bypasses column grants entirely —
    so a refusal proves the TRIGGER and not merely a missing privilege. That
    distinction is the brief's, and it is why this test does not `SET ROLE` to
    something unprivileged: a refusal from a privilege check would look
    identical and prove nothing about immutability.
    """
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        repo, plan = record_a_plan(conn, tag="frozen-" + fixture.RUN_TAG)
        with conn.cursor() as cur:
            cur.execute("SELECT current_user")
            actor = cur.fetchone()[0]
        assert actor, "running as a real role"

        for statement in (
                "UPDATE gc_plan_items SET object_key = 'moved'"
                " WHERE plan_id = %s",
                "UPDATE gc_plan_items SET version_id = 'v-other'"
                " WHERE plan_id = %s",
                "DELETE FROM gc_plan_items WHERE plan_id = %s",
                "UPDATE gc_plans SET candidate_checksum ="
                " 'sha256:' || repeat('0', 64) WHERE plan_id = %s",
                "DELETE FROM gc_plans WHERE plan_id = %s"):
            with pytest.raises(Exception) as caught:
                with conn.cursor() as cur:
                    cur.execute(statement, (plan.plan_id,))
            assert getattr(caught.value, "pgcode", None) == "RA011", statement
            conn.rollback()
    finally:
        conn.close()


def test_illegal_state_transitions_are_refused():
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        repo, plan = record_a_plan(conn, tag="trans-" + fixture.RUN_TAG)
        # COMPUTED straight to APPROVED skips the mandatory recomputation.
        with pytest.raises(Exception) as caught:
            with conn.cursor() as cur:
                cur.execute("UPDATE gc_plans SET state = 'APPROVED'"
                            " WHERE plan_id = %s", (plan.plan_id,))
        assert getattr(caught.value, "pgcode", None) == "RA011"
        conn.rollback()

        # And EXECUTING without an approval actor.
        with pytest.raises(Exception):
            with conn.cursor() as cur:
                cur.execute("UPDATE gc_plans SET state = 'EXECUTING'"
                            " WHERE plan_id = %s", (plan.plan_id,))
        conn.rollback()
    finally:
        conn.close()


def test_approval_is_recorded_with_its_own_actor_and_needs_a_recompute():
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        repo, plan = record_a_plan(conn, tag="approve-" + fixture.RUN_TAG)

        # Approval before recomputation is refused — the two-pass requirement.
        with pytest.raises(PlanRefused):
            repo.approve(plan.plan_id, approved_by="someone")
        conn.rollback()

        repo.recompute(plan.plan_id, surviving_keys=set(),
                       inventory=make_inventory([], "inv-2"),
                       recomputed_by="contract-test")
        conn.commit()
        approved = repo.approve(plan.plan_id, approved_by="contract-test")
        conn.commit()
        assert approved["approved_by"] == "contract-test"
        # SELF-APPROVAL IS PERMITTED AND RECORDED AS SUCH (P-H4): a
        # single-operator system, and the record is what keeps it reviewable.
        assert approved["self_approved"] is True
    finally:
        conn.close()


def test_a_candidate_that_reappears_is_excluded_by_status_not_deleted():
    """§4.11 step 5: excluded by STATUS, its row and the checksum untouched.

    A plan whose items were deleted to reflect a recomputation would be a plan
    that lies about what it computed — and the checksum would have to move to
    match, at which point it is evidence of nothing.
    """
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        key = "science/r/u/attempt-0000000001/gone.fits"
        repo, plan = record_a_plan(conn, keys=(key,),
                                   tag="recompute-" + fixture.RUN_TAG)
        before = repo.verify_checksum(plan.plan_id)

        excluded = repo.recompute(plan.plan_id, surviving_keys=set(),
                                  inventory=make_inventory([], "inv-2"),
                                  recomputed_by="contract-test")
        conn.commit()
        assert excluded == 1

        with conn.cursor() as cur:
            cur.execute("SELECT status FROM gc_plan_items WHERE plan_id = %s",
                        (plan.plan_id,))
            assert [r[0] for r in cur.fetchall()] == ["excluded-on-recompute"]
        # THE ROW SURVIVES AND THE CHECKSUM IS UNCHANGED.
        assert repo.verify_checksum(plan.plan_id) == before
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Criterion 10 — execution safety.
# ---------------------------------------------------------------------------
def approved_plan(conn, keys, tag):
    repo, plan = record_a_plan(conn, keys=keys, tag=tag)
    surviving = {(BUCKET, key, "v1") for key in keys}
    repo.recompute(plan.plan_id, surviving_keys=surviving,
                   inventory=make_inventory([make_object(k) for k in keys],
                                            "inv-2"),
                   recomputed_by="t")
    conn.commit()
    repo.approve(plan.plan_id, approved_by="t")
    conn.commit()
    return repo, plan


def item_statuses(conn, plan_id):
    with conn.cursor() as cur:
        cur.execute("SELECT object_key, status, acted_version_id"
                    "  FROM gc_plan_items WHERE plan_id = %s"
                    " ORDER BY object_key", (plan_id,))
        return cur.fetchall()


def test_deletion_targets_the_exact_recorded_version():
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        key = "science/r/u/attempt-0000000001/a.fits"
        repo, plan = approved_plan(conn, (key,), "exact-" + fixture.RUN_TAG)
        s3 = StubS3(versions={(BUCKET, key): "v1"})
        Executor(conn, s3, actor="t").execute(plan.plan_id,
                                              commit=conn.commit)
        assert s3.delete_calls == [(BUCKET, key, "v1")]
        assert item_statuses(conn, plan.plan_id)[0][1] == "deleted"
    finally:
        conn.close()


def test_an_object_whose_version_changed_after_planning_is_not_deleted():
    """The versioned-bucket delete-marker hazard, asserted directly.

    The plan recorded v1; S3 now reports v2, meaning something wrote this key
    after planning. The plan's judgement does not cover those bytes, so the
    item is skipped — and crucially NO DELETE IS ATTEMPTED, asserted by the
    stub's own call log rather than inferred from the status.
    """
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        key = "science/r/u/attempt-0000000001/moved.fits"
        repo, plan = approved_plan(conn, (key,), "moved-" + fixture.RUN_TAG)
        s3 = StubS3(versions={(BUCKET, key): "v2"})
        Executor(conn, s3, actor="t").execute(plan.plan_id,
                                              commit=conn.commit)
        assert s3.delete_calls == [], "no delete may be attempted"
        row = item_statuses(conn, plan.plan_id)[0]
        assert row[1] == "skipped-fenced"
        assert row[2] == "v2", "the version actually seen is recorded"
    finally:
        conn.close()


def test_an_object_registered_between_planning_and_execution_is_skipped():
    """The final re-verification, inside the fence."""
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        key = "science/r/u/attempt-0000000001/bound.fits"
        repo, plan = approved_plan(conn, (key,), "reg-" + fixture.RUN_TAG)
        s3 = StubS3(versions={(BUCKET, key): "v1"})
        Executor(conn, s3, actor="t").execute(
            plan.plan_id, commit=conn.commit,
            still_referenced=lambda item: True)
        assert s3.delete_calls == []
        assert item_statuses(conn, plan.plan_id)[0][1] == "skipped-fenced"
    finally:
        conn.close()


def test_an_already_absent_object_resolves_as_already_absent_not_an_error():
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        key = "science/r/u/attempt-0000000001/absent.fits"
        repo, plan = approved_plan(conn, (key,), "absent-" + fixture.RUN_TAG)
        s3 = StubS3(versions={})
        outcomes = Executor(conn, s3, actor="t").execute(plan.plan_id,
                                                         commit=conn.commit)
        assert outcomes[0].status == "already-absent"
        assert item_statuses(conn, plan.plan_id)[0][1] == "already-absent"
    finally:
        conn.close()


def test_one_objects_failure_records_its_outcome_and_the_run_continues():
    """Per-object failure, never process-terminating (rules 17 and 22)."""
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        bad = "science/r/u/attempt-0000000001/bad.fits"
        good = "science/r/u/attempt-0000000001/good.fits"
        repo, plan = approved_plan(conn, (bad, good),
                                   "fail-" + fixture.RUN_TAG)
        s3 = StubS3(versions={(BUCKET, bad): "v1", (BUCKET, good): "v1"},
                    fail_on=(bad,))
        outcomes = Executor(conn, s3, actor="t").execute(plan.plan_id,
                                                         commit=conn.commit)
        by_status = {o.status for o in outcomes}
        assert "failed" in by_status and "deleted" in by_status
        rows = dict((r[0], r[1]) for r in item_statuses(conn, plan.plan_id))
        assert rows[bad] == "failed"
        assert rows[good] == "deleted", "the run continued past the failure"
    finally:
        conn.close()


def test_the_fence_fails_closed_when_it_cannot_be_acquired():
    """A registration holding the fence blocks the deletion of that key.

    The registration-starts-IMMEDIATELY-BEFORE boundary: the counterpart holds
    the fence when GC arrives, GC cannot acquire it, and the item is skipped
    while the run continues with the rest.
    """
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        key = "science/r/u/attempt-0000000001/fenced.fits"
        other = "science/r/u/attempt-0000000001/free.fits"
        repo, plan = approved_plan(conn, (key, other),
                                   "fence-" + fixture.RUN_TAG)
        # The counterpart takes the fence first, as a registration would.
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO gc_fences (bucket, object_key, holder,"
                " holder_kind, expires_at)"
                " VALUES (%s, %s, 'registrar', 'registration',"
                "         now() + interval '5 minutes')", (BUCKET, key))
        conn.commit()
        try:
            s3 = StubS3(versions={(BUCKET, key): "v1",
                                  (BUCKET, other): "v1"})
            Executor(conn, s3, actor="gc").execute(plan.plan_id,
                                                    commit=conn.commit)
            rows = dict((r[0], r[1])
                        for r in item_statuses(conn, plan.plan_id))
            assert rows[key] == "skipped-fenced"
            assert (BUCKET, key, "v1") not in s3.delete_calls
            assert rows[other] == "deleted", "the run continued"
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM gc_fences WHERE bucket = %s"
                            "   AND object_key = %s", (BUCKET, key))
            conn.commit()
    finally:
        conn.close()


def test_a_registration_starting_immediately_after_binds_a_version_gc_left():
    """The other boundary ordering.

    GC deletes the EXACT version the plan recorded. A registration that starts
    immediately after the critical section and publishes new bytes creates a
    NEW version, which the plan never named and GC therefore never touched —
    so the registration binds live bytes. Asserted through the stub's version
    map: after GC's delete, a fresh publish is visible and was not deleted.
    """
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        key = "science/r/u/attempt-0000000001/after.fits"
        repo, plan = approved_plan(conn, (key,), "after-" + fixture.RUN_TAG)
        s3 = StubS3(versions={(BUCKET, key): "v1"})
        Executor(conn, s3, actor="gc").execute(plan.plan_id,
                                                commit=conn.commit)
        assert s3.delete_calls == [(BUCKET, key, "v1")]
        # The registrar publishes afterwards; a new version appears.
        s3.versions[(BUCKET, key)] = "v2"
        assert s3.head_version(BUCKET, key) == "v2"
        # And nothing in the executed plan can touch it: the plan named v1.
        assert (BUCKET, key, "v2") not in s3.delete_calls
    finally:
        conn.close()


def test_a_crash_inside_the_critical_section_is_resolved_by_rechecking_s3():
    """`in-flight` recovery: re-check S3, never guess.

    An item whose intent was committed and whose outcome was not is left
    `in-flight`. Recovery asks S3 what actually happened rather than assuming
    the delete did or did not run — which is the whole reason the intent is
    committed separately from the outcome.
    """
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        key = "science/r/u/attempt-0000000001/crashed.fits"
        repo, plan = approved_plan(conn, (key,), "crash-" + fixture.RUN_TAG)
        # Simulate the crash: intent committed, outcome never written.
        with conn.cursor() as cur:
            cur.execute("UPDATE gc_plan_items SET status = 'in-flight',"
                        "       intent_at = now()"
                        " WHERE plan_id = %s", (plan.plan_id,))
        conn.commit()

        # Case A: the delete had NOT happened — S3 still has v1.
        s3 = StubS3(versions={(BUCKET, key): "v1"})
        Executor(conn, s3, actor="gc").execute(plan.plan_id,
                                                commit=conn.commit)
        assert s3.head_calls, "recovery asked S3 rather than guessing"
        assert item_statuses(conn, plan.plan_id)[0][1] == "deleted"
    finally:
        conn.close()


def test_a_crash_whose_delete_had_already_happened_resolves_as_absent():
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        key = "science/r/u/attempt-0000000001/crashed2.fits"
        repo, plan = approved_plan(conn, (key,), "crash2-" + fixture.RUN_TAG)
        with conn.cursor() as cur:
            cur.execute("UPDATE gc_plan_items SET status = 'in-flight',"
                        "       intent_at = now()"
                        " WHERE plan_id = %s", (plan.plan_id,))
        conn.commit()
        s3 = StubS3(versions={})          # the delete had run before the crash
        Executor(conn, s3, actor="gc").execute(plan.plan_id,
                                                commit=conn.commit)
        assert s3.delete_calls == [], "no second delete was attempted"
        assert item_statuses(conn, plan.plan_id)[0][1] == "already-absent"
    finally:
        conn.close()


def test_a_partially_executed_plan_resumes_only_on_unresolved_items():
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        done = "science/r/u/attempt-0000000001/done.fits"
        todo = "science/r/u/attempt-0000000001/todo.fits"
        repo, plan = approved_plan(conn, (done, todo),
                                   "resume-" + fixture.RUN_TAG)
        with conn.cursor() as cur:
            cur.execute("UPDATE gc_plan_items SET status = 'deleted',"
                        "       outcome_at = now()"
                        " WHERE plan_id = %s AND object_key = %s",
                        (plan.plan_id, done))
        conn.commit()

        unresolved = repo.unresolved_items(plan.plan_id)
        assert [i.object_key for i in unresolved] == [todo]

        s3 = StubS3(versions={(BUCKET, todo): "v1"})
        Executor(conn, s3, actor="gc").execute(plan.plan_id,
                                                commit=conn.commit)
        assert s3.delete_calls == [(BUCKET, todo, "v1")], \
            "the already-deleted item was not re-attempted"
    finally:
        conn.close()


def test_a_terminal_item_cannot_be_reopened():
    """`deleted` and `already-absent` are terminal, enforced by trigger."""
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        key = "science/r/u/attempt-0000000001/terminal.fits"
        repo, plan = approved_plan(conn, (key,), "term-" + fixture.RUN_TAG)
        with conn.cursor() as cur:
            cur.execute("UPDATE gc_plan_items SET status = 'deleted'"
                        " WHERE plan_id = %s", (plan.plan_id,))
        conn.commit()
        with pytest.raises(Exception) as caught:
            with conn.cursor() as cur:
                cur.execute("UPDATE gc_plan_items SET status = 'pending'"
                            " WHERE plan_id = %s", (plan.plan_id,))
        assert getattr(caught.value, "pgcode", None) == "RA011"
        conn.rollback()
    finally:
        conn.close()


def test_the_fence_re_verifies_the_discharge_watermark(monkeypatch):
    """N2: the watermark check at planning time is a SNAPSHOT.

    A terminal-record writer can raise `terminal_record_sequence` immediately
    after the plan reads it, making registration lag again and need the very
    object GC is about to delete — and exact-version deletion does NOT help
    there, because the new registration wants that same version. So the
    discharge predicate is re-evaluated inside the fence, and a lapse skips
    the item.

    The owner lookup is patched to report the lapse, because provoking a real
    terminal-record advance mid-critical-section would need a second writer
    racing this test; what matters is that the executor ASKS and honours the
    answer, which is what the shipped version never did.
    """
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        key = "science/r/u/attempt-0000000001/lapsed.fits"
        repo, plan = approved_plan(conn, (key,), "lapse-" + fixture.RUN_TAG)

        # The item must carry an attributed attempt for the recheck to fire.
        with conn.cursor() as cur:
            cur.execute("UPDATE gc_plan_items SET attributed_attempt_id = 1"
                        " WHERE plan_id = %s", (plan.plan_id,))
        conn.commit()

        # The owner is no longer fully discharged: its registration watermark
        # now lags a terminal-record sequence that advanced after planning.
        monkeypatch.setattr(
            "pipeline.gc.reference_sql.owners",
            lambda execute, attempt_ids=None: {
                1: {"unit_state": "complete",
                    "registered_record_sequence": 3,
                    "terminal_record_sequence": 9,
                    "live_attempt_count": 0}})

        s3 = StubS3(versions={(BUCKET, key): "v1"})
        Executor(conn, s3, actor="gc").execute(plan.plan_id,
                                                commit=conn.commit)
        assert s3.delete_calls == [], (
            "the item was deleted although its owner stopped being fully "
            "discharged between planning and execution")
        row = item_statuses(conn, plan.plan_id)[0]
        assert row[1] == "skipped-fenced"
    finally:
        conn.close()


def test_a_still_discharged_owner_is_not_skipped_by_the_recheck():
    """The converse, so the recheck cannot pass by refusing everything."""
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        key = "science/r/u/attempt-0000000001/ok.fits"
        repo, plan = approved_plan(conn, (key,), "nolapse-" + fixture.RUN_TAG)
        # No attributed attempt: the recheck has nothing to look up and must
        # not invent a refusal.
        s3 = StubS3(versions={(BUCKET, key): "v1"})
        Executor(conn, s3, actor="gc").execute(plan.plan_id,
                                                commit=conn.commit)
        assert s3.delete_calls == [(BUCKET, key, "v1")]
    finally:
        conn.close()


def test_execute_refuses_without_a_real_commit_callable():
    """The intent/outcome protocol NEEDS a commit between its two halves.

    Passing a no-op would make the crash-safety property untrue while looking
    identical, so it is refused rather than defaulted.
    """
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        with pytest.raises(ValueError) as caught:
            Executor(conn, StubS3(), actor="t").execute(1, commit=None)
        assert "commit" in str(caught.value)
    finally:
        conn.close()


def test_an_unapproved_plan_cannot_execute():
    """Compute, recompute, approve and execute are distinct recorded steps."""
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        key = "science/r/u/attempt-0000000001/unapproved.fits"
        repo, plan = record_a_plan(conn, keys=(key,),
                                   tag="unapp-" + fixture.RUN_TAG)
        with pytest.raises(PlanRefused):
            Executor(conn, StubS3(), actor="t").execute(plan.plan_id,
                                                         commit=conn.commit)
    finally:
        conn.close()
