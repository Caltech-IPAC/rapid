"""Database-backed tests for rapidpipe.runs.repository against a real
PostgreSQL with the run-model migration applied.

Skips cleanly if PGHOST is unset (see conftest.py). Run locally against a
throwaway database with database/apply-migrations.sh already applied, or
let CI (.github/workflows/db-migrations.yml) run it after the applier.
"""

from __future__ import annotations

import pytest

from rapidpipe.db.ids import new_ulid
from rapidpipe.runs import repository as repo


# ======================================================================
# Helpers: build up a run/unit/attempt/instance chain the tests need.
# ======================================================================

def _make_run(conn, kind="production", owner="brusholme", max_attempts=2, **overrides):
    kwargs = dict(
        kind=kind,
        owner=owner,
        purpose="test",
        selected_stages=["difference"],
        code_revision="abc123",
        image_digest="sha256:deadbeef",
        schema_version="1",
        settings_overlay_ref=None,
        input_selection_ref=None,
        lane="default",
        resource_profile="default",
        database_target="rapid",
        max_attempts_per_unit=max_attempts,
        auto_promote=False,
        check_policy_ref=None,
    )
    kwargs.update(overrides)
    return repo.create_run(conn, **kwargs)


def _make_unit(conn, run_id, stage="difference", unit_id="e001/SCA01"):
    repo.add_unit(conn, run_id, stage, "detector-image", unit_id)
    return stage, unit_id


def _succeed_and_select(conn, run_id, stage, unit_id):
    """Allocate an attempt, record success, select it. Returns attempt id."""
    attempt_id = repo.allocate_attempt(conn, run_id, stage, unit_id)
    repo.record_attempt_result(
        conn, attempt_id, exit_code=0, disposition="succeeded",
        output_location=f"runs/{run_id}/{stage}/{unit_id}/{attempt_id}",
        execution_record={
            "source_revision": "abc123",
            "schema_version": "1",
            "settings_hash": "sha256:xyz",
        },
        scheduler_job_id="batch-job-1",
    )
    repo.select_attempt(conn, attempt_id)
    return attempt_id


def _register_simple_instance(
    conn, run_id, stage, attempt_id, instance_id=None, kind="difference-image",
    logical_key=None, input_products=None,
):
    instance_id = instance_id or new_ulid()
    logical_key = logical_key if logical_key is not None else {"unit": "e001/SCA01"}
    manifest = {
        "run": run_id,
        "unit": {"kind": "detector-image", "id": "e001/SCA01"},
        "stage": stage,
        "attempt": attempt_id,
        "inputs": {
            "manifest": "s3://inputs/manifest.json",
            "products": input_products or {},
            "result_sets": [],
        },
        "outputs": [
            {
                "kind": kind,
                "format_version": "1",
                "instance": instance_id,
                "key": logical_key,
                "primary": f"diff/{instance_id}.fits",
                "members": [
                    {"role": "difference", "path": f"diff/{instance_id}.fits",
                     "bytes": 100, "sha256": "sha256:" + "0" * 64},
                ],
            },
        ],
    }
    repo.register_manifest(conn, manifest, registering_attempt_id=attempt_id)
    return instance_id


def _full_chain_to_current_candidate(conn, run_id, instance_id=None, logical_key=None, unit_id=None):
    """Run a unit to a registered, selected-attempt candidate instance."""
    stage, unit_id = _make_unit(conn, run_id, unit_id=unit_id or new_ulid())
    attempt_id = _succeed_and_select(conn, run_id, stage, unit_id)
    instance_id = _register_simple_instance(
        conn, run_id, stage, attempt_id, instance_id=instance_id, logical_key=logical_key)
    return stage, unit_id, attempt_id, instance_id


# ======================================================================
# create_run
# ======================================================================

def test_create_run_happy_path(conn):
    run_id = _make_run(conn)
    assert run_id
    with conn.cursor() as cur:
        cur.execute("SELECT kind, owner, state FROM runs WHERE id = %s", (run_id,))
        kind, owner, state = cur.fetchone()
    assert kind == "production"
    assert owner == "brusholme"
    assert state == "open"


def test_create_run_with_seed_run(conn):
    seed = _make_run(conn, kind="scratch")
    run_id = _make_run(conn, seed_run=seed)
    with conn.cursor() as cur:
        cur.execute("SELECT seed_run FROM runs WHERE id = %s", (run_id,))
        (seed_run,) = cur.fetchone()
    assert seed_run == seed


def test_create_run_refuses_unknown_seed_run(conn):
    with pytest.raises(repo.RunNotFound):
        _make_run(conn, seed_run=new_ulid())


# ======================================================================
# add_unit / bind_unit_inputs
# ======================================================================

def test_add_unit_happy_path(conn):
    run_id = _make_run(conn)
    stage, unit_id = _make_unit(conn, run_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM units WHERE run = %s AND stage = %s AND unit_id = %s",
            (run_id, stage, unit_id),
        )
        (state,) = cur.fetchone()
    assert state == "pending"


def test_add_unit_refuses_on_deleting_run(conn):
    run_id = _make_run(conn, kind="scratch")
    repo.mark_run_deleting(conn, run_id, requested_by="brusholme")
    with pytest.raises(repo.RunDeletingOrDeleted):
        repo.add_unit(conn, run_id, "difference", "detector-image", "e001/SCA01")


def test_bind_unit_inputs_happy_path(conn):
    run_id = _make_run(conn)
    stage, unit_id = _make_unit(conn, run_id)
    _, _, _, producer_instance = _full_chain_to_current_candidate(conn, _make_run(conn))
    repo.bind_unit_inputs(conn, run_id, stage, unit_id, [producer_instance])
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM unit_inputs ui
            JOIN units u ON u.id = ui.unit
            WHERE u.run = %s AND u.stage = %s AND u.unit_id = %s
            """,
            (run_id, stage, unit_id),
        )
        (count,) = cur.fetchone()
    assert count == 1


def test_bind_unit_inputs_refuses_on_deleting_run(conn):
    run_id = _make_run(conn, kind="scratch")
    stage, unit_id = _make_unit(conn, run_id)
    repo.mark_run_deleting(conn, run_id, requested_by="brusholme")
    with pytest.raises(repo.RunDeletingOrDeleted):
        repo.bind_unit_inputs(conn, run_id, stage, unit_id, [new_ulid()])


# ======================================================================
# allocate_attempt
# ======================================================================

def test_allocate_attempt_happy_path(conn):
    run_id = _make_run(conn)
    stage, unit_id = _make_unit(conn, run_id)
    attempt_id = repo.allocate_attempt(conn, run_id, stage, unit_id)
    assert attempt_id
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM units WHERE run = %s AND stage = %s AND unit_id = %s",
            (run_id, stage, unit_id),
        )
        (state,) = cur.fetchone()
    assert state == "running"


def test_allocate_attempt_refuses_on_terminal_unit(conn):
    run_id = _make_run(conn)
    stage, unit_id, attempt_id, _ = _full_chain_to_current_candidate(conn, run_id)
    with pytest.raises(repo.UnitTerminal):
        repo.allocate_attempt(conn, run_id, stage, unit_id)


def test_allocate_attempt_refuses_on_deleting_run(conn):
    run_id = _make_run(conn, kind="scratch")
    stage, unit_id = _make_unit(conn, run_id)
    repo.mark_run_deleting(conn, run_id, requested_by="brusholme")
    with pytest.raises(repo.RunDeletingOrDeleted):
        repo.allocate_attempt(conn, run_id, stage, unit_id)


def test_allocate_attempt_refuses_when_allowance_exhausted(conn):
    run_id = _make_run(conn, max_attempts=1)
    stage, unit_id = _make_unit(conn, run_id)
    repo.allocate_attempt(conn, run_id, stage, unit_id)
    with pytest.raises(repo.AttemptAllowanceExhausted):
        repo.allocate_attempt(conn, run_id, stage, unit_id)


# ======================================================================
# record_attempt_result
# ======================================================================

def test_record_attempt_result_happy_path(conn):
    run_id = _make_run(conn)
    stage, unit_id = _make_unit(conn, run_id)
    attempt_id = repo.allocate_attempt(conn, run_id, stage, unit_id)
    repo.record_attempt_result(
        conn, attempt_id, exit_code=0, disposition="succeeded",
        output_location="runs/x", execution_record={
            "source_revision": "abc", "schema_version": "1",
            "settings_hash": "sha256:x"},
        scheduler_job_id="job-1",
    )
    with conn.cursor() as cur:
        cur.execute("SELECT disposition, exit_code FROM attempts WHERE id = %s", (attempt_id,))
        disposition, exit_code = cur.fetchone()
        cur.execute("SELECT source_revision FROM execution_records WHERE attempt = %s", (attempt_id,))
        (source_revision,) = cur.fetchone()
    assert disposition == "succeeded"
    assert exit_code == 0
    assert source_revision == "abc"


def test_record_attempt_result_retryable_failure_returns_unit_to_ready(conn):
    run_id = _make_run(conn, max_attempts=2)
    stage, unit_id = _make_unit(conn, run_id)
    attempt_id = repo.allocate_attempt(conn, run_id, stage, unit_id)
    repo.record_attempt_result(
        conn, attempt_id, exit_code=75, disposition="transient",
        output_location="runs/x", execution_record={
            "source_revision": "abc", "schema_version": "1", "settings_hash": "h"},
        scheduler_job_id="job-1",
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM units WHERE run = %s AND stage = %s AND unit_id = %s",
            (run_id, stage, unit_id))
        (state,) = cur.fetchone()
    assert state == "ready"


def test_record_attempt_result_exhausted_retryable_failure_fails_unit(conn):
    run_id = _make_run(conn, max_attempts=1)
    stage, unit_id = _make_unit(conn, run_id)
    attempt_id = repo.allocate_attempt(conn, run_id, stage, unit_id)
    repo.record_attempt_result(
        conn, attempt_id, exit_code=75, disposition="transient",
        output_location="runs/x", execution_record={
            "source_revision": "abc", "schema_version": "1", "settings_hash": "h"},
        scheduler_job_id="job-1",
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM units WHERE run = %s AND stage = %s AND unit_id = %s",
            (run_id, stage, unit_id))
        (state,) = cur.fetchone()
    assert state == "failed"


# ======================================================================
# select_attempt
# ======================================================================

def test_select_attempt_happy_path(conn):
    run_id = _make_run(conn)
    stage, unit_id = _make_unit(conn, run_id)
    attempt_id = _succeed_and_select(conn, run_id, stage, unit_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state, selected_attempt FROM units "
            "WHERE run = %s AND stage = %s AND unit_id = %s",
            (run_id, stage, unit_id))
        state, selected = cur.fetchone()
    assert state == "complete"
    assert selected == attempt_id


def test_select_attempt_refuses_if_not_succeeded(conn):
    run_id = _make_run(conn)
    stage, unit_id = _make_unit(conn, run_id)
    attempt_id = repo.allocate_attempt(conn, run_id, stage, unit_id)
    repo.record_attempt_result(
        conn, attempt_id, exit_code=70, disposition="failed",
        output_location="runs/x", execution_record={
            "source_revision": "abc", "schema_version": "1", "settings_hash": "h"},
        scheduler_job_id="job-1",
    )
    with pytest.raises(repo.AttemptNotSucceeded):
        repo.select_attempt(conn, attempt_id)


def test_select_attempt_refuses_if_already_selected(conn):
    run_id = _make_run(conn, max_attempts=2)
    stage, unit_id = _make_unit(conn, run_id)
    first_attempt = _succeed_and_select(conn, run_id, stage, unit_id)

    # A second attempt on the same unit cannot happen through
    # allocate_attempt (the unit is now terminal), so exercise the
    # refusal directly against a second, independently allocated attempt
    # row inserted the way allocate_attempt would have, bypassing its
    # terminal-unit check to isolate select_attempt's own guard.
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM units WHERE run=%s AND stage=%s AND unit_id=%s",
                    (run_id, stage, unit_id))
        (unit_row_id,) = cur.fetchone()
        second_attempt = new_ulid()
        cur.execute(
            "INSERT INTO attempts (id, run, stage, unit, output_location, disposition) "
            "VALUES (%s, %s, %s, %s, %s, 'succeeded')",
            (second_attempt, run_id, stage, unit_row_id, "runs/y"),
        )
    with pytest.raises(repo.UnitAlreadySelected):
        repo.select_attempt(conn, second_attempt)


# ======================================================================
# register_manifest
# ======================================================================

def test_register_manifest_happy_path(conn):
    run_id = _make_run(conn)
    stage, unit_id, attempt_id, instance_id = _full_chain_to_current_candidate(conn, run_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT custody, run, producing_stage FROM product_instances WHERE id = %s",
            (instance_id,))
        custody, inst_run, producing_stage = cur.fetchone()
        cur.execute("SELECT count(*) FROM product_members WHERE instance = %s", (instance_id,))
        (member_count,) = cur.fetchone()
    assert custody == "candidate"  # production run -> candidate custody
    assert inst_run == run_id
    assert producing_stage == stage
    assert member_count == 1


def test_register_manifest_scratch_run_makes_scratch_custody(conn):
    run_id = _make_run(conn, kind="scratch")
    _, _, _, instance_id = _full_chain_to_current_candidate(conn, run_id)
    with conn.cursor() as cur:
        cur.execute("SELECT custody FROM product_instances WHERE id = %s", (instance_id,))
        (custody,) = cur.fetchone()
    assert custody == "scratch"


def test_register_manifest_replay_is_a_no_op(conn):
    run_id = _make_run(conn)
    stage, unit_id, attempt_id, instance_id = _full_chain_to_current_candidate(conn, run_id)
    # Replay the identical manifest again.
    _register_simple_instance(conn, run_id, stage, attempt_id, instance_id=instance_id)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM product_instances WHERE id = %s", (instance_id,))
        (count,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM product_members WHERE instance = %s", (instance_id,))
        (member_count,) = cur.fetchone()
    assert count == 1
    assert member_count == 1  # not duplicated


def test_register_manifest_conflicting_replay_raises(conn):
    run_id = _make_run(conn)
    stage, unit_id, attempt_id, instance_id = _full_chain_to_current_candidate(conn, run_id)
    with pytest.raises(repo.ManifestConflict):
        _register_simple_instance(
            conn, run_id, stage, attempt_id, instance_id=instance_id,
            logical_key={"unit": "different-key"})


def test_register_manifest_records_dependency_edges(conn):
    upstream_run = _make_run(conn)
    _, _, _, producer_instance = _full_chain_to_current_candidate(conn, upstream_run)

    run_id = _make_run(conn)
    stage, unit_id = _make_unit(conn, run_id)
    attempt_id = _succeed_and_select(conn, run_id, stage, unit_id)
    consumer_instance = _register_simple_instance(
        conn, run_id, stage, attempt_id,
        input_products={"reference-image": producer_instance})
    with conn.cursor() as cur:
        cur.execute(
            "SELECT producer_instance FROM dependencies WHERE consumer_instance = %s",
            (consumer_instance,))
        rows = [r[0] for r in cur.fetchall()]
    assert producer_instance in rows


# ======================================================================
# promote
# ======================================================================

def test_promote_happy_path(conn):
    run_id = _make_run(conn)
    key = {"unit": "e001/SCA01", "v": "promote-happy"}
    stage, unit_id, attempt_id, instance_id = _full_chain_to_current_candidate(
        conn, run_id, logical_key=key)

    promotion_id = repo.promote(
        conn, who="brusholme", reason="regular operations",
        changes=[("difference-image", key, None, instance_id)],
    )
    assert promotion_id
    with conn.cursor() as cur:
        cur.execute("SELECT custody FROM product_instances WHERE id = %s", (instance_id,))
        (custody,) = cur.fetchone()
    assert custody == "current"


def test_promote_refuses_whole_request_on_mismatch(conn):
    run_id = _make_run(conn)
    key_a = {"unit": "a", "v": "mismatch-a"}
    key_b = {"unit": "b", "v": "mismatch-b"}
    _, _, _, instance_a = _full_chain_to_current_candidate(conn, run_id, logical_key=key_a)
    _, _, _, instance_b = _full_chain_to_current_candidate(conn, run_id, logical_key=key_b)

    # instance_a promotes fine standalone; claim a wrong expected-before
    # for key_b to force the whole batch to refuse.
    with pytest.raises(repo.PromotionRefused):
        repo.promote(
            conn, who="brusholme", reason="test",
            changes=[
                ("difference-image", key_a, None, instance_a),
                ("difference-image", key_b, new_ulid(), instance_b),  # wrong expected-before
            ],
        )
    # Refused as a whole: instance_a must NOT have been promoted either.
    with conn.cursor() as cur:
        cur.execute("SELECT custody FROM product_instances WHERE id = %s", (instance_a,))
        (custody,) = cur.fetchone()
    assert custody == "candidate"


def test_promote_reversal_restores_previous_selection(conn):
    run_id = _make_run(conn)
    key = {"unit": "e001/SCA01", "v": "reversal"}
    _, _, _, first_instance = _full_chain_to_current_candidate(conn, run_id, logical_key=key)
    repo.promote(
        conn, who="brusholme", reason="initial",
        changes=[("difference-image", key, None, first_instance)],
    )

    # Reprocess: a new instance for the same logical key, from a fresh run.
    run_id_2 = _make_run(conn)
    _, _, _, second_instance = _full_chain_to_current_candidate(conn, run_id_2, logical_key=key)
    repo.promote(
        conn, who="brusholme", reason="reprocess",
        changes=[("difference-image", key, first_instance, second_instance)],
    )
    with conn.cursor() as cur:
        cur.execute("SELECT custody FROM product_instances WHERE id = %s", (second_instance,))
        (custody,) = cur.fetchone()
    assert custody == "current"

    # Reversal: promote the inverse mapping.
    repo.promote(
        conn, who="brusholme", reason="reversal",
        changes=[("difference-image", key, second_instance, first_instance)],
    )
    with conn.cursor() as cur:
        cur.execute("SELECT id, custody FROM product_instances WHERE id IN (%s, %s)",
                     (first_instance, second_instance))
        rows = dict(cur.fetchall())
    assert rows[first_instance] == "current"
    assert rows[second_instance] == "candidate"


# ======================================================================
# mark_run_deleting / mark_run_deleted
# ======================================================================

def test_mark_run_deleting_happy_path(conn):
    run_id = _make_run(conn, kind="scratch")
    repo.mark_run_deleting(conn, run_id, requested_by="brusholme")
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM runs WHERE id = %s", (run_id,))
        (state,) = cur.fetchone()
    assert state == "deleting"


def test_mark_run_deleting_then_mark_run_deleted(conn):
    run_id = _make_run(conn, kind="scratch")
    repo.mark_run_deleting(conn, run_id, requested_by="brusholme")
    repo.mark_run_deleted(conn, run_id)
    with conn.cursor() as cur:
        cur.execute("SELECT state, deleted_at FROM runs WHERE id = %s", (run_id,))
        state, deleted_at = cur.fetchone()
    assert state == "deleted"
    assert deleted_at is not None


def test_mark_run_deleting_refuses_non_scratch(conn):
    run_id = _make_run(conn, kind="production")
    with pytest.raises(repo.DeletionRefused):
        repo.mark_run_deleting(conn, run_id, requested_by="brusholme")


def test_mark_run_deleting_refuses_wrong_owner(conn):
    run_id = _make_run(conn, kind="scratch", owner="alice")
    with pytest.raises(repo.DeletionRefused):
        repo.mark_run_deleting(conn, run_id, requested_by="bob")


def test_mark_run_deleting_refuses_running_attempt(conn):
    run_id = _make_run(conn, kind="scratch")
    stage, unit_id = _make_unit(conn, run_id)
    repo.allocate_attempt(conn, run_id, stage, unit_id)  # left running, disposition NULL
    with pytest.raises(repo.DeletionRefused):
        repo.mark_run_deleting(conn, run_id, requested_by="brusholme")


def test_mark_run_deleting_refuses_outside_dependency(conn):
    run_id = _make_run(conn, kind="scratch")
    key = {"unit": "e001/SCA01", "v": "outside-dep"}
    stage, unit_id, attempt_id, producer_instance = _full_chain_to_current_candidate(
        conn, run_id, logical_key=key)

    # Another run's unit depends on this scratch run's instance.
    consumer_run = _make_run(conn, kind="scratch")
    consumer_stage, consumer_unit_id = _make_unit(conn, consumer_run)
    consumer_attempt = _succeed_and_select(conn, consumer_run, consumer_stage, consumer_unit_id)
    _register_simple_instance(
        conn, consumer_run, consumer_stage, consumer_attempt,
        input_products={"difference-image": producer_instance})

    with pytest.raises(repo.DeletionRefused):
        repo.mark_run_deleting(conn, run_id, requested_by="brusholme")


def test_mark_run_deleted_refuses_if_not_deleting(conn):
    run_id = _make_run(conn, kind="scratch")
    with pytest.raises(repo.DeletionRefused):
        repo.mark_run_deleted(conn, run_id)
