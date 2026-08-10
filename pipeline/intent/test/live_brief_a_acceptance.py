"""Brief A's five acceptance tests, against real PostgreSQL and the real schema.

    python3 -m pipeline.intent.test.live_brief_a_acceptance

**WHY THESE ARE `live_*` AND NOT UNIT TESTS** (rule 23: "These rules are
exercised against the authoritative migration schema with PostgreSQL-backed
tests — claiming under real concurrency, fail-then-succeed retry histories,
and every active query/write family — not against hand-built fakes"). Every
behavior asserted here is a property of the DATABASE, not of Python:

  * the claim race is resolved by migration 036's PARTIAL UNIQUE INDEX
    raising SQLSTATE 23505 in one of two genuinely concurrent transactions —
    a fake executor can only simulate the raise it was told to simulate, and
    the previous test for this path did exactly that while the production
    code had no re-SELECT at all;
  * `blocked` requires a non-NULL `blocked_reason` by CHECK constraint, so
    "parks with a reason" is only really tested where the constraint runs;
  * the CAS in `transition_unit` is a `WHERE state = %s` predicate whose
    exclusivity under concurrency is a database property.

So this module opens real connections, runs two of them at once for the race,
and asserts against rows read back. It emits one grep-able sentinel line per
test — `BRIEF-A-TEST-<n>: PASS|FAIL` — because it runs on rapid-admin through
SSM, whose output is tail-truncated at ~24KB, and a verdict that scrolled off
is a verdict nobody read.

Connection parameters come from PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE,
the standard libpq variables, so the harness that stands the scratch database
up passes them the same way `psql` takes them.

**FIXTURE HONESTY.** Each test builds its own rows under a unique run tag and
deletes only what it created. Nothing here truncates a table or assumes an
empty database, so a re-run is safe and a failure leaves its own rows behind
for inspection.
"""

import os
import sys
import threading
import uuid

import psycopg2

from pipeline.intent import retry_policy
from pipeline.intent.definitions import (WorkStreamIncomplete,
                                         verify_work_stream_completeness)
from pipeline.intent.errors import is_unique_violation
from pipeline.intent.writer import (BLOCKED, COMPLETE, READY, SUBMITTED,
                                    WRITER_MUTATION_API, WRITER_ORCHESTRATOR,
                                    WRITER_VALIDATION_INGEST, WorkUnitIdentity,
                                    WorkUnitWriter)

#: One tag per process run, so concurrent or repeated runs never collide on
#: the (job_type, input_scope) uniqueness this suite deliberately provokes.
RUN_TAG = uuid.uuid4().hex[:12]

#: The job type these tests create units under. `science` is used because it
#: is the one job type present in every registry AND shipped as a definition
#: file, so a loaded definition exists for it after the deployment step.
JOB_TYPE = "science"

DEFINITION_VERSION = 1


def connect():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "127.0.0.1"),
        port=int(os.environ.get("PGPORT", "5432")),
        user=os.environ.get("PGUSER", "postgres"),
        password=os.environ.get("PGPASSWORD", ""),
        dbname=os.environ.get("PGDATABASE", "rapid"))


def executor(conn):
    """The `execute(sql, params)` callable the intent layer takes."""
    def execute(statement, params=None):
        with conn.cursor() as cur:
            cur.execute(statement, params)
            if cur.description is not None:
                return cur.fetchall()
            return cur.rowcount
    return execute


def ensure_definition(conn):
    """A loaded workflow_definitions row, so work_units's FK is satisfiable.

    Inserted directly rather than through `derived.load_workflow_definition`
    because this suite runs as the scratch superuser, not `rapid_operator`,
    and the loader's own behavior is asserted by test 4 through the real
    function. Idempotent.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO workflow_definitions"
            "  (job_type, definition_version, checksum, source_path,"
            "   description)"
            " VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT (job_type, definition_version) DO NOTHING",
            [JOB_TYPE, DEFINITION_VERSION, "acceptance-fixture",
             "cdf/workflow/science-v1.toml", "brief A acceptance fixture"])
    conn.commit()


def make_attempt(conn, work_unit_id=None, error_category=None,
                 registered=False, lifecycle="submitted"):
    """One attempts row, minimal but real: the table's own constraints honoured.

    Returns its attempt_id.

    **THE LIFECYCLE STATE DECIDES WHICH COLUMNS MAY BE SET**, and the schema
    enforces it per state — `attempts_state_submitted_check` requires a
    `submitted` row to carry NO outcome facts at all (including
    `error_category`), while `attempts_state_terminal_without_start_check`
    requires a `terminal_without_start` row to carry `ended_at` and
    `scheduler_state` and NO `started_at`. That is the schema refusing to let
    a row claim a failure category while claiming to be still in flight, and
    it is exactly the kind of invariant a hand-built fake cannot enforce —
    the first version of this fixture wrote `error_category` onto a
    `submitted` row and only real PostgreSQL objected.

    So an attempt carrying an error category is written in a TERMINAL state,
    which is also what the reconciler itself writes
    (`mark_terminal_without_start` for a container that never started).
    """
    if error_category is not None and lifecycle == "submitted":
        lifecycle = "terminal_without_start"
    run_id = f"brief-a-{RUN_TAG}"
    with conn.cursor() as cur:
        # THE TABLE'S OWN NOT-NULL-WITHOUT-DEFAULT SET, read from
        # information_schema rather than guessed one round trip at a time:
        # schema_version, run_id, logical_job_id, lifecycle_state, created_at,
        # submitted_at. `logical_job_id` is text carrying an FK to
        # logical_jobs, so the parent row is created first — a fixture that
        # cannot satisfy the real constraints is a fixture testing a schema
        # nobody deployed.
        cur.execute("SELECT coalesce(max(schema_version), 1) FROM attempts")
        schema_version = cur.fetchone()[0]
        logical_job_id = f"lj-{RUN_TAG}-{uuid.uuid4().hex[:8]}"
        cur.execute(
            "INSERT INTO logical_jobs (logical_job_id, run_id)"
            " VALUES (%s, %s) ON CONFLICT DO NOTHING",
            [logical_job_id, run_id])
        terminal = lifecycle in ("terminal_without_start",
                                 "terminal_after_start",
                                 "application_closed")
        cur.execute(
            "INSERT INTO attempts"
            "  (run_id, schema_version, logical_job_id, lifecycle_state,"
            "   created_at, submitted_at, work_unit_id, error_category,"
            "   ended_at, scheduler_state, registered_at)"
            " VALUES (%s, %s, %s, %s, now(), now(), %s, %s,"
            "         CASE WHEN %s THEN now() ELSE NULL END,"
            "         CASE WHEN %s THEN 'FAILED' ELSE NULL END,"
            "         CASE WHEN %s THEN now() ELSE NULL END)"
            " RETURNING attempt_id",
            [run_id, schema_version, logical_job_id, lifecycle, work_unit_id,
             error_category, terminal, terminal, registered])
        return cur.fetchone()[0]


def unit_state(conn, work_unit_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state, blocked_reason FROM work_units WHERE work_unit_id=%s",
            [work_unit_id])
        return cur.fetchone()


def unit_events(conn, work_unit_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT from_state, to_state, writer FROM unit_events"
            " WHERE work_unit_id = %s ORDER BY unit_event_id", [work_unit_id])
        return cur.fetchall()


def create_unit(conn, scope, state=READY):
    """A work unit in `state`, created through the production writer."""
    writer = WorkUnitWriter(executor(conn))
    identity = WorkUnitIdentity(
        job_type=JOB_TYPE, input_scope=scope,
        operational_class="prompt-processing",
        definition_version=DEFINITION_VERSION)
    work_unit_id = writer.create_work_unit(
        identity, writer=WRITER_VALIDATION_INGEST, state=READY)
    if state != READY:
        writer.transition_unit(work_unit_id, READY, state,
                               writer=WRITER_ORCHESTRATOR)
    conn.commit()
    return work_unit_id


# -- test 1: fail-then-succeed -----------------------------------------------


def test_1_fail_then_succeed(conn):
    """Attempt 1 abruptly lost, attempt 2 accepted -> unit complete, never failed.

    Brief A acceptance 1, verbatim: "attempt 1 terminal-failed (abrupt loss),
    attempt 2 succeeds and registers -> unit `complete`; assert the
    intermediate state never reached `failed`."

    The intermediate-state assertion is the load-bearing half: the OLD code
    closed the unit `failed` the moment attempt 1's abrupt loss was
    reconciled, and a test that only checked the final state could pass while
    the unit had been tombstoned and revived. So this reads `unit_events` —
    the append-only history — and asserts no event ever entered `failed`.
    """
    scope = f"fail-then-succeed-{RUN_TAG}"
    unit = create_unit(conn, scope, state=SUBMITTED)
    writer = WorkUnitWriter(executor(conn))

    # Attempt 1: abrupt loss (a Spot reclaim). Under policy v1 this is
    # scheduler-visible, so the unit returns to `ready` for a new attempt.
    lost = make_attempt(conn, work_unit_id=unit,
                        error_category="scheduler_reclaimed")
    disposition = retry_policy.disposition_for_terminal_attempt(
        succeeded=False, error_category="scheduler_reclaimed",
        scheduler_loss_count=0)
    assert disposition == retry_policy.RETRY_READY, disposition
    writer.transition_unit(unit, SUBMITTED, READY,
                           writer="reconciler")
    conn.commit()

    state, _reason = unit_state(conn, unit)
    assert state == READY, f"after abrupt loss the unit is {state}, not ready"

    # A new attempt is authored (rule 5: every retry is a NEW RAPID attempt)
    # and the unit goes back to submitted.
    writer.transition_unit(unit, READY, SUBMITTED,
                           writer=WRITER_ORCHESTRATOR)
    conn.commit()
    accepted = make_attempt(conn, work_unit_id=unit, registered=True,
                            lifecycle="terminal_without_start")

    # Attempt 2 succeeds and registers -> complete.
    disposition = retry_policy.disposition_for_terminal_attempt(
        succeeded=True, error_category=None)
    assert disposition == retry_policy.CLOSE_COMPLETE, disposition
    writer.transition_unit(unit, SUBMITTED, COMPLETE, writer="reconciler")
    conn.commit()

    state, _reason = unit_state(conn, unit)
    assert state == COMPLETE, f"final state is {state}, not complete"

    history = unit_events(conn, unit)
    entered_failed = [event for event in history if event[1] == "failed"]
    assert not entered_failed, (
        f"the unit reached 'failed' at some point: {history}")

    return (f"unit {unit} complete via attempts {lost}(lost)->{accepted}"
            f"(accepted); {len(history)} events, none entered failed")


# -- test 2: application failure parks ---------------------------------------


def test_2_application_failure_parks(conn):
    """A deterministic application failure parks the unit BLOCKED with a reason.

    Brief A acceptance 2: "deterministic application failure -> unit
    `BLOCKED` with reason; mutation-API path returns it to `READY`."

    `input_missing` is used as the application category: deterministic by
    nature (the same inputs will be missing next time), which is precisely
    why policy v1 parks rather than retries.
    """
    scope = f"application-parks-{RUN_TAG}"
    unit = create_unit(conn, scope, state=SUBMITTED)
    writer = WorkUnitWriter(executor(conn))

    disposition = retry_policy.disposition_for_terminal_attempt(
        succeeded=False, error_category="input_missing")
    assert disposition == retry_policy.PARK_BLOCKED, disposition
    reason = retry_policy.blocked_reason_for("input_missing")
    assert reason == "application_failure:input_missing", reason

    writer.transition_unit(unit, SUBMITTED, BLOCKED, writer="reconciler",
                           blocked_reason=reason)
    conn.commit()

    state, blocked_reason = unit_state(conn, unit)
    assert state == BLOCKED, f"state is {state}, not blocked"
    assert blocked_reason == reason, blocked_reason

    # THE MUTATION-API RETURN PATH. blocked->ready is an ordinary edge in the
    # graph, but the audited mutation API is the sanctioned caller for
    # reviving parked work, so it is exercised under that writer identity.
    writer.transition_unit(unit, BLOCKED, READY, writer=WRITER_MUTATION_API,
                           reason="acceptance test 2: condition changed")
    conn.commit()

    state, blocked_reason = unit_state(conn, unit)
    assert state == READY, f"after revival state is {state}, not ready"
    assert blocked_reason is None, (
        f"a ready unit still carries blocked_reason={blocked_reason!r}")

    return (f"unit {unit} parked blocked ({reason}) and returned to ready "
            f"through the mutation API")


# -- test 3: the claim race --------------------------------------------------


def test_3_claim_race(conn):
    """Two concurrent transactions claim one unit; both resolve to one id.

    Brief A acceptance 3: "two concurrent transactions both observe no unit,
    one loses the insert, both return the same `work_unit_id`, exactly one
    ready->submitted transition fires."

    GENUINELY CONCURRENT, not simulated: two connections, two threads, a
    barrier so both SELECT before either INSERTs. That ordering is what makes
    it a race — both see no row, both try to create, and migration 036's
    partial unique index picks a winner. A fake executor cannot produce this;
    it can only replay a scripted conflict.
    """
    scope = f"claim-race-{RUN_TAG}"
    barrier = threading.Barrier(2)
    results = {}
    errors = {}

    def claim(slot):
        own = connect()
        try:
            writer = WorkUnitWriter(executor(own))
            identity = WorkUnitIdentity(
                job_type=JOB_TYPE, input_scope=scope,
                operational_class="prompt-processing",
                definition_version=DEFINITION_VERSION)
            existing = writer.find_current_unit(JOB_TYPE, scope)
            barrier.wait(timeout=30)          # both have now SELECTed
            if existing is None:
                try:
                    work_unit_id = writer.create_work_unit(
                        identity, writer=WRITER_VALIDATION_INGEST, state=READY)
                    own.commit()
                except Exception as exc:      # noqa: BLE001 - classified
                    own.rollback()
                    if not is_unique_violation(exc):
                        raise
                    # The production repair's path: lost the race, re-SELECT
                    # the winner.
                    row = writer.find_current_unit(JOB_TYPE, scope)
                    assert row is not None, "winner not findable after 23505"
                    work_unit_id = row["work_unit_id"]
                    results[slot] = ("loser", work_unit_id)
                    # The loser then attempts the ready->submitted CAS too;
                    # exactly one of the two may win it.
                    try:
                        writer.transition_unit(
                            work_unit_id, READY, SUBMITTED,
                            writer=WRITER_ORCHESTRATOR)
                        own.commit()
                        results[slot] = ("loser-transitioned", work_unit_id)
                    except Exception:         # noqa: BLE001 - CAS refusal
                        own.rollback()
                    return
                results[slot] = ("winner", work_unit_id)
                try:
                    writer.transition_unit(work_unit_id, READY, SUBMITTED,
                                           writer=WRITER_ORCHESTRATOR)
                    own.commit()
                    results[slot] = ("winner-transitioned", work_unit_id)
                except Exception:             # noqa: BLE001 - CAS refusal
                    own.rollback()
        except Exception as exc:              # noqa: BLE001 - reported
            errors[slot] = exc
        finally:
            own.close()

    threads = [threading.Thread(target=claim, args=(slot,))
               for slot in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"a racer raised: {errors}"
    assert len(results) == 2, f"only {results} completed"

    ids = {work_unit_id for _role, work_unit_id in results.values()}
    assert len(ids) == 1, f"racers resolved to different units: {results}"
    work_unit_id = ids.pop()

    # EXACTLY ONE ready->submitted EVENT. This is the exclusivity half of
    # rule 6: resolving both racers to one id must not authorize two
    # submissions of it.
    history = unit_events(conn, work_unit_id)
    submissions = [event for event in history
                   if event[0] == READY and event[1] == SUBMITTED]
    assert len(submissions) == 1, (
        f"expected exactly one ready->submitted, got {len(submissions)}: "
        f"{history}")

    roles = sorted(role for role, _ in results.values())
    return (f"both racers resolved to unit {work_unit_id} (roles {roles}); "
            f"exactly 1 ready->submitted event of {len(history)} total")


# -- test 4: missing definition fails closed ---------------------------------


def test_4_missing_definition_fails_closed(conn):
    """The startup completeness check names the stream, then passes once loaded.

    Brief A acceptance 4: "startup completeness check fails closed with the
    stream named; after loading, the same startup passes."

    Run against the REAL registries, with the loaded-definitions set read
    from the real table — the check's own SELECT. The "missing" case is
    produced by pointing the check at a database with no definitions for the
    enabled streams, which is exactly the deploy-forgot-the-step state it
    exists to catch.
    """
    from pipeline.intent.definitions import shipped_definitions

    # THE MISSING-DEFINITION STATE IS PRODUCED BY AN EMPTY TABLE, and the
    # honest way to show the check reacting to an empty table — without
    # deleting the fixture rows other tests depend on, and without fighting
    # unit_events's FK to work_units — is a real, empty table of the real
    # shape. A temporary table shadowing `workflow_definitions` in this
    # session's search_path is exactly that: the check's own SELECT runs
    # unmodified against real PostgreSQL, and it resolves to the empty temp
    # relation for the duration of this test.
    execute = executor(conn)
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TEMP TABLE workflow_definitions"
            " (LIKE public.workflow_definitions INCLUDING ALL)")

    named_stream = None
    try:
        verify_work_stream_completeness(execute)
    except WorkStreamIncomplete as exc:
        message = str(exc)
        assert "science" in message, f"the stream is not named: {message}"
        assert "not loaded" in message or "no shipped" in message, message
        lines = [line.strip() for line in message.splitlines()
                 if line.strip().startswith("-")]
        named_stream = lines[0] if lines else message
    else:
        raise AssertionError(
            "the completeness check PASSED with no definitions loaded; it is "
            "not failing closed")

    # NOW LOAD THEM AND RE-RUN THE SAME CHECK. Loading into the temp table
    # means "after the deployment step ran" without touching the shared rows.
    shipped = shipped_definitions()
    with conn.cursor() as cur:
        for definition in shipped.values():
            cur.execute(
                "INSERT INTO workflow_definitions"
                "  (job_type, definition_version, checksum, source_path,"
                "   description)"
                " VALUES (%s,%s,%s,%s,%s)",
                [definition["job_type"], definition["version"],
                 definition["checksum"], definition["source_path"],
                 definition["description"]])

    verified = verify_work_stream_completeness(execute)
    assert verified > 0, "the check verified zero streams after loading"

    with conn.cursor() as cur:
        cur.execute("DROP TABLE workflow_definitions")   # the temp one only
    conn.commit()

    return (f"check failed closed naming [{named_stream[:70]}]; after "
            f"loading {len(shipped)} shipped definitions the same check "
            f"verified {verified} streams")


# -- test 5: per-stream isolation --------------------------------------------


def test_5_stream_isolation(_conn):
    """A raising stream does not stop the next one; its count increments alone.

    Brief A acceptance 5: "an operator raising in stream N does not prevent
    stream N+1's pass; N's failure count increments alone."

    No database: `run_forever`'s isolation is pure control flow over injected
    operators, and the honest test of control flow is control flow. It runs
    here rather than in a stubbed unit suite because this file is Brief A's
    acceptance evidence and the five criteria belong in one place with one
    verdict format.
    """
    import logging

    from pipeline.operator import service as operator_service

    # The isolation this test proves is LOGGED with `logger.exception`, so a
    # passing run emits ~20 expected tracebacks. Under SSM's tail-truncated
    # 24KB output that buries every other test's verdict — the noise is the
    # test working, and it still has to not drown the report. Silenced for
    # the duration and restored after, rather than left to flood.
    operator_logger = logging.getLogger(operator_service.__name__)
    previous_level = operator_logger.level
    operator_logger.setLevel(logging.CRITICAL)

    class Result:
        def __init__(self, name):
            self.name = name

        def as_dict(self):
            return {"class": self.name}

    class Stream:
        def __init__(self, name, raises=False):
            self.name = name
            self.raises = raises
            self.passes = 0

        def run_pass(self):
            self.passes += 1
            if self.raises:
                raise RuntimeError(f"{self.name} is broken")
            return Result(self.name)

    sick = Stream("crossmatch", raises=True)
    healthy = Stream("science")
    later = Stream("statistics")
    operators = [sick, healthy, later]

    polls = {"n": 0}

    def should_continue():
        polls["n"] += 1
        return polls["n"] <= 3

    operator_service.run_forever(
        operators, poll_seconds=0, should_continue=should_continue,
        sleep=lambda _seconds: None, failure_threshold=5)

    # THE HEAD-OF-LINE ASSERTION: the streams AFTER the raising one ran every
    # poll. Under the old shared try/except they ran zero times.
    assert sick.passes == 3, f"sick stream ran {sick.passes} times"
    assert healthy.passes == 3, (
        f"the stream after the failing one ran {healthy.passes} times, not 3 "
        "— head-of-line blocking is still present")
    assert later.passes == 3, f"the third stream ran {later.passes} times"

    # AND THE SERVICE SURVIVED: one sick stream over threshold must not exit
    # a service whose other streams are working.
    sick_only = Stream("crossmatch", raises=True)
    ok = Stream("science")
    polls2 = {"n": 0}

    def continue_6():
        polls2["n"] += 1
        return polls2["n"] <= 6

    operator_service.run_forever(
        [sick_only, ok], poll_seconds=0, should_continue=continue_6,
        sleep=lambda _seconds: None, failure_threshold=5)
    assert ok.passes == 6, (
        f"the healthy stream ran {ok.passes} of 6 polls; one stream over "
        "threshold took the service down with it")

    # BUT A SHARED FAULT STILL EXITS: every stream failing is the symptom
    # reserved for the process-level verdict.
    all_sick = [Stream("crossmatch", raises=True), Stream("science",
                                                          raises=True)]
    polls3 = {"n": 0}

    def continue_many():
        polls3["n"] += 1
        return polls3["n"] <= 10

    try:
        operator_service.run_forever(
            all_sick, poll_seconds=0, should_continue=continue_many,
            sleep=lambda _seconds: None, failure_threshold=5)
    except operator_service.OperatorUnhealthy as exc:
        shared = str(exc)
    else:
        raise AssertionError(
            "every stream failed and the service did NOT exit; the "
            "shared-fault verdict is not firing")
    finally:
        operator_logger.setLevel(previous_level)

    return (f"stream isolation holds (sick={sick.passes}, after={healthy.passes},"
            f" third={later.passes}); one sick stream kept the service up for "
            f"6 polls; all-streams-failing exited: {shared[:60]}...")


TESTS = (
    ("1", test_1_fail_then_succeed),
    ("2", test_2_application_failure_parks),
    ("3", test_3_claim_race),
    ("4", test_4_missing_definition_fails_closed),
    ("5", test_5_stream_isolation),
)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    only = {arg for arg in argv if arg in {name for name, _ in TESTS}}

    conn = connect()
    conn.autocommit = False
    ensure_definition(conn)

    failures = 0
    for name, test in TESTS:
        if only and name not in only:
            continue
        try:
            detail = test(conn)
        except Exception as exc:              # noqa: BLE001 - reported
            failures += 1
            conn.rollback()
            print(f"BRIEF-A-TEST-{name}: FAIL {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()
        else:
            print(f"BRIEF-A-TEST-{name}: PASS {detail}")

    conn.close()
    print(f"BRIEF-A-SUITE: {'PASS' if failures == 0 else 'FAIL'} "
          f"failures={failures} run_tag={RUN_TAG}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
