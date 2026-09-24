"""Database-backed tests for the run lifecycle beyond registration:
unselect, vbest maintenance, promote_run, rollback_promotion, finish_run,
expiry defaults, and scratch-run deletion, expiry and pinning
(rapidpipe.runs.repository, rapidpipe.runs.cleanup).

Skips cleanly if PGHOST is unset (see conftest.py). Most tests run inside
the conftest's never-committed transaction. ``delete_run`` and
``expire_runs`` commit by design (the ``deleting`` state must be durable
before cleanup starts), so the tests that call them commit too and use
fresh ULIDs in every logical key and prefix so their committed rows never
meet another test's; CI's database is created fresh per job.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from rapidpipe.db.ids import new_ulid
from rapidpipe.db.psfs import register_psf
from rapidpipe.runs import cleanup
from rapidpipe.runs import repository as repo
from tests.unit.fakes3 import FakeVersionedS3

from .test_repository import TEST_KIND, _make_run, _make_unit, _register_simple_instance

MD5 = "9e107d9d372bb6826bd81d3542a419d6"
SCRATCH_BUCKET = "scratch-bucket"
EXEC_RECORD = {"source_revision": "abc123", "schema_version": "1", "settings_hash": "h"}


# ======================================================================
# Helpers
# ======================================================================

def _selected_attempt(conn, run_id, *, stage="difference", unit_id=None, output_root=None):
    """A unit with one succeeded, selected attempt; returns (unit_id, attempt_id).

    ``output_root``, when given, records the attempt's output location as
    ``<output_root>/runs/<run>/<stage>/<unit>/<attempt>`` (as submit_unit
    does); otherwise allocate_attempt's local placeholder is kept.
    """
    unit_id = unit_id or new_ulid()
    _make_unit(conn, run_id, stage=stage, unit_id=unit_id)
    attempt_id = repo.allocate_attempt(conn, run_id, stage, unit_id, outputs_root=output_root)
    with conn.cursor() as cur:
        cur.execute("SELECT output_location FROM attempts WHERE id = %s", (attempt_id,))
        (location,) = cur.fetchone()
    repo.record_attempt_result(
        conn, attempt_id, exit_code=0, disposition="succeeded",
        output_location=location, execution_record=EXEC_RECORD,
        scheduler_job_id="job-" + attempt_id)
    repo.select_attempt(conn, attempt_id)
    return unit_id, attempt_id


def _candidate(conn, run_id, *, kind=TEST_KIND, key=None, output_root=None):
    """One registered instance from a selected attempt; returns (instance, attempt)."""
    key = key if key is not None else {"k": new_ulid()}
    _unit, attempt_id = _selected_attempt(conn, run_id, output_root=output_root)
    instance = _register_simple_instance(
        conn, run_id, "difference", attempt_id, kind=kind, logical_key=key)
    return instance, attempt_id


def _psf_candidate(conn, run_id, key):
    """A registered ``psf`` instance with its ``psfs`` row (vbest 0)."""
    instance, attempt_id = _candidate(conn, run_id, kind="psf", key=key)
    register_psf(
        conn,
        entry={
            "kind": "psf", "format_version": "1", "instance": instance, "key": key,
            "primary": "psf/psf.fits",
            "members": [{"role": "psf", "path": "psf/psf.fits", "bytes": 3,
                         "sha256": "sha256:" + "3" * 64}],
            "registration": {"md5": MD5, "status": 1},
        },
        run_id=run_id, attempt_id=attempt_id, output_location="/tmp/psf-test")
    return instance


def _psf_key():
    return {"filter": "F184", "detector": "7", "version": new_ulid()}


def _custody(conn, instance):
    with conn.cursor() as cur:
        cur.execute("SELECT custody FROM product_instances WHERE id = %s", (instance,))
        return cur.fetchone()[0]


def _vbest(conn, instance):
    with conn.cursor() as cur:
        cur.execute("SELECT vbest FROM psfs WHERE instance = %s", (instance,))
        return cur.fetchone()[0]


def _changes(conn, promotion_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, logical_key, before_instance, after_instance "
            "FROM promotion_changes WHERE promotion = %s ORDER BY kind", (promotion_id,))
        return cur.fetchall()


def _run_state(conn, run_id):
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM runs WHERE id = %s", (run_id,))
        return cur.fetchone()[0]


# ======================================================================
# create_run: expires_at
# ======================================================================

def test_create_run_scratch_expires_14_days_after_creation(conn):
    run_id = _make_run(conn, kind="scratch")
    with conn.cursor() as cur:
        cur.execute("SELECT expires_at - created FROM runs WHERE id = %s", (run_id,))
        (lifetime,) = cur.fetchone()
    assert lifetime == timedelta(days=14)


def test_create_run_production_never_expires_and_explicit_expiry_wins(conn):
    production = _make_run(conn, kind="production")
    explicit_at = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    explicit = _make_run(conn, kind="scratch", expires_at=explicit_at)
    with conn.cursor() as cur:
        cur.execute("SELECT id, expires_at FROM runs WHERE id IN (%s, %s)", (production, explicit))
        rows = dict(cur.fetchall())
    assert rows[production] is None
    assert rows[explicit] == explicit_at


# ======================================================================
# promote: unselect, same-instance refusal, vbest
# ======================================================================

def test_promote_with_after_none_unselects(conn):
    run_id = _make_run(conn)
    key = {"k": new_ulid()}
    instance, _ = _candidate(conn, run_id, key=key)
    repo.promote(conn, "brusholme", "select", [(TEST_KIND, key, None, instance)])
    assert _custody(conn, instance) == "current"

    promotion_id = repo.promote(
        conn, "brusholme", "unselect", [(TEST_KIND, key, instance, None)])

    assert _custody(conn, instance) == "candidate"
    ((kind, logical_key, before, after),) = _changes(conn, promotion_id)
    assert (kind, logical_key, before, after) == (TEST_KIND, key, instance, None)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM current_selection WHERE kind = %s AND logical_key = %s",
                    (TEST_KIND, json.dumps(key)))
        assert cur.fetchone()[0] == 0


def test_promote_refuses_after_equal_to_before(conn):
    run_id = _make_run(conn)
    key = {"k": new_ulid()}
    instance, _ = _candidate(conn, run_id, key=key)
    repo.promote(conn, "brusholme", "select", [(TEST_KIND, key, None, instance)])
    with pytest.raises(repo.PromotionRefused):
        repo.promote(conn, "brusholme", "again", [(TEST_KIND, key, instance, instance)])
    with pytest.raises(repo.PromotionRefused):
        repo.promote(conn, "brusholme", "nothing", [(TEST_KIND, {"k": new_ulid()}, None, None)])


def test_promote_records_request_context(conn):
    run_id = _make_run(conn)
    key = {"k": new_ulid()}
    instance, _ = _candidate(conn, run_id, key=key)
    promotion_id = repo.promote(
        conn, "brusholme", "r", [(TEST_KIND, key, None, instance)],
        request_context={"ticket": "T-1"})
    with conn.cursor() as cur:
        cur.execute("SELECT request_context FROM promotions WHERE id = %s", (promotion_id,))
        assert cur.fetchone()[0] == {"ticket": "T-1"}


def test_vbest_follows_promotion_and_rollback(conn):
    key = _psf_key()
    first_run = _make_run(conn)
    first = _psf_candidate(conn, first_run, key)
    assert _vbest(conn, first) == 0

    first_promotion = repo.promote_run(conn, first_run, "brusholme", "first")
    assert _vbest(conn, first) == 1

    second_run = _make_run(conn)
    second = _psf_candidate(conn, second_run, key)
    second_promotion = repo.promote_run(conn, second_run, "brusholme", "reprocess")
    assert (_vbest(conn, first), _vbest(conn, second)) == (0, 1)
    ((_, _, before, after),) = _changes(conn, second_promotion)
    assert (before, after) == (first, second)

    rollback = repo.rollback_promotion(conn, second_promotion, "brusholme", "bad reprocess")
    assert (_vbest(conn, first), _vbest(conn, second)) == (1, 0)
    assert (_custody(conn, first), _custody(conn, second)) == ("current", "candidate")

    # Rolling back the very first promotion unselects the key entirely.
    repo.rollback_promotion(conn, rollback, "brusholme", "undo the undo")
    assert (_vbest(conn, first), _vbest(conn, second)) == (0, 1)
    assert first_promotion  # recorded, reversible chain intact


# ======================================================================
# promote_run
# ======================================================================

def test_promote_run_promotes_one_candidate_per_key_across_kinds(conn):
    run_id = _make_run(conn)
    diff_key, catalog_key = {"k": new_ulid()}, {"k": new_ulid()}
    diff, _ = _candidate(conn, run_id, kind=TEST_KIND, key=diff_key)
    catalog, _ = _candidate(conn, run_id, kind="source-catalog", key=catalog_key)

    promotion_id = repo.promote_run(conn, run_id, "brusholme", "deliver")

    assert (_custody(conn, diff), _custody(conn, catalog)) == ("current", "current")
    changes = _changes(conn, promotion_id)  # ordered by kind
    assert [(c[0], c[2], c[3]) for c in changes] == [
        ("source-catalog", None, catalog), (TEST_KIND, None, diff)]
    with conn.cursor() as cur:
        cur.execute("SELECT request_context FROM promotions WHERE id = %s", (promotion_id,))
        assert cur.fetchone()[0] == {"run": run_id}


def test_promote_run_kinds_filter_and_replaces_the_current_instance(conn):
    key = {"k": new_ulid()}
    old_run = _make_run(conn)
    old, _ = _candidate(conn, old_run, key=key)
    repo.promote_run(conn, old_run, "brusholme", "first")

    run_id = _make_run(conn)
    new, _ = _candidate(conn, run_id, key=key)
    other, _ = _candidate(conn, run_id, kind="source-catalog")

    promotion_id = repo.promote_run(
        conn, run_id, "brusholme", "only images", kinds=[TEST_KIND])

    ((kind, _key, before, after),) = _changes(conn, promotion_id)
    assert (kind, before, after) == (TEST_KIND, old, new)
    assert _custody(conn, other) == "candidate"
    assert (_custody(conn, old), _custody(conn, new)) == ("candidate", "current")


def test_promote_run_skips_candidates_from_unselected_attempts(conn):
    run_id = _make_run(conn)
    selected, _ = _candidate(conn, run_id)

    unit_id = new_ulid()
    _make_unit(conn, run_id, unit_id=unit_id)
    attempt_id = repo.allocate_attempt(conn, run_id, "difference", unit_id)
    repo.record_attempt_result(
        conn, attempt_id, exit_code=0, disposition="succeeded",
        output_location="runs/x", execution_record=EXEC_RECORD, scheduler_job_id="j")
    unselected = _register_simple_instance(
        conn, run_id, "difference", attempt_id, logical_key={"k": new_ulid()})

    promotion_id = repo.promote_run(conn, run_id, "brusholme", "deliver")

    assert [c[3] for c in _changes(conn, promotion_id)] == [selected]
    assert _custody(conn, unselected) == "candidate"


def test_promote_run_refuses_scratch(conn):
    run_id = _make_run(conn, kind="scratch")
    _candidate(conn, run_id)
    with pytest.raises(repo.PromotionRefused, match="scratch never leaves scratch"):
        repo.promote_run(conn, run_id, "brusholme", "no")


def test_promote_run_refuses_two_candidates_for_one_key(conn):
    run_id = _make_run(conn)
    key = {"k": new_ulid()}
    _candidate(conn, run_id, key=key)
    _candidate(conn, run_id, key=key)
    with pytest.raises(repo.PromotionRefused, match="more than one candidate"):
        repo.promote_run(conn, run_id, "brusholme", "ambiguous")


def test_promote_run_refuses_when_nothing_to_promote(conn):
    run_id = _make_run(conn)
    with pytest.raises(repo.PromotionRefused, match="nothing to promote"):
        repo.promote_run(conn, run_id, "brusholme", "empty")
    _candidate(conn, run_id, kind="source-catalog")
    with pytest.raises(repo.PromotionRefused, match="nothing to promote"):
        repo.promote_run(conn, run_id, "brusholme", "filtered out", kinds=["psf"])


# ======================================================================
# rollback_promotion
# ======================================================================

def test_rollback_promotion_restores_the_prior_selection(conn):
    key = {"k": new_ulid()}
    first_run, second_run = _make_run(conn), _make_run(conn)
    first, _ = _candidate(conn, first_run, key=key)
    repo.promote_run(conn, first_run, "brusholme", "first")
    second, _ = _candidate(conn, second_run, key=key)
    promotion_id = repo.promote_run(conn, second_run, "brusholme", "second")

    rollback_id = repo.rollback_promotion(conn, promotion_id, "brusholme", "revert")

    assert (_custody(conn, first), _custody(conn, second)) == ("current", "candidate")
    ((_, _, before, after),) = _changes(conn, rollback_id)
    assert (before, after) == (second, first)
    with conn.cursor() as cur:
        cur.execute("SELECT request_context FROM promotions WHERE id = %s", (rollback_id,))
        assert cur.fetchone()[0] == {"rollback_of": promotion_id}


def test_rollback_promotion_refused_after_a_later_promotion_changed_the_key(conn):
    key = {"k": new_ulid()}
    first_run, second_run = _make_run(conn), _make_run(conn)
    first, _ = _candidate(conn, first_run, key=key)
    first_promotion = repo.promote_run(conn, first_run, "brusholme", "first")
    second, _ = _candidate(conn, second_run, key=key)
    repo.promote_run(conn, second_run, "brusholme", "second")

    with pytest.raises(repo.PromotionRefused):
        repo.rollback_promotion(conn, first_promotion, "brusholme", "too late")
    assert (_custody(conn, first), _custody(conn, second)) == ("candidate", "current")


def test_rollback_promotion_refuses_an_unknown_promotion(conn):
    with pytest.raises(repo.PromotionRefused):
        repo.rollback_promotion(conn, new_ulid(), "brusholme", "nope")


# ======================================================================
# finish_run
# ======================================================================

def test_finish_run_happy_path(conn):
    run_id = _make_run(conn)
    _selected_attempt(conn, run_id)
    repo.finish_run(conn, run_id)
    with conn.cursor() as cur:
        cur.execute("SELECT state, finished_at FROM runs WHERE id = %s", (run_id,))
        state, finished_at = cur.fetchone()
    assert state == "finished"
    assert finished_at is not None


def test_finish_run_refusals(conn):
    empty = _make_run(conn)
    with pytest.raises(repo.RunNotFinishable, match="no units"):
        repo.finish_run(conn, empty)

    running = _make_run(conn)
    _selected_attempt(conn, running)
    unit_id = new_ulid()
    _make_unit(conn, running, unit_id=unit_id)
    repo.allocate_attempt(conn, running, "difference", unit_id)
    with pytest.raises(repo.RunNotFinishable, match="not yet"):
        repo.finish_run(conn, running)

    done = _make_run(conn)
    _selected_attempt(conn, done)
    repo.finish_run(conn, done)
    with pytest.raises(repo.RunNotFinishable, match="'finished'"):
        repo.finish_run(conn, done)


# ======================================================================
# delete_run (commits)
# ======================================================================

def _addpsf_row(conn, *, run_id=None):
    """A bare ``psfs`` row through dev's addPSF; run columns NULL unless set later."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT psfid FROM addPSF(
                cast(1 as smallint), cast(7 as smallint),
                cast(%s as character varying(255)), cast(%s as character varying(32)),
                cast(1 as smallint)
            ) AS (psfid integer, version smallint)
            """,
            (f"/tmp/null-run-{new_ulid()}.fits", MD5))
        return cur.fetchone()[0]


def _psf_run_row(conn, run_id, output_root):
    """A ``psfs`` row belonging to ``run_id`` (instance + attempt from the run)."""
    _unit, attempt_id = _selected_attempt(conn, run_id, output_root=output_root)
    key = _psf_key()
    instance = _register_simple_instance(
        conn, run_id, "difference", attempt_id, kind="psf", logical_key=key)
    register_psf(
        conn,
        entry={
            "kind": "psf", "format_version": "1", "instance": instance, "key": key,
            "primary": "psf/psf.fits",
            "members": [{"role": "psf", "path": "psf/psf.fits", "bytes": 3,
                         "sha256": "sha256:" + "3" * 64}],
            "registration": {"md5": MD5, "status": 1},
        },
        run_id=run_id, attempt_id=attempt_id, output_location="/tmp/psf-delete")
    return instance, attempt_id


def _seed_attempt_objects(s3, conn, attempt_id, *, versions=2):
    with conn.cursor() as cur:
        cur.execute("SELECT output_location FROM attempts WHERE id = %s", (attempt_id,))
        (location,) = cur.fetchone()
    bucket, _, prefix = location[len("s3://"):].partition("/")
    s3.seed(bucket, f"{prefix}/manifest.json", versions=versions)
    s3.seed(bucket, f"{prefix}/psf/psf.fits", versions=1, delete_marker=True)
    return bucket, prefix


def test_delete_run_removes_only_this_runs_objects_and_rows(conn):
    root = f"s3://{SCRATCH_BUCKET}/test-{new_ulid()}"
    run_id = _make_run(conn, kind="scratch")
    instance, attempt_id = _psf_run_row(conn, run_id, root)
    other_run = _make_run(conn, kind="scratch")
    other_instance, other_attempt = _psf_run_row(conn, other_run, root)
    null_run_psfid = _addpsf_row(conn)
    conn.commit()

    s3 = FakeVersionedS3()
    _bucket, prefix = _seed_attempt_objects(s3, conn, attempt_id)
    _other_bucket, other_prefix = _seed_attempt_objects(s3, conn, other_attempt)

    report = cleanup.delete_run(
        conn, run_id, "brusholme", s3_client=s3, scratch_bucket=SCRATCH_BUCKET)

    assert report.run_id == run_id and not report.already_deleted
    assert (report.objects_deleted, report.versions_deleted) == (2, 4)
    assert report.rows_deleted["psfs"] == 1
    assert set(report.rows_deleted) == set(cleanup.SCIENCE_TABLES)
    assert report.instances_marked == 1
    assert s3.remaining(SCRATCH_BUCKET, prefix + "/") == []
    assert len(s3.remaining(SCRATCH_BUCKET, other_prefix + "/")) == 4

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM psfs WHERE run = %s", (run_id,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM psfs WHERE instance = %s", (other_instance,))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM psfs WHERE psfid = %s", (null_run_psfid,))
        assert cur.fetchone()[0] == 1
        # Tombstones stay: run, units, attempts, instances, members.
        cur.execute("SELECT state, deleted_at IS NOT NULL FROM runs WHERE id = %s", (run_id,))
        assert cur.fetchone() == ("deleted", True)
        cur.execute("SELECT count(*) FROM attempts WHERE run = %s", (run_id,))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM units WHERE run = %s", (run_id,))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT deletion_state FROM product_instances WHERE id = %s", (instance,))
        assert cur.fetchone()[0] == "deleted"
        cur.execute("SELECT count(*) FROM product_members WHERE instance = %s", (instance,))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT deletion_state FROM product_instances WHERE id = %s", (other_instance,))
        assert cur.fetchone()[0] == "retained"

    again = cleanup.delete_run(conn, run_id, "brusholme", s3_client=s3,
                               scratch_bucket=SCRATCH_BUCKET)
    assert again.already_deleted


def test_delete_run_refuses_a_non_scratch_bucket_before_marking(conn):
    run_id = _make_run(conn, kind="scratch")
    _instance, attempt_id = _psf_run_row(conn, run_id, f"s3://project-bucket/test-{new_ulid()}")
    conn.commit()
    s3 = FakeVersionedS3()
    _seed_attempt_objects(s3, conn, attempt_id)

    with pytest.raises(repo.DeletionRefused, match="outside the scratch bucket"):
        cleanup.delete_run(conn, run_id, "brusholme", s3_client=s3,
                           scratch_bucket=SCRATCH_BUCKET)
    conn.rollback()
    # Preflight refuses before the run is marked: it keeps its state.
    assert _run_state(conn, run_id) == "open"
    assert len(s3.remaining("project-bucket")) == 4
    assert [c for c in s3.calls if c[0] == "delete_objects"] == []
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM psfs WHERE run = %s", (run_id,))
        assert cur.fetchone()[0] == 1


def test_delete_run_refuses_a_location_outside_the_runs_prefix(conn):
    run_id = _make_run(conn, kind="scratch")
    _instance, attempt_id = _psf_run_row(conn, run_id, f"s3://{SCRATCH_BUCKET}/t-{new_ulid()}")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE attempts SET output_location = %s WHERE id = %s",
            (f"s3://{SCRATCH_BUCKET}/elsewhere/{attempt_id}", attempt_id))
    conn.commit()
    s3 = FakeVersionedS3()
    s3.seed(SCRATCH_BUCKET, f"elsewhere/{attempt_id}/manifest.json")

    with pytest.raises(repo.DeletionRefused, match=f"not under runs/{run_id}/"):
        cleanup.delete_run(conn, run_id, "brusholme", s3_client=s3,
                           scratch_bucket=SCRATCH_BUCKET)
    conn.rollback()
    assert _run_state(conn, run_id) == "open"
    assert len(s3.remaining(SCRATCH_BUCKET, "elsewhere/")) == 1


def test_delete_run_s3_error_leaves_deleting_and_the_database_untouched_then_resumes(conn):
    run_id = _make_run(conn, kind="scratch")
    instance, attempt_id = _psf_run_row(conn, run_id, f"s3://{SCRATCH_BUCKET}/t-{new_ulid()}")
    conn.commit()
    s3 = FakeVersionedS3()
    _bucket, prefix = _seed_attempt_objects(s3, conn, attempt_id)
    s3.fail_keys.add(f"{prefix}/manifest.json")

    with pytest.raises(cleanup.CleanupFailed):
        cleanup.delete_run(conn, run_id, "brusholme", s3_client=s3,
                           scratch_bucket=SCRATCH_BUCKET)
    conn.rollback()
    assert _run_state(conn, run_id) == "deleting"
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM psfs WHERE run = %s", (run_id,))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT deletion_state FROM product_instances WHERE id = %s", (instance,))
        assert cur.fetchone()[0] == "retained"

    # Resuming a 'deleting' run skips the fence and finishes the cleanup.
    s3.fail_keys.clear()
    report = cleanup.delete_run(conn, run_id, "someone-else", s3_client=s3,
                                scratch_bucket=SCRATCH_BUCKET)
    assert report.rows_deleted["psfs"] == 1
    assert report.instances_marked == 1
    assert s3.remaining(SCRATCH_BUCKET, prefix + "/") == []
    assert _run_state(conn, run_id) == "deleted"


def test_delete_run_keeps_the_fence(conn):
    production = _make_run(conn, kind="production")
    with pytest.raises(repo.DeletionRefused):
        cleanup.delete_run(conn, production, "brusholme", s3_client=FakeVersionedS3(),
                           scratch_bucket=SCRATCH_BUCKET)
    conn.rollback()

    foreign = _make_run(conn, kind="scratch", owner="alice")
    with pytest.raises(repo.DeletionRefused):
        cleanup.delete_run(conn, foreign, "bob", s3_client=FakeVersionedS3(),
                           scratch_bucket=SCRATCH_BUCKET)


def test_delete_run_with_no_s3_outputs_needs_no_bucket(conn, monkeypatch):
    for name in ("RAPIDPIPE_SCRATCH_BUCKET", "RAPIDPIPE_OUTPUTS_ROOT_SCRATCH",
                 "RAPIDPIPE_OUTPUTS_ROOT"):
        monkeypatch.delenv(name, raising=False)
    run_id = _make_run(conn, kind="scratch")
    _selected_attempt(conn, run_id)  # local placeholder location
    conn.commit()
    report = cleanup.delete_run(conn, run_id, "brusholme")
    assert (report.objects_deleted, report.versions_deleted) == (0, 0)
    assert _run_state(conn, run_id) == "deleted"


# ======================================================================
# expire_runs / pin_run (commit)
# ======================================================================

def test_expire_runs_deletes_only_unpinned_expired_scratch_runs(conn):
    # A fixed past window no other test uses, so the sweep sees only these.
    expired_at = datetime(2001, 3, 4, tzinfo=timezone.utc)
    now = expired_at + timedelta(days=1)

    expired = _make_run(conn, kind="scratch", expires_at=expired_at)
    finished = _make_run(conn, kind="scratch", expires_at=expired_at)
    _selected_attempt(conn, finished)
    repo.finish_run(conn, finished)
    pinned = _make_run(conn, kind="scratch", expires_at=expired_at)
    cleanup.pin_run(conn, pinned, True)
    production = _make_run(conn, kind="production", expires_at=expired_at)
    later = _make_run(conn, kind="scratch", expires_at=now + timedelta(days=1))
    busy = _make_run(conn, kind="scratch", expires_at=expired_at)
    unit_id = new_ulid()
    _make_unit(conn, busy, unit_id=unit_id)
    repo.allocate_attempt(conn, busy, "difference", unit_id)  # unresolved: fence refuses
    conn.commit()

    reports = cleanup.expire_runs(conn, now=now, s3_client=FakeVersionedS3(),
                                  scratch_bucket=SCRATCH_BUCKET)
    by_run = {r.run_id: r for r in reports}

    assert {expired, finished, busy} <= set(by_run)
    assert not {pinned, production, later} & set(by_run)
    assert by_run[expired].refused is None
    assert by_run[finished].refused is None
    assert by_run[busy].refused and "unresolved" in by_run[busy].refused
    assert _run_state(conn, expired) == "deleted"
    assert _run_state(conn, finished) == "deleted"
    assert _run_state(conn, busy) == "open"
    assert _run_state(conn, pinned) == "open"
    assert _run_state(conn, later) == "open"


def test_pin_run_sets_and_clears_the_flag(conn):
    run_id = _make_run(conn, kind="scratch")
    cleanup.pin_run(conn, run_id, True)
    with conn.cursor() as cur:
        cur.execute("SELECT pinned FROM runs WHERE id = %s", (run_id,))
        assert cur.fetchone()[0] is True
    cleanup.pin_run(conn, run_id, False)
    with conn.cursor() as cur:
        cur.execute("SELECT pinned FROM runs WHERE id = %s", (run_id,))
        assert cur.fetchone()[0] is False
    with pytest.raises(repo.RunNotFound):
        cleanup.pin_run(conn, new_ulid(), True)


# ======================================================================
# Amendments (supervisor step 3, 2026-09-24): A1 fence, A2 vbest,
# A3 eligibility, A6 sweeper predicate, A7 final attempt location
# ======================================================================

def test_allocate_attempt_records_the_final_location(conn):
    run_id = _make_run(conn, kind="scratch")
    unit_id = new_ulid()
    _make_unit(conn, run_id, unit_id=unit_id)
    attempt_id = repo.allocate_attempt(
        conn, run_id, "difference", unit_id, outputs_root="s3://bucket/root")
    with conn.cursor() as cur:
        cur.execute("SELECT output_location FROM attempts WHERE id = %s", (attempt_id,))
        (location,) = cur.fetchone()
    assert location == f"s3://bucket/root/runs/{run_id}/difference/{unit_id}/{attempt_id}"


def test_finished_run_admits_no_new_unit_or_attempt(conn):
    run_id = _make_run(conn)
    _selected_attempt(conn, run_id)
    repo.finish_run(conn, run_id)
    with pytest.raises(repo.RunDeletingOrDeleted, match="admits no new work"):
        repo.add_unit(conn, run_id, "difference", "detector-image", new_ulid())


def test_register_manifest_refused_on_a_deleting_run(conn):
    run_id = _make_run(conn, kind="scratch")
    _unit, attempt_id = _selected_attempt(conn, run_id)
    repo.mark_run_deleting(conn, run_id, requested_by="brusholme")
    with pytest.raises(repo.RunDeletingOrDeleted):
        _register_simple_instance(conn, run_id, "difference", attempt_id,
                                  logical_key={"k": new_ulid()})
    with pytest.raises(repo.RunDeletingOrDeleted):
        repo.record_attempt_result(
            conn, attempt_id, exit_code=0, disposition="succeeded",
            output_location="runs/x", execution_record=EXEC_RECORD,
            scheduler_job_id="j")


def test_dependency_on_a_deleting_producer_is_refused(conn):
    producer_run = _make_run(conn, kind="scratch")
    producer, _ = _candidate(conn, producer_run)
    repo.mark_run_deleting(conn, producer_run, requested_by="brusholme")

    consumer_run = _make_run(conn, kind="scratch")
    _unit, attempt_id = _selected_attempt(conn, consumer_run)
    with pytest.raises(repo.RunDeletingOrDeleted, match=producer_run):
        _register_simple_instance(
            conn, consumer_run, "difference", attempt_id,
            logical_key={"k": new_ulid()}, input_products={TEST_KIND: producer})


def test_promote_of_a_mapped_kind_with_no_dev_row_touches_no_vbest(conn):
    run_id = _make_run(conn)
    instance, _ = _candidate(conn, run_id, kind="psf", key=_psf_key())  # no psfs row
    promotion_id = repo.promote_run(conn, run_id, "brusholme", "no dev row")
    assert _custody(conn, instance) == "current"
    repo.rollback_promotion(conn, promotion_id, "brusholme", "undo")
    assert _custody(conn, instance) == "candidate"
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM psfs WHERE instance = %s", (instance,))
        assert cur.fetchone()[0] == 0


def test_promote_never_rewrites_a_dev_written_row(conn):
    """A dev row linked to an instance but written by no run (``run`` NULL,
    as an import run links one) keeps dev's own vbest on promote and
    rollback. The schema's together-CHECK (20260923-07) does not allow
    that row shape yet, so this test drops the CHECK inside its own
    never-committed transaction to build it."""
    run_id = _make_run(conn)
    key = _psf_key()
    instance = _psf_candidate(conn, run_id, key)
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE psfs DROP CONSTRAINT psfs_run_columns_together")
        cur.execute(
            "UPDATE psfs SET run = NULL, attempt = NULL, vbest = 2 WHERE instance = %s",
            (instance,))

    promotion_id = repo.promote_run(conn, run_id, "brusholme", "import")
    assert _custody(conn, instance) == "current"
    assert _vbest(conn, instance) == 2
    repo.rollback_promotion(conn, promotion_id, "brusholme", "undo")
    assert _custody(conn, instance) == "candidate"
    assert _vbest(conn, instance) == 2


def test_promote_refuses_a_kind_or_key_that_does_not_match_the_instance(conn):
    run_id = _make_run(conn)
    key = {"k": new_ulid()}
    instance, _ = _candidate(conn, run_id, key=key)
    with pytest.raises(repo.PromotionRefused, match="not the requested"):
        repo.promote(conn, "brusholme", "wrong kind", [("other-kind", key, None, instance)])
    with pytest.raises(repo.PromotionRefused, match="not the requested"):
        repo.promote(conn, "brusholme", "wrong key",
                     [(TEST_KIND, {"k": new_ulid()}, None, instance)])


def test_promote_refuses_a_deleted_after_instance(conn):
    run_id = _make_run(conn)
    key = {"k": new_ulid()}
    instance, _ = _candidate(conn, run_id, key=key)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE product_instances SET deletion_state = 'deleted' WHERE id = %s",
            (instance,))
    with pytest.raises(repo.PromotionRefused, match="not retained"):
        repo.promote(conn, "brusholme", "gone", [(TEST_KIND, key, None, instance)])


def test_promote_refuses_an_incomplete_result_set(conn):
    run_id = _make_run(conn)
    _unit, attempt_id = _selected_attempt(conn, run_id)
    instance, key = new_ulid(), {"k": new_ulid()}
    repo.register_manifest(conn, {
        "run": run_id, "unit": {"kind": "detector-image", "id": "u"},
        "stage": "load", "attempt": attempt_id,
        "inputs": {"manifest": "m", "products": {}, "result_sets": []},
        "outputs": [{"kind": "source-set", "format_version": "1", "instance": instance,
                     "key": key, "primary": None, "members": []}],
    }, registering_attempt_id=attempt_id)
    with conn.cursor() as cur:
        cur.execute("UPDATE result_sets SET complete = false WHERE instance = %s", (instance,))
    with pytest.raises(repo.PromotionRefused, match="incomplete result set"):
        repo.promote(conn, "brusholme", "partial", [("source-set", key, None, instance)])


def test_the_sweeper_refuses_a_pinned_or_unexpired_run(conn):
    past = datetime(2001, 5, 6, tzinfo=timezone.utc)
    pinned = _make_run(conn, kind="scratch", expires_at=past)
    cleanup.pin_run(conn, pinned, True)
    unexpired = _make_run(conn, kind="scratch")  # default: now() + 14 days
    expired = _make_run(conn, kind="scratch", owner="alice", expires_at=past)

    with pytest.raises(repo.DeletionRefused, match="pinned"):
        repo.mark_run_deleting(conn, pinned, cleanup.EXPIRY_ACTOR, expiry=True)
    with pytest.raises(repo.DeletionRefused, match="not expired"):
        repo.mark_run_deleting(conn, unexpired, cleanup.EXPIRY_ACTOR, expiry=True)
    # The owner check is replaced, not added: another owner's expired run passes.
    repo.mark_run_deleting(conn, expired, cleanup.EXPIRY_ACTOR, expiry=True)
    assert _run_state(conn, expired) == "deleting"
