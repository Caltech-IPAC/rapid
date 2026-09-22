"""Database-backed, end-to-end tests for rapidpipe.launch.batch against a
real PostgreSQL with the run-model migration applied.

Batch and S3 are exercised through fakes only (tests/unit/fakebatch.py,
tests/unit/fakes3.py) -- no AWS calls, no account ids, queue or
definition names, bucket names or hostnames. Following
tests/db/test_local_runner.py's pattern for the admit half of these
tests: admit runs as a real subprocess through run_stage_locally, so its
manifest names a real run/attempt this test's own connection can then
query.

Skips cleanly if PGHOST is unset (see conftest.py).
"""

from __future__ import annotations

import json
import os

import pytest

from rapidpipe.launch import batch as launch_batch
from rapidpipe.runs import repository as repo
from rapidpipe.runs.local import run_stage_locally
from tests.unit.fakebatch import FakeBatch
from tests.unit.fakes3 import FakeS3
from tests.unit.test_admit import _build_delivery

from .test_repository import _make_run


def _cleanup_exposure(exposure_external_id: str) -> None:
    import psycopg2

    conn = psycopg2.connect(
        host=os.environ["PGHOST"],
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.environ.get("PGPASSWORD", ""),
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM exposures WHERE external_id = %s "
                "AND NOT EXISTS (SELECT 1 FROM l2files WHERE expid = exposures.expid)",
                (exposure_external_id,))
    finally:
        conn.close()


@pytest.fixture()
def batch_env(monkeypatch, tmp_path):
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "test-queue")
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_DEFINITION", "test-def")
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", "s3://test-bucket/prefix")
    return tmp_path


# ======================================================================
# submit_unit
# ======================================================================

def test_submit_unit_records_attempt_and_scheduler_job(conn, batch_env):
    run_id = _make_run(conn, kind="scratch", max_attempts=2)
    conn.commit()

    fake = FakeBatch()
    submission = launch_batch.submit_unit(
        conn, run_id=run_id, stage="admit", unit_kind="detector-image",
        unit_id="db-submit-001/SCA07", inputs_location="s3://in-bucket/in-prefix",
        client=fake)
    conn.commit()

    assert submission.output_location == (
        f"s3://test-bucket/prefix/runs/{run_id}/admit/"
        f"db-submit-001/SCA07/{submission.attempt_id}")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT output_location, scheduler_job_id, disposition "
            "FROM attempts WHERE id = %s",
            (submission.attempt_id,))
        output_location, scheduler_job_id, disposition = cur.fetchone()

    assert output_location == submission.output_location
    assert scheduler_job_id == submission.job_id
    assert disposition is None


# ======================================================================
# resolve_inputs_from_stage
# ======================================================================

def test_resolve_inputs_from_stage_after_admit(conn, tmp_path):
    run_id = _make_run(conn, kind="scratch", max_attempts=1)
    conn.commit()

    inputs_dir, _ = _build_delivery(tmp_path)
    outputs_root = tmp_path / "outputs"
    admit_unit_id = "db-resolve-001/SCA07"

    admit_result = run_stage_locally(
        conn, run_id=run_id, stage="admit", unit_kind="detector-image",
        unit_id=admit_unit_id, inputs=str(inputs_dir), outputs_root=str(outputs_root))
    conn.commit()

    instance_id = None
    try:
        assert admit_result.disposition == "succeeded"
        assert admit_result.selected is True

        from rapidpipe.products.manifest import Manifest

        admit_manifest = Manifest.read(admit_result.manifest_path)
        instance_id = admit_manifest.outputs[0].instance

        location = launch_batch.resolve_inputs_from_stage(
            conn, run_id=run_id, unit_id=admit_unit_id, upstream_stage="admit")
        assert location == admit_result.output_location
    finally:
        if instance_id is not None:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM l2filemeta WHERE rid IN "
                    "(SELECT rid FROM l2files WHERE instance = %s)", (instance_id,))
                cur.execute("DELETE FROM l2files WHERE instance = %s", (instance_id,))
            conn.commit()
        from tests.unit.test_admit import EXPOSURE_ID
        _cleanup_exposure(EXPOSURE_ID)


def test_resolve_inputs_from_stage_refuses_before_selection(conn):
    run_id = _make_run(conn, kind="scratch", max_attempts=2)
    conn.commit()

    stage, unit_id = "admit", "db-resolve-unselected/SCA07"
    repo.add_unit(conn, run_id, stage, "detector-image", unit_id)
    conn.commit()
    repo.allocate_attempt(conn, run_id, stage, unit_id)
    conn.commit()

    with pytest.raises(launch_batch.DependencyIncomplete):
        launch_batch.resolve_inputs_from_stage(
            conn, run_id=run_id, unit_id=unit_id, upstream_stage=stage)


def test_resolve_inputs_from_stage_refuses_unknown_unit(conn):
    run_id = _make_run(conn, kind="scratch", max_attempts=1)
    conn.commit()

    with pytest.raises(launch_batch.DependencyIncomplete):
        launch_batch.resolve_inputs_from_stage(
            conn, run_id=run_id, unit_id="no-such-unit", upstream_stage="admit")


# ======================================================================
# reconcile
# ======================================================================

def _submit_one(conn, fake_batch, *, run_id, unit_id="reconcile-001/SCA07"):
    return launch_batch.submit_unit(
        conn, run_id=run_id, stage="admit", unit_kind="detector-image",
        unit_id=unit_id, inputs_location="s3://in-bucket/in-prefix",
        client=fake_batch)


def test_reconcile_succeeded_with_valid_manifest(conn, batch_env):
    run_id = _make_run(conn, kind="scratch", max_attempts=2)
    conn.commit()

    fake_batch = FakeBatch()
    submission = _submit_one(conn, fake_batch, run_id=run_id)
    conn.commit()

    fake_s3 = FakeS3()
    manifest = {
        "schema_version": "1", "run": run_id,
        "unit": {"kind": "detector-image", "id": "reconcile-001/SCA07"},
        "stage": "admit", "attempt": submission.attempt_id,
        "execution_record": f"exec/{submission.attempt_id}.json",
        "inputs": {"manifest": "x", "products": {}, "result_sets": []},
        "outputs": [],
    }
    bucket = "test-bucket"
    prefix = submission.output_location[len(f"s3://{bucket}/"):]
    fake_s3.seed(bucket, f"{prefix}/manifest.json", json.dumps(manifest).encode())

    fake_batch.set_status(submission.job_id, "SUCCEEDED")

    results = launch_batch.reconcile(
        conn, run_id=run_id, client=fake_batch, s3_client=fake_s3)
    conn.commit()

    assert len(results) == 1
    result = results[0]
    assert result.disposition == "succeeded"
    assert result.selected is True

    with conn.cursor() as cur:
        cur.execute(
            "SELECT disposition, exit_code FROM attempts WHERE id = %s",
            (submission.attempt_id,))
        disposition, exit_code = cur.fetchone()
        assert disposition == "succeeded"
        assert exit_code == 0

        cur.execute(
            "SELECT selected_attempt FROM units WHERE run = %s AND stage = 'admit' "
            "AND unit_id = %s", (run_id, "reconcile-001/SCA07"))
        (selected_attempt,) = cur.fetchone()
        assert selected_attempt == submission.attempt_id


def test_reconcile_succeeded_without_manifest_is_failed(conn, batch_env):
    run_id = _make_run(conn, kind="scratch", max_attempts=2)
    conn.commit()

    fake_batch = FakeBatch()
    submission = _submit_one(conn, fake_batch, run_id=run_id, unit_id="reconcile-002/SCA07")
    conn.commit()

    fake_batch.set_status(submission.job_id, "SUCCEEDED")

    results = launch_batch.reconcile(
        conn, run_id=run_id, client=fake_batch, s3_client=FakeS3())
    conn.commit()

    assert results[0].disposition == "failed"
    assert results[0].selected is False

    with conn.cursor() as cur:
        cur.execute(
            "SELECT disposition, exit_code FROM attempts WHERE id = %s",
            (submission.attempt_id,))
        disposition, exit_code = cur.fetchone()
        assert disposition == "failed"
        assert exit_code == 0


def test_reconcile_failed_exit_75_is_transient(conn, batch_env):
    run_id = _make_run(conn, kind="scratch", max_attempts=2)
    conn.commit()

    fake_batch = FakeBatch()
    submission = _submit_one(conn, fake_batch, run_id=run_id, unit_id="reconcile-003/SCA07")
    conn.commit()

    fake_batch.set_status(submission.job_id, "FAILED", container_exit_code=75)

    results = launch_batch.reconcile(
        conn, run_id=run_id, client=fake_batch, s3_client=FakeS3())
    conn.commit()

    assert results[0].disposition == "transient"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT disposition, exit_code FROM attempts WHERE id = %s",
            (submission.attempt_id,))
        disposition, exit_code = cur.fetchone()
        assert disposition == "transient"
        assert exit_code == 75

        # A retryable failure returns the unit to 'ready' while attempts remain.
        cur.execute(
            "SELECT state FROM units WHERE run = %s AND stage = 'admit' "
            "AND unit_id = %s", (run_id, "reconcile-003/SCA07"))
        (state,) = cur.fetchone()
        assert state == "ready"


def test_reconcile_failed_no_exit_code_is_killed(conn, batch_env):
    run_id = _make_run(conn, kind="scratch", max_attempts=2)
    conn.commit()

    fake_batch = FakeBatch()
    submission = _submit_one(conn, fake_batch, run_id=run_id, unit_id="reconcile-004/SCA07")
    conn.commit()

    fake_batch.set_status(submission.job_id, "FAILED", status_reason="Task failed")

    results = launch_batch.reconcile(
        conn, run_id=run_id, client=fake_batch, s3_client=FakeS3())
    conn.commit()

    assert results[0].disposition == "killed"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT disposition, exit_code FROM attempts WHERE id = %s",
            (submission.attempt_id,))
        disposition, exit_code = cur.fetchone()
        assert disposition == "killed"
        assert exit_code is None


def test_reconcile_missing_job_id_is_lost(conn, batch_env):
    run_id = _make_run(conn, kind="scratch", max_attempts=2)
    conn.commit()

    fake_batch = FakeBatch()
    submission = _submit_one(conn, fake_batch, run_id=run_id, unit_id="reconcile-005/SCA07")
    conn.commit()

    fake_batch.forget(submission.job_id)

    results = launch_batch.reconcile(
        conn, run_id=run_id, client=fake_batch, s3_client=FakeS3())
    conn.commit()

    assert results[0].disposition == "lost"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT disposition FROM attempts WHERE id = %s", (submission.attempt_id,))
        (disposition,) = cur.fetchone()
        assert disposition == "lost"


def test_reconcile_running_job_left_untouched(conn, batch_env):
    run_id = _make_run(conn, kind="scratch", max_attempts=2)
    conn.commit()

    fake_batch = FakeBatch()
    submission = _submit_one(conn, fake_batch, run_id=run_id, unit_id="reconcile-006/SCA07")
    conn.commit()

    fake_batch.set_status(submission.job_id, "RUNNING")

    results = launch_batch.reconcile(
        conn, run_id=run_id, client=fake_batch, s3_client=FakeS3())
    conn.commit()

    assert results[0].disposition is None

    with conn.cursor() as cur:
        cur.execute(
            "SELECT disposition FROM attempts WHERE id = %s", (submission.attempt_id,))
        (disposition,) = cur.fetchone()
        assert disposition is None


# ======================================================================
# cancel
# ======================================================================

def test_cancel_calls_terminate_job_and_records_nothing(conn, batch_env):
    run_id = _make_run(conn, kind="scratch", max_attempts=2)
    conn.commit()

    fake_batch = FakeBatch()
    submission = _submit_one(conn, fake_batch, run_id=run_id, unit_id="cancel-001/SCA07")
    conn.commit()

    launch_batch.cancel(
        conn, attempt_id=submission.attempt_id, reason="operator cancel", client=fake_batch)

    assert fake_batch.terminated == [
        {"jobId": submission.job_id, "reason": "operator cancel"}]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT disposition FROM attempts WHERE id = %s", (submission.attempt_id,))
        (disposition,) = cur.fetchone()
        assert disposition is None
