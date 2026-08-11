"""Contract tests: rule 7's ambiguous-submission protocol (brief C1,
acceptance criterion 1).

    "a submission record driven to UNKNOWN resolves FOUND via identity
     re-query (fake Batch answer shape) and LOST via negative re-query past
     the horizon; `submit_job` is demonstrably not re-invoked for the same
     record; BOUND path unchanged."

**THESE SKIP WHERE DRAFT MIGRATION 044 IS ABSENT**, which is everywhere the
authoritative stream is applied without this branch's drafts — CI included.
The `submissions` table is a change request against `rapid_systems`, not part
of the deployed schema, so the tier probes for it (`fixture.has_table`) and
skips cleanly rather than failing. The rapid-admin acceptance run applies base
+ drafts and therefore executes them.

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
"""

import datetime

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
