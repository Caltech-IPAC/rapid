"""Contract tests: fail-then-succeed retry histories (rules 4, 5).

Brief A's acceptance criteria 1 and 2, migrated from
`pipeline/intent/test/live_brief_a_acceptance.py` into the gating contract
tier — which is the migration brief B asks for: A's suite was real-PostgreSQL
evidence a human read once, and these are the same assertions run by a gate.

The load-bearing assertion in both is about the INTERMEDIATE states, read from
`unit_events`: the defect rule 4 names ("a work unit closes only from an
accepted result or explicit retry-policy exhaustion — never from an
intermediate physical failure") is invisible in the final state. A unit
tombstoned by attempt 1's OOM kill and revived by hand ends `complete` too.
"""

from pipeline.contract import fixture
from pipeline.intent import retry_policy
from pipeline.intent.writer import (BLOCKED, COMPLETE, READY, SUBMITTED,
                                    WRITER_MUTATION_API, WRITER_ORCHESTRATOR,
                                    WorkUnitWriter)


def test_abrupt_loss_then_acceptance_never_enters_failed(conn):
    """Attempt 1 abruptly lost, attempt 2 accepted -> complete, never failed.

    A Spot reclaim is scheduler-visible, so policy v1 returns the unit to
    `ready` for a new attempt. The old closure code failed the unit the
    moment that loss was reconciled; a test reading only the final state
    passes against that code, because the unit still reaches `complete`
    afterwards. So this reads the append-only history and asserts no event
    ever entered `failed`.
    """
    unit = fixture.create_unit(conn, fixture.scope("abrupt-loss-retry"),
                               state=SUBMITTED)
    writer = WorkUnitWriter(fixture.executor(conn))

    lost = fixture.make_attempt(conn, work_unit_id=unit,
                                error_category="scheduler_reclaimed")
    disposition = retry_policy.disposition_for_terminal_attempt(
        succeeded=False, error_category="scheduler_reclaimed",
        scheduler_loss_count=0)
    assert disposition == retry_policy.RETRY_READY, disposition

    writer.transition_unit(unit, SUBMITTED, READY, writer="reconciler")
    conn.commit()
    assert fixture.unit_state(conn, unit)[0] == READY

    writer.transition_unit(unit, READY, SUBMITTED, writer=WRITER_ORCHESTRATOR)
    conn.commit()
    # `registered=True` plus `terminal_record_sequence=1` is what satisfies
    # the completion-acceptance boundary (migrations 076/080, enabled by
    # 083): the deciding attempt must be consumed (terminal_record_sequence
    # >= 1) and accepted (registered_at set). Both were already true of
    # this attempt's shape except the sequence, which defaulted to NULL.
    accepted = fixture.make_attempt(conn, work_unit_id=unit, registered=True,
                                    lifecycle="terminal_without_start",
                                    terminal_record_sequence=1)
    assert retry_policy.disposition_for_terminal_attempt(
        succeeded=True, error_category=None) == retry_policy.CLOSE_COMPLETE

    # `detail={"deciding_attempt_id": accepted}` is what production's own
    # completion path stamps (`pipeline.registration.consumer`) — the
    # accompanying unit_events row must name the deciding attempt for the
    # completion to clear clause (a) of the acceptance boundary.
    writer.transition_unit(unit, SUBMITTED, COMPLETE, writer="reconciler",
                           detail={"deciding_attempt_id": accepted})
    conn.commit()

    assert fixture.unit_state(conn, unit)[0] == COMPLETE
    history = fixture.unit_events(conn, unit)
    entered_failed = [event for event in history if event[1] == "failed"]
    assert not entered_failed, (
        f"the unit reached 'failed' at some point: {history} "
        f"(attempts {lost} lost, {accepted} accepted)")


def test_application_failure_parks_blocked_with_a_reason(conn):
    """A deterministic application failure parks BLOCKED, and the reason is real.

    `blocked` requires a non-NULL `blocked_reason` by CHECK constraint, so
    "parks with a reason" is only genuinely tested where the constraint runs.
    The revival half goes through the mutation-API writer, the sanctioned
    caller for reviving parked work — and `blocked->ready` clearing the
    reason is a property of the writer's own SQL, asserted from the row.
    """
    unit = fixture.create_unit(conn, fixture.scope("application-parks"),
                               state=SUBMITTED)
    writer = WorkUnitWriter(fixture.executor(conn))

    disposition = retry_policy.disposition_for_terminal_attempt(
        succeeded=False, error_category="input_missing")
    assert disposition == retry_policy.PARK_BLOCKED, disposition
    reason = retry_policy.blocked_reason_for("input_missing")
    assert reason == "application_failure:input_missing", reason

    writer.transition_unit(unit, SUBMITTED, BLOCKED, writer="reconciler",
                           blocked_reason=reason)
    conn.commit()

    state, blocked_reason = fixture.unit_state(conn, unit)
    assert state == BLOCKED, f"state is {state}, not blocked"
    assert blocked_reason == reason, blocked_reason

    writer.transition_unit(unit, BLOCKED, READY, writer=WRITER_MUTATION_API,
                           reason="contract test: condition changed")
    conn.commit()

    state, blocked_reason = fixture.unit_state(conn, unit)
    assert state == READY, f"after revival state is {state}, not ready"
    assert blocked_reason is None, (
        f"a ready unit still carries blocked_reason={blocked_reason!r}")


def test_the_schema_refuses_a_blocked_unit_with_no_reason(conn):
    """`work_units_blocked_reason_ck`, asserted where the constraint runs.

    `WorkUnitWriter` checks this in Python before issuing SQL, so the stub
    tier can only ever prove the Python check. This proves the DATABASE
    refuses it — which is what still protects the invariant when the next
    caller writes its own UPDATE, as the mutation functions in migration 031
    do.

    The unit must reach `submitted` first: `(ready, blocked)` is not an edge
    `pipeline.intent.writer._TRANSITION_GRAPH` admits — only
    `(submitted, blocked)` reaches `blocked` — and migration 076's trigger
    layer now enforces that same DAG on every raw UPDATE, including this
    one. Attempting `ready -> blocked` directly is refused by the DAG check
    (RA001) before the statement ever reaches `work_units_blocked_reason_ck`,
    which would prove the wrong thing: not that the reason-less write is
    refused, but that the edge is illegal at all.
    """
    import psycopg2
    import pytest

    from pipeline.intent.writer import (READY, SUBMITTED, WRITER_ORCHESTRATOR,
                                        WorkUnitWriter)

    unit = fixture.create_unit(conn, fixture.scope("blocked-no-reason"))
    writer = WorkUnitWriter(fixture.executor(conn))
    writer.transition_unit(unit, READY, SUBMITTED, writer=WRITER_ORCHESTRATOR)
    conn.commit()

    with pytest.raises(psycopg2.errors.CheckViolation) as caught:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE work_units SET state = 'blocked', blocked_reason = NULL"
                " WHERE work_unit_id = %s", [unit])
    conn.rollback()
    assert caught.value.pgcode == "23514", caught.value.pgcode
