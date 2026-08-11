"""Contract tests: the ad-hoc mutation tools, wrapped (brief G, criterion 4).

    4. "A wrapped tool invocation (supersede path, against fixture rows)
        writes its audit row and honors dry-run default."

**THESE SKIP WHERE DRAFT MIGRATION 047 IS ABSENT** — the audit row goes
through `derived.record_external_action`, which 047 creates. See
`test_operator_mutations.py`'s header for why the drafts are staged rather
than applied.

WHAT IS UNDER TEST, AND WHAT IS DELIBERATELY NOT. The wrapping is the
change: mandatory reason, idempotency key, dry-run default, and a durable
audit row for an action whose target is outside this database. That is
asserted here, for real, against the real audit table.

The wrapped tools' own behaviour is NOT re-tested. `supersede_lost_evidence`
keeps its per-row precondition re-check and its publish-before-flag ordering
untouched, and `terminate_batch_jobs` keeps its listing and termination
logic; re-asserting them here would be testing code this brief did not
change, and doing it through a stub would test the stub.

NO LIVE AWS, EVER. Criterion 4 says "against fixture rows", and the brief is
explicit that G3's tool wrapping is tested against fixtures and never live
jobs. The Batch client is injected; the object store is injected. Both
doubles REFUSE the operations the test does not expect (a `terminate_job`
during a dry run raises rather than returning a plausible acknowledgement),
because a double that cannot refuse cannot catch the defect it exists to
catch.
"""

import uuid

import pytest

from pipeline.contract import fixture
from pipeline.operatorctl import actions
from pipeline.operatorctl.batch import terminate_jobs_audited
from pipeline.operatorctl.contract import ExpectedStateMismatch

pytestmark = pytest.mark.usefixtures("_requires_draft_047")


@pytest.fixture
def _requires_draft_047(conn):
    """Skip unless DRAFT 047 is applied. Probed, never assumed."""
    if not actions.draft_schema_present(conn):
        pytest.skip(
            "DRAFT migration 047 (record_external_action) is not applied; "
            "the wrapped-tool tests need it and skip cleanly without it")


def _key(name):
    return "brief-g-%s-%s-%s" % (name, fixture.RUN_TAG, uuid.uuid4().hex[:8])


def _audit_rows(conn, key):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT audit_id, action_class, action_tier, target_scope, "
            "       reason, dry_run, rows_affected, detail, idempotency_key "
            "  FROM derived.mutation_audit "
            " WHERE idempotency_key = %s ORDER BY audit_id", [key])
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, r)) for r in cur.fetchall()]


class _RefusingBatchClient:
    """A Batch client that refuses what it was not told to expect.

    `terminate_job` raises unless the test explicitly permitted
    termination. That is the point: a dry-run test whose double happily
    accepted terminations would pass whether or not the dry-run default
    worked.
    """

    def __init__(self, jobs, allow_terminate=False):
        self._jobs = jobs
        self.allow_terminate = allow_terminate
        self.terminated = []

    def get_paginator(self, name):
        assert name == "list_jobs"
        return self

    def paginate(self, jobQueue=None, jobStatus=None):   # noqa: N803 — boto3
        return [{"jobSummaryList": [j for j in self._jobs
                                    if j["status"] == jobStatus]}]

    def terminate_job(self, jobId=None, reason=None):     # noqa: N803 — boto3
        if not self.allow_terminate:
            raise AssertionError(
                "terminate_job called when no termination was expected — "
                "the dry-run default did not hold")
        self.terminated.append((jobId, reason))


def _jobs(count, status="RUNNING"):
    return [{"jobId": "job-%s-%d" % (fixture.RUN_TAG, i),
             "jobName": "fixture-%d" % i, "status": status}
            for i in range(count)]


# ---------------------------------------------------------------------------
# The Batch termination path.
# ---------------------------------------------------------------------------
def test_batch_termination_dry_run_audits_and_terminates_nothing(conn):
    """Dry-run is the default and it reaches AWS not at all.

    The audit row is still written — a rehearsal is recorded, exactly as
    the database actions record theirs — and 030's CHECK forbids it
    claiming rows changed, so `rows_affected` is 0 even though three jobs
    are in scope. The scope and the count live in `detail`.
    """
    client = _RefusingBatchClient(_jobs(3), allow_terminate=False)
    key = _key("batch-dry")

    result, scope = terminate_jobs_audited(
        conn, key, "rapid-queue", ["RUNNING"],
        "brief G criterion 4: rehearsal", dry_run=True,
        session_factory=lambda: client, out=open("/dev/null", "w"))

    assert result["dry_run"] is True
    assert client.terminated == []

    rows = _audit_rows(conn, key)
    assert len(rows) == 1, "a dry run of an external action is audited too"
    assert rows[0]["action_class"] == "external_batch_terminate"
    assert rows[0]["action_tier"] == "operate"
    assert rows[0]["dry_run"] is True
    assert rows[0]["rows_affected"] == 0
    assert rows[0]["detail"]["job_count"] == 3
    assert rows[0]["reason"] == "brief G criterion 4: rehearsal"
    assert scope == "batch:queue=rapid-queue:states=RUNNING"


def test_batch_termination_apply_audits_the_real_action(conn):
    """The applied path terminates and records what it terminated.

    The ledger records operator ACTIONS, not only database mutations —
    this row is the entire durable trace that three Batch jobs were
    killed, by whom, and why. The tool it wraps printed that to stdout
    and kept nothing.
    """
    jobs = _jobs(3)
    client = _RefusingBatchClient(jobs, allow_terminate=True)
    key = _key("batch-apply")

    result, _ = terminate_jobs_audited(
        conn, key, "rapid-queue", ["RUNNING"],
        "brief G criterion 4: the real thing", dry_run=False,
        session_factory=lambda: client, out=open("/dev/null", "w"))

    assert result["dry_run"] is False
    assert len(client.terminated) == 3
    # The reason travels to AWS as well as to the ledger, so the Batch
    # console shows the same account of why as the audit history does.
    assert all(r == "brief G criterion 4: the real thing"
               for _, r in client.terminated)

    rows = _audit_rows(conn, key)
    assert len(rows) == 1
    assert rows[0]["dry_run"] is False
    assert rows[0]["rows_affected"] == 3
    assert len(rows[0]["detail"]["job_ids"]) == 3


def test_batch_termination_replays_rather_than_terminating_twice(conn):
    """A repeat under one key does not issue a second round of kills.

    The failure this prevents is severe and plausible: an operator whose
    terminate command appeared to hang re-runs it, and without a key the
    second run lists the queue afresh and kills whatever is in it now —
    including jobs submitted since, which nobody decided to kill.
    """
    client = _RefusingBatchClient(_jobs(2), allow_terminate=True)
    key = _key("batch-replay")
    devnull = open("/dev/null", "w")

    first, _ = terminate_jobs_audited(
        conn, key, "rapid-queue", ["RUNNING"], "brief G criterion 4",
        dry_run=False, session_factory=lambda: client, out=devnull)
    second, _ = terminate_jobs_audited(
        conn, key, "rapid-queue", ["RUNNING"], "brief G criterion 4",
        dry_run=False, session_factory=lambda: client, out=devnull)

    assert first["replayed"] is False
    assert second["replayed"] is True
    assert len(client.terminated) == 2, (
        "the replay issued a second round of terminations")
    assert len(_audit_rows(conn, key)) == 1


def test_batch_termination_refuses_on_expected_state_mismatch(conn):
    """The queue moved since the operator looked; the decision is stale."""
    client = _RefusingBatchClient(_jobs(5), allow_terminate=False)
    key = _key("batch-mismatch")

    with pytest.raises(ExpectedStateMismatch):
        terminate_jobs_audited(
            conn, key, "rapid-queue", ["RUNNING"], "brief G criterion 4",
            expected_state={"jobs": 2}, dry_run=False,
            session_factory=lambda: client, out=open("/dev/null", "w"))

    assert client.terminated == []
    assert _audit_rows(conn, key) == [], (
        "a refused external action wrote an audit row")


# ---------------------------------------------------------------------------
# The supersede path.
# ---------------------------------------------------------------------------
class _FakeStore:
    """An object store whose contents the test states explicitly.

    `evidence_is_absent` asks it whether a key exists; nothing else here
    depends on the store, and a `put` during a dry run raises because a
    dry run must not publish a closure record.
    """

    def __init__(self, present_keys=(), allow_put=False):
        self._present = set(present_keys)
        self.allow_put = allow_put
        self.puts = []

    def exists(self, key):
        return key in self._present

    def get(self, key):
        if key not in self._present:
            raise KeyError(key)
        return b"{}"

    def put(self, key, body, **kwargs):
        if not self.allow_put:
            raise AssertionError(
                "a closure record was published during a dry run")
        self.puts.append(key)


def test_supersede_dry_run_audits_and_publishes_nothing(conn):
    """The wrapped supersede pass honours the dry-run default.

    Run against zero candidate rows: the assertion under test is the
    WRAPPING — that a reason is captured, an audit row is written, and
    nothing is published — not the tool's row-selection logic, which this
    brief did not change and which its own module already owns.
    """
    from pipeline.operatorctl.supersede import supersede_audited

    store = _FakeStore(allow_put=False)
    key = _key("supersede-dry")
    run_prefix = "brief-g-%s-" % fixture.RUN_TAG

    result, scope = supersede_audited(
        conn, key, run_prefix, "brief G criterion 4: supersede rehearsal",
        dry_run=True, store=store, out=open("/dev/null", "w"))

    assert result["dry_run"] is True
    assert store.puts == []

    rows = _audit_rows(conn, key)
    assert len(rows) == 1
    assert rows[0]["action_class"] == "external_evidence_supersede"
    assert rows[0]["dry_run"] is True
    assert rows[0]["rows_affected"] == 0
    assert rows[0]["reason"] == "brief G criterion 4: supersede rehearsal"
    assert rows[0]["detail"]["run_prefix"] == run_prefix
    assert scope == "attempts:run_prefix=%s" % run_prefix


def test_supersede_captures_a_reason_the_original_tool_never_had(conn):
    """The gap G3 names: the tool had `--apply` friction but no reason.

    A mandatory reason at this boundary is the difference between a
    ledger that says "someone superseded eleven attempts under prefix X
    on Tuesday" and one that says nothing at all, which is what the
    unwrapped tool left behind.
    """
    from pipeline.operatorctl.supersede import supersede_audited

    key = _key("supersede-reason")
    run_prefix = "brief-g-reason-%s-" % fixture.RUN_TAG

    supersede_audited(
        conn, key, run_prefix, "closing out W8 evidence loss",
        dry_run=False, store=_FakeStore(allow_put=True),
        out=open("/dev/null", "w"))

    rows = _audit_rows(conn, key)
    assert len(rows) == 1
    assert rows[0]["reason"] == "closing out W8 evidence loss"
    assert rows[0]["dry_run"] is False
