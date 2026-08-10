"""Contract tests: work-unit CAS transitions and the partial-unique-index race
(rule 6).

Two distinct database properties live here, and the stub tier can express
neither:

  * the **partial unique index** on `work_units (job_type, input_scope) WHERE
    superseded_by_unit_id IS NULL` (migration 036) picks a winner between two
    concurrent creators. A fake executor can only replay a scripted conflict —
    and it did: the previous test for this path simulated the 23505 while the
    production code had no re-SELECT at all, so the fake agreed with a
    production path that could not work.
  * the **CAS** in `transition_unit` is a `WHERE state = %s` predicate whose
    exclusivity under concurrency is a property of PostgreSQL's row locking,
    not of the Python that emits it.
"""

import threading

import pytest

from pipeline.contract import fixture
from pipeline.intent.errors import is_unique_violation
from pipeline.intent.writer import (READY, SUBMITTED, WRITER_ORCHESTRATOR,
                                    WRITER_VALIDATION_INGEST, WorkUnitIdentity,
                                    WorkUnitNotFound, WorkUnitWriter)


def test_two_concurrent_creators_resolve_to_one_unit(conn, second_conn):
    """Both racers observe no unit, one loses the insert, both return one id.

    Genuinely concurrent: two connections, two threads, a barrier so both
    SELECT before either INSERTs. That ordering is what makes it a race —
    both see no row, both try to create, and the partial unique index picks
    a winner. The loser's repair path (catch 23505, re-SELECT the winner) is
    production code in `pipeline/seams.py`, reproduced here at the writer
    level so the failure isolates to the writer rather than to the seam's
    orchestration.
    """
    input_scope = fixture.scope("claim-race")
    barrier = threading.Barrier(2)
    results = {}
    errors = {}

    def claim(slot, connection):
        try:
            writer = WorkUnitWriter(fixture.executor(connection))
            identity = WorkUnitIdentity(
                job_type=fixture.JOB_TYPE, input_scope=input_scope,
                operational_class="prompt-processing",
                definition_version=fixture.DEFINITION_VERSION)
            existing = writer.find_current_unit(fixture.JOB_TYPE, input_scope)
            barrier.wait(timeout=30)          # both have now SELECTed
            if existing is not None:
                results[slot] = ("found", existing["work_unit_id"])
                return
            try:
                work_unit_id = writer.create_work_unit(
                    identity, writer=WRITER_VALIDATION_INGEST, state=READY)
                connection.commit()
                results[slot] = ("winner", work_unit_id)
            except Exception as exc:          # noqa: BLE001 - classified
                connection.rollback()
                if not is_unique_violation(exc):
                    raise
                row = writer.find_current_unit(fixture.JOB_TYPE, input_scope)
                assert row is not None, "winner not findable after 23505"
                results[slot] = ("loser", row["work_unit_id"])
        except Exception as exc:              # noqa: BLE001 - reported
            errors[slot] = exc

    threads = [threading.Thread(target=claim, args=(slot, connection))
               for slot, connection in (("a", conn), ("b", second_conn))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"a racer raised: {errors}"
    assert len(results) == 2, f"only {results} completed"
    ids = {work_unit_id for _role, work_unit_id in results.values()}
    assert len(ids) == 1, f"racers resolved to different units: {results}"

    # AND THE INDEX HELD: exactly one non-superseded row for that identity.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM work_units"
            " WHERE job_type = %s AND input_scope = %s"
            "   AND superseded_by_unit_id IS NULL",
            [fixture.JOB_TYPE, input_scope])
        assert cur.fetchone()[0] == 1, "more than one current unit survived"


def test_only_one_of_two_concurrent_transitions_wins_the_cas(conn, second_conn):
    """One ready->submitted CAS succeeds; the other finds the row moved.

    This is the exclusivity half of rule 6: resolving two racers to one unit
    must not authorize two submissions of it. Both transactions read the unit
    as `ready` and both attempt the same transition; the CAS predicate means
    exactly one UPDATE matches a row, and the loser gets zero rows —
    `WorkUnitNotFound`, the writer's name for "another writer moved it".

    A single-connection test cannot produce this: it would serialize the two
    updates and the second would simply see `submitted` on re-read.
    """
    input_scope = fixture.scope("cas-exclusivity")
    unit = fixture.create_unit(conn, input_scope)

    barrier = threading.Barrier(2)
    outcomes = {}

    def transition(slot, connection):
        writer = WorkUnitWriter(fixture.executor(connection))
        barrier.wait(timeout=30)
        try:
            writer.transition_unit(unit, READY, SUBMITTED,
                                   writer=WRITER_ORCHESTRATOR)
            connection.commit()
            outcomes[slot] = "won"
        except WorkUnitNotFound:
            connection.rollback()
            outcomes[slot] = "refused"
        except Exception as exc:              # noqa: BLE001 - reported
            connection.rollback()
            outcomes[slot] = f"raised:{type(exc).__name__}"

    threads = [threading.Thread(target=transition, args=(slot, connection))
               for slot, connection in (("a", conn), ("b", second_conn))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    won = [slot for slot, outcome in outcomes.items() if outcome == "won"]
    assert len(won) == 1, f"expected exactly one winner, got {outcomes}"

    # ONE ready->submitted EVENT, not two. The event history is the audit
    # trail rule 3 requires; a second event would mean a second authorized
    # submission of one logical work unit.
    history = fixture.unit_events(conn, unit)
    submissions = [event for event in history
                   if event[0] == READY and event[1] == SUBMITTED]
    assert len(submissions) == 1, (
        f"expected exactly one ready->submitted, got {len(submissions)}: "
        f"{history}")


def test_a_cas_against_the_wrong_from_state_writes_nothing(conn):
    """The CAS refuses a stale expectation and leaves the row untouched.

    The sequential complement to the concurrent test: a caller holding a
    stale read of `ready` for a unit already `submitted` must not move it.
    Asserted from the row AND from the event history — a writer that raised
    but had already written its event row would be worse than one that
    silently succeeded.
    """
    unit = fixture.create_unit(conn, fixture.scope("stale-cas"),
                               state=SUBMITTED)
    writer = WorkUnitWriter(fixture.executor(conn))
    before = fixture.unit_events(conn, unit)

    with pytest.raises(WorkUnitNotFound):
        writer.transition_unit(unit, READY, SUBMITTED,
                               writer=WRITER_ORCHESTRATOR)
    conn.rollback()

    assert fixture.unit_state(conn, unit)[0] == SUBMITTED
    assert fixture.unit_events(conn, unit) == before, (
        "the refused transition still appended an event")


def test_jsonb_detail_survives_a_real_driver(conn):
    """A dict `detail` round-trips through psycopg2 — the fake accepted more.

    `_as_jsonb` exists because "psycopg2 does not adapt a bare dict on write"
    — found live on the first real-driver campaign creation, 2026-08-09,
    after the fake executor had accepted dicts happily for months. That is a
    live-vs-double divergence in the adapter layer, so it belongs in the tier
    that uses the real adapter.
    """
    unit = fixture.create_unit(conn, fixture.scope("jsonb-detail"))
    writer = WorkUnitWriter(fixture.executor(conn))
    writer.transition_unit(unit, READY, SUBMITTED, writer=WRITER_ORCHESTRATOR,
                           reason="contract: jsonb round trip",
                           detail={"probe": "value", "n": 1})
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT detail FROM unit_events"
            " WHERE work_unit_id = %s AND detail IS NOT NULL"
            " ORDER BY unit_event_id DESC LIMIT 1", [unit])
        stored = cur.fetchone()[0]

    # psycopg2 decodes jsonb back to a dict, so a successful write is
    # observable as structure rather than as a quoted string.
    assert stored == {"probe": "value", "n": 1}, stored
