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

import datetime
import threading

import pytest

from observability.attempts import AttemptIdentity, AttemptWriter
from pipeline.contract import fixture


def _writer(conn):
    return AttemptWriter(fixture.executor(conn))


def _identity(run_id, logical_job_id):
    """The identity triple this suite claims under.

    An exposure/SCA-grain identity, which is what `science` attempts carry;
    `field`/`processing_date` stay unset because a row never carries a field
    number smeared into `exposure_id` as a sentinel.
    """
    return AttemptIdentity(run_id=run_id, logical_job_id=logical_job_id,
                           exposure_id=1, sca=1)


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _scheduler_job_id(logical_job_id):
    """A scheduler job id unique to this logical job.

    **THE RESOLVER KEYS ON IT AND REFUSES TO CROSS IDENTITIES.** Reusing one
    scheduler job id across two logical jobs raises from inside PL/pgSQL:
    "scheduler job X attempt 1 is attempt N, which belongs to run R/logical
    job A, not to run R/logical job B. Refusing to resolve across
    identities." That is rule 3 enforced in the database — a scheduler
    execution answers for exactly one logical job — and it is another
    invariant no fake in this repository expresses. Deriving the id from the
    logical job keeps each test's fixture honest about it.
    """
    return f"job-{logical_job_id}"


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
    logical_job_id, run_id = fixture.make_logical_job(conn, with_binding=True)
    conn.commit()

    barrier = threading.Barrier(2)
    results = {}
    errors = {}

    def resolve(slot, connection):
        try:
            writer = _writer(connection)
            moment = _now()
            barrier.wait(timeout=30)
            attempt_id = writer.resolve_attempt(
                _identity(run_id, logical_job_id), moment, moment,
                scheduler_job_id=_scheduler_job_id(logical_job_id),
                application_attempt_index=1, scheduler_attempt_index=1)
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


def test_a_second_scheduler_attempt_is_a_new_attempt_row(conn):
    """Rule 5: a retry is a NEW attempt row, not an update of the old one.

    **THE TWO INDICES ANSWER DIFFERENT QUESTIONS**, which this test exists to
    pin and which the resolver enforces: `scheduler_attempt_index` identifies
    the physical execution the row records, and `application_attempt_index` is
    the claim staked on it. A retry is therefore a new SCHEDULER attempt, and
    resolving one scheduler attempt under two application claims is refused
    outright — "attempt N is already claimed by application attempt 1; caller
    claims attempt 2. Refusing to hand one row to two attempts."

    That refusal is the identity chain of rule 3 holding at the database
    boundary, and it is invisible to a fake resolver, which has no notion of
    which index means what.
    """
    logical_job_id, run_id = fixture.make_logical_job(conn, with_binding=True)
    conn.commit()
    writer = _writer(conn)

    identity = _identity(run_id, logical_job_id)
    moment = _now()

    first = writer.resolve_attempt(
        identity, moment, moment,
        scheduler_job_id=_scheduler_job_id(logical_job_id),
        application_attempt_index=1, scheduler_attempt_index=1)
    conn.commit()

    second = writer.resolve_attempt(
        identity, moment, moment,
        scheduler_job_id=_scheduler_job_id(logical_job_id),
        application_attempt_index=2, scheduler_attempt_index=2)
    conn.commit()

    assert first != second, (
        "the second scheduler attempt resolved onto the first's row; every "
        "retry is a new RAPID attempt (rule 5)")
    rows = _attempt_rows(conn, logical_job_id)
    assert len(rows) == 2, f"expected two attempt rows, got {rows}"


def test_one_scheduler_attempt_is_never_claimed_by_two_applications(conn):
    """The resolver refuses to hand one row to two application claims.

    The complement of the test above: holding `scheduler_attempt_index`
    fixed while advancing `application_attempt_index` asks for exactly the
    thing rule 3 forbids — one physical execution answering for two logical
    claims. Pinned here so the refusal cannot quietly become an overwrite.
    """
    import psycopg2

    logical_job_id, run_id = fixture.make_logical_job(conn, with_binding=True)
    conn.commit()
    writer = _writer(conn)
    identity = _identity(run_id, logical_job_id)
    moment = _now()

    writer.resolve_attempt(
        identity, moment, moment,
        scheduler_job_id=_scheduler_job_id(logical_job_id),
        application_attempt_index=1, scheduler_attempt_index=1)
    conn.commit()

    with pytest.raises(psycopg2.errors.RaiseException) as caught:
        writer.resolve_attempt(
            identity, moment, moment,
            scheduler_job_id=_scheduler_job_id(logical_job_id),
            application_attempt_index=2, scheduler_attempt_index=1)
    conn.rollback()

    assert "Refusing to hand one row to two attempts" in str(caught.value)


def test_resolving_the_same_index_twice_is_idempotent(conn):
    """The same identity resolved twice, sequentially, is one row.

    The sequential half of the concurrent test above — a late-starting
    runtime resolving an identity the reconciler already discovered. Cheap to
    assert, and it fails differently from the concurrent case (no lock
    contention involved), so a resolver broken in only one of the two ways is
    still caught.
    """
    logical_job_id, run_id = fixture.make_logical_job(conn, with_binding=True)
    conn.commit()
    writer = _writer(conn)

    identity = _identity(run_id, logical_job_id)
    moment = _now()

    first = writer.resolve_attempt(
        identity, moment, moment,
        scheduler_job_id=_scheduler_job_id(logical_job_id),
        application_attempt_index=1, scheduler_attempt_index=1)
    conn.commit()
    again = writer.resolve_attempt(
        identity, moment, moment,
        scheduler_job_id=_scheduler_job_id(logical_job_id),
        application_attempt_index=1, scheduler_attempt_index=1)
    conn.commit()

    assert first == again, f"re-resolving produced a second row: {first} vs {again}"
    assert len(_attempt_rows(conn, logical_job_id)) == 1


def test_the_resolver_refuses_a_logical_job_with_no_binding(conn):
    """An attempt cannot be claimed under a job with no sealed binding (rule 7).

    THE SCHEMA ENFORCES THE SEALED SUBMISSION, not convention. The resolver's
    INSERT copies the execution binding from `logical_jobs`, and
    `attempts_state_submitted_check` requires a `submitted` row at
    `schema_version >= 2` to carry a job-definition ARN, an image digest and
    a manifest checksum. So a binding-less logical job cannot produce an
    attempt at all: "the sealed submission pins its manifest checksum and
    exact queue/job-definition/image identities" is a database invariant here,
    not a code review item.

    Found by this suite rather than reasoned out: the first version of the
    fixture created binding-less logical jobs, and the resolver refused them
    from inside PL/pgSQL. Nothing in this repository could have refused it —
    which is the argument for the tier, made by the tier.
    """
    import psycopg2

    logical_job_id, run_id = fixture.make_logical_job(conn)   # no binding
    conn.commit()
    writer = _writer(conn)
    moment = _now()

    with pytest.raises(psycopg2.errors.CheckViolation) as caught:
        writer.resolve_attempt(
            _identity(run_id, logical_job_id), moment, moment,
            scheduler_job_id=_scheduler_job_id(logical_job_id),
            application_attempt_index=1, scheduler_attempt_index=1)
    conn.rollback()

    assert caught.value.pgcode == "23514", caught.value.pgcode
    assert "attempts_state_submitted_check" in str(caught.value)


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
