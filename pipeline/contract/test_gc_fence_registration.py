"""Contract tests: the registration half of the GC fence (brief H3/H4).

`pipeline.gc.execute.Executor` already held the GC half of `gc_fences`, and
`pipeline.contract.test_gc_execution.test_the_fence_fails_closed_when_it_
cannot_be_acquired` already proved GC observes a PRE-INSERTED
`holder_kind='registration'` row correctly — but that test hand-inserts the
row; it never exercises `pipeline.registration.consumer._bind_fence`, the
code this brief adds, and it never runs the two sides GENUINELY
concurrently. This file closes both gaps:

  * `pipeline.gc.fence` — the shared acquire/release/held_by module both
    `Executor` and `consumer._bind_fence` now call, so a change to one
    side's SQL cannot silently drift from the other's;
  * `consumer._bind_fence` acquiring/releasing REAL fence rows over REAL
    keys, driven by a REAL two-connection race
    (`pipeline.contract.test_attempt_claiming`'s `threading.Barrier`
    idiom, reused here for the identical reason: the interesting property
    — which side's INSERT the database's own row lock resolves first, and
    whether the LOSING side genuinely blocks rather than merely being
    asked nicely to wait — is untestable against any fake).

**THE SQL IS REAL** (`test_gc_execution.py`'s own header, restated here
because it is equally true of this file): `gc_fences`'s `(bucket,
object_key)` unique index, its `ON CONFLICT ... WHERE expires_at < now()`
reclaim clause, and the two genuinely concurrent transactions this module
drives are exactly what a Python fake cannot express.

**NEEDS A REAL DATABASE — NOT RUN HERE.** Auto-marked `contract` by
`pipeline/contract/conftest.py`; excluded from the default `pytest`
selection (`addopts = "-m 'not contract and not live'"`); the rapid-admin
acceptance run and CI's contract-tier job execute this, never this
session (project rule: nothing executes locally for RAPID beyond
stub-tier no-I/O pytest).
"""

import threading

import pytest

from pipeline.contract import fixture
from pipeline.contract.test_gc_execution import (
    BUCKET, StubS3, approved_plan, item_statuses, require_gc_schema)
from pipeline.gc import fence as gc_fence
from pipeline.gc.execute import Executor
from pipeline.registration.consumer import BindFenced, _bind_fence

pytestmark = pytest.mark.contract


def require_gc_fences_table(conn):
    """Skip unless `gc_fences` itself is deployed.

    `gc_fences` is the one table this file's tests need in common; several
    of them never touch `gc_plans`/`gc_plan_items` at all (the pure
    `pipeline.gc.fence` tests), so probing for those via
    `require_gc_schema` would skip tests that do not need them for the
    wrong reason. Probed the same way `fixture.has_table` documents:
    schema presence is asked of the catalog, never assumed.
    """
    if not fixture.has_table(conn, "gc_fences"):
        pytest.skip("DRAFT (gc_fences) is not applied")


def _fence_rows(conn, bucket, object_key):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT holder, holder_kind, expires_at FROM gc_fences"
            " WHERE bucket = %s AND object_key = %s", (bucket, object_key))
        return cur.fetchall()


def _conn_executor(conn):
    """The bare `execute(sql, params)` shape `pipeline.gc.fence` takes,
    committing after every call — the identical shape
    `consumer._fence_conn_executor` uses, reimplemented here rather than
    imported so a test of the fence module does not depend on a private
    helper of the module under test on the OTHER side of the race.
    """
    def execute(statement, params=None):
        with conn.cursor() as cur:
            cur.execute(statement, params)
            if cur.description is not None:
                result = cur.fetchall()
            else:
                result = cur.rowcount
        conn.commit()
        return result
    return execute


# ---------------------------------------------------------------------------
# `pipeline.gc.fence` itself: the shared primitive, exercised directly.
# ---------------------------------------------------------------------------
def test_acquire_fence_is_exclusive_across_holder_kinds(conn):
    """A live `gc` fence blocks a `registration` acquisition, and vice versa.

    This is the property `Executor`'s own docstring calls "the counterpart's
    participation cannot be verified" — made concrete: the ONE shared
    `ON CONFLICT ... WHERE expires_at < now()` clause is what refuses a
    second holder_kind's acquisition over a live fence, regardless of
    which kind asked first.
    """
    require_gc_fences_table(conn)
    key = "science/r/u/attempt-fence-exclusive/x.fits"
    execute = _conn_executor(conn)
    try:
        assert gc_fence.acquire_fence(
            execute, bucket=BUCKET, object_key=key, holder="gc-1",
            holder_kind=gc_fence.HOLDER_GC)
        # The SAME holder_kind, a different holder: also blocked. Exclusivity
        # is per-KEY, not per-(key, holder_kind) — two GC workers must not
        # both believe they hold the same key either.
        assert not gc_fence.acquire_fence(
            execute, bucket=BUCKET, object_key=key, holder="gc-2",
            holder_kind=gc_fence.HOLDER_GC)
        assert not gc_fence.acquire_fence(
            execute, bucket=BUCKET, object_key=key, holder="registrar-1",
            holder_kind=gc_fence.HOLDER_REGISTRATION)

        held = gc_fence.held_by(execute, bucket=BUCKET, object_key=key)
        assert held is not None
        holder, holder_kind, _ = held
        assert (holder, holder_kind) == ("gc-1", gc_fence.HOLDER_GC)
    finally:
        gc_fence.release_fence(execute, bucket=BUCKET, object_key=key,
                               holder="gc-1")


def test_release_only_removes_the_named_holders_row(conn):
    """`release_fence` is scoped by holder — it cannot delete someone else's
    live fence out from under them.

    The case this guards: holder A's lease expires, holder B reclaims the
    same key, and A's (now-stale) code path finally gets around to calling
    release. A `DELETE ... WHERE holder = %s` naming A's own identity
    leaves B's row untouched; a release keyed only on `(bucket,
    object_key)` would not.
    """
    require_gc_fences_table(conn)
    key = "science/r/u/attempt-fence-release/x.fits"
    execute = _conn_executor(conn)
    assert gc_fence.acquire_fence(
        execute, bucket=BUCKET, object_key=key, holder="first",
        holder_kind=gc_fence.HOLDER_GC, lease_seconds=0)
    # Zero-second lease: already expired by the time the next acquisition
    # asks, so "second" reclaims it exactly as a crash-recovery acquirer
    # would.
    import time
    time.sleep(0.05)
    assert gc_fence.acquire_fence(
        execute, bucket=BUCKET, object_key=key, holder="second",
        holder_kind=gc_fence.HOLDER_REGISTRATION)

    # "first" releases late. Its own row is long gone (reclaimed by
    # "second"); this must be a no-op, not a deletion of "second"'s row.
    gc_fence.release_fence(execute, bucket=BUCKET, object_key=key,
                           holder="first")
    held = gc_fence.held_by(execute, bucket=BUCKET, object_key=key)
    assert held is not None and held[0] == "second", (
        "a late release from the PRIOR holder deleted the CURRENT "
        "holder's live fence")

    gc_fence.release_fence(execute, bucket=BUCKET, object_key=key,
                           holder="second")
    assert gc_fence.held_by(execute, bucket=BUCKET, object_key=key) is None


def test_an_expired_lease_is_reclaimed_not_treated_as_still_held(conn):
    """Crash recovery's basic mechanic: an expired fence is free, unswept.

    No sweeper runs in this test — none exists in this codebase; expiry is
    judged by the acquiring statement's own WHERE clause (`pipeline.gc.
    fence.acquire_fence`'s docstring). This pins that the row is reclaimed
    the INSTANT the lease elapses, not on some later pass.
    """
    require_gc_fences_table(conn)
    key = "science/r/u/attempt-fence-expiry/x.fits"
    execute = _conn_executor(conn)
    assert gc_fence.acquire_fence(
        execute, bucket=BUCKET, object_key=key, holder="crashed-holder",
        holder_kind=gc_fence.HOLDER_REGISTRATION, lease_seconds=0)
    import time
    time.sleep(0.05)

    # A THIRD PARTY reclaims it — not the crashed holder retrying, a
    # DIFFERENT actor, which is exactly the crash-recovery shape: the
    # crashed process is gone and something else (GC, or a fresh
    # registration pass) is the one that finds the key free.
    assert gc_fence.acquire_fence(
        execute, bucket=BUCKET, object_key=key, holder="recovering-actor",
        holder_kind=gc_fence.HOLDER_GC), (
        "an expired lease was treated as still held")
    held = gc_fence.held_by(execute, bucket=BUCKET, object_key=key)
    assert held[0] == "recovering-actor"
    gc_fence.release_fence(execute, bucket=BUCKET, object_key=key,
                           holder="recovering-actor")


# ---------------------------------------------------------------------------
# `consumer._bind_fence`: the registration side's own acquisition, over a
# real record.
# ---------------------------------------------------------------------------
def _record_with_products(attempt_id, keys):
    """A minimal terminal-record body `_bind_fence_keys` can read.

    Only the `products` list matters here — `published()` (`pipeline.
    registration.products`) indexes exactly this shape, and `_bind_fence`
    never reads anything else off the record.
    """
    return {
        "attempt_id": attempt_id,
        "products": [
            {"name": "product-%d" % i, "uri": "s3://%s/%s" % (BUCKET, key),
             "checksum": "c%d" % i}
            for i, key in enumerate(keys)
        ],
    }


def test_bind_fence_acquires_and_releases_over_every_published_key(conn):
    """`_bind_fence` fences every `s3://` URI the record publishes, and
    releases all of them on a clean exit.
    """
    require_gc_fences_table(conn)
    keys = ("science/r/u/attempt-bind-1/a.fits",
           "science/r/u/attempt-bind-1/b.fits")
    record = _record_with_products(1, keys)

    with _bind_fence(conn, record, attempt_id=1):
        for key in keys:
            rows = _fence_rows(conn, BUCKET, key)
            assert len(rows) == 1, "the fence was not visible mid-hold"
            assert rows[0][1] == gc_fence.HOLDER_REGISTRATION

    for key in keys:
        assert _fence_rows(conn, BUCKET, key) == [], (
            "a fence key was not released on a clean exit")


def test_bind_fence_releases_on_exception_too(conn):
    """A failure inside the fenced block still releases every key it took."""
    require_gc_fences_table(conn)
    key = "science/r/u/attempt-bind-2/a.fits"
    record = _record_with_products(2, (key,))

    with pytest.raises(RuntimeError):
        with _bind_fence(conn, record, attempt_id=2):
            raise RuntimeError("simulated registration body failure")

    assert _fence_rows(conn, BUCKET, key) == [], (
        "an exception inside the fenced block left the fence held")


def test_bind_fence_raises_bindfenced_when_gc_already_holds_the_key(conn):
    """The registration side's own fail-closed behaviour, mirroring
    `test_gc_execution.test_the_fence_fails_closed_when_it_cannot_be_
    acquired`'s GC-side assertion.
    """
    require_gc_fences_table(conn)
    key = "science/r/u/attempt-bind-3/a.fits"
    execute = _conn_executor(conn)
    assert gc_fence.acquire_fence(
        execute, bucket=BUCKET, object_key=key, holder="gc-holder",
        holder_kind=gc_fence.HOLDER_GC)
    try:
        record = _record_with_products(3, (key,))
        with pytest.raises(BindFenced):
            with _bind_fence(conn, record, attempt_id=3):
                pytest.fail("the fenced block ran while GC held the key")
    finally:
        gc_fence.release_fence(execute, bucket=BUCKET, object_key=key,
                               holder="gc-holder")


# ---------------------------------------------------------------------------
# The two genuinely concurrent interleavings (the brief's acceptance bar).
# ---------------------------------------------------------------------------
def test_gc_first_then_registration_blocks_the_registrar(conn, second_conn):
    """GC-starts-first: GC's fence wins the race; registration observes it
    live and refuses to bind, genuinely concurrently — not simulated by
    pre-inserting a row on one connection before the other ever runs.

    A `threading.Barrier` holds both sides until each has entered, so
    neither can complete before the other starts (the same idiom
    `test_attempt_claiming.test_two_concurrent_resolvers_converge_on_one_
    attempt` uses for the identical reason: the interesting property is
    untestable without a REAL race).
    """
    require_gc_fences_table(conn)
    key = "science/r/u/attempt-race-gc-first/x.fits"
    record = _record_with_products(101, (key,))

    barrier = threading.Barrier(2)
    results = {}
    errors = {}

    def gc_side():
        execute = _conn_executor(conn)
        try:
            barrier.wait(timeout=30)
            # GC's own lease is long enough to still be live when the
            # registrar's attempt (below) runs — this pins the ORDERING,
            # not a lucky timing race against a lease that might already
            # have expired.
            acquired = gc_fence.acquire_fence(
                execute, bucket=BUCKET, object_key=key, holder="gc-actor",
                holder_kind=gc_fence.HOLDER_GC, lease_seconds=30)
            results["gc_acquired"] = acquired
        except Exception as exc:                      # noqa: BLE001
            errors["gc"] = exc

    def registration_side():
        try:
            barrier.wait(timeout=30)
            # Give the GC side a head start inside the barrier release —
            # both threads are unblocked at once, but the assertion below
            # is about the OUTCOME (registration must not proceed while
            # GC's fence is live), not about which nanosecond wins; a
            # short, bounded settle avoids a flaky false pass on a host
            # where the threads happen to interleave the other way this
            # one run.
            import time
            time.sleep(0.2)
            with _bind_fence(second_conn, record, attempt_id=101):
                results["registration_proceeded"] = True
        except BindFenced:
            results["registration_proceeded"] = False
        except Exception as exc:                      # noqa: BLE001
            errors["registration"] = exc

    threads = [threading.Thread(target=gc_side),
              threading.Thread(target=registration_side)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"a side raised: {errors}"
    assert results.get("gc_acquired") is True
    assert results.get("registration_proceeded") is False, (
        "registration bound a key GC's fence held live")

    execute = _conn_executor(conn)
    gc_fence.release_fence(execute, bucket=BUCKET, object_key=key,
                           holder="gc-actor")


def test_registration_first_then_gc_is_blocked_by_the_registrar(
        conn, second_conn):
    """The other boundary ordering: registration starts first, holds the
    fence for the duration of its (simulated, slow) bind, and GC's own
    `Executor.execute()` — the real production entry point, not a bare
    `acquire_fence` call — skips the item as `skipped-fenced` rather than
    deleting it.
    """
    require_gc_schema(conn)
    require_gc_fences_table(conn)
    key = "science/r/u/attempt-race-reg-first/x.fits"
    other = "science/r/u/attempt-race-reg-first/free.fits"
    record = _record_with_products(102, (key,))
    repo, plan = approved_plan(conn, (key, other),
                               "race-reg-first-" + fixture.RUN_TAG)

    barrier = threading.Barrier(2)
    started = threading.Event()
    release = threading.Event()
    errors = {}

    def registration_side():
        try:
            barrier.wait(timeout=30)
            with _bind_fence(second_conn, record, attempt_id=102):
                started.set()
                # Hold the fence until the GC side has had its chance to
                # observe it live — released the instant GC's attempt is
                # done, never on a fixed sleep alone, so the test is bounded
                # by an event rather than a guessed duration.
                release.wait(timeout=30)
        except Exception as exc:                      # noqa: BLE001
            errors["registration"] = exc
            started.set()
            release.set()

    def gc_side():
        try:
            barrier.wait(timeout=30)
            assert started.wait(timeout=30), (
                "registration never entered its fenced section")
            s3 = StubS3(versions={(BUCKET, key): "v1", (BUCKET, other): "v1"})
            Executor(conn, s3, actor="gc").execute(plan.plan_id,
                                                    commit=conn.commit)
        except Exception as exc:                      # noqa: BLE001
            errors["gc"] = exc
        finally:
            release.set()

    threads = [threading.Thread(target=registration_side),
              threading.Thread(target=gc_side)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"a side raised: {errors}"
    rows = dict((r[0], r[1]) for r in item_statuses(conn, plan.plan_id))
    assert rows[key] == "skipped-fenced", (
        "GC deleted a key the registrar's fence held live")
    assert rows[other] == "deleted", (
        "the fenced key blocked the run instead of only that item")


def test_exact_version_identity_survives_the_registration_fence(conn):
    """The fence is over `(bucket, object_key)`, never `version_id` — and
    that is deliberate (`pipeline.gc.execute`'s module docstring: exact-
    version deletion is a SEPARATE, independent layer). This pins that a
    registration fence over a key does not let GC's version check be
    skipped: GC still re-verifies `head_version` against the PLANNED
    version inside whatever fence it does acquire, so a key whose version
    moved is still refused on version grounds even when no fence blocks it
    at all.
    """
    require_gc_schema(conn)
    require_gc_fences_table(conn)
    key = "science/r/u/attempt-race-version/x.fits"
    repo, plan = approved_plan(conn, (key,),
                               "race-version-" + fixture.RUN_TAG)

    # No fence held by anyone — the version mismatch alone must refuse.
    s3 = StubS3(versions={(BUCKET, key): "v2"})  # plan recorded v1
    Executor(conn, s3, actor="gc").execute(plan.plan_id, commit=conn.commit)
    rows = dict((r[0], r[1]) for r in item_statuses(conn, plan.plan_id))
    assert rows[key] == "skipped-fenced"
    assert (BUCKET, key, "v1") not in s3.delete_calls
    assert _fence_rows(conn, BUCKET, key) == [], (
        "GC's own fence was not released after a version-mismatch skip")


def test_crash_recovery_a_registration_fence_left_behind_expires(
        conn, second_conn):
    """A registrar that crashes mid-bind leaves its fence row behind; the
    NEXT acquirer — here, GC — reclaims it once the lease elapses, with no
    separate sweep step, exactly as `pipeline.gc.fence.acquire_fence`'s
    docstring describes for the identical GC-crash case.
    """
    require_gc_fences_table(conn)
    key = "science/r/u/attempt-race-crash/x.fits"
    execute_a = _conn_executor(conn)

    # Simulate the crash: acquire and never release (a real crash never
    # calls `_bind_fence`'s `finally`).
    assert gc_fence.acquire_fence(
        execute_a, bucket=BUCKET, object_key=key, holder="crashed-registrar",
        holder_kind=gc_fence.HOLDER_REGISTRATION, lease_seconds=0)
    import time
    time.sleep(0.05)

    execute_b = _conn_executor(second_conn)
    assert gc_fence.acquire_fence(
        execute_b, bucket=BUCKET, object_key=key, holder="gc-recovery",
        holder_kind=gc_fence.HOLDER_GC), (
        "GC could not reclaim a registration fence left by a crashed "
        "registrar past its lease")
    held = gc_fence.held_by(execute_b, bucket=BUCKET, object_key=key)
    assert held[0] == "gc-recovery"
    gc_fence.release_fence(execute_b, bucket=BUCKET, object_key=key,
                           holder="gc-recovery")
