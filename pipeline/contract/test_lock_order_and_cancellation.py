"""Contract tests: rule 9's unified lock order and cancellation (brief C3,
acceptance criteria 3 and 4).

    3. "two concurrent transitions (e.g. cancellation vs closure) on one work
        unit serialize — no interleaving anomaly; lock order test passes for
        every disposition pair."
    4. "cancel a ready and a submitted unit; state, audit record, and
        non-consumption of attempts all assert; cancelled is terminal (no
        accidental revival by 040's function or the mutation API)."

THE LOCK TESTS RUN EVERYWHERE. The work-unit advisory lock is application
code, not schema — `pg_advisory_xact_lock` needs no migration — so criterion
3's serialization is asserted in CI as well as on rapid-admin. Only the tests
that write `state='cancelled'` or call `derived.cancel_work_units` need DRAFTS
045/046, and those probe and skip.

Why this tier and not the stub tier: an advisory lock's exclusivity is
observable only between two genuinely concurrent transactions on two
connections. `test_registration_watermark.py` makes the same point about the
existing namespaces — "advisory-lock SCOPING has no Python-observable
expression at all" — and a fake that reported "lock acquired" would agree with
code holding no lock whatsoever.
"""

import threading
import time

import pytest

from pipeline.contract import fixture
from pipeline.intent import lock as intent_lock
from pipeline.intent.writer import (BLOCKED, CANCELLED, COMPLETE, FAILED,
                                    QUARANTINED, READY, SUBMITTED,
                                    WRITER_MUTATION_API, WRITER_ORCHESTRATOR,
                                    WRITER_RECONCILER, IllegalTransition,
                                    WorkUnitWriter)


def _lock_holders(conn, work_unit_id):
    """How many sessions hold this unit's advisory lock, per `pg_locks`.

    Asked of the database rather than inferred from the code's own belief:
    the whole question is whether the lock the code thinks it took is a lock
    PostgreSQL agrees exists, keyed where the code thinks it is keyed.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
            "   AND classid = %s AND objid = %s AND granted",
            [intent_lock.WORK_UNIT_NAMESPACE, work_unit_id])
        return cur.fetchone()[0]


def test_the_lock_is_real_and_keyed_on_the_work_unit(conn, second_conn):
    """A held lock is visible in pg_locks under the WU namespace and the unit id.

    The foundation criterion 3 rests on. If this key were wrong — a different
    namespace, or the attempt id — every serialization test below would still
    pass while serializing nothing, because two writers would each take an
    uncontended lock.
    """
    work_unit_id = fixture.create_unit(conn, fixture.scope("lock-visible"))

    with intent_lock.work_unit_lock(conn, work_unit_id) as held:
        assert held is True
        # Observed from the OTHER connection: a lock only this session can see
        # is not a lock.
        assert _lock_holders(second_conn, work_unit_id) == 1
        second_conn.rollback()

    conn.commit()
    assert _lock_holders(second_conn, work_unit_id) == 0
    second_conn.rollback()


def test_a_second_writer_cannot_take_a_held_lock(conn, second_conn):
    """try_-acquire fails while another transaction holds it. Real exclusion."""
    work_unit_id = fixture.create_unit(conn, fixture.scope("lock-excludes"))

    with intent_lock.work_unit_lock(conn, work_unit_id) as held:
        assert held is True
        with intent_lock.work_unit_lock(second_conn, work_unit_id,
                                        blocking=False) as second_held:
            assert second_held is False
        second_conn.rollback()
    conn.commit()

    # ...and once released, the same acquisition succeeds — proving the
    # refusal above was the lock and not something structural.
    with intent_lock.work_unit_lock(second_conn, work_unit_id,
                                    blocking=False) as after:
        assert after is True
    second_conn.rollback()


DISPOSITION_PAIRS = [
    # (from_state, first transition, second transition) — the disposition
    # pairs rule 9 enumerates: "Cancellation, quarantine, retry, blocked-
    # parking, closure and acceptance all take the same work-unit lock".
    # Each pair is two writers racing one unit from one state; exactly one
    # must win, and the loser must discover it rather than overwrite.
    (SUBMITTED, COMPLETE, FAILED),          # closure vs closure
    (SUBMITTED, COMPLETE, QUARANTINED),     # acceptance vs quarantine
    (SUBMITTED, BLOCKED, READY),            # park vs scheduler-retry
    (SUBMITTED, FAILED, QUARANTINED),       # closure vs quarantine
    (READY, SUBMITTED, QUARANTINED),        # submission vs quarantine
    (BLOCKED, READY, QUARANTINED),          # release vs quarantine
]


def _unit_in_state(conn, scope_name, state):
    """A work unit in `state`, built through the production writer.

    `fixture.create_unit` cannot reach BLOCKED: it creates READY and
    transitions, and `ready -> blocked` is not an edge the graph admits (a
    unit is parked from `submitted` by the reconciler, or created blocked by
    gathering). So blocked units are CREATED blocked here, with the reason
    `work_units_blocked_reason_ck` requires — the same call
    `submission.blocked.record_blocked` makes in production.
    """
    from pipeline.intent.writer import (WRITER_VALIDATION_INGEST,
                                        WorkUnitIdentity)

    if state != BLOCKED:
        return fixture.create_unit(conn, scope_name, state=state)

    writer = WorkUnitWriter(fixture.executor(conn))
    work_unit_id = writer.create_work_unit(
        WorkUnitIdentity(
            job_type=fixture.JOB_TYPE, input_scope=scope_name,
            operational_class="prompt-processing",
            definition_version=fixture.DEFINITION_VERSION),
        writer=WRITER_VALIDATION_INGEST, state=BLOCKED,
        blocked_reason="missing_dependency:contract_fixture")
    conn.commit()
    return work_unit_id


@pytest.mark.parametrize("from_state,first,second", DISPOSITION_PAIRS)
def test_concurrent_dispositions_serialize(conn, second_conn, from_state,
                                           first, second):
    """Two dispositions on one unit: one wins, one is refused. No interleaving.

    Criterion 3, "for every disposition pair". Genuinely concurrent — two
    connections, two threads, a barrier so both are inside `transition_unit`
    before either commits — so the serialization under test is the lock's and
    the CAS's, not the test's own ordering.

    The assertion is deliberately about the OUTCOME rather than about timing:
    exactly one transition succeeds, the unit lands in exactly that state, and
    exactly one `unit_events` row records it. An interleaving anomaly would
    show up as two successes, or as a unit whose state disagrees with its own
    event history — which is the corruption rule 9's lock exists to prevent.
    """
    scope_name = fixture.scope(f"pair-{first}-{second}")
    work_unit_id = _unit_in_state(conn, scope_name, from_state)
    conn.commit()

    barrier = threading.Barrier(2)
    outcomes = {}

    def attempt(slot, connection, to_state):
        writer = WorkUnitWriter(fixture.executor(connection))
        blocked_reason = "contract:race" if to_state == BLOCKED else None
        try:
            barrier.wait(timeout=30)
            writer.transition_unit(
                work_unit_id, from_state, to_state,
                writer=(WRITER_MUTATION_API
                        if (from_state, to_state) in
                        ((FAILED, READY), (QUARANTINED, READY))
                        else WRITER_RECONCILER),
                blocked_reason=blocked_reason)
            connection.commit()
            outcomes[slot] = ("won", to_state)
        except Exception as exc:  # noqa: BLE001 - the loser's refusal is data
            connection.rollback()
            outcomes[slot] = ("lost", type(exc).__name__)

    threads = [
        threading.Thread(target=attempt, args=(0, conn, first)),
        threading.Thread(target=attempt, args=(1, second_conn, second)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    won = [slot for slot, (verdict, _) in outcomes.items() if verdict == "won"]
    assert len(won) == 1, f"expected exactly one winner, got {outcomes}"

    winning_state = outcomes[won[0]][1]
    state, _reason = fixture.unit_state(conn, work_unit_id)
    assert state == winning_state

    # The history agrees with the state: one creation event, one transition.
    # Two transitions here would be the interleaving anomaly itself.
    events = fixture.unit_events(conn, work_unit_id)
    transitions = [e for e in events if e[0] == from_state]
    assert len(transitions) == 1
    assert transitions[0][1] == winning_state


# ---------------------------------------------------------------------------
# Cancellation (criterion 4) — DRAFT 045/046 gated
# ---------------------------------------------------------------------------

@pytest.fixture
def _requires_cancelled_state(conn):
    """Skip unless DRAFT 045 amended the state CHECK. Probed, never assumed."""
    if not fixture.admits_state(conn, CANCELLED):
        pytest.skip(
            "DRAFT migration 045 (work_units 'cancelled' state) is not "
            "applied; cancellation tests skip cleanly without it")


@pytest.fixture
def _requires_cancel_function(conn, _requires_cancelled_state):
    """Skip unless DRAFT 046's mutation-API function exists."""
    if not fixture.has_function(conn, "cancel_work_units"):
        pytest.skip(
            "DRAFT migration 046 (derived.cancel_work_units) is not applied; "
            "the audited-cancellation tests skip cleanly without it")


@pytest.mark.parametrize("state", [READY, SUBMITTED])
def test_cancel_a_ready_and_a_submitted_unit(conn, _requires_cancel_function,
                                             state):
    """State, audit record and non-consumption of attempts all assert.

    Criterion 4's first half, over both states it names. The cancellation goes
    through `derived.cancel_work_units` — the audited mutation API — rather
    than through a Python writer, because 030's one-path rule reserves every
    write to `derived.mutation_audit` to its SECURITY DEFINER functions.
    """
    from pipeline.intent import cancellation

    execute = fixture.executor(conn)
    work_unit_id = fixture.create_unit(
        conn, fixture.scope(f"cancel-{state}"), state=state)
    conn.commit()

    result = cancellation.cancel_work_units(
        execute, [work_unit_id], reason="contract test cancellation",
        dry_run=False, dispatcher="contract-suite")
    conn.commit()

    assert result["units_cancelled"] == 1
    assert result["refused"] == []
    assert fixture.unit_state(conn, work_unit_id)[0] == CANCELLED

    # THE AUDIT RECORD (brief C3: "recording who/why... compose with the
    # mutation-audit machinery"). Asserted against the real ledger, including
    # that the reason survived — a cancellation nobody can explain later is
    # the failure the mandatory-reason CHECK exists to prevent.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT actor, action_class, action_tier, reason, dry_run,"
            "       rows_affected"
            "  FROM derived.mutation_audit WHERE audit_id = %s",
            [result["audit_id"]])
        actor, action_class, tier, reason, dry_run, affected = cur.fetchone()
    assert action_class == cancellation.ACTION_CLASS
    assert tier == "decide"
    assert reason == "contract test cancellation"
    assert dry_run is False and affected == 1
    assert actor  # session_user, recorded by the function, never by the caller

    # NO ATTEMPT IS CONSUMED: cancellation is a statement about intent, and
    # attempts record physical execution.
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM attempts WHERE work_unit_id = %s",
                    [work_unit_id])
        assert cur.fetchone()[0] == 0

    # The transition is in the unit's own history too, under mutation_api.
    events = fixture.unit_events(conn, work_unit_id)
    assert (state, CANCELLED, WRITER_MUTATION_API) in events


def test_cancelled_is_terminal_against_the_transition_graph(conn):
    """No edge out of CANCELLED exists for any writer — including mutation_api.

    Criterion 4's terminality half, at the application layer. Needs no draft
    schema: the graph is Python, and refusing an edge it does not admit
    happens before any SQL is issued.

    Reviving cancelled work is CREATING work, which the design already models
    as a new unit with supersession recording the relationship — so the
    absence of this edge is the design position, not an oversight.
    """
    writer = WorkUnitWriter(fixture.executor(conn))
    for target in (READY, SUBMITTED, BLOCKED, COMPLETE, FAILED, QUARANTINED):
        with pytest.raises(IllegalTransition):
            writer.transition_unit(
                1, CANCELLED, target, writer=WRITER_MUTATION_API,
                blocked_reason=("x" if target == BLOCKED else None))


def test_a_cancelled_unit_is_not_revived_by_the_scoped_retry(
        conn, _requires_cancel_function):
    """040's function returns `failed` units to ready and must not touch this one.

    Criterion 4's "no accidental revival by 040's function" — the mechanical
    reason cancellation is a seventh state rather than a spelling of `failed`.
    Asserted by running the real function against a database containing a
    cancelled unit and checking it stayed cancelled.
    """
    from pipeline.intent import cancellation

    execute = fixture.executor(conn)
    work_unit_id = fixture.create_unit(
        conn, fixture.scope("cancel-no-revival"), state=SUBMITTED)
    conn.commit()

    cancellation.cancel_work_units(
        execute, [work_unit_id], reason="contract: revival check",
        dry_run=False)
    conn.commit()
    assert fixture.unit_state(conn, work_unit_id)[0] == CANCELLED

    with conn.cursor() as cur:
        cur.execute(
            "SELECT derived.retry_parked_attempts(%s, %s, %s, %s)",
            [f"contract-{fixture.RUN_TAG}", "contract: retry sweep", 50,
             False])
        cur.fetchone()
    conn.commit()

    assert fixture.unit_state(conn, work_unit_id)[0] == CANCELLED


def test_terminal_units_are_refused_not_raised(conn, _requires_cancel_function):
    """A complete unit is reported in `refused`, with the state it was found in.

    The report-what-you-could-not-do posture this migration stream uses
    throughout (031's scale advisory, 040's units_not_failed). One call over a
    mixed set does the part it can and tells the caller precisely what it did
    not — which is what makes a bulk cancellation usable.
    """
    from pipeline.intent import cancellation

    execute = fixture.executor(conn)
    cancellable = fixture.create_unit(
        conn, fixture.scope("cancel-mixed-ready"), state=READY)
    terminal = fixture.create_unit(
        conn, fixture.scope("cancel-mixed-done"), state=SUBMITTED)
    WorkUnitWriter(execute).transition_unit(
        terminal, SUBMITTED, COMPLETE, writer=WRITER_RECONCILER)
    conn.commit()

    result = cancellation.cancel_work_units(
        execute, [cancellable, terminal], reason="contract: mixed set",
        dry_run=False)
    conn.commit()

    assert result["units_cancelled"] == 1
    refused = {entry["work_unit_id"]: entry for entry in result["refused"]}
    assert set(refused) == {terminal}
    assert refused[terminal]["state"] == COMPLETE

    assert fixture.unit_state(conn, cancellable)[0] == CANCELLED
    assert fixture.unit_state(conn, terminal)[0] == COMPLETE


def test_a_dry_run_changes_nothing_and_still_audits(conn,
                                                    _requires_cancel_function):
    """The rehearsal reports what would happen and writes rows_affected = 0.

    030's CHECK (`NOT dry_run OR coalesce(rows_affected, 0) = 0`) is what
    makes that honest at the ledger rather than only in the function's own
    accounting — so a rehearsal can never be read as a cancellation.
    """
    from pipeline.intent import cancellation

    execute = fixture.executor(conn)
    work_unit_id = fixture.create_unit(
        conn, fixture.scope("cancel-dry-run"), state=READY)
    conn.commit()

    result = cancellation.cancel_work_units(
        execute, [work_unit_id], reason="contract: rehearsal", dry_run=True)
    conn.commit()

    assert result["dry_run"] is True
    assert result["units_cancelled"] == 1      # would cancel
    assert fixture.unit_state(conn, work_unit_id)[0] == READY   # but did not

    with conn.cursor() as cur:
        cur.execute("SELECT dry_run, rows_affected FROM derived.mutation_audit"
                    " WHERE audit_id = %s", [result["audit_id"]])
        assert cur.fetchone() == (True, 0)


def test_cancellation_serializes_against_a_concurrent_closure(
        conn, second_conn, _requires_cancel_function):
    """Criterion 3's headline pair, with the real cancellation path.

    The two writers reach one unit from the two mechanisms the discipline had
    to unify: `derived.cancel_work_units` in SQL and `transition_unit` in
    Python. Before brief C these took no work-unit lock at all — the SQL side
    had none and the Python side had none — so they were serialized by nothing
    but the CAS. Exactly one now wins, and the loser discovers it.
    """
    from pipeline.intent import cancellation

    work_unit_id = fixture.create_unit(
        conn, fixture.scope("cancel-vs-closure"), state=SUBMITTED)
    conn.commit()

    barrier = threading.Barrier(2)
    outcomes = {}

    def cancel():
        try:
            barrier.wait(timeout=30)
            result = cancellation.cancel_work_units(
                fixture.executor(conn), [work_unit_id],
                reason="contract: cancel vs closure", dry_run=False)
            conn.commit()
            outcomes["cancel"] = bool(result["units_cancelled"])
        except Exception:  # noqa: BLE001 - refusal is the expected loss mode
            conn.rollback()
            outcomes["cancel"] = False

    def close():
        try:
            barrier.wait(timeout=30)
            time.sleep(0.05)   # bias toward contention, not toward an order
            WorkUnitWriter(fixture.executor(second_conn)).transition_unit(
                work_unit_id, SUBMITTED, COMPLETE, writer=WRITER_RECONCILER)
            second_conn.commit()
            outcomes["close"] = True
        except Exception:  # noqa: BLE001
            second_conn.rollback()
            outcomes["close"] = False

    threads = [threading.Thread(target=cancel),
               threading.Thread(target=close)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert sum(1 for won in outcomes.values() if won) == 1, outcomes
    state, _reason = fixture.unit_state(conn, work_unit_id)
    expected = CANCELLED if outcomes.get("cancel") else COMPLETE
    assert state == expected

    events = fixture.unit_events(conn, work_unit_id)
    assert len([e for e in events if e[0] == SUBMITTED]) == 1
