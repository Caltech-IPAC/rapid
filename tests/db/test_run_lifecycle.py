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

from .test_repository import _make_run, _make_unit, _register_simple_instance

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
    attempt_id = repo.allocate_attempt(conn, run_id, stage, unit_id)
    location = (f"{output_root}/runs/{run_id}/{stage}/{unit_id}/{attempt_id}"
                if output_root else f"runs/{run_id}/{stage}/{unit_id}/{attempt_id}")
    repo.record_attempt_result(
        conn, attempt_id, exit_code=0, disposition="succeeded",
        output_location=location, execution_record=EXEC_RECORD,
        scheduler_job_id="job-" + attempt_id)
    repo.select_attempt(conn, attempt_id)
    return unit_id, attempt_id


def _candidate(conn, run_id, *, kind="difference-image", key=None, output_root=None):
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
    repo.promote(conn, "brusholme", "select", [("difference-image", key, None, instance)])
    assert _custody(conn, instance) == "current"

    promotion_id = repo.promote(
        conn, "brusholme", "unselect", [("difference-image", key, instance, None)])

    assert _custody(conn, instance) == "candidate"
    ((kind, logical_key, before, after),) = _changes(conn, promotion_id)
    assert (kind, logical_key, before, after) == ("difference-image", key, instance, None)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM current_selection WHERE kind = %s AND logical_key = %s",
                    ("difference-image", json.dumps(key)))
        assert cur.fetchone()[0] == 0


def test_promote_refuses_after_equal_to_before(conn):
    run_id = _make_run(conn)
    key = {"k": new_ulid()}
    instance, _ = _candidate(conn, run_id, key=key)
    repo.promote(conn, "brusholme", "select", [("difference-image", key, None, instance)])
    with pytest.raises(repo.PromotionRefused):
        repo.promote(conn, "brusholme", "again", [("difference-image", key, instance, instance)])
    with pytest.raises(repo.PromotionRefused):
        repo.promote(conn, "brusholme", "nothing", [("difference-image", {"k": new_ulid()}, None, None)])


def test_promote_records_request_context(conn):
    run_id = _make_run(conn)
    key = {"k": new_ulid()}
    instance, _ = _candidate(conn, run_id, key=key)
    promotion_id = repo.promote(
        conn, "brusholme", "r", [("difference-image", key, None, instance)],
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
    diff, _ = _candidate(conn, run_id, kind="difference-image", key=diff_key)
    catalog, _ = _candidate(conn, run_id, kind="source-catalog", key=catalog_key)

    promotion_id = repo.promote_run(conn, run_id, "brusholme", "deliver")

    assert (_custody(conn, diff), _custody(conn, catalog)) == ("current", "current")
    changes = _changes(conn, promotion_id)
    assert [(c[0], c[2], c[3]) for c in changes] == [
        ("difference-image", None, diff), ("source-catalog", None, catalog)]
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
        conn, run_id, "brusholme", "only images", kinds=["difference-image"])

    ((kind, _key, before, after),) = _changes(conn, promotion_id)
    assert (kind, before, after) == ("difference-image", old, new)
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


def test_delete_run_refuses_a_non_scratch_bucket_then_resumes(conn):
    run_id = _make_run(conn, kind="scratch")
    _instance, attempt_id = _psf_run_row(conn, run_id, f"s3://project-bucket/test-{new_ulid()}")
    conn.commit()
    s3 = FakeVersionedS3()
    _seed_attempt_objects(s3, conn, attempt_id)

    with pytest.raises(repo.DeletionRefused, match="outside the scratch bucket"):
        cleanup.delete_run(conn, run_id, "brusholme", s3_client=s3,
                           scratch_bucket=SCRATCH_BUCKET)
    conn.rollback()
    assert _run_state(conn, run_id) == "deleting"
    assert len(s3.remaining("project-bucket")) == 4
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM psfs WHERE run = %s", (run_id,))
        assert cur.fetchone()[0] == 1

    # Resuming a 'deleting' run skips the fence and finishes the cleanup.
    report = cleanup.delete_run(conn, run_id, "brusholme", s3_client=s3,
                                scratch_bucket="project-bucket")
    assert report.rows_deleted["psfs"] == 1
    assert report.versions_deleted == 4
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
