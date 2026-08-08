"""Append superseding closure records for attempts whose evidence is gone.

The situation this exists for, stated exactly: an attempt reached a terminal
state, its row cites a closure record by key, sequence and checksum — and the
object that key names is no longer in the bucket. The 2026-08-06 evening
scratch cleanup removed test-era record objects, and seven `fixd-chain`
attempts from that day were left citing them.

The consequence is out of proportion to the cause. `registration.candidates()`
selects `terminal_after_start` rows whose record has not been registered;
`read_record` fetches `terminal_record_key`; `S3ObjectStore.get` translates a
NoSuchKey into `StorageError`; `register_batch` counts the failure; and the
run's exit code becomes 65. The virtualPipelineOperator runs registration
BEFORE it submits, so three unreadable objects abort every operator invocation
before any science work is submitted at all.

**Why this is a tool and not a reconciler fix.** The reconciler already
supersedes — `publish_closure_record` climbs to the next free sequence and the
highest sequence is the full account. But it only revisits terminal rows whose
scheduler facts have changed, and only inside `SUPERSESSION_WINDOW` (24 h,
Batch's own retention). These attempts are older than that and Batch has
nothing new to say about them, so the service will never reconsider them. The
window is right — widening it to sweep up an operator's cleanup would make
every terminal row eligible forever.

**What it writes, and what it refuses to write.** Two things per attempt,
in this order:

1. A reconciler-first closure record at the next free sequence, citing the
   absent object as its rejected predecessor with reason `absent`. Append
   only: the create-once put never overwrites, and the existing records —
   including the row's current citation — are left exactly as they are. The
   stale key stays on the row deliberately. It is evidence of what was lost,
   and `missing_or_contradictory` places no requirement on it.

2. `mark_missing_or_contradictory`, which is what actually clears the gate:
   that state is deliberately absent from `RECONCILED_STATES`, so a flagged
   attempt stops being a registration candidate. It is the design's final
   outcome for stores that disagree, which is what postgres and S3 are
   doing here.

No `error_category` is set, matching the reconciler's own
application-account-without-scheduler-facts path (`service.py`, "flagged
contradictory"). These attempts succeeded; `rapid_outcome` is `success` on
every one. What was lost is the evidence, not the work, and the allowlist has
no category for that — inventing one would assert a failure that did not
happen.

The tool re-verifies absence against S3 immediately before writing, per
attempt. A record that turns out to be readable is skipped and reported: the
premise of the whole operation is that the object is gone, and acting on a
stale premise is how an append-only tool does damage anyway.

Usage (read-only unless `--apply` is given)::

    python3 -m pipeline.reconciler.supersede_lost_evidence \\
        --run-prefix fixd-chain- [--apply]
"""

import argparse
import datetime
import json
import os
import sys

from observability.attempts import (AttemptWriter, LifecycleState,
                                    ReconciliationClass)
from pipeline.reconciler.closure import (REJECTED_ABSENT, build_closure_record,
                                         publish_closure_record)
from pipeline.runtime.boundaries import S3ObjectStore

#: The states worth examining. A row already flagged
#: `missing_or_contradictory` is not a registration candidate and needs
#: nothing from this tool.
BLOCKING_STATES = (
    LifecycleState.TERMINAL_AFTER_START.value,
    LifecycleState.TERMINAL_WITHOUT_START.value,
)

_SELECT = """
SELECT attempt_id, run_id, logical_job_id, scheduler_job_id,
       lifecycle_state, scheduler_state, rapid_outcome, product_disposition,
       error_category, exposure_id, sca, sky_tile,
       application_intended_exit, scheduler_observed_exit,
       created_at, submitted_at, started_at, ended_at,
       source_sha, container_digest, job_definition_rev, config_digest,
       terminal_record_key, terminal_record_sequence, terminal_record_checksum,
       closure_record_key, closure_record_sequence,
       registered_at, registered_record_sequence,
       binding_job_definition_arn, binding_job_definition_rev,
       binding_image_digest, binding_release_identity,
       binding_manifest_checksum, config_snapshot_key
  FROM attempts
 WHERE run_id LIKE %s
   AND lifecycle_state = ANY(%s)
   AND terminal_record_sequence >= 1
   AND (registered_record_sequence IS NULL
        OR registered_record_sequence < terminal_record_sequence)
 ORDER BY attempt_id
"""


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def select_rows(conn, run_prefix):
    """Rows whose cited record must be readable for registration to pass."""
    with conn.cursor() as cur:
        cur.execute(_SELECT, (f"{run_prefix}%", list(BLOCKING_STATES)))
        names = [description[0] for description in cur.description]
        rows = [dict(zip(names, row)) for row in cur.fetchall()]
    conn.rollback()  # read-only; do not hold a transaction open
    return rows


def evidence_is_absent(store, key):
    """True only when the store answers definitively that the object is gone.

    `head` returns None for not-found and raises for a store fault, and the
    difference decides whether this tool may act. A throttle or an
    AccessDenied is not evidence of absence, and treating it as such would
    terminalize an attempt whose record is sitting intact in the bucket —
    the exact failure `DEFERRED_STORE_FAULT` was introduced to prevent.
    """
    return store.head(key) is None


def supersede(conn, store, prefix, row, apply_changes, executor_factory):
    """Append one superseding record and flag the row. Returns a report dict.

    `executor_factory` builds the `AttemptWriter` executor from the
    connection. It is passed in rather than imported here because
    `rapid_db_connect` imports psycopg2 at module scope, and this module is
    otherwise driver-free — the same reason `registration.consumer` resolves
    its transaction helper at call time.
    """
    attempt_id = row["attempt_id"]
    key = row["terminal_record_key"]
    report = {"attempt_id": attempt_id, "run_id": row["run_id"],
              "cited_key": key, "cited_sequence": row["terminal_record_sequence"]}

    if not key:
        report["action"] = "skipped"
        report["reason"] = "row cites no terminal_record_key"
        return report

    try:
        absent = evidence_is_absent(store, key)
    except Exception as exc:  # noqa: BLE001 - a store fault is not absence
        report["action"] = "deferred"
        report["reason"] = f"store could not answer: {exc}"
        return report

    if not absent:
        report["action"] = "skipped"
        report["reason"] = "cited record is readable; premise does not hold"
        return report

    # The reconciler owns every sequence above the application's 0, and the
    # row's own sequence is the floor, so a replay writes the same sequence
    # and dedupes on the create-once put rather than climbing.
    sequence = max(int(row["terminal_record_sequence"]) + 1, 1)
    record = build_closure_record(
        row, observation=None, sequence=sequence, predecessor=None,
        rejected_key=key, rejected_reason=REJECTED_ABSENT,
        classification=ReconciliationClass.MISSING.value,
        now=_now())

    if not apply_changes:
        report["action"] = "would-supersede"
        report["sequence"] = sequence
        return report

    # The record goes first and the flag second, deliberately. If this run
    # dies between them the attempt still cites its original key and is still
    # a candidate — the same state it is in now, re-runnable. The reverse
    # order would flag a row whose superseding account was never written.
    published = publish_closure_record(store, prefix, row, record)
    report["published_key"] = published.key
    report["landed_sequence"] = published.sequence

    writer = AttemptWriter(executor_factory(conn))
    writer.mark_missing_or_contradictory(
        attempt_id,
        reconciliation_class=ReconciliationClass.MISSING.value,
        # postgres and s3 are the two stores that disagree here: the row
        # asserts a record the bucket does not hold. The reconciler's own
        # sites compare postgres against batch; naming the stores actually
        # compared is what the sources list is for.
        reconciliation_sources=["postgres", "s3"],
        detected_at=_now())
    report["action"] = "superseded"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="supersede-lost-evidence",
        description="Append superseding closure records for attempts whose "
                    "cited record object no longer exists.")
    parser.add_argument("--run-prefix", required=True,
                        help="run_id prefix to consider, e.g. 'fixd-chain-'")
    parser.add_argument("--apply", action="store_true",
                        help="write; without it the run only reports")
    args = parser.parse_args(argv)

    from database.modules.utils.rapid_db_connect import (ConnectionExecutor,
                                                         connection)

    bucket = os.environ["RAPID_RECORDS_BUCKET"]
    prefix = os.environ["RAPID_RECORDS_PREFIX"]
    store = S3ObjectStore(bucket)

    reports = []
    with connection("rapid-supersede-lost-evidence", lane="transaction") as conn:
        rows = select_rows(conn, args.run_prefix)
        for row in rows:
            reports.append(supersede(conn, store, prefix, row, args.apply,
                                     ConnectionExecutor))

    print(json.dumps({"apply": args.apply, "considered": len(reports),
                      "reports": reports}, indent=2, default=str))

    deferred = [r for r in reports if r.get("action") == "deferred"]
    return 75 if deferred else 0


if __name__ == "__main__":
    sys.exit(main())
