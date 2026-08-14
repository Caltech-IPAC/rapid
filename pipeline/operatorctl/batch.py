"""AWS Batch termination, brought under the mutation contract (brief G, G3).

``aws/terminate_batch_jobs.py`` terminates Batch jobs with a ``--dry-run``
flag and a ``--reason`` that DEFAULTS to "Terminated by script". It has no
actor, no idempotency key, no expected-state check, and its only record is
stdout — so the fleet's most consequential operator action is the one
action that leaves no trace anybody can audit afterwards.

WRAP, DON'T REWRITE. The listing and termination logic in that module is
correct and this module calls it: ``list_jobs`` and ``terminate_jobs`` are
imported and used as they are. What is added around them is the contract —
mandatory reason, idempotency key, expected-state refusal, and an audit row
in ``derived.mutation_audit`` per real invocation.

WHY AN AWS ACTION TAKES A DATABASE CONNECTION. The ledger records operator
ACTIONS, not only database mutations. A termination that happened is a fact
about the pipeline's history whether or not any row in this database
changed, and a ledger that omitted it would be a record of the safe actions
only. So this path opens the same operator session every other subcommand
opens, and writes through ``derived.record_external_action``.

THE ORDER, MATCHING `pipeline/operatorctl/gc.py`'s "record AFTER" PATTERN
(gc.py's own header names this exact defect: "the precedent's own defect is
not copied: `operatorctl/batch.py:80` records BEFORE the AWS action and its
prose claims a later update the code never performs"). `derived.mutation_
audit` is append-only — 030's one-path rule keeps no role holding UPDATE on
it — so there never was a later update to make the docstring's old claim
true; the row committed before the loop simply sat there, permanently
recording `rows_affected=len(jobs)` regardless of what the loop below it
actually managed to terminate. The fix is not a later UPDATE (there is no
such statement to reach for) but the other half of gc.py's pattern: replay
is checked FIRST via a read-only call to `derived.mutation_replay` — no row
written, nothing to undo if the call is abandoned before AWS is touched —
terminations are attempted per-job with each one's own outcome recorded,
and `record_external_action` is called EXACTLY ONCE, AFTER, with the REAL
per-job tally. A crash before that point leaves no audit row at all rather
than a false one; an operator who saw the tool die mid-run has the queue
itself to check before deciding whether to re-run. A re-run under the SAME
key after a crash is not itself replay-protected here (nothing was
recorded to replay against), but the re-listing pass narrows the risk: it
lists jobs in `--states` (RUNNING by default) fresh from AWS, so a job the
first attempt already terminated has left that state and is not re-listed
or re-terminated — only jobs the first attempt did not reach, or that
remained RUNNING despite it, are acted on again.
"""

import sys

from pipeline.operatorctl.actions import record_external_action
from pipeline.operatorctl.contract import ExpectedStateMismatch


def _scheduler_job_ids_for_scope(conn, attempt_ids, logical_job_ids):
    """The `scheduler_job_id`s named by `--attempt-id`/`--logical-job-id`.

    Returns `None` when the caller supplied neither — meaning "no attempt
    scope was asked for", distinct from an empty set, which would mean "an
    attempt scope was asked for and it named zero live jobs" and should
    narrow the population to nothing rather than be mistaken for "no
    scoping requested" and fall through to the unscoped queue/states
    population.

    A `NULL scheduler_job_id` (an array child before its post-Batch
    backfill, or a row `_bind_scheduler_jobs` never reached — see
    `pipeline.seams.SubmissionBookkeepingFailed`) resolves to nothing to
    terminate for that attempt: there is no Batch job id to terminate by,
    and this is a narrowing filter, not a promise that every named attempt
    scope resolves to a live job.
    """
    if not attempt_ids and not logical_job_ids:
        return None
    clauses = []
    params = []
    if attempt_ids:
        clauses.append("attempt_id = ANY(%s)")
        params.append(list(attempt_ids))
    if logical_job_ids:
        clauses.append("logical_job_id = ANY(%s)")
        params.append(list(logical_job_ids))
    sql = ("SELECT scheduler_job_id FROM attempts"
          " WHERE (%s) AND scheduler_job_id IS NOT NULL"
          % " OR ".join(clauses))
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return {row[0] for row in cur.fetchall()}


def terminate_jobs_audited(conn, idempotency_key, queue, states, reason,
                           expected_state=None, dry_run=True, region=None,
                           profile=None, policy_citation=None, out=None,
                           session_factory=None, attempt_ids=None,
                           logical_job_ids=None):
    """List, refuse-or-terminate, and audit. Returns (result, target_scope).

    ``session_factory`` exists for the contract tests: the audit and
    idempotency behaviour is what this module adds and what needs
    testing, and it must be testable without terminating a real job.
    A test passing a stub proves the ledger path; the AWS path itself is
    the wrapped module's, unchanged and already in use.

    ``attempt_ids``/``logical_job_ids`` NARROW the queue/states population
    to jobs whose `scheduler_job_id` matches one of the named attempts —
    they never widen it. A job Batch returns for `queue`/`states` that
    belongs to none of the named attempts is dropped from `jobs` before
    anything below this point sees it: the expected-state check, the audit
    row, and the termination loop all act on the narrowed population, so
    `--expect-candidates`-style refusal continues to refuse against
    exactly what will be acted on.
    """
    out = out or sys.stdout
    scope = "batch:queue=%s:states=%s" % (queue, ",".join(states))
    # THE SCOPE'S NARROWING SUFFIX IS APPENDED HERE, FROM THE CALLER'S OWN
    # ARGUMENTS — before any AWS or database listing call, and unconditional
    # on whether narrowing was actually requested (`allowed_job_ids is not
    # None` used to gate this same suffix; it no longer needs to, since
    # nothing here depends on `jobs`). This is what lets `scope` — and
    # therefore the replay lookup below — be computed with NO calls at all:
    # `_scheduler_job_ids_for_scope`'s only other job, narrowing the actual
    # `jobs` list, still happens after the replay check, once one is known
    # to be needed.
    if attempt_ids or logical_job_ids:
        scope += ":attempt_ids=%s:logical_job_ids=%s" % (
            ",".join(str(a) for a in (attempt_ids or ())) or "-",
            ",".join(logical_job_ids or ()) or "-")

    # REPLAY IS CHECKED FIRST, READ-ONLY, BEFORE ANY AWS CALL OR DATABASE
    # LISTING QUERY — never by writing the audit row up front and
    # inspecting its `replayed` flag afterward (the shape this replaced:
    # see the module docstring on why that made `rows_affected` a promise
    # the code never kept). A replay means this exact key already
    # terminated this exact scope; terminating again would be the second
    # mutation the key exists to prevent, so the recorded outcome is
    # returned and NEITHER AWS NOR `_scheduler_job_ids_for_scope`'s own
    # database query is reached at all. `_replay_lookup` also raises
    # `IdempotencyConflict` when the key was used for a different
    # action/scope, exactly as `record_external_action` itself would.
    replay = _replay_lookup(conn, idempotency_key, "external_batch_terminate",
                            scope)
    if replay is not None:
        return replay, scope

    # ONE client for the whole invocation: the listing pass and every
    # termination share it, so credentials resolve once without a global
    # holding a client between unrelated invocations.
    client = _client(region, profile, session_factory)
    jobs = _list_all(client, queue, states)

    allowed_job_ids = _scheduler_job_ids_for_scope(
        conn, attempt_ids, logical_job_ids)
    if allowed_job_ids is not None:
        jobs = [job for job in jobs if job["jobId"] in allowed_job_ids]

    # Expected state checked HERE, before the audit row and before any
    # termination: the operator said how many jobs they were acting on, and
    # a queue that has moved means the decision was made about a different
    # population. Raised as the same typed refusal draft 047 raises for the
    # database actions, so the CLI presents both identically.
    if expected_state is not None and "jobs" in expected_state:
        if expected_state["jobs"] != len(jobs):
            raise ExpectedStateMismatch(
                "expected-state mismatch: caller expected %s job(s) in "
                "scope, found %s" % (expected_state["jobs"], len(jobs)))

    detail = {"queue": queue, "states": list(states),
              "job_count": len(jobs),
              "job_ids": [j["jobId"] for j in jobs[:200]]}
    if allowed_job_ids is not None:
        detail["attempt_ids"] = list(attempt_ids or ())
        detail["logical_job_ids"] = list(logical_job_ids or ())
    # Bounded at 200 ids: the audit row records what was acted on, and a
    # queue of ten thousand jobs would otherwise put a megabyte of jsonb in
    # a history row. The count above is exact regardless.
    if len(jobs) > 200:
        detail["job_ids_truncated"] = True

    if dry_run:
        # THE DRY RUN IS AUDITED TOO (unchanged from before) — a rehearsal
        # is recorded, and 030's CHECK forbids it claiming rows changed, so
        # `rows_affected=0` here is correct rather than `len(jobs)`: no job
        # was terminated, dry-run or not.
        result = record_external_action(
            conn, idempotency_key, "external_batch_terminate", scope, reason,
            expected_state=expected_state, dry_run=True, rows_affected=0,
            detail=detail, policy_citation=policy_citation)
        for job in jobs:
            print("[dry-run] would terminate %s (%s) [%s]"
                  % (job["jobId"], job.get("jobName", "?"),
                     job.get("status", "?")), file=out)
        return result, scope

    # THE REAL WORK, PER JOB, BEFORE THE AUDIT ROW EXISTS AT ALL. Each
    # job's own outcome is caught individually — one job Batch refuses (a
    # state race, a permission edge, a job that finished between the list
    # and the terminate call) does not abort the rest of the batch, and
    # does not inflate the count of what actually happened.
    terminated_ids = []
    failures = []
    for job in jobs:
        try:
            client.terminate_job(jobId=job["jobId"], reason=reason)
        except Exception as exc:                      # noqa: BLE001
            failures.append({"job_id": job["jobId"], "error": str(exc)})
            print("FAILED to terminate %s (%s): %s"
                  % (job["jobId"], job.get("jobName", "?"), exc), file=out)
            continue
        terminated_ids.append(job["jobId"])
        print("terminated %s (%s)" % (job["jobId"], job.get("jobName", "?")),
              file=out)

    # THE AUDIT ROW IS WRITTEN AFTER, WITH THE REAL COUNT — the count of
    # jobs actually terminated, never `len(jobs)`. `detail` carries the
    # failures too, so a partial run's ledger entry says which jobs did not
    # go and why, not only how many.
    if failures:
        detail["failures"] = failures
    detail["terminated_job_ids"] = terminated_ids[:200]
    result = record_external_action(
        conn, idempotency_key, "external_batch_terminate", scope, reason,
        expected_state=expected_state, dry_run=False,
        rows_affected=len(terminated_ids), detail=detail,
        policy_citation=policy_citation)

    return result, scope


def _replay_lookup(conn, idempotency_key, action_class, target_scope):
    """Read-only replay check via `derived.mutation_replay`, or None.

    The same STABLE function `record_external_action`'s own SQL calls
    internally (`047-idempotency-and-expected-state.sql`), called here
    directly so the check can run BEFORE any AWS call without writing a
    row — this function never inserts, it only reads `derived.mutation_
    audit` for a prior REAL (non-dry-run) row under this key. Raises the
    same `IdempotencyConflict` a genuine reuse-for-a-different-action
    would raise inside `record_external_action`, via the same SQLSTATE
    classification in `contract.classify`.
    """
    from pipeline.operatorctl.contract import call_function
    return call_function(
        conn, "SELECT derived.mutation_replay(%s, %s, %s)",
        (idempotency_key, action_class, target_scope))


def _list_all(client, queue, states):
    """Every job in the named states, using the wrapped module's lister."""
    from aws.terminate_batch_jobs import list_jobs
    jobs = []
    for state in states:
        jobs.extend(list_jobs(client, queue, state))
    return jobs


def _client(region, profile, session_factory):
    """Build the Batch client for this invocation.

    NOT CACHED ACROSS CALLS, and the reason is a defect this module
    shipped with: the first version memoized on ``(region, profile)`` in
    a module-level dict, so a second call in the same process reused the
    first call's client. Under a caller with a real boto3 session that is
    merely a stale credential; under the contract tests — where both keys
    are ``None`` — it meant the second test terminated jobs through the
    FIRST test's client, and the injected double correctly refused. The
    double caught it, which is what a double that can refuse is for.

    The saving the cache bought was one session construction per
    invocation. `terminate_jobs_audited` builds a client for the listing
    pass and one per terminated job, which is worth avoiding — so the
    caller resolves it ONCE and passes it down, rather than a global
    holding it between unrelated invocations.
    """
    if session_factory is not None:
        return session_factory()
    import boto3                                  # noqa: PLC0415
    return boto3.Session(region_name=region,
                         profile_name=profile).client("batch")
