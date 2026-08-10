"""Contract tests: attempt acquisition under real concurrency (rules 6, 3, 5).

`resolve_attempt` is a PL/pgSQL function defined by migration 013 in
`rapid_systems`. **This repository does not contain it.** `observability.
attempts.AttemptWriter.resolve_attempt` issues one `SELECT resolve_attempt(...)`
and trusts a contract described only in prose: "a transaction-scoped advisory
lock per logical job, a post-lock recheck, and two partial unique indexes make
a scheduler retry, a reconciler-discovered retry, and a late-starting runtime
all resolve to one row."

Every word of that is untestable against a fake — a fake resolver returns
whatever id the test told it to return, which is why the stub tier's version
of this asserts only that the Python passes its arguments through. These tests
assert the property itself: two genuinely concurrent transactions, and one
row at the end.
"""

import threading

import pytest

from observability.attempts import AttemptWriter
from pipeline.contract import fixture


def _writer(conn):
    return AttemptWriter(fixture.executor(conn))


def _attempt_rows(conn, logical_job_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT attempt_id, lifecycle_state, application_attempt_index"
            "  FROM attempts WHERE logical_job_id = %s ORDER BY attempt_id",
            [logical_job_id])
        return cur.fetchall()


def test_two_concurrent_resolvers_converge_on_one_attempt(conn, second_conn):
    """Two transactions resolving the same identity at once produce ONE row.

    THE RACE IS REAL, NOT SCRIPTED. Both connections open a transaction and
    both call `resolve_attempt` for the same (run_id, logical_job_id,
    application_attempt_index); a barrier holds each until the other has
    entered, so neither can complete before the other starts. Whichever
    reaches the advisory lock first creates the row; the second blocks on
    that lock, and — this is the part no fake can model — its post-lock
    recheck must SEE the first one's row and return that id rather than
    inserting a second.

    A resolver without the recheck passes a stubbed test and fails here with
    a unique violation or two rows. That is the whole point of the tier.
    """
    logical_job_id, run_id = fixture.make_logical_job(conn)
    conn.commit()

    barrier = threading.Barrier(2)
    results = {}
    errors = {}

    def resolve(slot, connection):
        try:
            writer = _writer(connection)
            barrier.wait(timeout=30)
            attempt_id = writer.resolve_attempt(
                run_id=run_id, logical_job_id=logical_job_id,
                scheduler_job_id=f"job-{fixture.RUN_TAG}",
                application_attempt_index=1, scheduler_attempt_index=1,
                exposure_id=1, sca=1, sky_tile=None)
            connection.commit()
            results[slot] = attempt_id
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            connection.rollback()
            errors[slot] = exc

    threads = [threading.Thread(target=resolve, args=(slot, connection))
               for slot, connection in (("a", conn), ("b", second_conn))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"a resolver raised: {errors}"
    assert len(results) == 2, f"only {results} completed"
    assert results["a"] == results["b"], (
        f"the two resolvers produced different attempt ids: {results} — the "
        "post-lock recheck did not see the winner's row")

    rows = _attempt_rows(conn, logical_job_id)
    assert len(rows) == 1, (
        f"one identity resolved to {len(rows)} attempt rows: {rows}")


def test_a_second_application_attempt_index_is_a_new_attempt(conn):
    """Rule 5: a retry is a NEW attempt row, not an update of the old one.

    Same logical job, a different `application_attempt_index` — which is what
    a RAPID retry is. The resolver must not converge these onto one row: the
    partial unique index that makes the concurrent case converge is scoped to
    the index, and a resolver that ignored it would silently overwrite the
    history rule 3 requires (four distinct records, each answering one
    question).
    """
    logical_job_id, run_id = fixture.make_logical_job(conn)
    conn.commit()
    writer = _writer(conn)

    first = writer.resolve_attempt(
        run_id=run_id, logical_job_id=logical_job_id,
        scheduler_job_id=f"job-{fixture.RUN_TAG}-1",
        application_attempt_index=1, scheduler_attempt_index=1,
        exposure_id=1, sca=1, sky_tile=None)
    conn.commit()

    second = writer.resolve_attempt(
        run_id=run_id, logical_job_id=logical_job_id,
        scheduler_job_id=f"job-{fixture.RUN_TAG}-2",
        application_attempt_index=2, scheduler_attempt_index=1,
        exposure_id=1, sca=1, sky_tile=None)
    conn.commit()

    assert first != second, (
        "attempt 2 resolved onto attempt 1's row; every retry is a new RAPID "
        "attempt (rule 5)")
    rows = _attempt_rows(conn, logical_job_id)
    assert len(rows) == 2, f"expected two attempt rows, got {rows}"


def test_resolving_the_same_index_twice_is_idempotent(conn):
    """The same identity resolved twice, sequentially, is one row.

    The sequential half of the concurrent test above — a late-starting
    runtime resolving an identity the reconciler already discovered. Cheap to
    assert, and it fails differently from the concurrent case (no lock
    contention involved), so a resolver broken in only one of the two ways is
    still caught.
    """
    logical_job_id, run_id = fixture.make_logical_job(conn)
    conn.commit()
    writer = _writer(conn)

    first = writer.resolve_attempt(
        run_id=run_id, logical_job_id=logical_job_id,
        scheduler_job_id=f"job-{fixture.RUN_TAG}",
        application_attempt_index=1, scheduler_attempt_index=1,
        exposure_id=1, sca=1, sky_tile=None)
    conn.commit()
    again = writer.resolve_attempt(
        run_id=run_id, logical_job_id=logical_job_id,
        scheduler_job_id=f"job-{fixture.RUN_TAG}",
        application_attempt_index=1, scheduler_attempt_index=1,
        exposure_id=1, sca=1, sky_tile=None)
    conn.commit()

    assert first == again, f"re-resolving produced a second row: {first} vs {again}"
    assert len(_attempt_rows(conn, logical_job_id)) == 1


def test_the_schema_refuses_an_outcome_on_an_in_flight_attempt(conn):
    """`attempts_state_submitted_check`: a `submitted` row carries no outcome.

    THE DOUBLE CANNOT REFUSE THIS AND THE DATABASE CAN. A fake executor
    accepts any column set it is handed — brief A's fixture wrote
    `error_category` onto a `submitted` row and passed until it met real
    PostgreSQL. This test pins that refusal so the constraint's removal is a
    test failure rather than a silent widening.
    """
    import psycopg2

    logical_job_id, run_id = fixture.make_logical_job(conn)
    conn.commit()

    with pytest.raises(psycopg2.errors.CheckViolation) as caught:
        with conn.cursor() as cur:
            cur.execute("SELECT coalesce(max(schema_version), 1) FROM attempts")
            schema_version = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO attempts"
                "  (run_id, schema_version, logical_job_id, lifecycle_state,"
                "   created_at, submitted_at, error_category)"
                " VALUES (%s, %s, %s, 'submitted', now(), now(),"
                "         'input_missing')",
                [run_id, schema_version, logical_job_id])
    conn.rollback()

    # SQLSTATE, never message text (rule 12, and `pipeline/intent/errors.py`
    # exists to enforce exactly this): 23514 is check_violation.
    assert caught.value.pgcode == "23514", caught.value.pgcode
