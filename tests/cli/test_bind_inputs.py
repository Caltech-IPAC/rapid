"""Behavioural tests of input binding at submission (supervisor step 9,
2026-09-25, R4) and of the deletion guard ignoring a deleted consumer's
tombstones (R3): argv in, exit code / stderr and ``unit_inputs`` rows out,
against a real PostgreSQL with Batch and S3 faked.

Every test here uses the launcher's real input-manifest read
(``real_input_manifest``; the suite-wide stub in ``tests/conftest.py`` is
off), so each ``--inputs`` location is seeded with a manifest in FakeS3
first. Product instances are registered directly, as
``test_run_lifecycle.py``'s module docstring explains.
"""

from __future__ import annotations

import json

import pytest

from rapidpipe.db.ids import new_ulid

from .conftest import FAKE_BUCKET
from .test_run_lifecycle import (
    _create_run,
    _kv,
    _register_candidate,
    _submit_and_complete,
)

pytestmark = pytest.mark.real_input_manifest

#: ``test_run_lifecycle._submit``'s default ``--inputs``.
DELIVERY = ("in-bucket", "in-prefix")


def _manifest(*, instances=(), result_sets=(), stage="input-set", unit="U"):
    outputs = [
        {"kind": "test-product", "format_version": "1", "instance": instance,
         "key": {"unit": unit}, "primary": f"l2/{instance}.fits",
         "members": [{"role": "image", "path": f"l2/{instance}.fits", "bytes": 1,
                      "sha256": "sha256:" + "0" * 64}]}
        for instance in instances]
    return {
        "schema_version": "1", "run": "R", "unit": {"kind": "detector-image", "id": unit},
        "stage": stage, "attempt": new_ulid(), "execution_record": "exec/x.json",
        "inputs": {"manifest": "x", "products": {}, "result_sets": list(result_sets)},
        "outputs": outputs,
    }


def _seed(fake_s3, bucket, prefix, manifest) -> str:
    fake_s3.seed(bucket, f"{prefix}/manifest.json", json.dumps(manifest).encode())
    return f"s3://{bucket}/{prefix}"


def _seed_delivery(fake_s3):
    """A delivery manifest (admit's input) at ``test_run_lifecycle``'s
    default ``--inputs``: one output entry whose instance nothing
    registered."""
    return _seed(fake_s3, *DELIVERY, _manifest(instances=[new_ulid()], stage="delivery"))


def _bound(db, run_id, unit_id):
    with db.cursor() as cur:
        cur.execute(
            "SELECT ui.producer_instance FROM unit_inputs ui JOIN units u ON u.id = ui.unit "
            "WHERE u.run = %s AND u.unit_id = %s ORDER BY 1", (run_id, unit_id))
        return [row[0] for row in cur.fetchall()]


def _producer(cli, db, fake_batch, fake_s3, *, kind="scratch"):
    """A run holding one registered instance; returns ``(run, instance)``."""
    _seed_delivery(fake_s3)
    run_id = _create_run(cli, db, kind=kind, stages="admit")
    attempt_id, _job, _out = _submit_and_complete(
        cli, fake_batch, fake_s3, run_id, unit_id=f"p-{new_ulid()}")
    instance, _key = _register_candidate(db, run_id, "admit", attempt_id)
    return run_id, instance


def _consumer_inputs(fake_s3, instance, *, unregistered):
    return _seed(fake_s3, FAKE_BUCKET, f"inputs/{new_ulid()}",
                 _manifest(instances=[instance, unregistered], result_sets=[new_ulid()]))


def test_a_difference_unit_binds_its_registered_inputs_at_submission(
        cli, db, fake_batch, fake_s3, batch_env):
    _producer_run, instance = _producer(cli, db, fake_batch, fake_s3)
    inputs = _consumer_inputs(fake_s3, instance, unregistered=new_ulid())
    consumer = _create_run(cli, db, stages="difference")

    result = cli("run", "submit", consumer, "difference", "--unit", "U", "--inputs", inputs)
    assert result.rc == 0, result.err
    # Only the registered instance binds; the unregistered output and
    # result-set names bind nothing.
    assert _bound(db, consumer, "U") == [instance]


def test_an_admit_unit_binds_nothing_from_a_delivery_manifest(
        cli, db, fake_batch, fake_s3, batch_env):
    inputs = _seed_delivery(fake_s3)
    run_id = _create_run(cli, db, stages="admit")
    result = cli("run", "submit", run_id, "admit", "--unit", "U", "--inputs", inputs)
    assert result.rc == 0, result.err
    assert _bound(db, run_id, "U") == []


def test_a_missing_input_manifest_refuses_the_submission_with_65(
        cli, db, fake_batch, fake_s3, batch_env):
    run_id = _create_run(cli, db, stages="difference")
    result = cli("run", "submit", run_id, "difference", "--unit", "U",
                 "--inputs", f"s3://{FAKE_BUCKET}/inputs/{new_ulid()}")
    assert result.rc == 65, result.err
    assert "input manifest" in result.err and "refusing to submit" in result.err
    assert fake_batch.submitted == []
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM units WHERE run = %s", (run_id,))
        assert cur.fetchone() == (0,)
        cur.execute("SELECT count(*) FROM attempts WHERE run = %s", (run_id,))
        assert cur.fetchone() == (0,)


def test_a_missing_input_manifest_refuses_run_start_with_65(
        cli, db, fake_batch, fake_s3, batch_env):
    run_id = _create_run(cli, db, stages="difference")
    result = cli("run", "start", run_id, "--unit", "U",
                 "--inputs", f"s3://{FAKE_BUCKET}/inputs/{new_ulid()}", "--no-wait")
    assert result.rc == 65, result.err
    assert fake_batch.submitted == []


def test_a_retry_rebinds_nothing_new(cli, db, fake_batch, fake_s3, batch_env):
    _producer_run, instance = _producer(cli, db, fake_batch, fake_s3)
    inputs = _consumer_inputs(fake_s3, instance, unregistered=new_ulid())
    consumer = _create_run(cli, db, stages="difference", extra=("--max-attempts", "3"))

    first = cli("run", "submit", consumer, "difference", "--unit", "U", "--inputs", inputs)
    assert first.rc == 0, first.err
    fake_batch.set_status(_kv(first.out, "job"), "FAILED", container_exit_code=75)
    assert cli("run", "reconcile", consumer).rc == 0

    second = cli("run", "submit", consumer, "difference", "--unit", "U", "--inputs", inputs)
    assert second.rc == 0, second.err
    assert _kv(second.out, "attempt") != _kv(first.out, "attempt")
    assert _bound(db, consumer, "U") == [instance]


def test_a_live_consumer_blocks_its_producers_deletion_until_it_is_deleted(
        cli, db, fake_batch, fake_s3, fake_versioned_s3, batch_env):
    producer, instance = _producer(cli, db, fake_batch, fake_s3)
    inputs = _consumer_inputs(fake_s3, instance, unregistered=new_ulid())
    consumer = _create_run(cli, db, stages="difference")
    submitted = cli("run", "submit", consumer, "difference", "--unit", "U", "--inputs", inputs)
    assert submitted.rc == 0, submitted.err

    # R4 + R3: bound at submission, so the producer is guarded from then on,
    # before the consumer registers any output.
    refused = cli("run", "delete", producer)
    assert refused.rc == 64, refused.out + refused.err
    assert "unit_inputs" in refused.err
    with db.cursor() as cur:
        cur.execute("SELECT state FROM runs WHERE id = %s", (producer,))
        assert cur.fetchone()[0] != "deleting"

    fake_batch.set_status(_kv(submitted.out, "job"), "FAILED", container_exit_code=70)
    assert cli("run", "reconcile", consumer).rc == 0
    deleted_consumer = cli("run", "delete", consumer)
    assert deleted_consumer.rc == 0, deleted_consumer.err

    # The consumer's binding stays as history and no longer blocks.
    assert _bound(db, consumer, "U") == [instance]
    deleted_producer = cli("run", "delete", producer)
    assert deleted_producer.rc == 0, deleted_producer.err
    with db.cursor() as cur:
        cur.execute("SELECT state FROM runs WHERE id = %s", (producer,))
        assert cur.fetchone() == ("deleted",)


def test_a_seeded_only_failed_rerun_reads_the_seeds_recorded_inputs_and_binds_the_new_unit(
        cli, db, fake_batch, fake_s3, batch_env):
    producer_run, instance = _producer(cli, db, fake_batch, fake_s3)
    seed = _create_run(cli, db, kind="production", stages="difference")
    # The input set lives under the seed's own prefix, and names a second
    # instance that is not registered yet when the seed submits.
    late = new_ulid()
    inputs = _seed(fake_s3, FAKE_BUCKET, f"scratch/runs/{seed}/inputs/difference/U",
                   _manifest(instances=[instance, late]))
    failed = cli("run", "submit", seed, "difference", "--unit", "U", "--inputs", inputs)
    assert failed.rc == 0, failed.err
    assert _bound(db, seed, "U") == [instance]
    fake_batch.set_status(_kv(failed.out, "job"), "FAILED", container_exit_code=70)
    assert cli("run", "reconcile", seed).rc == 0

    # Registered after the seed's binding, before the re-run.
    with db.cursor() as cur:
        cur.execute("SELECT selected_attempt FROM units WHERE run = %s AND stage = 'admit'",
                    (producer_run,))
        producer_attempt = cur.fetchone()[0]
    from tests.db.test_repository import TEST_KIND, _register_simple_instance
    _register_simple_instance(db.connection, producer_run, "admit", producer_attempt,
                              instance_id=late, kind=TEST_KIND,
                              logical_key={"k": new_ulid()})

    created = cli("run", "create", "--seed", seed, "--only-failed")
    assert created.rc == 0, created.err
    rerun = created.out.strip()
    db.track_run(rerun)

    # No --inputs: run start resolves the seed attempt's recorded location
    # (under the seed's prefix), which the launcher reads for real, and
    # binds to the NEW run's unit -- including the instance the seed's
    # own binding (copied at seeding) never had.
    started = cli("run", "start", rerun, "--unit", "U", "--no-wait")
    assert started.rc == 0, started.err
    command = fake_batch.submitted[-1]["containerOverrides"]["command"]
    assert command[command.index("--inputs") + 1] == inputs
    assert _bound(db, rerun, "U") == sorted([instance, late])
    assert _bound(db, seed, "U") == [instance]


# ----------------------------------------------------------------------
# Codex amendment to R4: binding fences the producer's run.
# ----------------------------------------------------------------------

def test_binding_an_input_of_a_deleted_producer_is_refused_with_65(
        cli, db, fake_batch, fake_s3, fake_versioned_s3, batch_env):
    producer, instance = _producer(cli, db, fake_batch, fake_s3)
    assert cli("run", "delete", producer).rc == 0
    inputs = _consumer_inputs(fake_s3, instance, unregistered=new_ulid())
    consumer = _create_run(cli, db, stages="difference")
    submitted_before = len(fake_batch.submitted)

    result = cli("run", "submit", consumer, "difference", "--unit", "U", "--inputs", inputs)
    assert result.rc == 65, result.err
    assert f"run {producer!r}, which is 'deleted'" in result.err
    assert len(fake_batch.submitted) == submitted_before
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM attempts WHERE run = %s", (consumer,))
        assert cur.fetchone() == (0,)
    assert _bound(db, consumer, "U") == []


def test_an_uncommitted_binding_holds_the_producer_run_against_deletion(
        cli, db, fake_batch, fake_s3, batch_env):
    import psycopg2
    import psycopg2.errors

    from rapidpipe.runs import repository

    from .conftest import _pg_params

    producer, instance = _producer(cli, db, fake_batch, fake_s3)
    consumer = _create_run(cli, db, stages="difference")

    binder = psycopg2.connect(**_pg_params())
    try:
        repository.add_unit(binder, consumer, "difference", "detector-image", "U")
        repository.bind_unit_inputs(binder, consumer, "difference", "U", [instance])
        # mark_run_deleting takes the producer's run row FOR UPDATE; while
        # the binding is uncommitted, that lock cannot be had.
        with db.cursor() as cur:
            cur.execute("SET lock_timeout = '200ms'")
            with pytest.raises(psycopg2.errors.LockNotAvailable):
                cur.execute("SELECT 1 FROM runs WHERE id = %s FOR UPDATE", (producer,))
            cur.execute("SET lock_timeout = 0")
    finally:
        binder.rollback()
        binder.close()
