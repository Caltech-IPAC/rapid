"""Contract tests: the refusal battery for R5's enforcement layer (migrations
076/077/078 — campaign ruling R5, task brief C2).

**THESE SKIP WHERE MIGRATION 076/077 IS ABSENT.** 076/077/078 are, at the
time this file is written, freshly rehearsed and pinned but not yet the
oldest-deployed baseline every contract run assumes — the probe
(`fixture.has_function(conn, "transition_work_unit")`) asks the catalog,
never assumes, matching every other DRAFT-gated file in this tier.

WHAT THIS FILE COVERS, per the task brief's own enumeration, and where:

  * direct table access            — section 1 (078's revoke, catalog +
                                      behavioural)
  * every old definer function     — section 2 (`cancel_work_units`,
                                      `retry_parked_attempts` still transition
                                      legally through 076's trigger layer —
                                      076's own header proves this safe; this
                                      section is the acceptance evidence for
                                      that proof, not a new capability)
  * writer spoofing                — section 3. Two findings, not one:
                                      076's IMMEDIATE writer check cannot
                                      fire through `derived.
                                      transition_work_unit`'s own
                                      UPDATE-then-INSERT order (a real gap,
                                      documented and pinned as CURRENT
                                      behaviour, not silently patched over
                                      with a wrong expectation), and
                                      `WorkUnitWriter`'s own Python
                                      `WrongWriterForTransition` check —
                                      unaffected by the SQL-layer gap, and
                                      what every production caller actually
                                      goes through — IS the refusal that
                                      holds today.
  * mismatched attempts            — section 4 (completion-acceptance clause
                                      (b): the deciding attempt does not
                                      belong to the unit being completed)
  * unconsumed terminal sequences  — section 4 (clause (c):
                                      terminal_record_sequence < 1)
  * supersession cycles            — section 5 (076's multi-row acyclicity
                                      walk, `work_units_check_supersession`)
  * the withheld case both ways    — section 4 (clause (d): a CONSUMED
                                      withheld disposition passes; an
                                      UNCONSUMED one — no registered_at, no
                                      effect_confirmed, and no consumed
                                      terminal_record_sequence — still fails)

**SECTION 4 SETS THE GUC LOCALLY, INSIDE ITS OWN TRANSACTION, EVERY TIME.**
076's completion-acceptance clause is gated OFF by default
(`rapid.enforce_completion_acceptance`, current_setting default 'off') —
076's own header explains why: the reconciler's current image can
legitimately close a unit before registration/effect-confirmation has run
for its deciding attempt, so enforcing the clause unconditionally would
break production. This file does NOT flip that default: every section-4 test
that needs the clause ENFORCED does `SET LOCAL rapid.enforce_completion_
acceptance = 'on'` as the first statement of its own transaction — `SET
LOCAL` reverts automatically at COMMIT or ROLLBACK, so the session-level
(and therefore database-level) default is never durably changed by running
this suite. This is the same discipline `test_alert_outbox_grants.py` uses
for `SET LOCAL ROLE`.

**"OLD DEFINER FUNCTION" MEANS WHAT 076's OWN SAFETY ANALYSIS NAMES.** 076's
header §4(i) lists exactly two pre-existing SECURITY DEFINER functions that
write `work_units.state` outside `WorkUnitWriter`: `derived.
cancel_work_units` (046) and `derived.retry_parked_attempts` (031 family).
Both hardcode `writer = 'mutation_api'` on their own `unit_events` INSERT —
verified by 076's header, re-verified here behaviourally rather than by
re-reading their bodies a second time. Section 2 is the acceptance test for
that claim: both functions still transition units after 076 lands, proving
the trigger's edge table is a superset of what they issue, not a narrower
one that would strand them.
"""

import psycopg2
import pytest

from pipeline.contract import fixture
from pipeline.intent import cancellation
from pipeline.intent.writer import (
    BLOCKED, COMPLETE, FAILED, QUARANTINED, READY, SUBMITTED,
    WRITER_MUTATION_API, WRITER_ORCHESTRATOR, WRITER_RECONCILER,
    WRITER_VALIDATION_INGEST, WorkUnitIdentity, WorkUnitWriter)

pytestmark = pytest.mark.usefixtures("_requires_r5")


@pytest.fixture
def _requires_r5(conn):
    """Skip unless 076/077 are applied. Probed, never assumed."""
    if not fixture.has_function(conn, "transition_work_unit"):
        pytest.skip(
            "migration 077 (derived.transition_work_unit and siblings) is "
            "not applied; the R5 refusal battery needs it and skips "
            "cleanly without it")


def _role_exists(conn, role):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
        return cur.fetchone() is not None


def _table_priv(conn, role, table, privilege):
    with conn.cursor() as cur:
        cur.execute("SELECT has_table_privilege(%s, %s, %s)",
                    [role, table, privilege])
        return cur.fetchone()[0]


def _as_role_expect_insufficient_privilege(conn, role, statement, params=None):
    """Attempt one statement AS `role`, inside its own SAVEPOINT.

    Same mechanism `test_alert_outbox_grants.py` establishes: the contract
    connection is a superuser, which may `SET ROLE` to any role regardless
    of membership, and `SET LOCAL ROLE` scopes the impersonation to the
    current transaction so it unwinds at COMMIT/ROLLBACK with nothing left
    to undo.
    """
    from psycopg2 import sql

    with conn.cursor() as cur:
        cur.execute("SAVEPOINT role_attempt")
        cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
    try:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute(statement, params)
    finally:
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT role_attempt")


def _expect_ra001(conn, statement, params=None, match=None):
    """Attempt one statement and assert 076/077 refuse it with RA001.

    A SAVEPOINT-scoped attempt, mirroring `test_operator_grants.py`'s own
    convention of asserting the SQLSTATE rather than message text — RA001
    (`pipeline.intent.errors`, `pipeline.operatorctl.contract`) is the
    custom code 077's functions and 076's triggers both raise for every
    refusal, so this is the one predicate every case in this file's
    refusal battery can share.
    """
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT refusal_attempt")
    try:
        with pytest.raises(psycopg2.Error) as raised:
            with conn.cursor() as cur:
                cur.execute(statement, params)
        assert getattr(raised.value, "pgcode", None) == "RA001", (
            f"expected SQLSTATE RA001, got {getattr(raised.value, 'pgcode', None)}: "
            f"{raised.value}")
        if match is not None:
            assert match in str(raised.value), (
                f"expected {match!r} in the error, got: {raised.value}")
    finally:
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT refusal_attempt")


# ============================================================================
# 1. DIRECT TABLE ACCESS — 078's revoke
# ============================================================================

def test_rapid_pipeline_write_holds_no_raw_update_on_work_units(conn):
    """The catalog fact 078 exists to establish."""
    if not _role_exists(conn, "rapid_pipeline_write"):
        pytest.skip("rapid_pipeline_write is not present in this database")
    assert _table_priv(conn, "rapid_pipeline_write", "work_units",
                       "UPDATE") is False, (
        "rapid_pipeline_write holds raw UPDATE on work_units; 078 should "
        "have revoked it once the writer switch (C1) shipped")


def test_rapid_pipeline_write_can_still_select_and_insert(conn):
    """078 touches UPDATE only — 036's SELECT/INSERT grants are untouched,
    per 078's own header ("this migration touches only UPDATE")."""
    if not _role_exists(conn, "rapid_pipeline_write"):
        pytest.skip("rapid_pipeline_write is not present in this database")
    assert _table_priv(conn, "rapid_pipeline_write", "work_units",
                       "SELECT") is True
    assert _table_priv(conn, "rapid_pipeline_write", "work_units",
                       "INSERT") is True


def test_a_raw_update_is_really_refused_as_the_pipeline_writer(conn):
    """Behavioural counterpart: PostgreSQL itself refuses the raw UPDATE.

    Mirrors `test_alert_outbox_grants.py`'s split between catalog and
    behavioural tests — a passing catalog test with a failing behavioural
    one would mean the grant map lies.
    """
    if not _role_exists(conn, "rapid_pipeline_write"):
        pytest.skip("rapid_pipeline_write is not present in this database")
    unit_id = fixture.create_unit(conn, fixture.scope("grants-raw-update"))
    conn.commit()
    try:
        _as_role_expect_insufficient_privilege(
            conn, "rapid_pipeline_write",
            "UPDATE work_units SET state = 'submitted' WHERE work_unit_id = %s",
            [unit_id])
    finally:
        conn.rollback()


def test_rapid_pipeline_write_can_execute_the_three_functions(conn):
    """The route 078's revoke leaves in place, confirmed by grant."""
    if not _role_exists(conn, "rapid_pipeline_write"):
        pytest.skip("rapid_pipeline_write is not present in this database")
    for function, nargs in (("transition_work_unit", 8),
                            ("amend_blocked_reason", 2),
                            ("supersede_unit", 4)):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT p.oid::regprocedure::text FROM pg_proc p"
                "  JOIN pg_namespace n ON n.oid = p.pronamespace"
                " WHERE n.nspname = 'derived' AND p.proname = %s"
                "   AND p.pronargs = %s", [function, nargs])
            row = cur.fetchone()
        assert row is not None, f"derived.{function}/{nargs} not found"
        with conn.cursor() as cur:
            cur.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                        ["rapid_pipeline_write", row[0]])
            assert cur.fetchone()[0] is True, (
                f"rapid_pipeline_write lacks EXECUTE on derived.{function}")


# ============================================================================
# 2. EVERY OLD DEFINER FUNCTION — cancel_work_units, retry_parked_attempts
#    still transition legally through 076's trigger layer
# ============================================================================

def test_cancel_work_units_still_transitions_through_the_trigger_layer(conn):
    """046's function, unchanged, still lands a ready->cancelled transition
    now that 076's trigger sees every UPDATE it issues.

    076's header §4(i) verified `cancel_work_units` hardcodes writer=
    'mutation_api' on its own unit_events INSERT and writes only edges the
    DAG admits for that writer — this is the behavioural proof of that
    claim, not a fresh capability.
    """
    if not fixture.has_function(conn, "cancel_work_units"):
        pytest.skip("DRAFT migration 046 (derived.cancel_work_units) is not "
                    "applied; skipping cleanly")
    execute = fixture.executor(conn)
    unit_id = fixture.create_unit(conn, fixture.scope("old-definer-cancel"))
    conn.commit()

    result = cancellation.cancel_work_units(
        execute, [unit_id], reason="C2: old-definer regression", dry_run=False)
    conn.commit()

    assert result["units_cancelled"] == 1
    assert fixture.unit_state(conn, unit_id)[0] == "cancelled"
    events = fixture.unit_events(conn, unit_id)
    assert (READY, "cancelled", WRITER_MUTATION_API) in events


def test_retry_parked_attempts_still_transitions_through_the_trigger_layer(
        conn):
    """040/053/061/062/063's function, unchanged, still returns a failed
    unit to ready now that 076's trigger sees its UPDATE too."""
    if not fixture.has_function(conn, "retry_parked_attempts"):
        pytest.skip("derived.retry_parked_attempts is not applied; "
                    "skipping cleanly")
    # FAILED is reachable only via submitted -> failed (two hops from
    # creation) — fixture.create_unit only reaches states directly
    # adjacent to READY, so this is written out rather than reused.
    execute = fixture.executor(conn)
    writer = WorkUnitWriter(execute)
    unit_id = writer.create_work_unit(
        _identity(fixture.scope("old-definer-retry")),
        writer=WRITER_VALIDATION_INGEST, state=READY)
    writer.transition_unit(unit_id, READY, SUBMITTED, writer=WRITER_ORCHESTRATOR)
    writer.transition_unit(unit_id, SUBMITTED, FAILED, writer=WRITER_RECONCILER)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT derived.retry_parked_attempts(%s, %s, %s, %s)",
            [f"c2-{fixture.RUN_TAG}", "C2: old-definer regression", 50, False])
        cur.fetchone()
    conn.commit()

    assert fixture.unit_state(conn, unit_id)[0] == READY
    events = fixture.unit_events(conn, unit_id)
    assert (FAILED, READY, WRITER_MUTATION_API) in events


# ============================================================================
# 3. WRITER SPOOFING — the five mutation-api-only edges
# ============================================================================
#
# **FINDING, STATED HERE RATHER THAN FORCED INTO A PASSING TEST.** 076's own
# writer-restriction clause reads the ACCOMPANYING `unit_events` row's
# `writer` column, matched by `work_unit_id`/`to_state`/`occurred_at`
# (076:480-506) — and its own comment explains why a MISSING match is not
# itself a refusal: "writer.py's own sequence is UPDATE work_units THEN
# INSERT unit_events... at the moment THIS trigger fires... the event row
# often does not exist yet". `derived.transition_work_unit` (077) follows
# the IDENTICAL UPDATE-then-INSERT order inside ONE function call (verified
# by reading 077:172/192 directly), so for EVERY call through it, the row
# trigger fires strictly BEFORE the accompanying `unit_events` INSERT —
# `v_actual_writer` is therefore ALWAYS NULL at the moment the immediate
# check runs, and the `v_actual_writer IS NOT NULL AND v_actual_writer <>
# v_required_writer` condition can never be TRUE for this call pattern. The
# DEFERRED coupling trigger (076 §5) does not compensate: it checks that A
# matching `unit_events` row EXISTS, never which `writer` it carries. NET
# EFFECT: calling `derived.transition_work_unit` directly with `p_writer`
# set to any of the four vocabulary values — including a value 077's own
# vocabulary check accepts but this edge does not require — is NOT refused
# by 076/077 for the five mutation-only edges. `test_
# a_mutation_only_edge_is_NOT_refused_at_the_sql_layer_a_known_gap` below is
# the acceptance evidence for this finding, not a wrong expectation.
#
# THE ACTUAL ENFORCEMENT POINT TODAY IS PYTHON. `WorkUnitWriter.
# transition_unit`'s own `WrongWriterForTransition` check (unchanged by C1 —
# see that commit) runs BEFORE any SQL, for every caller that goes through
# `WorkUnitWriter` rather than calling `derived.transition_work_unit`
# directly — which is every production call site in this repo (grepped:
# `pipeline.registration.consumer`, `pipeline.reconciler.service`,
# `submission.blocked`, `pipeline.intent.cancellation`'s own writer=
# 'mutation_api' hardcoding). `test_writer_spoofing_is_refused_by_
# workunitwriter_in_python` is that coverage — the REAL refusal a
# production caller hits, at the layer that actually enforces it.

MUTATION_ONLY_EDGES = [
    (FAILED, READY),
    (QUARANTINED, READY),
    (BLOCKED, "cancelled"),
    (READY, "cancelled"),
    (SUBMITTED, "cancelled"),
]


def _unit_in_state(conn, scope_name, state):
    """A work unit in `state`, reached via a LEGAL multi-hop path through
    the real graph — `fixture.create_unit` only reaches states directly
    adjacent to READY, which does not cover FAILED or BLOCKED.

    THE GRAPH HAS NO `(READY, BLOCKED)` EDGE — only `(BLOCKED, READY)` and
    `(SUBMITTED, BLOCKED)` touch `blocked` at all (`pipeline.intent.writer.
    _TRANSITION_GRAPH`). So BLOCKED is reached the same two-hop way as
    SUBMITTED/FAILED (via submitted), not grouped with QUARANTINED (which
    genuinely IS reachable directly from READY).
    """
    execute = fixture.executor(conn)
    writer = WorkUnitWriter(execute)
    unit_id = writer.create_work_unit(
        _identity(scope_name), writer=WRITER_VALIDATION_INGEST, state=READY)
    conn.commit()
    if state == READY:
        return unit_id
    if state == QUARANTINED:
        writer.transition_unit(unit_id, READY, state, writer=WRITER_ORCHESTRATOR)
        conn.commit()
        return unit_id
    # SUBMITTED, FAILED, BLOCKED: all pass through submitted first.
    writer.transition_unit(unit_id, READY, SUBMITTED, writer=WRITER_ORCHESTRATOR)
    conn.commit()
    if state == SUBMITTED:
        return unit_id
    if state == BLOCKED:
        writer.transition_unit(
            unit_id, SUBMITTED, state, writer=WRITER_RECONCILER,
            blocked_reason="C2 fixture: parked for a mutation-only-edge test")
        conn.commit()
        return unit_id
    writer.transition_unit(unit_id, SUBMITTED, state, writer=WRITER_RECONCILER)
    conn.commit()
    return unit_id


@pytest.mark.parametrize("from_state,to_state", MUTATION_ONLY_EDGES)
def test_a_mutation_only_edge_is_NOT_refused_at_the_sql_layer_a_known_gap(
        conn, from_state, to_state):
    """DOCUMENTS THE GAP explained above: a raw call to
    `derived.transition_work_unit` with a spoofed (non-mutation_api) writer
    on one of the five operator-only edges SUCCEEDS, because 076's
    immediate writer check cannot see a `unit_events` row 077's own
    UPDATE-then-INSERT order has not written yet at trigger-fire time.
    This test asserts the CURRENT behaviour precisely so a future fix (an
    AFTER/DEFERRED writer check, or restructuring 077 to INSERT before
    UPDATE) has a red test to turn green — it is acceptance evidence for a
    finding, not approval of the gap.
    """
    unit_id = _unit_in_state(
        conn, fixture.scope(f"spoof-gap-{from_state}-{to_state}"), from_state)
    conn.commit()
    spoofed_writer = WRITER_VALIDATION_INGEST
    with conn.cursor() as cur:
        cur.execute(
            "SELECT derived.transition_work_unit(%s, %s, %s, %s, %s, %s, %s, %s)",
            [unit_id, from_state, to_state, spoofed_writer,
             ("spoofed" if to_state == "cancelled" else None),
             "C2: documents the SQL-layer writer-spoof gap", None, True])
    conn.commit()
    # The transition LANDED, under the wrong writer, unrefused — the gap.
    assert fixture.unit_state(conn, unit_id)[0] == to_state
    events = fixture.unit_events(conn, unit_id)
    assert (from_state, to_state, spoofed_writer) in events, (
        "expected the spoofed writer to be exactly what landed, since "
        "nothing rewrites it — if this assertion ever fails because the "
        "call was refused instead, the SQL-layer gap this test documents "
        "has been closed and this test should be rewritten to expect RA001")


def test_writer_spoofing_is_refused_by_workunitwriter_in_python(conn):
    """The REAL refusal every production caller actually hits: Python's own
    `WrongWriterForTransition`, in `WorkUnitWriter.transition_unit`, before
    any SQL is issued. One edge (failed->ready) stands for all five — the
    check is uniform over the edge/writer pair, not edge-specific."""
    from pipeline.intent.writer import WrongWriterForTransition

    unit_id = _unit_in_state(conn, fixture.scope("spoof-python-refusal"), FAILED)
    conn.commit()
    execute = fixture.executor(conn)
    writer = WorkUnitWriter(execute)
    with pytest.raises(WrongWriterForTransition):
        writer.transition_unit(unit_id, FAILED, READY,
                               writer=WRITER_VALIDATION_INGEST)
    # Nothing was issued to the database — the refusal is in Python.
    assert fixture.unit_state(conn, unit_id)[0] == FAILED


@pytest.mark.parametrize("from_state,to_state", MUTATION_ONLY_EDGES)
def test_the_same_edge_succeeds_with_the_correct_writer(
        conn, from_state, to_state):
    """The control for the gap test above: the same edge, called the same
    raw way, succeeds when the writer IS mutation_api — proving 076 does
    not refuse the EDGE itself for any reason, only ever the (currently
    unenforced) writer pairing."""
    unit_id = _unit_in_state(
        conn, fixture.scope(f"spoof-control-{from_state}-{to_state}"),
        from_state)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT derived.transition_work_unit(%s, %s, %s, %s, %s, %s, %s, %s)",
            [unit_id, from_state, to_state, WRITER_MUTATION_API,
             ("control" if to_state == "cancelled" else None),
             "spoof control", None, True])
    conn.commit()
    assert fixture.unit_state(conn, unit_id)[0] == to_state


# ============================================================================
# 4. COMPLETION-ACCEPTANCE — mismatched attempts, unconsumed terminal
#    sequences, and the withheld case both ways. GUC enforced LOCALLY,
#    inside each test's own transaction (see module docstring).
# ============================================================================

def _identity(scope_name):
    return WorkUnitIdentity(
        job_type=fixture.JOB_TYPE, input_scope=scope_name,
        operational_class="prompt-processing",
        definition_version=fixture.DEFINITION_VERSION)


def _make_terminal_attempt(conn, work_unit_id, *, registered=False,
                           product_disposition="published",
                           terminal_record_sequence=1):
    """A `terminal_after_start` attempt bound to `work_unit_id`, satisfying
    migration 014's full sixteen-column CHECK (`attempts_state_terminal_
    after_start_check`) — copied from `pipeline.contract.test_effect_
    completion._make_terminal_attempt`'s own INSERT shape (that module's
    docstring: "`fixture.make_completed_attempt` is the closest existing
    helper but takes no `work_unit_id`... this is that same INSERT shape
    ... with both parameterized"), with `registered`/`registered_record_
    sequence` added since section 4's completion-acceptance tests need
    clause (d)'s `registered_at` branch as well as the `product_disposition`
    branch. Not reused from that module directly — a private duplicate,
    same reasoning `pipeline.intent.writer`'s own docstring gives for
    repeating `Executor` rather than importing a sibling's: this file has
    no other dependency on `test_effect_completion`'s internals, and
    importing a `test_*` module from another `test_*` module is not this
    tier's convention.

    `terminal_record_sequence` DEFAULTS TO 1 (consumed), not NULL — the
    CHECK requires it non-NULL for `terminal_after_start` at schema_version
    >= 2 unconditionally, unrelated to 076's GUC-gated clause. A caller
    wanting the UNCONSUMED case passes `terminal_record_sequence=0`
    (`attempts_terminal_record_sequence_check` permits NULL or >= 0, so 0
    is the realistic "not yet consumed" shape a terminal_after_start row
    can actually carry — NULL itself is refused by the state CHECK).
    """
    import uuid

    logical_job_id, run_id = fixture.make_logical_job(conn)
    tag = uuid.uuid4().hex[:8]
    registered_at_expr = "now()" if registered else "NULL"
    registered_sequence = 1 if registered else None
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
            "   product_disposition,"
            "   binding_job_definition_arn, binding_image_digest,"
            "   binding_manifest_checksum, terminal_record_key,"
            "   terminal_record_sequence, work_unit_id,"
            f"   registered_at, registered_record_sequence)"
            " VALUES (%s, %s, %s, 'terminal_after_start',"
            "         now(), now(), now(), now(),"
            "         %s, 'SUCCEEDED', 0,"
            "         %s, 'sha256:' || %s, 1,"
            "         'sha256:' || %s, 0, 'success',"
            "         %s,"
            "         %s, 'sha256:' || %s,"
            "         'sha256:' || %s, %s,"
            "         %s, %s,"
            f"        {registered_at_expr}, %s)"
            " RETURNING attempt_id",
            [run_id, schema_version, logical_job_id,
             f"job-{tag}", f"sha-{tag}", tag, tag,
             product_disposition,
             f"arn:aws:batch:us-east-1:account:job-definition/f-{tag}:1",
             tag, tag,
             f"records/{fixture.RUN_TAG}/{tag}.json",
             terminal_record_sequence, work_unit_id,
             registered_sequence])
        return cur.fetchone()[0]


def _submitted_unit_with_attempt(conn, scope_name, *, registered=False,
                                 product_disposition="published",
                                 terminal_record_sequence=1):
    """A `submitted` work unit and one attempt row scoped to it — the shape
    `_close_work_unit`'s real caller always has (an attempt belonging to
    the unit it is about to complete). See `_make_terminal_attempt` for
    the CHECK-constraint reasoning behind the defaults.
    """
    execute = fixture.executor(conn)
    writer = WorkUnitWriter(execute)
    unit_id = writer.create_work_unit(
        _identity(scope_name), writer=WRITER_VALIDATION_INGEST, state=READY)
    writer.transition_unit(unit_id, READY, SUBMITTED, writer=WRITER_ORCHESTRATOR)
    conn.commit()
    attempt_id = _make_terminal_attempt(
        conn, unit_id, registered=registered,
        product_disposition=product_disposition,
        terminal_record_sequence=terminal_record_sequence)
    conn.commit()
    return unit_id, attempt_id


def _complete_with_guc_enforced(conn, unit_id, attempt_id, reason):
    """Enforce the GUC for exactly this attempt, inside its own SAVEPOINT,
    then attempt submitted -> complete for `attempt_id`."""
    import json

    with conn.cursor() as cur:
        cur.execute("SET LOCAL rapid.enforce_completion_acceptance = 'on'")
        cur.execute(
            "SELECT derived.transition_work_unit(%s, %s, %s, %s, %s, %s, %s, %s)",
            # `p_detail` is jsonb — psycopg2 does not adapt a bare dict
            # (`pipeline.intent.writer._as_jsonb`'s own reasoning, found
            # live on the real driver: "can't adapt type 'dict'"), so this
            # is serialized text, matching `_as_jsonb`'s own convention.
            [unit_id, SUBMITTED, COMPLETE, WRITER_RECONCILER, None, reason,
             json.dumps({"deciding_attempt_id": attempt_id}), True])


def test_mismatched_attempt_is_refused_when_enforced(conn):
    """Clause (b): the deciding attempt named in `detail` does not belong to
    THIS unit. A copy-paste/wrong-id bug, not a hypothetical attacker — 076's
    own header names this exact risk.
    """
    unit_id, _own_attempt = _submitted_unit_with_attempt(
        conn, fixture.scope("mismatched-attempt"), registered=True)
    _other_unit, other_attempt_id = _submitted_unit_with_attempt(
        conn, fixture.scope("mismatched-attempt-other"), registered=True)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SAVEPOINT mismatched")
    try:
        with pytest.raises(psycopg2.Error) as raised:
            _complete_with_guc_enforced(
                conn, unit_id, other_attempt_id, "C2: mismatched attempt")
        assert getattr(raised.value, "pgcode", None) == "RA001"
        assert "does not satisfy the acceptance boundary" in str(raised.value)
    finally:
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT mismatched")
    conn.rollback()


def test_unconsumed_terminal_sequence_is_refused_when_enforced(conn):
    """Clause (c): the deciding attempt's terminal_record_sequence is 0
    (never consumed — `attempts_terminal_record_sequence_check` requires
    NULL or >= 0, and `terminal_after_start`'s own CHECK requires NON-NULL
    at schema_version >= 2, so 0 rather than NULL is the realistic
    "unconsumed" shape a terminal_after_start row can actually carry) —
    even though it IS registered, satisfying (d) alone is not enough; (c)
    and (d) are both required."""
    unit_id, attempt_id = _submitted_unit_with_attempt(
        conn, fixture.scope("unconsumed-sequence"), registered=True,
        terminal_record_sequence=0)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SAVEPOINT unconsumed")
    try:
        with pytest.raises(psycopg2.Error) as raised:
            _complete_with_guc_enforced(
                conn, unit_id, attempt_id, "C2: unconsumed sequence")
        assert getattr(raised.value, "pgcode", None) == "RA001"
        assert "does not satisfy the acceptance boundary" in str(raised.value)
    finally:
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT unconsumed")
    conn.rollback()


def test_registered_and_consumed_attempt_completes_when_enforced(conn):
    """The control case for clauses (b)/(c)/(d): registered_at set AND
    terminal_record_sequence >= 1 AND the attempt belongs to the unit ->
    the completion succeeds even with the GUC on."""
    unit_id, attempt_id = _submitted_unit_with_attempt(
        conn, fixture.scope("acceptance-control"), registered=True,
        terminal_record_sequence=1)
    conn.commit()

    _complete_with_guc_enforced(conn, unit_id, attempt_id, "C2: control")
    conn.commit()
    assert fixture.unit_state(conn, unit_id)[0] == COMPLETE


def test_withheld_consumed_completes_when_enforced(conn):
    """THE WITHHELD CASE, FIRST HALF: a CONSUMED withheld disposition IS
    deliberate acceptance and must PASS clause (d) — `product_disposition
    = 'withheld'` alone satisfies the acceptance half of the OR, and with
    terminal_record_sequence >= 1 (consumed) the completion succeeds.
    `registered_at` is deliberately NOT set here — withheld is the OTHER
    branch of clause (d)'s OR, and this test is what would catch a
    regression that required registered_at unconditionally.
    """
    unit_id, attempt_id = _submitted_unit_with_attempt(
        conn, fixture.scope("withheld-consumed"), registered=False,
        product_disposition="withheld", terminal_record_sequence=1)
    conn.commit()

    _complete_with_guc_enforced(conn, unit_id, attempt_id, "C2: withheld consumed")
    conn.commit()
    assert fixture.unit_state(conn, unit_id)[0] == COMPLETE


def test_withheld_unconsumed_is_refused_when_enforced(conn):
    """THE WITHHELD CASE, SECOND HALF: an UNCONSUMED withheld disposition
    must NOT pass the completion trigger. `product_disposition = 'withheld'`
    alone, with terminal_record_sequence=0 (unconsumed — see
    test_unconsumed_terminal_sequence_is_refused_when_enforced on why 0
    rather than NULL) and NO registered_at, fails clause (c) even though
    clause (d)'s OR is satisfied by the withheld value — both (c) and (d)
    are required together, and this is the case that would catch a
    regression treating `withheld` as sufficient on its own, without
    consumption.
    """
    unit_id, attempt_id = _submitted_unit_with_attempt(
        conn, fixture.scope("withheld-unconsumed"), registered=False,
        product_disposition="withheld", terminal_record_sequence=0)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SAVEPOINT withheld_unconsumed")
    try:
        with pytest.raises(psycopg2.Error) as raised:
            _complete_with_guc_enforced(
                conn, unit_id, attempt_id, "C2: withheld unconsumed")
        assert getattr(raised.value, "pgcode", None) == "RA001"
        assert "does not satisfy the acceptance boundary" in str(raised.value)
    finally:
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT withheld_unconsumed")
    conn.rollback()


def test_completion_is_unenforced_by_default(conn):
    """The GUC's OWN default: with no `SET LOCAL`, a completion with NO
    deciding_attempt_id at all still succeeds — proving this suite's other
    section-4 tests are exercising an OPT-IN check, not the deployed
    behaviour, and confirming 076's header's own safety claim that the
    running image is unaffected by clause (d) until the GUC is flipped for
    real.
    """
    unit_id, _attempt_id = _submitted_unit_with_attempt(
        conn, fixture.scope("acceptance-unenforced"))
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT derived.transition_work_unit(%s, %s, %s, %s, %s, %s, %s, %s)",
            [unit_id, SUBMITTED, COMPLETE, WRITER_RECONCILER, None,
             "C2: unenforced default", None, True])
    conn.commit()
    assert fixture.unit_state(conn, unit_id)[0] == COMPLETE


# ============================================================================
# 5. SUPERSESSION CYCLES — 076's multi-row acyclicity walk
# ============================================================================

def test_a_two_hop_cycle_is_refused(conn):
    """A -> B, then B -> A: 036's single-row CHECK cannot see this (neither
    row's own `superseded_by_unit_id` equals ITS OWN id), but 076's walk
    does — the exact gap 076's header names ("A -> B -> C -> A across three
    DIFFERENT rows is not something a single-row CHECK can see at all").
    This is the 2-hop case; the 3-hop case below is the header's own
    example, checked separately since a walk that catches a 2-cycle but
    not a 3-cycle would be a real, distinct defect.
    """
    execute = fixture.executor(conn)
    writer = WorkUnitWriter(execute)
    unit_a = writer.create_work_unit(
        _identity(fixture.scope("cycle2-a")), writer=WRITER_VALIDATION_INGEST)
    unit_b = writer.create_work_unit(
        _identity(fixture.scope("cycle2-b")), writer=WRITER_VALIDATION_INGEST)
    conn.commit()

    writer.supersede_unit(unit_a, unit_b, writer=WRITER_MUTATION_API)
    conn.commit()

    _expect_ra001(
        conn,
        "SELECT derived.supersede_unit(%s, %s, %s, %s)",
        [unit_b, unit_a, WRITER_MUTATION_API, "C2: close the 2-cycle"],
        match="cycle")


def test_a_three_hop_cycle_is_refused(conn):
    """A -> B -> C -> A: 076's header's own worked example."""
    execute = fixture.executor(conn)
    writer = WorkUnitWriter(execute)
    unit_a = writer.create_work_unit(
        _identity(fixture.scope("cycle3-a")), writer=WRITER_VALIDATION_INGEST)
    unit_b = writer.create_work_unit(
        _identity(fixture.scope("cycle3-b")), writer=WRITER_VALIDATION_INGEST)
    unit_c = writer.create_work_unit(
        _identity(fixture.scope("cycle3-c")), writer=WRITER_VALIDATION_INGEST)
    conn.commit()

    writer.supersede_unit(unit_a, unit_b, writer=WRITER_MUTATION_API)
    writer.supersede_unit(unit_b, unit_c, writer=WRITER_MUTATION_API)
    conn.commit()

    _expect_ra001(
        conn,
        "SELECT derived.supersede_unit(%s, %s, %s, %s)",
        [unit_c, unit_a, WRITER_MUTATION_API, "C2: close the 3-cycle"],
        match="cycle")


def test_a_linear_chain_is_not_a_cycle(conn):
    """The control case: A -> B -> C, no closing edge, succeeds — the walk
    must refuse an actual cycle, not merely refuse a chain longer than one
    hop."""
    execute = fixture.executor(conn)
    writer = WorkUnitWriter(execute)
    unit_a = writer.create_work_unit(
        _identity(fixture.scope("chain-a")), writer=WRITER_VALIDATION_INGEST)
    unit_b = writer.create_work_unit(
        _identity(fixture.scope("chain-b")), writer=WRITER_VALIDATION_INGEST)
    unit_c = writer.create_work_unit(
        _identity(fixture.scope("chain-c")), writer=WRITER_VALIDATION_INGEST)
    conn.commit()

    writer.supersede_unit(unit_a, unit_b, writer=WRITER_MUTATION_API)
    writer.supersede_unit(unit_b, unit_c, writer=WRITER_MUTATION_API)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT superseded_by_unit_id FROM work_units"
            " WHERE work_unit_id = %s", [unit_a])
        assert cur.fetchone()[0] == unit_b
        cur.execute(
            "SELECT superseded_by_unit_id FROM work_units"
            " WHERE work_unit_id = %s", [unit_b])
        assert cur.fetchone()[0] == unit_c


def test_reassigning_an_already_superseded_pointer_is_refused(conn):
    """Set-once, restated at the trigger for every writer — including this
    raw SQL call, which bypasses `supersede_unit`'s own CAS entirely by
    going straight at the column.
    """
    execute = fixture.executor(conn)
    writer = WorkUnitWriter(execute)
    unit_a = writer.create_work_unit(
        _identity(fixture.scope("set-once-a")), writer=WRITER_VALIDATION_INGEST)
    unit_b = writer.create_work_unit(
        _identity(fixture.scope("set-once-b")), writer=WRITER_VALIDATION_INGEST)
    unit_c = writer.create_work_unit(
        _identity(fixture.scope("set-once-c")), writer=WRITER_VALIDATION_INGEST)
    conn.commit()

    writer.supersede_unit(unit_a, unit_b, writer=WRITER_MUTATION_API)
    conn.commit()

    _expect_ra001(
        conn,
        "UPDATE work_units SET superseded_by_unit_id = %s"
        " WHERE work_unit_id = %s",
        [unit_c, unit_a],
        match="set-once pointer")
