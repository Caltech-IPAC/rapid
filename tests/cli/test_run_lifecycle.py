"""Behavioural, black-box tests of the ``rapidpipe run`` subcommands: argv
in, exit code / stdout / stderr and database state out.

Every run/unit/attempt is created through the CLI itself
(``rapidpipe run create``/``submit``/``reconcile``/...), against a real
PostgreSQL with the run-model migration applied, with Batch and S3 faked
(``tests/unit/fakebatch.py``, ``tests/unit/fakes3.py``) through the seams
``tests/cli/conftest.py`` monkeypatches.

One exception: registering a product instance. ``rapidpipe.launch.batch.
reconcile`` only marks a Batch-run attempt succeeded and selects it --
registering the instance(s) its manifest describes is the real stage
process's own job (every ``rapidpipe.stages.*`` module calls
``repository.register_manifest`` itself, writing directly to the database
before it publishes its manifest to S3). No stage process ever runs in
this suite (that needs the pipeline image or real science fixtures, out
of scope for a CLI run-lifecycle suite), so ``_register_candidate`` below
calls ``repository.register_manifest`` directly, the same way
``tests/db/test_repository.py``'s ``_register_simple_instance`` and
``tests/db/test_run_lifecycle.py``'s ``_candidate`` do, using
``TEST_KIND`` -- a product kind with no ``dev`` table, so no science rows
are needed either (see that module's own comment on ``TEST_KIND``).

Each test creates its own runs and product instances and tracks them on
the ``db`` fixture (``db.track_run``/``db.track_promotion``), which
deletes them at teardown in FK order -- no test depends on another's
rows, and none are order-dependent.
"""

from __future__ import annotations

import json

import pytest

from rapidpipe.db.ids import new_ulid
from tests.db.test_repository import TEST_KIND, _register_simple_instance

from .conftest import FAKE_BUCKET


# ======================================================================
# Small helpers shared by every test below.
# ======================================================================

def _kv(line: str, key: str) -> str:
    """The value of ``key=...`` in a line of ``key=value key2=value2``
    output, as ``run submit``/``run reconcile`` print."""
    for token in line.split():
        if token.startswith(f"{key}="):
            return token[len(key) + 1:]
    raise AssertionError(f"{key}= not found in {line!r}")


def _create_run(cli, db, *, kind="scratch", purpose="cli-lifecycle",
                 stages="admit", owner=None, extra=()):
    args = ["run", "create", "--kind", kind, "--purpose", purpose, "--stages", stages]
    if owner is not None:
        args += ["--owner", owner]
    args = args + list(extra)
    result = cli(*args)
    assert result.rc == 0, result.err
    run_id = result.out.strip()
    assert run_id
    db.track_run(run_id)
    return run_id


def _submit(cli, run_id, stage, unit_id, *, inputs="s3://in-bucket/in-prefix"):
    return cli("run", "submit", run_id, stage, "--unit", unit_id, "--inputs", inputs)


def _seed_manifest(fake_s3, output_location, *, run_id, stage, unit_id, attempt_id):
    bucket = FAKE_BUCKET
    assert output_location.startswith(f"s3://{bucket}/")
    prefix = output_location[len(f"s3://{bucket}/"):]
    manifest = {
        "schema_version": "1", "run": run_id,
        "unit": {"kind": "detector-image", "id": unit_id},
        "stage": stage, "attempt": attempt_id,
        "execution_record": f"exec/{attempt_id}.json",
        "inputs": {"manifest": "x", "products": {}, "result_sets": []},
        "outputs": [],
    }
    fake_s3.seed(bucket, f"{prefix}/manifest.json", json.dumps(manifest).encode())
    return bucket, prefix


def _submit_and_complete(cli, fake_batch, fake_s3, run_id, *, unit_id, stage="admit"):
    """``run submit`` then ``run reconcile`` with the job SUCCEEDED and a
    valid manifest seeded: the unit ends up complete, with this attempt
    selected. Returns ``(attempt_id, job_id, output_location)``."""
    submitted = _submit(cli, run_id, stage, unit_id)
    assert submitted.rc == 0, submitted.err
    attempt_id = _kv(submitted.out, "attempt")
    job_id = _kv(submitted.out, "job")
    output_location = _kv(submitted.out, "outputs")

    fake_batch.set_status(job_id, "SUCCEEDED")
    _seed_manifest(fake_s3, output_location, run_id=run_id, stage=stage,
                    unit_id=unit_id, attempt_id=attempt_id)

    reconciled = cli("run", "reconcile", run_id)
    assert reconciled.rc == 0, reconciled.err
    assert f"attempt={attempt_id}" in reconciled.out
    assert "disposition=succeeded" in reconciled.out
    assert "selected=True" in reconciled.out
    return attempt_id, job_id, output_location


def _register_candidate(db, run_id, stage, attempt_id, *, key=None):
    key = key if key is not None else {"k": new_ulid()}
    instance_id = _register_simple_instance(
        db.connection, run_id, stage, attempt_id, kind=TEST_KIND, logical_key=key)
    return instance_id, key


def _counts(db, run_id):
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM units WHERE run = %s", (run_id,))
        units = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM attempts WHERE run = %s", (run_id,))
        attempts = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM product_instances WHERE run = %s", (run_id,))
        instances = cur.fetchone()[0]
    return units, attempts, instances


# ======================================================================
# run create / list / show
# ======================================================================

def test_run_create_prints_id_and_records_purpose_stages_owner(cli, db):
    result = cli("run", "create", "--kind", "scratch", "--purpose", "cli create test",
                 "--stages", "admit, register", "--owner", "cli-tester")
    assert result.rc == 0, result.err
    run_id = result.out.strip()
    db.track_run(run_id)
    assert run_id

    with db.cursor() as cur:
        cur.execute(
            "SELECT purpose, selected_stages, owner, kind FROM runs WHERE id = %s",
            (run_id,))
        purpose, stages, owner, kind = cur.fetchone()
    assert purpose == "cli create test"
    assert stages == ["admit", "register"]
    assert owner == "cli-tester"
    assert kind == "scratch"


def test_run_list_filters_by_kind_owner_and_state(cli, db):
    scratch_run = _create_run(cli, db, kind="scratch", owner="cli-list-alice", purpose="p-a")
    production_run = _create_run(cli, db, kind="production", owner="cli-list-bob", purpose="p-b")

    by_kind = cli("run", "list", "--kind", "production")
    assert by_kind.rc == 0
    ids = {line.split("\t")[0] for line in by_kind.out.splitlines()}
    assert production_run in ids
    assert scratch_run not in ids

    by_owner = cli("run", "list", "--owner", "cli-list-alice")
    ids = {line.split("\t")[0] for line in by_owner.out.splitlines()}
    assert scratch_run in ids
    assert production_run not in ids

    by_state = cli("run", "list", "--state", "open")
    ids = {line.split("\t")[0] for line in by_state.out.splitlines()}
    assert {scratch_run, production_run} <= ids


def test_run_show_prints_the_run_and_its_units(cli, db, fake_batch, fake_s3, batch_env):
    run_id = _create_run(cli, db, kind="scratch", purpose="cli show test")
    submitted = _submit(cli, run_id, "admit", "cli-show-001/SCA07")
    assert submitted.rc == 0, submitted.err

    result = cli("run", "show", run_id)
    assert result.rc == 0
    assert f"id: {run_id}" in result.out
    assert "purpose: cli show test" in result.out
    assert "units:" in result.out
    assert "cli-show-001/SCA07" in result.out
    assert "attempts:" in result.out


def test_run_show_refuses_an_unknown_run(cli):
    result = cli("run", "show", new_ulid())
    assert result.rc == 1
    assert "no such run" in result.err


# ======================================================================
# run submit: env gating, attempt/scheduler-job recording
# ======================================================================

def test_run_submit_production_requires_its_own_environment(
        cli, db, fake_batch, fake_s3, monkeypatch):
    run_id = _create_run(cli, db, kind="production")
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "test-queue")
    monkeypatch.delenv("RAPIDPIPE_BATCH_JOB_DEFINITION_PRODUCTION", raising=False)
    monkeypatch.delenv("RAPIDPIPE_OUTPUTS_ROOT_PRODUCTION", raising=False)
    # The scratch-only fallbacks are deliberately absent too, so a
    # production run cannot silently borrow them (README, "Running on
    # Batch": "A production run never falls back to an unsuffixed
    # variable").
    monkeypatch.delenv("RAPIDPIPE_BATCH_JOB_DEFINITION", raising=False)
    monkeypatch.delenv("RAPIDPIPE_OUTPUTS_ROOT", raising=False)

    refused = _submit(cli, run_id, "admit", "cli-prod-env-001/SCA07")
    assert refused.rc == 64
    assert "RAPIDPIPE_" in refused.err

    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_DEFINITION_PRODUCTION", "test-def-production")
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT_PRODUCTION", f"s3://{FAKE_BUCKET}/production")
    ok = _submit(cli, run_id, "admit", "cli-prod-env-001/SCA07")
    assert ok.rc == 0, ok.err
    assert "attempt=" in ok.out and "job=" in ok.out and "outputs=" in ok.out


def test_run_submit_records_attempt_and_scheduler_job(cli, db, fake_batch, fake_s3, batch_env):
    run_id = _create_run(cli, db, kind="scratch")
    result = _submit(cli, run_id, "admit", "cli-submit-001/SCA07")
    assert result.rc == 0, result.err

    attempt_id = _kv(result.out, "attempt")
    job_id = _kv(result.out, "job")
    output_location = _kv(result.out, "outputs")
    assert output_location == (
        f"s3://{FAKE_BUCKET}/scratch/runs/{run_id}/admit/"
        f"cli-submit-001/SCA07/{attempt_id}")

    with db.cursor() as cur:
        cur.execute(
            "SELECT output_location, scheduler_job_id, disposition "
            "FROM attempts WHERE id = %s", (attempt_id,))
        location, scheduler_job_id, disposition = cur.fetchone()
    assert location == output_location
    assert scheduler_job_id == job_id
    assert disposition is None
    assert fake_batch.submitted, "submit_job was never called on the fake"


# ======================================================================
# run reconcile: completion, selection; registering a candidate (see
# module docstring for why registration itself is a direct repository
# call rather than a CLI one).
# ======================================================================

def test_run_reconcile_completes_and_selects_the_unit(cli, db, fake_batch, fake_s3, batch_env):
    run_id = _create_run(cli, db, kind="production")
    attempt_id, _job_id, _location = _submit_and_complete(
        cli, fake_batch, fake_s3, run_id, unit_id="cli-reconcile-001/SCA07")

    with db.cursor() as cur:
        cur.execute(
            "SELECT state, selected_attempt FROM units "
            "WHERE run = %s AND stage = 'admit' AND unit_id = %s",
            (run_id, "cli-reconcile-001/SCA07"))
        state, selected_attempt = cur.fetchone()
    assert state == "complete"
    assert selected_attempt == attempt_id

    instance_id, _key = _register_candidate(db, run_id, "admit", attempt_id)
    with db.cursor() as cur:
        cur.execute("SELECT custody, run FROM product_instances WHERE id = %s", (instance_id,))
        custody, instance_run = cur.fetchone()
    assert instance_run == run_id
    assert custody == "candidate"  # a production run's instances start candidate


def test_registered_instance_custody_is_scratch_for_a_scratch_run(
        cli, db, fake_batch, fake_s3, batch_env):
    run_id = _create_run(cli, db, kind="scratch")
    attempt_id, _job_id, _location = _submit_and_complete(
        cli, fake_batch, fake_s3, run_id, unit_id="cli-custody-001/SCA07")

    instance_id, _key = _register_candidate(db, run_id, "admit", attempt_id)
    with db.cursor() as cur:
        cur.execute("SELECT custody FROM product_instances WHERE id = %s", (instance_id,))
        (custody,) = cur.fetchone()
    assert custody == "scratch"


# ======================================================================
# run finish
# ======================================================================

def test_run_finish_refuses_while_a_unit_is_open_then_succeeds(
        cli, db, fake_batch, fake_s3, batch_env):
    run_id = _create_run(cli, db, kind="scratch")
    _submit_and_complete(cli, fake_batch, fake_s3, run_id, unit_id="cli-finish-001/SCA07")

    second = _submit(cli, run_id, "admit", "cli-finish-002/SCA07")
    assert second.rc == 0, second.err
    second_job_id = _kv(second.out, "job")
    fake_batch.set_status(second_job_id, "RUNNING")
    still_unresolved = cli("run", "reconcile", run_id)
    assert still_unresolved.rc == 0

    refused = cli("run", "finish", run_id)
    assert refused.rc == 64
    assert "not yet" in refused.err

    # The second unit's job now terminates (without a manifest a
    # SUCCEEDED status alone is not enough, so fail it outright).
    fake_batch.set_status(second_job_id, "FAILED", container_exit_code=1)
    cli("run", "reconcile", run_id)

    finished = cli("run", "finish", run_id)
    assert finished.rc == 0, finished.err

    with db.cursor() as cur:
        cur.execute("SELECT state, finished_at FROM runs WHERE id = %s", (run_id,))
        state, finished_at = cur.fetchone()
    assert state == "finished"
    assert finished_at is not None

    again = cli("run", "finish", run_id)
    assert again.rc == 64
    assert "'finished'" in again.err


# ======================================================================
# run promote / run rollback
# ======================================================================

def test_run_promote_refuses_a_scratch_run(cli, db):
    run_id = _create_run(cli, db, kind="scratch")
    result = cli("run", "promote", run_id, "--reason", "no")
    assert result.rc == 64
    assert "scratch never leaves scratch" in result.err


def test_run_promote_a_production_candidate_and_rollback(cli, db, fake_batch, fake_s3, batch_env):
    run_id = _create_run(cli, db, kind="production")
    attempt_id, _job_id, _location = _submit_and_complete(
        cli, fake_batch, fake_s3, run_id, unit_id="cli-promote-001/SCA07")
    instance_id, key = _register_candidate(db, run_id, "admit", attempt_id)

    promoted = cli(
        "run", "promote", run_id, "--reason", "cli promote test",
        "--who", "cli-promoter", "--allow-unreleased")
    assert promoted.rc == 0, promoted.err
    promotion_id = promoted.out.strip()
    assert promotion_id
    db.track_promotion(promotion_id)

    with db.cursor() as cur:
        cur.execute("SELECT who, reason FROM promotions WHERE id = %s", (promotion_id,))
        who, reason = cur.fetchone()
        assert (who, reason) == ("cli-promoter", "cli promote test")

        cur.execute(
            "SELECT kind, logical_key, before_instance, after_instance "
            "FROM promotion_changes WHERE promotion = %s", (promotion_id,))
        (kind, logical_key, before_instance, after_instance) = cur.fetchone()
        assert (kind, before_instance, after_instance) == (TEST_KIND, None, instance_id)
        assert logical_key == key

        cur.execute("SELECT custody FROM product_instances WHERE id = %s", (instance_id,))
        assert cur.fetchone()[0] == "current"

    rolled_back = cli("run", "rollback", promotion_id, "--reason", "cli rollback test")
    assert rolled_back.rc == 0, rolled_back.err
    rollback_id = rolled_back.out.strip()
    assert rollback_id
    db.track_promotion(rollback_id)

    with db.cursor() as cur:
        cur.execute("SELECT custody FROM product_instances WHERE id = %s", (instance_id,))
        assert cur.fetchone()[0] == "candidate"
        cur.execute(
            "SELECT count(*) FROM current_selection WHERE kind = %s AND logical_key = %s",
            (TEST_KIND, json.dumps(key)))
        assert cur.fetchone()[0] == 0


# ======================================================================
# run delete
# ======================================================================

def test_run_delete_refuses_a_production_run(cli, db):
    run_id = _create_run(cli, db, kind="production")
    result = cli("run", "delete", run_id)
    assert result.rc == 64


def test_run_delete_a_scratch_run_removes_its_objects_and_rows_only(
        cli, db, fake_batch, fake_s3, fake_versioned_s3, batch_env):
    run_id = _create_run(cli, db, kind="scratch")
    _attempt_id, _job_id, output_location = _submit_and_complete(
        cli, fake_batch, fake_s3, run_id, unit_id="cli-delete-001/SCA07")

    other_run = _create_run(cli, db, kind="scratch")
    _other_attempt, _other_job, other_location = _submit_and_complete(
        cli, fake_batch, fake_s3, other_run, unit_id="cli-delete-other-001/SCA07")

    bucket = FAKE_BUCKET
    prefix = output_location[len(f"s3://{bucket}/"):]
    other_prefix = other_location[len(f"s3://{bucket}/"):]
    fake_versioned_s3.seed(bucket, f"{prefix}/manifest.json", versions=2)
    fake_versioned_s3.seed(bucket, f"{other_prefix}/manifest.json", versions=1)

    before = _counts(db, run_id)
    other_before = _counts(db, other_run)

    result = cli("run", "delete", run_id)
    assert result.rc == 0, result.err
    assert f"run_id: {run_id}" in result.out
    assert "already_deleted: False" in result.out
    assert "objects_deleted: 1" in result.out
    assert "versions_deleted: 2" in result.out

    assert fake_versioned_s3.remaining(bucket, prefix + "/") == []
    assert len(fake_versioned_s3.remaining(bucket, other_prefix + "/")) == 1

    with db.cursor() as cur:
        cur.execute("SELECT state, deleted_at IS NOT NULL FROM runs WHERE id = %s", (run_id,))
        assert cur.fetchone() == ("deleted", True)
        cur.execute("SELECT state FROM runs WHERE id = %s", (other_run,))
        assert cur.fetchone()[0] == "open"

    # Tombstones: the run's own units/attempts rows are not purged, only
    # marked (the deleted run's state and its instances' deletion_state).
    after = _counts(db, run_id)
    assert after[:2] == before[:2]
    assert _counts(db, other_run) == other_before

    again = cli("run", "delete", run_id)
    assert again.rc == 0
    assert "already_deleted: True" in again.out


# ======================================================================
# run pin / unpin
# ======================================================================

def test_run_pin_and_unpin_flip_the_flag(cli, db):
    run_id = _create_run(cli, db, kind="scratch")

    pinned = cli("run", "pin", run_id)
    assert pinned.rc == 0
    with db.cursor() as cur:
        cur.execute("SELECT pinned FROM runs WHERE id = %s", (run_id,))
        assert cur.fetchone()[0] is True

    unpinned = cli("run", "unpin", run_id)
    assert unpinned.rc == 0
    with db.cursor() as cur:
        cur.execute("SELECT pinned FROM runs WHERE id = %s", (run_id,))
        assert cur.fetchone()[0] is False


# ======================================================================
# run cancel
# ======================================================================

def test_run_cancel_calls_terminate_job(cli, db, fake_batch, fake_s3, batch_env):
    run_id = _create_run(cli, db, kind="scratch")
    submitted = _submit(cli, run_id, "admit", "cli-cancel-001/SCA07")
    assert submitted.rc == 0, submitted.err
    attempt_id = _kv(submitted.out, "attempt")
    job_id = _kv(submitted.out, "job")

    result = cli("run", "cancel", attempt_id, "--reason", "operator cancel")
    assert result.rc == 0, result.err
    assert fake_batch.terminated == [{"jobId": job_id, "reason": "operator cancel"}]

    # cancel() records nothing itself -- Batch reports the termination
    # asynchronously, and only the next reconcile writes a disposition
    # (rapidpipe.launch.batch.cancel's own docstring; matches
    # tests/db/test_launch.py::test_cancel_calls_terminate_job_and_records_nothing).
    with db.cursor() as cur:
        cur.execute("SELECT disposition FROM attempts WHERE id = %s", (attempt_id,))
        assert cur.fetchone()[0] is None
