"""Contract tests: rule 7's ambiguous-submission protocol (brief C1,
acceptance criterion 1).

    "a submission record driven to UNKNOWN resolves FOUND via identity
     re-query (fake Batch answer shape) and LOST via negative re-query past
     the horizon; `submit_job` is demonstrably not re-invoked for the same
     record; BOUND path unchanged."

**THESE SKIP WHERE MIGRATION 044 IS ABSENT.** 044 (`submissions`) was a DRAFT
against `rapid_systems` when this module was first written; it and eight
siblings were adopted verbatim into `rapid_systems` main on 2026-08-12
(`migrations-draft/README.md`), and `smdc` CI's pinned revision
(`.github/workflows/contract-tests.yml`) now carries it — so these tests RUN
in CI today, rather than skipping there as the original docstring here said.
The probe (`fixture.has_table`) stays: any database genuinely short of 044
(a mid-rollout environment, an older pin) still skips cleanly instead of
failing, which is the property worth keeping regardless of where the
migration currently lives.

Why the contract tier: every state this protocol depends on is enforced by a
CHECK constraint that only PostgreSQL evaluates —
`submissions_call_once_ck` is what makes "the API call is never repeated for a
submission row" a property of the schema instead of a promise in a docstring,
and a fake executor that accepted a second `calling` write would agree with
code that cannot work. That is the same failure `test_work_unit_cas.py`'s
header describes and this tier exists to prevent.

THE BATCH CLIENT IS A DOUBLE, and it is one that can REFUSE. `fixture.py`'s
header states the discipline ("DOUBLES MUST BE ABLE TO REFUSE"): the fake here
counts `submit_job` invocations and the tests assert that count is zero, so
the double's job is to catch the protocol calling something it must not,
rather than to make the happy path pass.

**PACKAGE S ADDITIONS** (rule 7's reconciler wiring, the last rapid-side
PARTIAL): `resolve_open` had zero test coverage anywhere before this — every
test above drives `resolve` on one row — so the tests below are its first,
covering the per-row loop and one row's failure not stopping the pass.
`submission_for_attempt` is the new lookup `pipeline.reconciler.service`
needs to let a durable FOUND/LOST record decide over the submission-anchored
horizon; its tests cover the join through `attempts.submission_id` and the
no-link case the reconciler's fail-open posture depends on. The durability
test proves `resolve_open`'s commit boundary — undocumented and untested
before this package — from a second, independent connection.
"""

import datetime
import threading

import pytest

from pipeline.contract import fixture
from submission import protocol

pytestmark = pytest.mark.usefixtures("_requires_submissions_table")


@pytest.fixture
def _requires_submissions_table(conn):
    """Skip unless DRAFT 044 is applied. Probed, never assumed."""
    if not fixture.has_table(conn, "submissions"):
        pytest.skip(
            "DRAFT migration 044 (submissions) is not applied; the "
            "submission-protocol tests need it and skip cleanly without it")


class _FakeBatch:
    """A Batch double that records every call and can be told what to answer.

    `submit_job` raises by default rather than returning something plausible:
    no test in this file is entitled to call it, so the double refuses instead
    of silently permitting the one thing the protocol forbids.
    """

    def __init__(self, known_jobs=None):
        self.known_jobs = dict(known_jobs or {})
        self.submit_calls = []
        self.describe_calls = []

    def submit_job(self, **kwargs):
        self.submit_calls.append(kwargs)
        raise AssertionError(
            "submit_job was called for a submission row that already exists; "
            "rule 7 forbids repeating the API call for a submission record")

    def describe(self, job_name, job_queue):
        self.describe_calls.append((job_name, job_queue))
        return self.known_jobs.get(job_name)


def _prepare(conn, name):
    """One submission row in PREPARED, under this run's tag."""
    execute = fixture.executor(conn)
    run_id = f"{name}-{fixture.RUN_TAG}"
    submission_id = protocol.prepare(
        execute, run_id=run_id, job_type=fixture.JOB_TYPE,
        job_name=f"rapid-{run_id}", job_queue="contract-queue",
        job_definition="contract-jobdef",
        manifest_checksum="sha256:" + "2" * 64,
        manifest_uri=f"s3://contract/{run_id}/manifest.json",
        array_size=3)
    conn.commit()
    return submission_id, run_id


def _state(conn, submission_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state, scheduler_job_id, call_started_at, resolved_at"
            "  FROM submissions WHERE submission_id = %s", [submission_id])
        return cur.fetchone()


def test_the_bound_path_is_unchanged(conn):
    """PREPARED -> CALLING -> BOUND, the happy path. Criterion 1's last clause."""
    execute = fixture.executor(conn)
    submission_id, _ = _prepare(conn, "bound")

    assert _state(conn, submission_id)[0] == protocol.PREPARED
    protocol.mark_calling(execute, submission_id)
    conn.commit()

    state, job_id, call_started, resolved = _state(conn, submission_id)
    assert state == protocol.CALLING
    # The call marker is durable and the row is not yet resolved — which is
    # exactly the shape a crash here must leave behind for the re-query.
    assert call_started is not None and job_id is None and resolved is None

    protocol.mark_bound(execute, submission_id, "job-abc123")
    conn.commit()

    state, job_id, _call, resolved = _state(conn, submission_id)
    assert (state, job_id) == (protocol.BOUND, "job-abc123")
    assert resolved is not None


def test_unknown_resolves_found_by_identity_requery(conn):
    """The job exists; the re-query finds it and submit_job is never called.

    Criterion 1's first half. The fake answers the job name the row carries —
    the deterministic identity `build_submit_kwargs` assigns — which is what
    makes a positive answer possible without ever having received a jobId.
    """
    execute = fixture.executor(conn)
    submission_id, run_id = _prepare(conn, "found")
    job_name = f"rapid-{run_id}"

    protocol.mark_calling(execute, submission_id)
    protocol.mark_unknown(execute, submission_id, detail="ReadTimeoutError")
    conn.commit()

    state, job_id, _c, _r = _state(conn, submission_id)
    assert (state, job_id) == (protocol.UNKNOWN, None)

    batch = _FakeBatch(known_jobs={job_name: "job-recovered-1"})
    row = next(r for r in protocol.open_submissions(execute)
               if r["submission_id"] == submission_id)
    outcome = protocol.resolve(execute, row, batch.describe)
    conn.commit()

    assert outcome == protocol.FOUND
    state, job_id, _c, resolved = _state(conn, submission_id)
    assert (state, job_id) == (protocol.FOUND, "job-recovered-1")
    assert resolved is not None
    # The re-query asked by NAME, and the submit API was never touched.
    assert batch.describe_calls == [(job_name, "contract-queue")]
    assert batch.submit_calls == []


def test_unknown_resolves_lost_only_past_the_deadline(conn):
    """A negative answer before the deadline is "not yet", not "absent".

    Criterion 1's second half, and the property that keeps this protocol from
    reintroducing the defect it replaces: Batch is eventually consistent, so
    an early negative is not evidence. Only a negative PAST the backstop
    concludes LOST — never elapsed time alone, and never a single lookup.
    """
    execute = fixture.executor(conn)
    submission_id, _ = _prepare(conn, "lost")

    protocol.mark_calling(execute, submission_id)
    protocol.mark_unknown(execute, submission_id, detail="ConnectionReset")
    conn.commit()

    batch = _FakeBatch()  # knows no jobs: every answer is negative

    # BEFORE the deadline: still unknown, deliberately.
    row = next(r for r in protocol.open_submissions(execute)
               if r["submission_id"] == submission_id)
    assert protocol.resolve(execute, row, batch.describe) == protocol.UNKNOWN
    conn.commit()
    assert _state(conn, submission_id)[0] == protocol.UNKNOWN

    # PAST the deadline, same negative answer: now it means absent.
    row = next(r for r in protocol.open_submissions(execute)
               if r["submission_id"] == submission_id)
    past = row["resolution_deadline"] + datetime.timedelta(seconds=1)
    assert protocol.resolve(execute, row, batch.describe,
                            now=past) == protocol.LOST
    conn.commit()

    state, job_id, _c, resolved = _state(conn, submission_id)
    assert (state, job_id) == (protocol.LOST, None)
    assert resolved is not None
    # Two negative lookups, and still no submit_job for this row.
    assert len(batch.describe_calls) == 2
    assert batch.submit_calls == []


def test_the_call_is_never_repeated_for_one_row(conn):
    """The schema refuses a second `calling`, not merely the code.

    `submissions_call_once_ck` plus the CAS on `state = 'prepared'` mean a row
    that has called can never call again — "the API call is never repeated for
    a submission row" is enforced where it cannot be forgotten. A resubmission
    after LOST mints a NEW row, so counting rows answers "how many times did
    we call Batch for this work".
    """
    execute = fixture.executor(conn)
    submission_id, _ = _prepare(conn, "once")

    protocol.mark_calling(execute, submission_id)
    conn.commit()

    with pytest.raises(protocol.SubmissionStateConflict):
        protocol.mark_calling(execute, submission_id)
    conn.rollback()

    assert _state(conn, submission_id)[0] == protocol.CALLING


def test_an_unreachable_scheduler_resolves_nothing(conn):
    """A describe that RAISES leaves the row open and the error propagates.

    Concluding LOST from a failed lookup would authorize a duplicate
    submission on the strength of a network problem — the same class of error
    as concluding it from a clock, which is what this protocol exists to
    replace.
    """
    execute = fixture.executor(conn)
    submission_id, _ = _prepare(conn, "unreachable")
    protocol.mark_calling(execute, submission_id)
    protocol.mark_unknown(execute, submission_id, detail="Throttled")
    conn.commit()

    def broken(job_name, job_queue):
        raise RuntimeError("Batch is unreachable")

    row = next(r for r in protocol.open_submissions(execute)
               if r["submission_id"] == submission_id)
    with pytest.raises(RuntimeError):
        protocol.resolve(execute, row, broken)
    conn.rollback()

    assert _state(conn, submission_id)[0] == protocol.UNKNOWN


def test_resolve_open_resolves_every_open_row_in_one_pass(conn):
    """`resolve_open`'s first coverage (evidence pass: it had none).

    Three rows in one pass — FOUND, LOST, and left UNKNOWN — proving the
    per-row loop's counts and that one row's outcome does not affect another's.

    `conn` is the SHARED session connection (`conftest.py`: several tests
    need their writes visible to a second connection, which a
    wrapping-transaction-per-test would hide) — so by this point in a run
    other tests' rows may still be sitting `unknown`/`calling` too, and
    `resolve_open` legitimately resolves those in the same pass. This test
    therefore checks its OWN three rows' end states rather than asserting
    global counts, which would be a property of test execution order, not of
    `resolve_open`.
    """
    execute = fixture.executor(conn)
    found_id, found_run = _prepare(conn, "open-found")
    lost_id, _ = _prepare(conn, "open-lost")
    unknown_id, _ = _prepare(conn, "open-unknown")
    for submission_id in (found_id, lost_id, unknown_id):
        protocol.mark_calling(execute, submission_id)
    conn.commit()
    protocol.mark_unknown(execute, lost_id, detail="ConnectionReset")
    protocol.mark_unknown(execute, unknown_id, detail="ConnectionReset")
    conn.commit()

    found_name = f"rapid-{found_run}"
    batch = _FakeBatch(known_jobs={found_name: "job-recovered-2"})
    # `now` stays real (not pushed into the future): unknown_id's own
    # deadline is ~30 minutes out, so it is provably still open at call time,
    # and lost_id is resolved LOST separately below, past ITS OWN deadline
    # only — pushing `now` forward for the whole pass would also sweep any
    # other session-shared `unknown` row past its deadline, which is exactly
    # the false-global-count failure mode this test rewrite avoids.
    counts = protocol.resolve_open(execute, batch.describe)
    conn.commit()

    assert counts[protocol.FOUND] >= 1
    assert counts[protocol.UNKNOWN] >= 1
    assert counts["errors"] == 0
    assert _state(conn, found_id)[:2] == (protocol.FOUND, "job-recovered-2")
    assert _state(conn, unknown_id)[0] == protocol.UNKNOWN

    # lost_id: resolve it a second time, past ITS OWN deadline, isolated from
    # whatever else is open in the shared connection.
    with conn.cursor() as cur:
        cur.execute("SELECT resolution_deadline FROM submissions"
                    " WHERE submission_id = %s", [lost_id])
        deadline = cur.fetchone()[0]
    past = deadline + datetime.timedelta(seconds=1)
    row = next(r for r in protocol.open_submissions(execute)
               if r["submission_id"] == lost_id)
    assert protocol.resolve(execute, row, batch.describe,
                            now=past) == protocol.LOST
    conn.commit()

    assert _state(conn, lost_id)[0] == protocol.LOST
    assert batch.submit_calls == []


def test_resolve_open_one_rows_failure_does_not_stop_the_others(conn):
    """A `describe` that raises for one row must not abort the pass.

    `conn` is the shared session connection (see the previous test's note),
    so this checks `ok_id`/`bad_id`'s own end states rather than the pass's
    global counts, which other tests' leftover open rows would also feed.
    """
    execute = fixture.executor(conn)
    ok_id, ok_run = _prepare(conn, "open-ok")
    bad_id, bad_run = _prepare(conn, "open-bad")
    protocol.mark_calling(execute, ok_id)
    protocol.mark_calling(execute, bad_id)
    conn.commit()
    protocol.mark_unknown(execute, ok_id, detail="x")
    protocol.mark_unknown(execute, bad_id, detail="x")
    conn.commit()

    ok_name = f"rapid-{ok_run}"
    bad_name = f"rapid-{bad_run}"

    def selective_describe(job_name, job_queue):
        if job_name == bad_name:
            raise RuntimeError("Batch is unreachable for this one job")
        return "job-ok-1" if job_name == ok_name else None

    counts = protocol.resolve_open(execute, selective_describe)
    conn.commit()

    assert counts[protocol.FOUND] >= 1
    assert counts["errors"] >= 1
    assert _state(conn, ok_id)[:2] == (protocol.FOUND, "job-ok-1")
    # The failed row is untouched, exactly as a single resolve() would leave
    # it — resolve_open's per-row try/except must not write a partial result.
    assert _state(conn, bad_id)[0] == protocol.UNKNOWN


def _attach_one_attempt(conn, submission_id):
    """One real `attempts` row (schema-honest via `fixture.make_attempt`),
    linked to `submission_id` through the same `attach_attempts` production
    code uses. Returns the attempt_id.

    `lifecycle="terminal_without_start"`, matching every other contract file
    that calls `make_attempt` — the default `lifecycle="submitted"` needs the
    full binding triple at `schema_version >= 2`
    (`attempts_state_submitted_check`), which `make_logical_job` only writes
    when called `with_binding=True`. No caller anywhere in this repo uses
    the plain default against real PostgreSQL; found live by this test.
    """
    from pipeline.contract import fixture as fx
    execute = fixture.executor(conn)
    attempt_id = fx.make_attempt(conn, lifecycle="terminal_without_start")
    protocol.attach_attempts(execute, submission_id, [attempt_id])
    return attempt_id


def test_submission_for_attempt_reads_the_linked_row(conn):
    """`submission_for_attempt` — package S's new lookup, and its first
    coverage: the join through `attempts.submission_id`, exactly the FK
    `attach_attempts` maintains."""
    execute = fixture.executor(conn)
    submission_id, run_id = _prepare(conn, "lookup")
    attempt_id = _attach_one_attempt(conn, submission_id)
    conn.commit()

    found = protocol.submission_for_attempt(execute, attempt_id)

    assert found["submission_id"] == submission_id
    assert found["state"] == protocol.PREPARED
    assert found["job_name"] == f"rapid-{run_id}"
    assert found["job_queue"] == "contract-queue"

    protocol.mark_calling(execute, submission_id)
    conn.commit()
    updated = protocol.submission_for_attempt(execute, attempt_id)
    assert updated["state"] == protocol.CALLING


def test_submission_for_attempt_returns_none_with_no_link(conn):
    """No `submission_id` on the attempt (pre-044, or a failed-open
    submission pass) -> `None`, never an error. The reconciler's fail-open
    posture depends on this returning cleanly rather than raising."""
    from pipeline.contract import fixture as fx
    execute = fixture.executor(conn)
    attempt_id = fx.make_attempt(
        conn, lifecycle="terminal_without_start")  # no attach_attempts call

    assert protocol.submission_for_attempt(execute, attempt_id) is None


def test_two_concurrent_resolution_passes_do_not_double_transition_a_row(
        conn, second_conn):
    """Coverage gap 1: concurrent polls racing the resolution pass.

    `resolve_open` takes no row lock (`open_submissions`'s `_OPEN_SQL` is a
    plain SELECT — see its definition) — the only thing preventing two
    overlapping reconciler cycles from both resolving the same UNKNOWN row is
    `mark_lost`'s CAS (`_MARK_LOST_SQL`'s `WHERE ... AND state = 'unknown'`).
    Genuinely concurrent: two connections, two threads, a barrier so both
    read the row via `open_submissions` before either writes — that ordering
    is what makes it a race rather than two serialized calls that would
    trivially see `lost` on the second read.

    Both racers see a negative `describe` (the job stays permanently
    unfound), past the deadline, so both attempt `mark_lost` on the SAME row.
    Exactly one must win; the other must get zero CAS rows, which `resolve`
    surfaces as `SubmissionStateConflict` (`_require_one`'s only caller path
    here) rather than a silent second write or a corrupted mixed state.
    """
    submission_id, _run_id = _prepare(conn, "race")
    execute_a = fixture.executor(conn)
    protocol.mark_calling(execute_a, submission_id)
    conn.commit()
    past = (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=1))
    protocol.mark_unknown(execute_a, submission_id, detail="x",
                          horizon_seconds=-1)
    conn.commit()

    def never_found(job_name, job_queue):
        return None  # both racers see a negative re-query -> LOST

    barrier = threading.Barrier(2)
    outcomes = {}

    def race(slot, connection):
        execute = fixture.executor(connection)
        row = next(r for r in protocol.open_submissions(execute)
                   if r["submission_id"] == submission_id)
        barrier.wait(timeout=30)          # both have now SELECTed the row
        try:
            state = protocol.resolve(execute, row, never_found, now=past
                                     + datetime.timedelta(seconds=2))
            connection.commit()
            outcomes[slot] = ("won", state)
        except protocol.SubmissionStateConflict:
            connection.rollback()
            outcomes[slot] = ("refused", None)
        except Exception as exc:          # noqa: BLE001 - reported, not hidden
            connection.rollback()
            outcomes[slot] = (f"raised:{type(exc).__name__}", None)

    threads = [threading.Thread(target=race, args=(slot, connection))
               for slot, connection in (("a", conn), ("b", second_conn))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(outcomes) == 2, f"only {outcomes} completed"
    won = [slot for slot, (result, _state) in outcomes.items()
           if result == "won"]
    refused = [slot for slot, (result, _state) in outcomes.items()
              if result == "refused"]
    assert len(won) == 1, f"expected exactly one winner, got {outcomes}"
    assert len(refused) == 1, f"expected exactly one refusal, got {outcomes}"
    assert outcomes[won[0]][1] == protocol.LOST

    # AND THE ROW LANDED EXACTLY ONCE: no double transition, no split state.
    final_state, job_id, _call, resolved = _state(conn, submission_id)
    assert final_state == protocol.LOST
    assert job_id is None
    assert resolved is not None


def test_a_resolution_pass_is_visible_from_a_second_connection(conn,
                                                                second_conn):
    """Durability, criterion 13 — proven, not assumed.

    `resolve_open`'s docstring states no commit boundary (the evidence pass
    flagged this explicitly). The reconciler commits per resolved row
    (`ReconcilerService._resolve_submissions`'s own comment); this proves
    that choice actually lands: after a pass that resolves a row AND commits,
    an independent connection sees the committed state, not merely this one.
    """
    execute = fixture.executor(conn)
    submission_id, run_id = _prepare(conn, "durable")
    protocol.mark_calling(execute, submission_id)
    conn.commit()
    protocol.mark_unknown(execute, submission_id, detail="x")
    conn.commit()

    job_name = f"rapid-{run_id}"
    batch = _FakeBatch(known_jobs={job_name: "job-durable-1"})
    # `conn` is the shared session connection, so other tests' open rows may
    # also resolve in this same pass (see the earlier tests' notes) — this
    # test checks its OWN row's visibility, not the pass's global count.
    counts = protocol.resolve_open(execute, batch.describe)
    assert counts[protocol.FOUND] >= 1
    conn.commit()  # the commit boundary under test

    with second_conn.cursor() as cur:
        cur.execute("SELECT state, scheduler_job_id FROM submissions"
                    " WHERE submission_id = %s", [submission_id])
        state, job_id = cur.fetchone()
    assert (state, job_id) == (protocol.FOUND, "job-durable-1")


def test_an_interrupted_call_is_as_ambiguous_as_a_judged_one(conn):
    """A row stuck in CALLING gets a deadline and joins the same resolution.

    The process died between the API call and either outcome write. That row
    is exactly as ambiguous as an `unknown` one — a request was in flight and
    a job may exist — so it resolves the same way rather than being stranded
    in a state nothing sweeps.
    """
    execute = fixture.executor(conn)
    submission_id, _ = _prepare(conn, "interrupted")
    protocol.mark_calling(execute, submission_id)
    conn.commit()

    batch = _FakeBatch()
    row = next(r for r in protocol.open_submissions(execute)
               if r["submission_id"] == submission_id)
    assert protocol.resolve(execute, row, batch.describe) == protocol.UNKNOWN
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT state, resolution_deadline FROM submissions"
                    " WHERE submission_id = %s", [submission_id])
        state, deadline = cur.fetchone()
    assert state == protocol.UNKNOWN
    assert deadline is not None
    assert batch.submit_calls == []


# ---------------------------------------------------------------------------
# THE UNIFIED SUBMISSION-OUTCOME FACT (campaign C4).
#
# `resolve_submission_outcome` collapses three vocabularies that used to be
# read independently: `submissions.state` (this module's own FOUND/LOST),
# `attempts.lifecycle_state == missing_or_contradictory` (a durable flag on
# the ATTEMPT row itself), and the reconciler's `never_resolved` closure
# classification (not exercised here — it is a label `pipeline.reconciler.
# service._reconcile_unresolved` writes into a CLOSURE RECORD once THIS
# function has already answered LOST/PENDING via the horizon path; the
# closure-record shape is `pipeline.reconciler.test`'s territory, not this
# module's). These three tests are the acceptance bar stated for C4: "the
# three previously-divergent cases resolving to one answer" — a FOUND
# submission, a LOST submission, and an attempt already flagged
# CONTRADICTORY, each read through the SAME function and each producing
# exactly the `SubmissionOutcome` the case calls for.
# ---------------------------------------------------------------------------

def _make_attempt_with_submission(conn, submission_id):
    """A `submitted` attempt row linked to `submission_id` via the real FK
    `attach_attempts` maintains — not a hand-set column, so the join
    `resolve_submission_outcome` reads through is the real one."""
    from pipeline.contract import fixture

    attempt_id = fixture.make_attempt(conn)
    execute = fixture.executor(conn)
    attached = protocol.attach_attempts(execute, submission_id, [attempt_id])
    assert attached == 1, "the fixture's own attach must succeed"
    conn.commit()
    return attempt_id


def test_a_found_submission_resolves_found(conn):
    """Case 1 of 3: a durably FOUND submission's linked attempt reads FOUND."""
    from submission.protocol import SubmissionOutcome, resolve_submission_outcome

    execute = fixture.executor(conn)
    submission_id, run_id = _prepare(conn, "outcome-found")
    protocol.mark_calling(execute, submission_id)
    conn.commit()
    protocol.mark_found(execute, submission_id, "job-outcome-found-1")
    conn.commit()
    attempt_id = _make_attempt_with_submission(conn, submission_id)

    row = {"attempt_id": attempt_id, "submission_id": submission_id,
           "lifecycle_state": "submitted"}
    assert (resolve_submission_outcome(execute, row)
            == SubmissionOutcome.FOUND)


def test_a_lost_submission_resolves_lost(conn):
    """Case 2 of 3: a durably LOST submission's linked attempt reads LOST."""
    from submission.protocol import SubmissionOutcome, resolve_submission_outcome

    execute = fixture.executor(conn)
    submission_id, _ = _prepare(conn, "outcome-lost")
    protocol.mark_calling(execute, submission_id)
    conn.commit()
    protocol.mark_unknown(execute, submission_id, detail="x")
    conn.commit()
    protocol.mark_lost(execute, submission_id)
    conn.commit()
    attempt_id = _make_attempt_with_submission(conn, submission_id)

    row = {"attempt_id": attempt_id, "submission_id": submission_id,
           "lifecycle_state": "submitted"}
    assert (resolve_submission_outcome(execute, row)
            == SubmissionOutcome.LOST)


def test_a_flagged_attempt_resolves_contradictory_even_with_a_found_submission(
        conn):
    """Case 3 of 3: an attempt already `missing_or_contradictory` reads
    CONTRADICTORY — checked FIRST, before the submission record is even
    consulted, so a FOUND submission on the SAME attempt cannot override it.
    This is the property that makes the fact durable-once: the row's own
    recorded conclusion is authoritative over a fresh join, not raced against
    it on every read.
    """
    from observability.attempts import AttemptWriter
    from pipeline.contract import fixture
    from submission.protocol import SubmissionOutcome, resolve_submission_outcome

    execute = fixture.executor(conn)
    submission_id, _ = _prepare(conn, "outcome-contradictory")
    protocol.mark_calling(execute, submission_id)
    conn.commit()
    protocol.mark_found(execute, submission_id, "job-outcome-contradictory-1")
    conn.commit()
    attempt_id = _make_attempt_with_submission(conn, submission_id)

    AttemptWriter(execute).mark_missing_or_contradictory(
        attempt_id, reconciliation_class="missing",
        reconciliation_sources=["postgres", "batch"],
        detected_at=datetime.datetime.now(datetime.timezone.utc))
    conn.commit()

    row = {"attempt_id": attempt_id, "submission_id": submission_id,
           "lifecycle_state": "missing_or_contradictory"}
    assert (resolve_submission_outcome(execute, row)
            == SubmissionOutcome.CONTRADICTORY)
