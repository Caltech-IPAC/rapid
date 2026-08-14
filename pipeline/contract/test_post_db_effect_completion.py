"""Contract tests: a post-DB job type's full lifecycle through the
effect-confirmation completion boundary (2026-08-14 ruling — the six
post-DB job types become EFFECT-CLASS, closing through the same boundary
alert production already used, ruling R1 / migration 075).

**NEEDS CI's POSTGRES.** Everything here runs the REAL stage code
(`pipeline.stages.post_db`) against a REAL child table created through the
REAL `derived.create_child_table()` (rapid_systems migration 072), then the
REAL `pipeline.entrypoints.job._execute` disposition derivation, the REAL
`pipeline.registration.consumer` SKIP-under-lease path, and the REAL
`derived.transition_work_unit` completion-acceptance trigger with the GUC
enforced (migration 080/083) — none of which a stub double can be faithful
to, matching this tier's own reason for existing
(`pipeline.contract.fixture`'s module docstring, this directory's README).

WHAT IS COVERED, and by which test:

  * a post-DB unit's full submission -> execution -> confirmation ->
    completion, through the real stage function and the real trigger --
    `test_merge_dedup_full_lifecycle_completes_through_the_trigger`
  * the failed-confirmation retry path: a stage that cannot confirm its
    own write parks the unit under the SAME `disposition_for_unconfirmed_
    effect` ceiling alert production's `effect_unconfirmed` attempts use --
    `test_a_failed_verification_retries_then_confirms`
  * all six post-DB job types reach `effect_confirmed` and complete through
    the acceptance trigger with the GUC on -- `test_all_six_post_db_types_
    complete_through_the_acceptance_trigger`

Written, marked, NOT RUN here — the project rule is that nothing executes
locally for RAPID; the supervisor runs the contract tier on rapid-admin/CI.
"""

import uuid

import pytest
from psycopg2 import sql

from database.modules.utils.rapid_db_connect import transaction
from observability.attempts import ProductDisposition, RapidOutcome
from pipeline.contract import fixture
from pipeline.entrypoints.job import _EFFECT_OUTCOME_TO_DISPOSITION
from pipeline.intent import retry_policy
from pipeline.intent.writer import (BLOCKED, COMPLETE, READY, SUBMITTED,
                                    WorkUnitWriter)
from pipeline.registration import consumer
from pipeline.stages import catalog_db, post_db
from pipeline.stages.context import StageContext
from submission import payloads
from submission.manifest import ProcessingUnit
from submission.routes import (JOB_TYPE_CATALOG_LOAD, JOB_TYPE_CROSSMATCH,
                               JOB_TYPE_MERGE_CURRENCY, JOB_TYPE_MERGE_DEDUP,
                               JOB_TYPE_SOURCE_CURRENCY, JOB_TYPE_STATISTICS)
from pipeline.stages.sequences import EFFECT_CLASS_JOB_TYPES


# ---------------------------------------------------------------------------
# Fixture helpers local to this file
# ---------------------------------------------------------------------------

def _requires_075(conn):
    if not fixture.admits_state(conn, "effect_confirmed", table="attempts"):
        pytest.skip("migration 075 is not applied (product_disposition has "
                    "no effect_* values)")


def _requires_derived_create_child_table(conn):
    if not fixture.has_function(conn, "create_child_table", schema="derived"):
        pytest.skip("rapid_systems migration 072 is not applied "
                    "(derived.create_child_table is absent)")


def _field_number():
    """A field id that satisfies `catalog_db._FIELD_NAME` and is unlikely to
    collide across parallel contract runs -- 7 digits, matching the live
    Roman field-identifier shape `submission.gathering`'s own comments cite.
    """
    return 4_000_000 + (uuid.uuid4().int % 999_999)


def _make_terminal_attempt(conn, work_unit_id, product_disposition,
                           rapid_outcome="success",
                           terminal_record_sequence=1,
                           error_category=None):
    """A `terminal_after_start` attempt carrying a real, chosen
    `product_disposition`, bound to a work unit -- the identical INSERT
    shape `pipeline.contract.test_effect_completion._make_terminal_attempt`
    uses (that module's own docstring explains the column set; not imported
    across test files, per this tier's convention -- see `test_work_unit_
    grants.py`'s identical private duplicate).
    """
    logical_job_id, run_id = fixture.make_logical_job(conn)
    tag = uuid.uuid4().hex[:8]
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(schema_version), 1) FROM attempts")
        schema_version = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO attempts"
            "  (run_id, schema_version, logical_job_id, lifecycle_state,"
            "   created_at, submitted_at, started_at, ended_at,"
            "   scheduler_job_id, scheduler_state, scheduler_observed_exit,"
            "   source_sha, container_digest, job_definition_rev,"
            "   config_digest, process_exit_code, rapid_outcome,"
            "   product_disposition, error_category,"
            "   binding_job_definition_arn, binding_image_digest,"
            "   binding_manifest_checksum, terminal_record_key,"
            "   terminal_record_sequence, work_unit_id)"
            " VALUES (%s, %s, %s, 'terminal_after_start',"
            "         now(), now(), now(), now(),"
            "         %s, 'SUCCEEDED', 0,"
            "         %s, 'sha256:' || %s, 1,"
            "         'sha256:' || %s, 0, %s,"
            "         %s, %s,"
            "         %s, 'sha256:' || %s,"
            "         'sha256:' || %s, %s,"
            "         %s, %s)"
            " RETURNING attempt_id",
            [run_id, schema_version, logical_job_id,
             f"job-{tag}", f"sha-{tag}", tag, tag, rapid_outcome,
             product_disposition, error_category,
             f"arn:aws:batch:us-east-1:account:job-definition/f-{tag}:1",
             tag, tag,
             f"records/{fixture.RUN_TAG}/{tag}.json",
             terminal_record_sequence, work_unit_id])
        return cur.fetchone()[0]


def _decide_and_apply_skip(conn, attempt_id, work_unit_id,
                           product_disposition, rapid_outcome,
                           terminal_record_sequence):
    """The real SKIP-under-lease block `register_batch` runs -- identical
    to `test_effect_completion.py`'s own helper of the same name (private
    duplicate, same reasoning). Returns "consumed", "already_consumed" or
    "superseded".
    """
    with consumer._transaction(conn) as cur:
        consumer._acquire_attempt_lease(cur, attempt_id)
        watermark = consumer._reread_watermark(cur, attempt_id)
        assert watermark is not None, "attempt vanished under its own lease"
        consumed_sequence, current_terminal_sequence = watermark
        if (consumed_sequence is not None
                and consumed_sequence >= terminal_record_sequence):
            return "already_consumed"
        if (current_terminal_sequence is not None
                and current_terminal_sequence > terminal_record_sequence):
            return "superseded"
        consumer._apply_skip_disposition(
            attempt_id, work_unit_id, product_disposition, rapid_outcome,
            cur)
        consumer.mark_consumed(conn, attempt_id, terminal_record_sequence,
                               cursor=cur)
    return "consumed"


def _complete_with_guc_enforced(conn, unit_id, attempt_id, reason):
    """Enforce the completion-acceptance GUC for exactly this call, then
    attempt `submitted -> complete` -- identical to `test_work_unit_grants.
    py`'s own helper of the same name (private duplicate, same reasoning:
    this tier does not import one test module from another).
    """
    import json

    with conn.cursor() as cur:
        cur.execute("SET LOCAL rapid.enforce_completion_acceptance = 'on'")
        cur.execute(
            "SELECT derived.transition_work_unit(%s, %s, %s, %s, %s, %s, %s, %s)",
            [unit_id, SUBMITTED, COMPLETE, "reconciler", None, reason,
             json.dumps({"deciding_attempt_id": attempt_id}), True])
        cur.execute(
            "SET CONSTRAINTS work_units_check_event_recorded_trg IMMEDIATE")


class _FakeLogger:
    """A minimal stand-in for `RuntimeLogger` -- `StageContext.logger` is
    called (`.info`, `.error`, `.warning`) but never asserted on here; the
    real assertions are against the DATABASE, matching this tier's own
    reason for existing. `pytest`'s own captured-log fixture is not used
    because these tests want a plain, dependency-free double, not caplog's
    stdlib-logging integration.
    """

    def info(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def exception(self, *a, **k):
        pass


def _merge_dedup_context(conn, field, work_unit_id):
    payload = payloads.build(JOB_TYPE_MERGE_DEDUP, field=field,
                             target_table=f"merges_{field}")
    unit = ProcessingUnit(payload=payload)
    return StageContext(
        workdir=None, unit=unit, job_type=JOB_TYPE_MERGE_DEDUP,
        science={}, parameters={}, logger=_FakeLogger(), connection=conn)


# ---------------------------------------------------------------------------
# One post-DB type's full lifecycle: submission -> execution ->
# confirmation -> completion, through the real stage and the real trigger.
# merge-dedup is the simplest of the six (one stage, a should-find-nothing
# check, no upstream data dependency beyond an empty clone) -- the type
# most amenable to a from-scratch contract test that needs no fixture data
# beyond the child table itself.
# ---------------------------------------------------------------------------

def test_merge_dedup_full_lifecycle_completes_through_the_trigger(conn):
    """A clean `merges_<field>` clone: the dedup check finds nothing,
    confirms via a real post-commit re-query, and the resulting
    `effect_confirmed` attempt completes its work unit through the real
    completion-acceptance trigger with the GUC enforced.
    """
    _requires_075(conn)
    _requires_derived_create_child_table(conn)
    if not fixture.has_table(conn, "merges"):
        pytest.skip("no 'merges' prototype table on this database")

    field = _field_number()
    table = f"merges_{field}"

    # SUBMISSION: a real typed payload for a real work unit, exactly the
    # shape `submission.gathering`'s per-field sweep gatherers build.
    unit_id = fixture.create_unit(
        conn, fixture.scope(f"merge-dedup-lifecycle-{field}"),
        state=SUBMITTED)
    context = _merge_dedup_context(conn, field, unit_id)

    # EXECUTION: the real stage. `check_merge_duplicates` creates nothing
    # itself -- it requires the table exist (`catalog_db.require_table`) --
    # so the field's clone is made first, through the real `derived.
    # create_child_table`, exactly as `create_field_tables` would for a real
    # crossmatch unit sharing this field.
    with transaction(conn) as cursor:
        catalog_db.create_child_table(cursor, table, "merges")

    post_db.check_merge_duplicates(context)

    # CONFIRMATION: the stage's own post-write verification produced a real
    # `effect_outcome` stage fact, exactly as `pipeline.entrypoints.job.
    # _execute` reads it for an effect-class job type.
    assert context.has_product("effect_outcome")
    effect_outcome = context.product("effect_outcome")
    assert effect_outcome == post_db.EFFECT_OUTCOME_CONFIRMED
    assert JOB_TYPE_MERGE_DEDUP in EFFECT_CLASS_JOB_TYPES

    disposition = _EFFECT_OUTCOME_TO_DISPOSITION[effect_outcome]
    assert disposition == ProductDisposition.EFFECT_CONFIRMED.value

    conn.commit()

    # THE ATTEMPT RECORD: what `_execute`/`terminate` would have written for
    # this outcome.
    attempt_id = _make_terminal_attempt(
        conn, unit_id, product_disposition=disposition,
        rapid_outcome=RapidOutcome.SUCCESS.value, terminal_record_sequence=1)
    conn.commit()

    # THE REGISTRATION CONSUMER'S SKIP PATH: the real
    # `_apply_skip_disposition` EFFECT_CONFIRMED branch, under the real
    # per-attempt lease.
    result = _decide_and_apply_skip(
        conn, attempt_id, unit_id, disposition, RapidOutcome.SUCCESS.value,
        1)
    assert result == "consumed"

    state, _reason = fixture.unit_state(conn, unit_id)
    assert state == COMPLETE, (
        "an effect_confirmed SKIP verdict must close the work unit complete "
        "-- ruling R1's table, the same standard a REGISTER verdict's "
        "_complete_work_unit applies")

    # THE COMPLETION-ACCEPTANCE TRIGGER, DIRECTLY, WITH THE GUC ENFORCED:
    # `_apply_skip_disposition` already drove the SAME transition through
    # `WorkUnitWriter`, which does not force the GUC on -- this call proves
    # the identical transition ALSO satisfies the deferred acceptance
    # trigger's clause (d) (effect_confirmed OR registered_at) when the GUC
    # is explicitly enforced, on a FRESH unit at the same state, matching
    # `test_work_unit_grants.py`'s own pattern for asserting the trigger
    # rather than trusting an unenforced pass.
    second_unit_id = fixture.create_unit(
        conn, fixture.scope(f"merge-dedup-lifecycle-guc-{field}"),
        state=SUBMITTED)
    second_attempt_id = _make_terminal_attempt(
        conn, second_unit_id, product_disposition=disposition,
        rapid_outcome=RapidOutcome.SUCCESS.value, terminal_record_sequence=1)
    conn.commit()
    _complete_with_guc_enforced(
        conn, second_unit_id, second_attempt_id,
        "C-postdb: merge-dedup effect_confirmed under enforced GUC")
    conn.commit()
    assert fixture.unit_state(conn, second_unit_id)[0] == COMPLETE


def test_a_failed_verification_retries_then_confirms(conn):
    """THE FAILED-CONFIRMATION RETRY PATH. `_verify_effect`'s post-commit
    re-query is asked to expect a count the table cannot have (a
    deliberately wrong expectation, standing in for "the write did not
    land the way this attempt believes"), so it reports `unconfirmed` --
    the same retry ceiling `disposition_for_unconfirmed_effect` gives an
    alert-production `effect_unconfirmed` attempt. Under the ceiling: ready
    for another attempt. A later attempt that verifies correctly then
    confirms and completes, mirroring `test_effect_completion.py`'s own
    `test_unconfirmed_effect_retries_then_confirms` for the claim/confirm
    protocol.
    """
    _requires_075(conn)
    _requires_derived_create_child_table(conn)
    if not fixture.has_table(conn, "merges"):
        pytest.skip("no 'merges' prototype table on this database")

    field = _field_number()
    table = f"merges_{field}"
    with transaction(conn) as cursor:
        catalog_db.create_child_table(cursor, table, "merges")
    conn.commit()

    unit_id = fixture.create_unit(
        conn, fixture.scope(f"merge-dedup-retry-{field}"), state=SUBMITTED)
    context = _merge_dedup_context(conn, field, unit_id)

    # A DELIBERATELY WRONG EXPECTATION, driving `_verify_effect` down its
    # unconfirmed path against the REAL database -- the query it issues
    # really does run against `table`, and really does return a count that
    # disagrees with what this call claims to expect.
    outcome = post_db._verify_effect(
        conn, context, f"deliberately-wrong-expectation {table}",
        sql.SQL("SELECT count(*) FROM {t}").format(t=sql.Identifier(table)),
        (), 999999)
    assert outcome == post_db.EFFECT_OUTCOME_UNCONFIRMED

    disposition = _EFFECT_OUTCOME_TO_DISPOSITION[outcome]
    assert disposition == ProductDisposition.EFFECT_UNCONFIRMED.value

    first_attempt_id = _make_terminal_attempt(
        conn, unit_id, product_disposition=disposition,
        rapid_outcome=RapidOutcome.SUCCESS.value, terminal_record_sequence=1)
    conn.commit()

    result = _decide_and_apply_skip(
        conn, first_attempt_id, unit_id, disposition,
        RapidOutcome.SUCCESS.value, 1)
    assert result == "consumed"

    # UNDER THE CEILING: the first effect_unconfirmed attempt for this unit
    # goes back to READY, not BLOCKED (`disposition_for_unconfirmed_effect`
    # with `effect_attempt_count=1` is `RETRY_READY` -- see that function's
    # own docstring for the ceiling value).
    state, reason = fixture.unit_state(conn, unit_id)
    assert state == READY, (
        f"expected a retry under the ceiling, got state={state!r} "
        f"reason={reason!r}")

    # A LATER ATTEMPT VERIFIES CORRECTLY AND CONFIRMS. Standing in for the
    # operator resubmitting the retried-ready unit -- through the same
    # production writer `fixture.create_unit` itself uses, not a raw
    # UPDATE, matching that fixture's own "a fixture that writes rows the
    # production writer would never write tests a schema the application
    # does not use". Re-running the real stage this time carries no wrong
    # expectation, so its own post-commit re-query matches what it wrote
    # (nothing, on a clean table).
    WorkUnitWriter(fixture.executor(conn)).transition_unit(
        unit_id, READY, SUBMITTED, writer="orchestrator")
    conn.commit()

    second_context = _merge_dedup_context(conn, field, unit_id)
    post_db.check_merge_duplicates(second_context)
    assert second_context.product(
        "effect_outcome") == post_db.EFFECT_OUTCOME_CONFIRMED
    conn.commit()

    second_disposition = _EFFECT_OUTCOME_TO_DISPOSITION[
        second_context.product("effect_outcome")]
    second_attempt_id = _make_terminal_attempt(
        conn, unit_id, product_disposition=second_disposition,
        rapid_outcome=RapidOutcome.SUCCESS.value, terminal_record_sequence=2)
    conn.commit()

    result = _decide_and_apply_skip(
        conn, second_attempt_id, unit_id, second_disposition,
        RapidOutcome.SUCCESS.value, 2)
    assert result == "consumed"
    assert fixture.unit_state(conn, unit_id)[0] == COMPLETE


# ---------------------------------------------------------------------------
# All six post-DB job types complete through the acceptance trigger with
# the GUC on. The trigger's own predicate (migration 080's repair of 076)
# reads product_disposition/registered_at/terminal_record_sequence and
# nothing job-type-specific -- `attempts` carries no job_type column at all
# (`pipeline.registration.products` documents the same fact for the
# REGISTER path) -- so this is deliberately job-type-agnostic at the
# trigger layer while still naming each of the six explicitly, so a job
# type quietly dropped from `EFFECT_CLASS_JOB_TYPES` (task 1's own
# structural derivation) would fail this test's own membership assertion
# even though the trigger itself could not tell the difference.
# ---------------------------------------------------------------------------

_POST_DB_JOB_TYPES = (
    JOB_TYPE_CATALOG_LOAD, JOB_TYPE_CROSSMATCH, JOB_TYPE_STATISTICS,
    JOB_TYPE_MERGE_CURRENCY, JOB_TYPE_SOURCE_CURRENCY, JOB_TYPE_MERGE_DEDUP,
)


@pytest.mark.parametrize("job_type", _POST_DB_JOB_TYPES)
def test_all_six_post_db_types_complete_through_the_acceptance_trigger(
        conn, job_type):
    _requires_075(conn)
    assert job_type in EFFECT_CLASS_JOB_TYPES, (
        f"{job_type!r} is one of the six post-DB job types and must be "
        f"effect-class after the 2026-08-14 ruling")

    unit_id = fixture.create_unit(
        conn, fixture.scope(f"post-db-guc-{job_type}"), state=SUBMITTED)
    attempt_id = _make_terminal_attempt(
        conn, unit_id,
        product_disposition=ProductDisposition.EFFECT_CONFIRMED.value,
        rapid_outcome=RapidOutcome.SUCCESS.value, terminal_record_sequence=1)
    conn.commit()

    _complete_with_guc_enforced(
        conn, unit_id, attempt_id,
        f"C-postdb: {job_type} effect_confirmed under enforced GUC")
    conn.commit()
    assert fixture.unit_state(conn, unit_id)[0] == COMPLETE
