"""The supersede-lost-evidence tool, under the mutation contract (G3).

``pipeline/reconciler/supersede_lost_evidence.py`` has good bones and the
brief is explicit that they stay: per-row precondition re-verification
immediately before acting (evidence absence is re-checked against S3 for
each row, not once for the batch), and publish-before-flag ordering so a
mid-run crash leaves a re-runnable state rather than a flagged attempt
with no closure record. Both are load-bearing and neither is touched
here.

What it lacks is the contract: ``--apply`` friction but no captured
reason, no actor, no idempotency key, and no durable audit row — the
closure record and the flag ARE the effect, but nothing records who ran
the tool or why. This module adds exactly that, by calling the tool's own
``select_rows`` and ``supersede`` and wrapping the run in an audited
operator action.

WHY THE AUDIT ROW COUNTS ROWS SUPERSEDED, NOT ROWS CONSIDERED. The tool
defers rows whose precondition no longer holds — evidence that reappeared
between the query and the write — and a deferral is not a mutation. The
audit's ``rows_affected`` is the count actually superseded, so the ledger
answers "what changed" rather than "what was looked at"; the considered
and deferred counts go in ``detail``, where they inform without inflating
the number an auditor reads as the action's size.
"""

import os
import sys

from pipeline.operatorctl.actions import record_external_action


def supersede_audited(conn, idempotency_key, run_prefix, reason,
                      dry_run=True, policy_citation=None, out=None,
                      store=None, executor_factory=None):
    """Run the supersede pass under the contract. Returns (result, scope).

    ``store`` and ``executor_factory`` are injectable so the contract
    tests can exercise the audit and idempotency behaviour against
    fixture rows without an S3 bucket — the behaviour this module adds is
    what those tests are for. Left unset, both resolve exactly as the
    original tool resolves them.
    """
    out = out or sys.stdout
    scope = "attempts:run_prefix=%s" % run_prefix

    from pipeline.reconciler.supersede_lost_evidence import (select_rows,
                                                             supersede)

    if store is None:
        store, prefix = _resolve_store()
    else:
        prefix = os.environ.get("RAPID_RECORDS_PREFIX", "")

    if executor_factory is None:
        from database.modules.utils.rapid_db_connect import ConnectionExecutor
        executor_factory = ConnectionExecutor

    rows = select_rows(conn, run_prefix)

    # The per-row pass, with the tool's own precondition re-check and
    # ordering intact. `apply_changes` is the tool's parameter and carries
    # the same meaning as this command's `--apply`.
    reports = []
    for row in rows:
        reports.append(supersede(conn, store, prefix, row, not dry_run,
                                 executor_factory))

    superseded = [r for r in reports if r.get("action") == "superseded"]
    deferred = [r for r in reports if r.get("action") == "deferred"]

    detail = {"run_prefix": run_prefix,
              "considered": len(reports),
              "superseded": len(superseded),
              "deferred": len(deferred),
              "attempt_ids": [r.get("attempt_id") for r in superseded[:200]]}

    result = record_external_action(
        conn, idempotency_key, "external_evidence_supersede", scope, reason,
        dry_run=dry_run, rows_affected=len(superseded), detail=detail,
        policy_citation=policy_citation)

    for report in reports:
        print("  %s: %s" % (report.get("attempt_id"), report.get("action")),
              file=out)
    if deferred:
        print("  %d row(s) DEFERRED — evidence reappeared between the query "
              "and the write" % len(deferred), file=out)

    return result, scope


def _resolve_store():
    """Build the S3 object store exactly as the wrapped tool does.

    The identity matters and getting it wrong is a documented failure: the
    ambient credential is the host's instance role, which is deliberately
    not granted the records bucket. The reconciler's own helpers are
    reused rather than opening a third credential path that could drift
    from them.
    """
    from pipeline.reconciler.main import _assumed_session
    from pipeline.reconciler.supersede_lost_evidence import S3ObjectStore
    from pipeline.runtime.environment import resolve_region

    session = _assumed_session(os.environ.get("RAPID_RECONCILER_ROLE_ARN"),
                               resolve_region())
    bucket = os.environ["RAPID_RECORDS_BUCKET"]
    prefix = os.environ["RAPID_RECORDS_PREFIX"]
    return S3ObjectStore(bucket, client=session.client("s3")), prefix
