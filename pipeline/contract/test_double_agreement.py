"""Live-vs-double agreement probes: one per protocol (brief B acceptance 2).

**THE PROBLEM THIS SOLVES.** The stub tier's fakes have a documented history
of going stub-blind — accepting call shapes the live system refuses, so a test
passes against a fake and the same code fails in production. Two instances are
recorded in this repository's own comments: brief A's fixture wrote
`error_category` onto a `submitted` row (real PostgreSQL refused it,
`attempts_state_submitted_check`), and `_as_jsonb` exists because psycopg2
would not adapt a bare dict that the fake executor had accepted for months.

**THE MECHANISM.** For each protocol, this module builds a DELIBERATELY BROKEN
double — one that accepts a call the live system refuses — and asserts two
things in one test:

  1. the live system REFUSES the call (so the probe is testing a real
     refusal, not an imagined one), and
  2. the broken double ACCEPTS it (so the probe would have been silent).

A test that only asserted (1) would pass on a database that had quietly
dropped the constraint. Asserting (2) alongside is what makes each probe a
statement about the DOUBLE rather than about the schema: it fails the day
someone "fixes" a fake by making it permissive.

**"DOUBLES MUST BE ABLE TO REFUSE"** is the rule these encode. A double that
cannot refuse anything agrees with every implementation, including wrong ones.
"""

import psycopg2
import pytest

from pipeline.contract import fixture
from pipeline.intent.errors import is_unique_violation
from pipeline.intent.writer import (READY, WRITER_VALIDATION_INGEST,
                                    WorkUnitIdentity, WorkUnitWriter)


class PermissiveExecutor:
    """A double that accepts every statement and reports success.

    This is not a straw man: it is the SHAPE of an executor fake that has not
    been given a way to refuse — it satisfies
    `observability.attempts.Executor` and `pipeline.intent.writer.Executor`
    exactly, returns a plausible row for anything with a RETURNING clause,
    and a rowcount of 1 for everything else. Every constraint in the schema
    is invisible to it.
    """

    def __init__(self):
        self.statements = []

    def __call__(self, statement, params=None):
        self.statements.append((statement, params))
        if "RETURNING" in statement.upper() or statement.lstrip().upper().startswith(
                "SELECT"):
            return [(1,)]
        return 1


def test_probe_attempts_lifecycle_constraint(conn):
    """Protocol: the `attempts` lifecycle/outcome column contract.

    Live refuses an `error_category` on a `submitted` row
    (`attempts_state_submitted_check`); the permissive double accepts it.
    """
    logical_job_id, run_id = fixture.make_logical_job(conn)
    conn.commit()

    insert = ("INSERT INTO attempts"
              "  (run_id, schema_version, logical_job_id, lifecycle_state,"
              "   created_at, submitted_at, error_category)"
              " VALUES (%s, 2, %s, 'submitted', now(), now(), 'input_missing')"
              " RETURNING attempt_id")

    # (1) THE LIVE SYSTEM REFUSES.
    with pytest.raises(psycopg2.errors.CheckViolation) as caught:
        with conn.cursor() as cur:
            cur.execute(insert, [run_id, logical_job_id])
    conn.rollback()
    assert caught.value.pgcode == "23514"

    # (2) THE DOUBLE ACCEPTS — i.e. a suite built on it would be silent here.
    double = PermissiveExecutor()
    assert double(insert, [run_id, logical_job_id]) == [(1,)], (
        "the permissive double refused; this probe is no longer demonstrating "
        "a divergence")


def test_probe_work_unit_identity_uniqueness(conn):
    """Protocol: one current work unit per (job_type, input_scope).

    Live refuses the second insert through migration 036's partial unique
    index (SQLSTATE 23505); the permissive double accepts it, which is how a
    claim-race test can pass against a production path that has no re-SELECT
    at all — the exact defect brief A repaired.
    """
    input_scope = fixture.scope("double-probe-identity")
    writer = WorkUnitWriter(fixture.executor(conn))
    identity = WorkUnitIdentity(
        job_type=fixture.JOB_TYPE, input_scope=input_scope,
        operational_class="prompt-processing",
        definition_version=fixture.DEFINITION_VERSION)

    writer.create_work_unit(identity, writer=WRITER_VALIDATION_INGEST,
                            state=READY)
    conn.commit()

    # (1) LIVE REFUSES the duplicate identity.
    with pytest.raises(Exception) as caught:
        writer.create_work_unit(identity, writer=WRITER_VALIDATION_INGEST,
                                state=READY)
    conn.rollback()
    assert is_unique_violation(caught.value), (
        f"expected a unique violation, got {caught.value!r}")

    # (2) THE DOUBLE ACCEPTS the same second creation, twice over.
    double_writer = WorkUnitWriter(PermissiveExecutor())
    first = double_writer.create_work_unit(
        identity, writer=WRITER_VALIDATION_INGEST, state=READY)
    second = double_writer.create_work_unit(
        identity, writer=WRITER_VALIDATION_INGEST, state=READY)
    assert first == second == 1, (
        "the permissive double refused a duplicate identity; this probe is "
        "no longer demonstrating a divergence")


def test_probe_jsonb_adaptation(conn):
    """Protocol: parameter adaptation by the real driver.

    Live psycopg2 refuses a bare dict as a jsonb parameter ("can't adapt type
    'dict'"); the permissive double accepts it. This is the divergence that
    reached production on 2026-08-09 and produced `_as_jsonb`.
    """
    unit = fixture.create_unit(conn, fixture.scope("double-probe-jsonb"))
    statement = ("INSERT INTO unit_events"
                 "  (work_unit_id, occurred_at, from_state, to_state, writer,"
                 "   reason, detail)"
                 " VALUES (%s, now(), NULL, 'ready', 'validation_ingest',"
                 "         'probe', %s)")

    # (1) LIVE REFUSES a bare dict.
    with pytest.raises(psycopg2.ProgrammingError):
        with conn.cursor() as cur:
            cur.execute(statement, [unit, {"probe": "value"}])
    conn.rollback()

    # (2) THE DOUBLE ACCEPTS it.
    double = PermissiveExecutor()
    assert double(statement, [unit, {"probe": "value"}]) == 1, (
        "the permissive double refused a bare dict; this probe is no longer "
        "demonstrating a divergence")


def test_probe_watermark_monotonicity(conn):
    """Protocol: the registration watermark's CAS predicate.

    Live matches ZERO rows when asked to move the watermark backwards; the
    permissive double reports one row affected, which reads to the caller as
    "the watermark moved" — a fake that turns a refusal into a success.
    """
    from pipeline.registration.consumer import _MARK_REGISTERED_SQL

    attempt_id = fixture.make_attempt(
        conn, lifecycle="terminal_without_start", terminal_record_sequence=2)
    conn.commit()
    # _MARK_REGISTERED_SQL takes FIVE params: registered_at,
    # registered_record_sequence, consumed_record_sequence, attempt_id, and
    # the sequence the CAS predicate compares against (mark_registered's own
    # callers always pass the same value for the third and fifth). Four
    # params here previously starved psycopg2's placeholder substitution and
    # raised IndexError before the statement ever reached the server.
    with conn.cursor() as cur:
        cur.execute(_MARK_REGISTERED_SQL,
                    ("2026-01-01T00:00:00+00:00", 2, 2, attempt_id, 2))
    conn.commit()

    # (1) LIVE REFUSES the backwards move: zero rows matched.
    with conn.cursor() as cur:
        cur.execute(_MARK_REGISTERED_SQL,
                    ("2026-01-01T00:00:00+00:00", 1, 1, attempt_id, 1))
        live_rows = cur.rowcount
    conn.commit()
    assert live_rows == 0, f"live matched {live_rows} rows for a lower sequence"

    # (2) THE DOUBLE REPORTS SUCCESS for the identical call.
    double = PermissiveExecutor()
    assert double(_MARK_REGISTERED_SQL, (None, 1, 1, attempt_id, 1)) == 1, (
        "the permissive double refused the backwards watermark move; this "
        "probe is no longer demonstrating a divergence")


def test_probe_schema_contract_is_readable(conn):
    """Protocol: the schema preflight's own read of `schema_migrations`.

    Live reports the real applied set — which, on a database built by the
    contract harness, contains the full stream. The permissive double returns
    `[(1,)]` for the same SELECT, so a preflight built on it would verify
    against the filename `1` and pass on an EMPTY database. A startup gate
    that passes on an empty database is worse than no gate.
    """
    from pipeline.intent.schema_contract import (REQUIRED_MIGRATIONS,
                                                 applied_migrations,
                                                 verify_schema_contract)

    live = applied_migrations(fixture.executor(conn))
    assert "036-intent-schema-v1.sql" in live, (
        f"the contract database has not had the migration stream applied: "
        f"{sorted(live)[:5]}")
    assert verify_schema_contract(fixture.executor(conn)) == len(
        REQUIRED_MIGRATIONS)

    # (2) THE DOUBLE would pass the same check against nothing at all.
    double_applied = applied_migrations(PermissiveExecutor())
    assert double_applied == {1}, (
        f"the permissive double no longer fabricates a migration set: "
        f"{double_applied}")
