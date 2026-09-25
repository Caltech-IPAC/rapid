"""Behavioural, black-box tests of ``rapidpipe check list|run|show`` and the
check-policy promotion gate (``run create``/``run promote``): argv in, exit
code / stdout / stderr and database state out, against a real PostgreSQL
with Batch faked where a walk is driven (see ``test_run_lifecycle.py``'s
module docstring for the shared registration convention this module
reuses).

Building a candidate's science rows (``diffimmeta``, ``l2files``) and its
run/unit/attempt/instance chain is exactly what ``tests/db/test_checks.py``
already does for the database-level suite (supervisor step 6, WP-A); this
module imports those builders rather than duplicating them, and drives the
actual behaviour under test -- ``check list|run|show``, ``run create
--check-policy/--auto-promote``, ``run promote --check-policy`` -- only
through ``main(argv)``. Those builders call ``rapidpipe.runs.repository``
directly to allocate/select an attempt and register an instance, the same
"one exception" ``test_run_lifecycle.py``'s docstring describes, extended
here to a candidate's science rows because no stage process registers
those either.
"""

from __future__ import annotations

import json
import random

import pytest

from rapidpipe.checks import policy as policy_mod
from rapidpipe.checks.policy import load_policy_file
from rapidpipe.db.ids import new_ulid
from tests.db.test_checks import FIXTURES, STRICT, TRIAL, _diff_candidate, _l2, _selected_attempt
from tests.db.test_repository import TEST_KIND, _make_run, _register_simple_instance

from .conftest import FAKE_BUCKET
from .test_run_lifecycle import _kv, _seed_manifest

TRIAL_STAGE_NAME = "difference-image-statistics@1"


@pytest.fixture(autouse=True)
def _science_row_cleanup(db):
    """``diffimages``/``diffimmeta``/``l2files`` rows this module inserts
    directly (see ``_diff_candidate``/``_l2`` in ``tests/db/test_checks.py``)
    reference ``product_instances`` by a foreign key that module's own
    tests never need to drop -- inside ``tests/db``'s never-committed
    transaction there is nothing to clean up. Here, ``db``'s teardown
    really deletes ``product_instances`` (in FK order, conftest.py's
    ``_delete_run_rows``), which fails unless these science rows are
    deleted first. This fixture's teardown runs before ``db``'s own
    (it depends on ``db``, so it is torn down first), for every run id
    the test tracked -- including the throwaway auxiliary runs
    ``_catalog_source_set`` creates for its l2/difference scaffolding."""
    yield
    if not db.run_ids:
        return
    with db.cursor() as cur:
        cur.execute(
            "DELETE FROM checks WHERE instance IN "
            "(SELECT id FROM product_instances WHERE run = ANY(%s))", (db.run_ids,))
        cur.execute("DELETE FROM diffimmeta WHERE run = ANY(%s)", (db.run_ids,))
        cur.execute("DELETE FROM diffimages WHERE run = ANY(%s)", (db.run_ids,))
        cur.execute("DELETE FROM l2files WHERE run = ANY(%s)", (db.run_ids,))


def _catalog_source_set(db, run_id, *, rows, catalog_type="sextractor"):
    """A source-set candidate of ``run_id`` with a resolvable science
    identity (l2 -> difference -> source-set, ``tests/db/test_checks.py``'s
    ``_chain``), but with its l2/difference scaffolding registered in a
    throwaway auxiliary run, so ``run_id``'s only checkable candidates are
    the ones this module's tests build on purpose -- ``_chain`` itself
    registers that scaffolding in the candidate's own run, which would add
    an unwanted (no-diffimmeta) difference-image candidate here."""
    from tests.db.test_checks import _source_set

    conn = db.connection
    aux_run = _make_run(conn)
    db.track_run(aux_run)
    identity = (random.randrange(10**6, 10**9), 3, 1)
    l2 = _l2(conn, aux_run, expid=identity[0], sca=identity[1], fid=identity[2])
    attempt_id = _selected_attempt(conn, aux_run)
    diff = _register_simple_instance(
        conn, aux_run, "difference", attempt_id, kind="difference-image",
        logical_key={"l2": l2, "reference": "REF", "differencer": "sfft",
                     "settings_hash": new_ulid()})
    return _source_set(conn, run_id, key={"difference": diff, "catalog_type": catalog_type},
                       rows=rows)


def _checks_detail(db, instance):
    with db.cursor() as cur:
        cur.execute("SELECT detail FROM checks WHERE instance = %s ORDER BY happened_at, id",
                    (instance,))
        return [row[0] for row in cur.fetchall()]


def _promotion_row(db, promotion_id):
    with db.cursor() as cur:
        cur.execute("SELECT check_policy_version, check_result_ids::text[] FROM promotions "
                    "WHERE id = %s", (promotion_id,))
        return cur.fetchone()


def _promotions_count(db, run_id):
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM promotions WHERE request_context->>'run' = %s", (run_id,))
        return cur.fetchone()[0]


# ======================================================================
# check list
# ======================================================================

def test_check_list_prints_registered_checks_and_both_shipped_policies(cli):
    result = cli("check", "list")
    assert result.rc == 0, result.err
    out = result.out
    assert "check=difference-image-statistics@1 kind=difference-image" in out
    assert "check=catalog-counts-vs-reference@1 kind=source-set" in out
    assert f"policy={TRIAL} approval=trial" in out
    assert f"policy={STRICT} approval=trial" in out
    assert "difference-image-statistics@1(required)" in out
    assert "catalog-counts-vs-reference@1(advisory)" in out


# ======================================================================
# check run / check show, and the promotion gate under trial vs strict
# ======================================================================

def test_check_run_trial_passes_strict_fails_and_show_lists_newest_first(cli, db):
    run_id = _make_run(db.connection, kind="production")
    db.track_run(run_id)
    diff = _diff_candidate(db.connection, run_id)
    catalog = _catalog_source_set(db, run_id, rows=10)

    trial = cli("check", "run", run_id, "--policy", TRIAL)
    assert trial.rc == 0, trial.err
    trial_lines = trial.out.splitlines()
    assert len(trial_lines) == 2
    assert all(" outcome=passed " in line or line.endswith("outcome=passed") or
               " outcome=passed" in line for line in trial_lines)
    assert any(f"instance={diff} " in line and "difference-image-statistics@1" in line
               for line in trial_lines)
    assert any(f"instance={catalog} " in line and "catalog-counts-vs-reference@1" in line
               for line in trial_lines)

    diff_details = _checks_detail(db, diff)
    assert diff_details[-1]["policy"] == TRIAL
    from rapidpipe.checks.policy import load_policy
    assert diff_details[-1]["params"] == load_policy(TRIAL).find_check(
        "difference-image-statistics@1").params

    strict = cli("check", "run", run_id, "--policy", STRICT)
    assert strict.rc == 1
    strict_out = strict.out
    assert f"instance={diff} " in strict_out
    assert "check=difference-image-statistics@1 required=true outcome=failed" in strict_out

    shown = cli("check", "show", run_id)
    assert shown.rc == 0, shown.err
    lines = shown.out.splitlines()
    # Newest first: the strict-run rows (recorded second) precede the
    # trial-run rows (recorded first).
    assert len(lines) == 4
    strict_ids = {row["id"] for row in _checks_by_policy(db, run_id, STRICT)}
    trial_ids = {row["id"] for row in _checks_by_policy(db, run_id, TRIAL)}
    seen_strict_positions = [i for i, line in enumerate(lines)
                             if any(f"id={sid} " in line for sid in strict_ids)]
    seen_trial_positions = [i for i, line in enumerate(lines)
                            if any(f"id={tid} " in line for tid in trial_ids)]
    assert max(seen_strict_positions) < min(seen_trial_positions)


def _checks_by_policy(db, run_id, policy_ref):
    with db.cursor() as cur:
        cur.execute(
            "SELECT c.id, c.detail FROM checks c JOIN product_instances pi ON pi.id = c.instance "
            "WHERE pi.run = %s AND c.detail->>'policy' = %s", (run_id, policy_ref))
        return [{"id": row[0], "detail": row[1]} for row in cur.fetchall()]


# ======================================================================
# run promote --check-policy: refusal, success, and the params-matching
# rule (a strict result recorded after a trial one does not poison it).
# ======================================================================

def test_run_promote_check_policy_refuses_then_succeeds_unpoisoned_by_a_later_strict_run(
        cli, db):
    run_id = _make_run(db.connection, kind="production")
    db.track_run(run_id)
    diff = _diff_candidate(db.connection, run_id)
    catalog = _catalog_source_set(db, run_id, rows=10)

    trial_run = cli("check", "run", run_id, "--policy", TRIAL)
    assert trial_run.rc == 0, trial_run.err
    trial_ids = sorted(row["id"] for row in _checks_by_policy(db, run_id, TRIAL))

    strict_run = cli("check", "run", run_id, "--policy", STRICT)
    assert strict_run.rc == 1

    before = _promotions_count(db, run_id)
    refused = cli("run", "promote", run_id, "--reason", "strict", "--check-policy", STRICT)
    assert refused.rc == 64
    assert "required check difference-image-statistics@1" in refused.err
    assert f"instance {diff}" in refused.err
    assert refused.err.rstrip().endswith("; refusing")
    assert _promotions_count(db, run_id) == before

    promoted = cli("run", "promote", run_id, "--reason", "trial", "--check-policy", TRIAL)
    assert promoted.rc == 0, promoted.err
    promotion_id = promoted.out.strip()
    db.track_promotion(promotion_id)
    version, ids = _promotion_row(db, promotion_id)
    assert version == TRIAL
    assert sorted(ids) == trial_ids

    with db.cursor() as cur:
        cur.execute("SELECT custody FROM product_instances WHERE id IN (%s, %s)", (diff, catalog))
        assert {row[0] for row in cur.fetchall()} == {"current"}


# ======================================================================
# run promote: default policy resolution (none set -> rebuild-trial@1;
# runs.check_policy_ref set -> that one)
# ======================================================================

def test_run_promote_resolves_default_then_the_runs_check_policy_ref(cli, db):
    default_run = _make_run(db.connection, kind="production")
    db.track_run(default_run)
    _diff_candidate(db.connection, default_run)
    _catalog_source_set(db, default_run, rows=5)
    ran = cli("check", "run", default_run)
    assert ran.rc == 0, ran.err
    promoted = cli("run", "promote", default_run, "--reason", "no explicit policy")
    assert promoted.rc == 0, promoted.err
    promotion_id = promoted.out.strip()
    db.track_promotion(promotion_id)
    assert _promotion_row(db, promotion_id)[0] == TRIAL

    strict_run = _make_run(db.connection, kind="production", check_policy_ref=STRICT)
    db.track_run(strict_run)
    _catalog_source_set(db, strict_run, rows=5)  # advisory-only candidate
    ran2 = cli("check", "run", strict_run)
    assert ran2.rc == 1, ran2.err  # the catalog check fails under strict (missing_reference=fail)
    out2 = cli("run", "promote", strict_run, "--reason", "run's own check_policy_ref")
    assert out2.rc == 0, out2.err
    promotion_id2 = out2.out.strip()
    db.track_promotion(promotion_id2)
    assert _promotion_row(db, promotion_id2)[0] == STRICT


# ======================================================================
# A missing required result (no check has been run) refuses promotion.
# ======================================================================

def test_run_promote_refuses_a_missing_required_result(cli, db):
    run_id = _make_run(db.connection, kind="production")
    db.track_run(run_id)
    diff = _diff_candidate(db.connection, run_id)
    result = cli("run", "promote", run_id, "--reason", "no checks yet")
    assert result.rc == 64
    assert "has no result" in result.err
    assert f"instance {diff}" in result.err
    assert _promotions_count(db, run_id) == 0


# ======================================================================
# run create --auto-promote: refused under a policy that does not permit
# it; permitted under a lead-approved fixture policy; run start then
# calls maybe_auto_promote and promotes.
# ======================================================================

def test_run_create_auto_promote_refused_then_permitted_and_run_start_promotes(
        cli, db, fake_batch, fake_s3, batch_env, monkeypatch):
    refused = cli(
        "run", "create", "--kind", "production", "--purpose", "auto-promote refusal",
        "--stages", "admit", "--check-policy", TRIAL, "--auto-promote")
    assert refused.rc == 64
    assert "does not permit automatic promotion; lead approval pending" in refused.err

    fixture = load_policy_file(FIXTURES / "auto-trial@1.toml")
    monkeypatch.setitem(policy_mod._FIXTURE_POLICIES, "auto-trial@1", fixture)

    created = cli(
        "run", "create", "--kind", "production", "--purpose", "auto-promote demo",
        "--stages", "admit", "--check-policy", "auto-trial@1", "--auto-promote")
    assert created.rc == 0, created.err
    run_id = created.out.strip()
    db.track_run(run_id)
    with db.cursor() as cur:
        cur.execute("SELECT auto_promote, check_policy_ref FROM runs WHERE id = %s", (run_id,))
        assert cur.fetchone() == (True, "auto-trial@1")

    submitted = cli("run", "start", run_id, "--unit", "U", "--inputs",
                    "admit=s3://in-bucket/in-prefix", "--no-wait")
    assert submitted.rc == 0, submitted.err
    attempt_id = _kv(submitted.out, "attempt")
    job_id = _kv(submitted.out, "job")
    output_location = _kv(submitted.out, "outputs")
    fake_batch.set_status(job_id, "SUCCEEDED")
    _seed_manifest(fake_s3, output_location, run_id=run_id, stage="admit",
                   unit_id="U", attempt_id=attempt_id)

    # The released-image rule (step 5, R8) applies to automatic promotion
    # too (maybe_auto_promote calls promote_run with no --allow-unreleased):
    # a complete release whose image_digest matches this attempt's
    # execution record, so the only thing left to refuse promotion would
    # be the check policy itself.
    digest = "sha256:" + "ab" * 32
    tag = f"cli-test-{new_ulid()}"
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO releases (tag, source_revision, schema_version, image_digest, "
            "state, cut_by) VALUES (%s, %s, '1', %s, 'complete', 'cli-test')",
            (tag, "a" * 40, digest))
    try:
        bucket = FAKE_BUCKET
        prefix = output_location[len(f"s3://{bucket}/"):]
        exec_record = {"schema_version": "1", "source_revision": "a" * 40,
                       "settings_hash": "h", "image_digest": digest}
        fake_s3.seed(bucket, f"{prefix}/exec/{attempt_id}.json",
                    json.dumps(exec_record).encode())

        # No check applies to TEST_KIND, so a candidate of that kind passes
        # the auto-trial@1 gate trivially -- registration is the one
        # exception this suite's own convention allows
        # (test_run_lifecycle.py's module docstring): no stage process
        # runs here to do it itself.
        instance_id = _register_simple_instance(
            db.connection, run_id, "admit", attempt_id, kind=TEST_KIND,
            logical_key={"k": new_ulid()})

        result = cli("run", "start", run_id, "--unit", "U")
        assert result.rc == 0, result.err
        assert f"run={run_id} state=complete" in result.out
        assert "auto-promote promoted promotion=" in result.out
        promotion_id = _kv(result.out, "promotion")

        with db.cursor() as cur:
            cur.execute("SELECT custody FROM product_instances WHERE id = %s", (instance_id,))
            assert cur.fetchone()[0] == "current"
        assert _promotion_row(db, promotion_id)[0] == "auto-trial@1"
        db.track_promotion(promotion_id)
    finally:
        with db.cursor() as cur:
            cur.execute("DELETE FROM releases WHERE tag = %s", (tag,))


def test_run_start_prints_auto_promote_off_for_an_ordinary_run(
        cli, db, fake_batch, fake_s3, batch_env):
    """The ordinary path: no shipped policy permits automatic promotion, so
    every real run today prints 'auto-promote off' (R5)."""
    run_id = cli("run", "create", "--kind", "scratch", "--purpose", "auto-promote off",
                "--stages", "admit").out.strip()
    db.track_run(run_id)
    result = cli("run", "start", run_id, "--unit", "U", "--inputs",
                "admit=s3://in-bucket/in-prefix", "--no-wait")
    assert result.rc == 0, result.err
    job_id = _kv(result.out, "job")
    output_location = _kv(result.out, "outputs")
    attempt_id = _kv(result.out, "attempt")
    fake_batch.set_status(job_id, "SUCCEEDED")
    _seed_manifest(fake_s3, output_location, run_id=run_id, stage="admit",
                   unit_id="U", attempt_id=attempt_id)

    final = cli("run", "start", run_id, "--unit", "U")
    assert final.rc == 0, final.err
    assert f"auto-promote off (policy {TRIAL})" in final.out


# ======================================================================
# Help walk: 'check', 'check list|run|show' --help all exit 0.
# ======================================================================

def test_check_help_walk(cli):
    for argv in (
        ["check", "--help"],
        ["check", "list", "--help"],
        ["check", "run", "--help"],
        ["check", "show", "--help"],
    ):
        result = cli(*argv)
        assert result.rc == 0, (argv, result.err)
