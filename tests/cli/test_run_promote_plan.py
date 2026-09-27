"""Behavioural, black-box tests of ``rapidpipe run promote-plan`` and
``run promote --plan`` (supervisor step 5a, 2026-09-26, R5): argv in,
exit code / stdout / stderr and database state out, against a real
PostgreSQL with Batch and S3 faked (see
``tests/cli/test_run_lifecycle.py``'s module docstring for the shared
setup this module reuses).

``TEST_KIND`` instances get a stand-in slot pinned by
``tests.db.test_repository._register_simple_instance`` (which
``_register_candidate`` here calls), so these tests need no dev-table
(l2files/refimages/diffimages) fixture: the frozen-plan and stale-plan
mechanics are kind-agnostic (repository.promote_run groups by slot),
proven with real derivable kinds in ``tests/db/test_slots.py``.
"""

from __future__ import annotations

import json

from rapidpipe.db.ids import new_ulid
from tests.db.test_repository import TEST_KIND, by_slot

from .test_run_lifecycle import _create_run, _register_candidate, _submit_and_complete


def _write_plan(tmp_path, plan):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    return str(path)


def test_promote_plan_prints_json_and_writes_nothing(cli, db, fake_batch, fake_s3, batch_env):
    run_id = _create_run(cli, db, kind="production")
    attempt_id, _job_id, _location = _submit_and_complete(
        cli, fake_batch, fake_s3, run_id, unit_id="cli-plan-001/SCA07")
    instance_id, key = _register_candidate(db, run_id, "admit", attempt_id)

    result = cli("run", "promote-plan", run_id)
    assert result.rc == 0, result.err
    plan = json.loads(result.out)
    assert plan == [{
        "kind": TEST_KIND,
        "slot": by_slot(key)["slot"],
        "before": None,
        "after": instance_id,
    }]

    with db.cursor() as cur:
        cur.execute("SELECT custody FROM product_instances WHERE id = %s", (instance_id,))
        assert cur.fetchone()[0] == "candidate"  # writes nothing
        cur.execute("SELECT count(*) FROM promotions")
        before_count = cur.fetchone()[0]

    # A second call is unchanged (a read, not a write).
    again = cli("run", "promote-plan", run_id)
    assert again.rc == 0, again.err
    assert json.loads(again.out) == plan
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM promotions")
        assert cur.fetchone()[0] == before_count


def test_promote_with_plan_succeeds_when_nothing_moved(
        cli, db, fake_batch, fake_s3, batch_env, tmp_path):
    run_id = _create_run(cli, db, kind="production")
    attempt_id, _job_id, _location = _submit_and_complete(
        cli, fake_batch, fake_s3, run_id, unit_id="cli-plan-002/SCA07")
    instance_id, _key = _register_candidate(db, run_id, "admit", attempt_id)

    plan_result = cli("run", "promote-plan", run_id)
    assert plan_result.rc == 0, plan_result.err
    plan_path = _write_plan(tmp_path, json.loads(plan_result.out))

    applied = cli(
        "run", "promote", run_id, "--reason", "apply frozen plan",
        "--allow-unreleased", "--plan", plan_path)
    assert applied.rc == 0, applied.err
    promotion_id = applied.out.strip()
    assert promotion_id
    db.track_promotion(promotion_id)

    with db.cursor() as cur:
        cur.execute("SELECT custody FROM product_instances WHERE id = %s", (instance_id,))
        assert cur.fetchone()[0] == "current"
        cur.execute("SELECT slot FROM promotion_changes WHERE promotion = %s", (promotion_id,))
        (recorded_slot,) = cur.fetchone()
    assert recorded_slot == by_slot(_key)["slot"]


def test_promote_with_stale_plan_exits_64_and_writes_nothing(
        cli, db, fake_batch, fake_s3, batch_env, tmp_path):
    key = {"k": new_ulid()}
    run_id = _create_run(cli, db, kind="production")
    attempt_id, _job_id, _location = _submit_and_complete(
        cli, fake_batch, fake_s3, run_id, unit_id="cli-plan-003/SCA07")
    instance_id, _key = _register_candidate(db, run_id, "admit", attempt_id, key=key)

    plan_result = cli("run", "promote-plan", run_id)
    assert plan_result.rc == 0, plan_result.err
    plan_path = _write_plan(tmp_path, json.loads(plan_result.out))

    # A second run's candidate, over the SAME stand-in slot, promotes
    # first: the frozen plan's before=None no longer holds.
    other_run = _create_run(cli, db, kind="production")
    other_attempt, _job, _loc = _submit_and_complete(
        cli, fake_batch, fake_s3, other_run, unit_id="cli-plan-003b/SCA07")
    _other_instance, _ = _register_candidate(db, other_run, "admit", other_attempt, key=key)
    intervening = cli("run", "promote", other_run, "--reason", "intervene",
                       "--allow-unreleased")
    assert intervening.rc == 0, intervening.err
    db.track_promotion(intervening.out.strip())

    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM promotions")
        before_count = cur.fetchone()[0]

    stale = cli(
        "run", "promote", run_id, "--reason", "apply stale plan",
        "--allow-unreleased", "--plan", plan_path)
    assert stale.rc == 64
    assert "stale plan" in stale.err

    with db.cursor() as cur:
        cur.execute("SELECT custody FROM product_instances WHERE id = %s", (instance_id,))
        assert cur.fetchone()[0] == "candidate"  # nothing written
        cur.execute("SELECT count(*) FROM promotions")
        assert cur.fetchone()[0] == before_count


def test_promote_plan_unknown_run_exits_64(cli, db):
    result = cli("run", "promote-plan", new_ulid())
    assert result.rc == 64


def test_promote_plan_nothing_to_promote_exits_64(cli, db):
    run_id = _create_run(cli, db, kind="production")
    result = cli("run", "promote-plan", run_id)
    assert result.rc == 64
    assert "nothing to promote" in result.err
