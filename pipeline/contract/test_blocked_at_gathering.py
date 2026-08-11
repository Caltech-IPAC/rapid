"""Contract tests: rule 13's gathering-time BLOCKED work unit (brief C4,
acceptance criterion 5).

    "a gathering pass with insufficient coverage leaves a queryable BLOCKED
     unit with the dependency named; providing coverage and re-passing yields
     ready->submitted; no attempt rows exist for the blocked period."

NO DRAFT SCHEMA IS NEEDED HERE and these tests run everywhere, CI included.
`work_units.state = 'blocked'` and the mandatory `blocked_reason` are migration
036's, already deployed and already exercised by package A's park-on-
application-failure path. What was missing was purely a production writer for
the pre-attempt cause, so what is asserted is a code path, not a schema.

Why the contract tier rather than the stub tier: `work_units_blocked_reason_ck`
is a CHECK constraint, so "parks WITH a reason" is only really tested where the
constraint runs — the same argument `test_retry_history.py` makes. And the
release path's `blocked -> ready` is a CAS whose exclusivity is a property of
PostgreSQL row locking.
"""

from pipeline.contract import fixture
from pipeline.intent.writer import (BLOCKED, READY, SUBMITTED,
                                    WRITER_ORCHESTRATOR, WorkUnitWriter)
from submission import blocked as blocked_units


def _count_attempts_for_unit(conn, work_unit_id):
    """Attempt rows pointing at this unit. Criterion 5's "no attempt" half."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM attempts WHERE work_unit_id = %s",
                    [work_unit_id])
        return cur.fetchone()[0]


def test_insufficient_coverage_parks_a_queryable_blocked_unit(conn):
    """The dependency is named, the row is findable, and no attempt exists."""
    input_scope = fixture.scope("ref-coverage-short")

    work_unit_id = blocked_units.record_blocked(
        fixture.executor(conn), job_type=fixture.JOB_TYPE,
        input_scope=input_scope, operational_class="reference-construction",
        dependency=blocked_units.REFERENCE_COVERAGE)
    conn.commit()

    state, reason = fixture.unit_state(conn, work_unit_id)
    assert state == BLOCKED
    # The dependency is NAMED, not merely flagged: an operator reading this
    # column learns what is missing, which is the whole difference between
    # this and the INFO log it replaces.
    assert reason == "missing_dependency:reference_coverage"
    assert blocked_units.is_missing_dependency(reason)

    # NO ATTEMPT IS CONSUMED (rule 13, verbatim: "leave work BLOCKED without
    # consuming attempts"). Structural here — nothing in this path touches
    # `attempts` — and asserted anyway, because "the code does not do X" is a
    # claim that decays silently as the code changes.
    assert _count_attempts_for_unit(conn, work_unit_id) == 0


def test_a_blocked_unit_is_queryable_beside_application_failure_parks(conn):
    """Both causes share the state; the reason prefix tells them apart.

    Criterion 5's "queryable" half, and brief C4's "visible to the same
    queries as A's application-failure parks (they share the state; keep
    reasons distinguishable by prefix)". One query over `state='blocked'`
    finds both, which is what an operator wants; the prefixes are what lets a
    surface separate them again.
    """
    missing_scope = fixture.scope("blocked-missing-dep")
    failure_scope = fixture.scope("blocked-app-failure")
    execute = fixture.executor(conn)

    missing_id = blocked_units.record_blocked(
        execute, job_type=fixture.JOB_TYPE, input_scope=missing_scope,
        operational_class="reference-construction",
        dependency=blocked_units.REFERENCE_COVERAGE)

    # Package A's cause, written the way the reconciler writes it.
    from pipeline.intent.retry_policy import blocked_reason_for
    failure_id = fixture.create_unit(conn, failure_scope, state=SUBMITTED)
    WorkUnitWriter(execute).transition_unit(
        failure_id, SUBMITTED, BLOCKED, writer=WRITER_ORCHESTRATOR,
        blocked_reason=blocked_reason_for("input_missing"))
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT work_unit_id, blocked_reason FROM work_units"
            " WHERE state = 'blocked' AND work_unit_id = ANY(%s)"
            " ORDER BY work_unit_id",
            [[missing_id, failure_id]])
        rows = dict(cur.fetchall())

    # ONE query over the shared state found both causes.
    assert set(rows) == {missing_id, failure_id}
    assert blocked_units.is_missing_dependency(rows[missing_id])
    assert not blocked_units.is_missing_dependency(rows[failure_id])
    assert rows[failure_id].startswith("application_failure:")


def test_repeated_passes_do_not_mint_a_unit_per_poll(conn):
    """Find-or-create, because an unripe field is unripe on every pass.

    Gathering runs on a schedule and a field short of coverage tonight is
    short of it on every pass until new frames arrive. A create-only writer
    would mint a unit per poll until migration 036's partial unique index
    started refusing them — turning an ordinary early-survey state into a
    stream of errors.
    """
    input_scope = fixture.scope("ref-coverage-repeat")
    execute = fixture.executor(conn)

    first = blocked_units.record_blocked(
        execute, job_type=fixture.JOB_TYPE, input_scope=input_scope,
        operational_class="reference-construction",
        dependency=blocked_units.REFERENCE_COVERAGE)
    conn.commit()
    second = blocked_units.record_blocked(
        execute, job_type=fixture.JOB_TYPE, input_scope=input_scope,
        operational_class="reference-construction",
        dependency=blocked_units.REFERENCE_COVERAGE)
    conn.commit()

    assert first == second

    # And exactly one row exists under the identity — asserted against the
    # database rather than inferred from the two ids matching, because the
    # index's predicate (`superseded_by_unit_id IS NULL`) is the thing under
    # test and only the database evaluates it.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM work_units"
            " WHERE job_type = %s AND input_scope = %s"
            "   AND superseded_by_unit_id IS NULL",
            [fixture.JOB_TYPE, input_scope])
        assert cur.fetchone()[0] == 1

    # No unit_events row was written for the repeat: nothing transitioned, so
    # claiming an event would falsify the append-only history of transitions.
    events = fixture.unit_events(conn, first)
    assert len(events) == 1
    assert events[0][:2] == (None, BLOCKED)


def test_coverage_arriving_releases_the_unit_ready(conn):
    """blocked -> ready through the existing graph edge, then ready -> submitted.

    Criterion 5's second half: "providing coverage and re-passing yields
    ready->submitted". The release is the edge package A's graph already
    admits, so this asserts the wiring, not a new mechanism.
    """
    input_scope = fixture.scope("ref-coverage-arrives")
    execute = fixture.executor(conn)

    work_unit_id = blocked_units.record_blocked(
        execute, job_type=fixture.JOB_TYPE, input_scope=input_scope,
        operational_class="reference-construction",
        dependency=blocked_units.REFERENCE_COVERAGE)
    conn.commit()
    assert fixture.unit_state(conn, work_unit_id)[0] == BLOCKED

    released = blocked_units.release_blocked(
        execute, job_type=fixture.JOB_TYPE, input_scope=input_scope,
        writer_identity=WRITER_ORCHESTRATOR)
    conn.commit()

    assert released is True
    state, reason = fixture.unit_state(conn, work_unit_id)
    assert state == READY
    # The reason is cleared by the transition, which
    # `work_units_blocked_reason_ck` requires — a non-blocked row carrying a
    # blocked_reason is refused by the database.
    assert reason is None

    # ...and the released unit is now submittable, which is what makes the
    # release meaningful rather than cosmetic.
    WorkUnitWriter(execute).transition_unit(
        work_unit_id, READY, SUBMITTED, writer=WRITER_ORCHESTRATOR)
    conn.commit()
    assert fixture.unit_state(conn, work_unit_id)[0] == SUBMITTED

    # Still no attempt was consumed across the whole blocked period.
    assert _count_attempts_for_unit(conn, work_unit_id) == 0


def test_release_is_a_no_op_for_a_unit_that_was_never_blocked(conn):
    """A ready unit is left alone, and the caller is told nothing happened.

    The ordinary case on every pass over a field that always had coverage:
    the gatherer calls the releaser for each unit it yields, and almost none
    of them were ever parked. Reporting False rather than raising is what
    keeps that from being an exception per unit per poll.
    """
    input_scope = fixture.scope("ref-coverage-never-blocked")
    work_unit_id = fixture.create_unit(conn, input_scope)

    released = blocked_units.release_blocked(
        fixture.executor(conn), job_type=fixture.JOB_TYPE,
        input_scope=input_scope, writer_identity=WRITER_ORCHESTRATOR)
    conn.commit()

    assert released is False
    assert fixture.unit_state(conn, work_unit_id)[0] == READY
