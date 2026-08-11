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

THE ORDER, AND WHAT A CRASH BETWEEN THE TWO LEAVES. The audit row is
written and committed BEFORE the terminations are issued, and updated after
with the count actually terminated. A crash between them therefore leaves a
recorded intent with the jobs possibly still running — an operator sees an
action that may not have completed and can check the queue. The opposite
order would leave terminated jobs with no record at all, which is the
failure the audit exists to prevent: an unrecorded mutation is invisible,
an over-recorded one is merely inaccurate and self-evidently so on
inspection.
"""

import sys

from pipeline.operatorctl.actions import record_external_action
from pipeline.operatorctl.contract import ExpectedStateMismatch


def terminate_jobs_audited(conn, idempotency_key, queue, states, reason,
                           expected_state=None, dry_run=True, region=None,
                           profile=None, policy_citation=None, out=None,
                           session_factory=None):
    """List, refuse-or-terminate, and audit. Returns (result, target_scope).

    ``session_factory`` exists for the contract tests: the audit and
    idempotency behaviour is what this module adds and what needs
    testing, and it must be testable without terminating a real job.
    A test passing a stub proves the ledger path; the AWS path itself is
    the wrapped module's, unchanged and already in use.
    """
    out = out or sys.stdout
    scope = "batch:queue=%s:states=%s" % (queue, ",".join(states))

    # ONE client for the whole invocation: the listing pass and every
    # termination share it, so credentials resolve once without a global
    # holding a client between unrelated invocations.
    client = _client(region, profile, session_factory)
    jobs = _list_all(client, queue, states)

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
    # Bounded at 200 ids: the audit row records what was acted on, and a
    # queue of ten thousand jobs would otherwise put a megabyte of jsonb in
    # a history row. The count above is exact regardless.
    if len(jobs) > 200:
        detail["job_ids_truncated"] = True

    result = record_external_action(
        conn, idempotency_key, "external_batch_terminate", scope, reason,
        expected_state=expected_state, dry_run=dry_run,
        rows_affected=len(jobs), detail=detail,
        policy_citation=policy_citation)

    # A replay means this exact key already terminated this exact scope.
    # Terminating again would be the second mutation the key exists to
    # prevent, so the recorded outcome is returned and AWS is not touched.
    if result.get("replayed"):
        return result, scope

    for job in jobs:
        if dry_run:
            print("[dry-run] would terminate %s (%s) [%s]"
                  % (job["jobId"], job.get("jobName", "?"),
                     job.get("status", "?")), file=out)
        else:
            client.terminate_job(jobId=job["jobId"], reason=reason)
            print("terminated %s (%s)" % (job["jobId"],
                                          job.get("jobName", "?")), file=out)

    return result, scope


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
