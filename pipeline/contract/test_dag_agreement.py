"""Contract test: the work-unit and campaign transition DAGs live TWICE —
once as Python data (`pipeline.intent.writer._TRANSITION_GRAPH` and
`_CAMPAIGN_GRAPH`) and once as SQL (migration 076's two `IF/ELSIF` trigger
chains, `derived.work_units_check_transition()` and
`derived.campaigns_check_transition()`) — and nothing before this file kept
the two in agreement. 076's own header says the SQL was DERIVED from the
Python graphs at the time it was written ("every edge below is taken
verbatim from `pipeline.intent.writer._TRANSITION_GRAPH`... cross-checked
against the two CHECK constraints"), which is exactly the kind of one-time
agreement that silently drifts the next time either side is edited alone.

**THIS TEST REFLECTS THE PYTHON GRAPHS RATHER THAN HARDCODING THE EDGE
LIST.** It imports `_TRANSITION_GRAPH`/`_CAMPAIGN_GRAPH` and the state
vocabularies directly and derives both "every declared edge" and "every
non-edge in the state-space complement" from them at collection time. A
test that copied the edge list as a literal would agree with whichever
side it was copied FROM by construction and could drift from the OTHER
side exactly as silently as the two originals can drift from each other —
the failure mode this file exists to catch.

**WHAT "AGREEMENT" MEANS HERE, PRECISELY.** For work units: every edge
`_TRANSITION_GRAPH` declares must be ACCEPTED by the trigger (a state
actually lands, an event row is recorded) when driven through the writer
identity the edge allows; every ordered pair NOT in `_TRANSITION_GRAPH`
(excluding self-transitions, which 076's trigger explicitly no-ops rather
than refuses — `IF NEW.state IS NOT DISTINCT FROM OLD.state THEN RETURN
NEW`) must be REFUSED with SQLSTATE RA011 (invariant violation,
per migration 086). Campaigns: the same two
properties over `_CAMPAIGN_GRAPH`.

**WRITER-GATE SEMANTICS ARE HONORED, NOT RE-TESTED.** Five work-unit edges
are mutation_api-only. This file drives every EDGE through
`WorkUnitWriter.transition_unit` with a writer the edge actually admits
(`WRITER_MUTATION_API` for the five gated edges, `WRITER_ORCHESTRATOR` for
the rest) — the real production entry point, Python pre-check and all —
because accepting-the-declared-edges is what this file asserts, not
writer enforcement (that is `test_work_unit_grants.py`'s job, including
its on-record SQL-layer writer-spoof gap). For the REFUSED half (every
non-edge), the trigger's DAG check runs before its writer check in 076's
own IF/ELSIF chain (`v_edge_ok` is decided first; the writer branch only
runs `IF v_edge_ok`), so any vocabulary-legal writer reaches the DAG
refusal regardless of which writer is passed — driven here via
`derived.transition_work_unit` directly (bypassing
`WorkUnitWriter.transition_unit`'s OWN Python-side graph check, which
would refuse the very same call before any SQL ran and prove nothing
about the trigger) so the refusal observed is genuinely the SQL layer's,
not Python's duplicate of it.

**COMPLETION-ACCEPTANCE IS A SEPARATE, ORTHOGONAL GATE, SATISFIED, NOT
BYPASSED.** Migration 083 sets `rapid.enforce_completion_acceptance = 'on'`
at the database level for every connection opened after it, so the one
work-unit edge landing on COMPLETE (`submitted -> complete`) must carry a
`deciding_attempt_id` whose attempt satisfies 080's boundary (belongs to
the unit, `terminal_record_sequence >= 1`, and `registered_at` set or
`product_disposition` in `effect_confirmed`/`withheld`) or 080's DEFERRED
trigger refuses it for a reason that has nothing to do with DAG legality —
a false failure this file must not produce. Non-edges landing on COMPLETE
need no such care: 080 moved the completion-acceptance clause into the
DEFERRED trigger, a separate object from the immediate DAG-check trigger
this file's refusal assertions exercise, so an illegal edge into COMPLETE
is refused by the immediate trigger before any UPDATE lands and the
deferred trigger never runs. `_make_terminal_attempt` below is adapted
from `test_work_unit_grants.py`'s section-4 fixture (same INSERT shape,
same reasoning); not imported, matching this tier's own convention of a
private duplicate per file rather than a `test_*`-importing-`test_*`
dependency.

Every fixture here is real: real work units and campaigns created through
the production writers, real transitions attempted against real
PostgreSQL, real SQLSTATE read back — the fixture-honesty discipline
`pipeline/contract/fixture.py`'s own docstring states (unique run tags, no
truncation, nothing assumed empty).
"""

import itertools
import uuid

import pytest

from pipeline.contract import fixture
from pipeline.intent.writer import (_CAMPAIGN_GRAPH, _TRANSITION_GRAPH,
                                    CAMPAIGN_STATES, WORK_UNIT_STATES,
                                    ABANDONED, ACTIVE, BLOCKED,
                                    CAMPAIGN_COMPLETE, CANCELLED, COMPLETE,
                                    DEFINED, PAUSED, QUARANTINED,
                                    READY, SUBMITTED, WRITER_MUTATION_API,
                                    WRITER_ORCHESTRATOR,
                                    WRITER_VALIDATION_INGEST, CampaignWriter,
                                    WorkUnitIdentity, WorkUnitWriter)

#: RA011 — the SQLSTATE every INVARIANT-shaped 076 refusal (DAG, writer,
#: supersession, completion-acceptance) carries since migration 086 split
#: it out of RA001, which now means only a genuine CAS miss
#: (`pipeline.operatorctl.contract.SQLSTATE_INVARIANT_VIOLATION` vs
#: `SQLSTATE_EXPECTED_STATE_MISMATCH`). An illegal edge is the canonical
#: invariant case: no retry can ever make it legal. Read directly via
#: `sqlstate_of`-style attribute access rather than imported from
#: `pipeline.intent.errors`: that module's own header states it classifies
#: driver errors through the two families it documents (23505/23503), and
#: the RA0xx family is deliberately a THIRD, application-defined
#: vocabulary.
_RA011 = "RA011"


def _sqlstate(exc):
    for attribute in ("pgcode", "sqlstate"):
        code = getattr(exc, attribute, None)
        if isinstance(code, str) and len(code) == 5:
            return code
    return None


# ============================================================================
# WORK UNITS
# ============================================================================

_WORK_UNIT_EDGES = sorted(_TRANSITION_GRAPH)
_WORK_UNIT_NON_EDGES = sorted(
    pair for pair in itertools.permutations(sorted(WORK_UNIT_STATES), 2)
    if pair not in _TRANSITION_GRAPH)

#: Sanity on the derivation itself: 7 states, 42 ordered pairs, 14 declared
#: edges therefore 28 non-edges — asserted once here so a change to
#: WORK_UNIT_STATES that silently changed the state-space size would fail
#: loudly at collection rather than quietly shrinking coverage.
assert len(WORK_UNIT_STATES) == 7
assert len(_WORK_UNIT_EDGES) == 14
assert len(_WORK_UNIT_NON_EDGES) == 28


def _identity(scope_name):
    return WorkUnitIdentity(
        job_type=fixture.JOB_TYPE, input_scope=scope_name,
        operational_class="prompt-processing",
        definition_version=fixture.DEFINITION_VERSION)


def _make_terminal_attempt(conn, work_unit_id, *, registered=True,
                           product_disposition="published",
                           terminal_record_sequence=1):
    """A `terminal_after_start` attempt bound to `work_unit_id`, satisfying
    migration 014's full CHECK — the same INSERT shape
    `test_work_unit_grants.py::_make_terminal_attempt` uses, defaulted here
    to a SATISFYING shape (`registered=True`) since this file's only use of
    it is completing the one edge that needs the acceptance boundary
    cleared, not exercising its refusal cases (those are
    `test_work_unit_grants.py`'s job).
    """
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


def _unit_in_state(conn, scope_name, state):
    """A work unit in `state`, reached via a LEGAL path through the real
    graph and the real writer — `fixture.create_unit` only reaches states
    directly adjacent to READY. Adapted from
    `test_work_unit_grants.py::_unit_in_state`, extended to also reach
    COMPLETE and CANCELLED, which this file's "every non-edge is refused"
    sweep needs as SOURCE states that module's narrower fixture set never
    had to produce.
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
    if state == CANCELLED:
        writer.transition_unit(unit_id, READY, state, writer=WRITER_MUTATION_API)
        conn.commit()
        return unit_id
    # SUBMITTED, FAILED, BLOCKED, COMPLETE: all pass through submitted first.
    writer.transition_unit(unit_id, READY, SUBMITTED, writer=WRITER_ORCHESTRATOR)
    conn.commit()
    if state == SUBMITTED:
        return unit_id
    if state == BLOCKED:
        writer.transition_unit(
            unit_id, SUBMITTED, state, writer=WRITER_RECONCILER,
            blocked_reason="dag-agreement fixture: parked to reach BLOCKED")
        conn.commit()
        return unit_id
    if state == COMPLETE:
        attempt_id = _make_terminal_attempt(conn, unit_id)
        conn.commit()
        # NAMED, not ALL — same reasoning as `test_work_unit_grants.py`'s
        # `_complete_with_guc_enforced`: forces exactly the one deferred
        # trigger the completion-acceptance clause lives in to fire at the
        # next statement rather than at commit, without perturbing any
        # other deferred constraint the transaction might hold.
        with conn.cursor() as cur:
            cur.execute(
                "SET CONSTRAINTS work_units_check_event_recorded_trg"
                " IMMEDIATE")
        writer.transition_unit(
            unit_id, SUBMITTED, COMPLETE, writer=WRITER_RECONCILER,
            detail={"deciding_attempt_id": attempt_id})
        conn.commit()
        return unit_id
    writer.transition_unit(unit_id, SUBMITTED, state, writer=WRITER_RECONCILER)
    conn.commit()
    return unit_id


def _required_writer(from_state, to_state):
    """The writer `_TRANSITION_GRAPH` requires for this edge, or the
    ordinary default — the SAME lookup `WorkUnitWriter.transition_unit`
    itself does, read straight from the graph rather than re-stated as a
    second list here (a second list is exactly the kind of copy this
    file's own docstring warns drifts)."""
    declared = _TRANSITION_GRAPH[(from_state, to_state)]
    return declared if declared is not None else WRITER_ORCHESTRATOR


@pytest.mark.parametrize("from_state,to_state", _WORK_UNIT_EDGES)
def test_every_declared_work_unit_edge_is_accepted(conn, from_state, to_state):
    """Every edge `_TRANSITION_GRAPH` declares actually transitions the row
    and records its event, driven through the real writer with the writer
    identity the edge itself requires.
    """
    unit_id = _unit_in_state(
        conn, fixture.scope(f"dag-edge-{from_state}-{to_state}"), from_state)
    writer_id = _required_writer(from_state, to_state)
    execute = fixture.executor(conn)
    unit_writer = WorkUnitWriter(execute)

    kwargs = {}
    if to_state == BLOCKED:
        kwargs["blocked_reason"] = "dag-agreement: declared edge probe"
    if to_state == COMPLETE:
        # The one edge the completion-acceptance gate (083, on by default)
        # also checks. _unit_in_state already drove SUBMITTED through a
        # fresh unit with no attempt of its own here, so build one now,
        # exactly as `_close_work_unit`'s real callers do.
        attempt_id = _make_terminal_attempt(conn, unit_id)
        conn.commit()
        kwargs["detail"] = {"deciding_attempt_id": attempt_id}

    unit_writer.transition_unit(unit_id, from_state, to_state, writer=writer_id,
                                **kwargs)
    conn.commit()

    assert fixture.unit_state(conn, unit_id)[0] == to_state, (
        f"declared edge {from_state}->{to_state} did not land — the "
        f"Python graph and the 076 trigger disagree")
    events = fixture.unit_events(conn, unit_id)
    assert (from_state, to_state, writer_id) in events, (
        f"{from_state}->{to_state} landed but recorded no matching "
        f"unit_events row")


@pytest.mark.parametrize("from_state,to_state", _WORK_UNIT_NON_EDGES)
def test_every_non_edge_is_refused_by_the_trigger(conn, from_state, to_state):
    """Every ordered pair `_TRANSITION_GRAPH` does NOT declare is refused
    with RA011 by 076's trigger — driven through `derived.
    transition_work_unit` DIRECTLY rather than through
    `WorkUnitWriter.transition_unit`, whose own Python-side graph check
    would refuse the identical call before any SQL ran and prove only that
    Python agrees with itself. This is the half of "agreement" a call
    through the writer cannot demonstrate.
    """
    unit_id = _unit_in_state(
        conn, fixture.scope(f"dag-nonedge-{from_state}-{to_state}"), from_state)
    blocked_reason = ("dag-agreement: non-edge probe"
                      if to_state == BLOCKED else None)

    with pytest.raises(Exception) as caught:  # noqa: BLE001 - classified below
        with conn.cursor() as cur:
            cur.execute(
                "SELECT derived.transition_work_unit"
                "  (%s, %s, %s, %s, %s, %s, %s, %s)",
                [unit_id, from_state, to_state, WRITER_MUTATION_API,
                 blocked_reason, "dag-agreement: non-edge probe", None, True])
    conn.rollback()

    assert _sqlstate(caught.value) == _RA011, (
        f"{from_state}->{to_state}: expected RA011 (illegal transition), "
        f"got {caught.value!r} — the Python graph refuses this pair but "
        f"the SQL trigger did not")
    assert fixture.unit_state(conn, unit_id)[0] == from_state, (
        f"{from_state}->{to_state}: the row moved despite the raised "
        f"error — a refused transition must leave no partial write")


def test_self_transitions_are_a_no_op_not_a_refusal(conn):
    """076's trigger returns NEW unchanged for `NEW.state IS NOT DISTINCT
    FROM OLD.state` BEFORE the DAG check ever runs — the one case
    deliberately excluded from both `_WORK_UNIT_EDGES` and
    `_WORK_UNIT_NON_EDGES` above, asserted here on its own terms rather
    than silently absent from the sweep.
    """
    unit_id = _unit_in_state(conn, fixture.scope("dag-self-transition"), READY)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE work_units SET state = state WHERE work_unit_id = %s",
            [unit_id])
        assert cur.rowcount == 1
    conn.commit()
    assert fixture.unit_state(conn, unit_id)[0] == READY


# ============================================================================
# CAMPAIGNS
# ============================================================================

_CAMPAIGN_EDGES = sorted(_CAMPAIGN_GRAPH)
_CAMPAIGN_NON_EDGES = sorted(
    pair for pair in itertools.permutations(sorted(CAMPAIGN_STATES), 2)
    if pair not in _CAMPAIGN_GRAPH)

#: 5 states, 20 ordered pairs, 7 declared edges, 13 non-edges.
assert len(CAMPAIGN_STATES) == 5
assert len(_CAMPAIGN_EDGES) == 7
assert len(_CAMPAIGN_NON_EDGES) == 13


def _campaign_in_state(conn, name, state):
    """A campaign in `state`, reached via a legal path through the real
    writer (`CampaignWriter` has no 077-equivalent constrained function —
    `test_grant_matrix.py::test_078_campaigns_raw_update_is_the_explicit_
    carve_out` is the standing record of that — so this still goes through
    `CampaignWriter`'s own raw `UPDATE campaigns` for the ordinary path).
    """
    execute = fixture.executor(conn)
    writer = CampaignWriter(execute)
    campaign_id = writer.create_campaign(name, "prompt-processing")
    conn.commit()
    if state == DEFINED:
        return campaign_id
    if state == ABANDONED:
        writer.abandon_campaign(campaign_id, from_state=DEFINED)
        conn.commit()
        return campaign_id
    writer.activate_campaign(campaign_id)
    conn.commit()
    if state == ACTIVE:
        return campaign_id
    if state == CAMPAIGN_COMPLETE:
        writer.complete_campaign(campaign_id)
        conn.commit()
        return campaign_id
    if state == PAUSED:
        writer.pause_campaign(campaign_id)
        conn.commit()
        return campaign_id
    raise AssertionError(f"no known path to campaign state {state!r}")


def _campaign_state(conn, campaign_id):
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM campaigns WHERE campaign_id = %s",
                    [campaign_id])
        return cur.fetchone()[0]


@pytest.mark.parametrize("from_state,to_state", _CAMPAIGN_EDGES)
def test_every_declared_campaign_edge_is_accepted(conn, from_state, to_state):
    name = f"dag-campaign-edge-{from_state}-{to_state}-{uuid.uuid4().hex[:8]}"
    campaign_id = _campaign_in_state(conn, name, from_state)
    execute = fixture.executor(conn)
    execute("UPDATE campaigns SET state = %s WHERE campaign_id = %s",
           [to_state, campaign_id])
    conn.commit()
    assert _campaign_state(conn, campaign_id) == to_state, (
        f"declared campaign edge {from_state}->{to_state} did not land — "
        f"the Python graph and the 076 campaign trigger disagree")


@pytest.mark.parametrize("from_state,to_state", _CAMPAIGN_NON_EDGES)
def test_every_campaign_non_edge_is_refused_by_the_trigger(
        conn, from_state, to_state):
    name = f"dag-campaign-nonedge-{from_state}-{to_state}-{uuid.uuid4().hex[:8]}"
    campaign_id = _campaign_in_state(conn, name, from_state)
    execute = fixture.executor(conn)

    with pytest.raises(Exception) as caught:  # noqa: BLE001 - classified below
        execute("UPDATE campaigns SET state = %s WHERE campaign_id = %s",
               [to_state, campaign_id])
    conn.rollback()

    assert _sqlstate(caught.value) == _RA011, (
        f"campaign {from_state}->{to_state}: expected RA011, got "
        f"{caught.value!r} — the Python graph refuses this pair but the "
        f"SQL trigger did not")
    assert _campaign_state(conn, campaign_id) == from_state, (
        f"campaign {from_state}->{to_state}: the row moved despite the "
        f"raised error")


def test_campaign_self_transitions_are_a_no_op_not_a_refusal(conn):
    writer = CampaignWriter(fixture.executor(conn))
    campaign_id = writer.create_campaign(
        f"dag-campaign-self-{uuid.uuid4().hex[:8]}", "prompt-processing")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE campaigns SET state = state WHERE campaign_id = %s",
            [campaign_id])
        assert cur.rowcount == 1
    conn.commit()
    assert _campaign_state(conn, campaign_id) == DEFINED
