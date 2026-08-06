"""The record-written/row-not-closed crash boundary, against the real stack.

ONE case, run against the real database, the real constraints and a real S3
records store — review finding #14, the round-2 P0.

Why it is its own probe rather than a battery case run in place: the W8 battery
also exercises retention tagging, which needs `s3:GetObjectTagging` on the
diagnostics bucket. rapid-db-instance-role has no grant on
`roman-rapid-records` at all and can only assume `rapid-migration-runner-role`
— not `rapid-orchestrator-role` — so the full battery cannot complete from the
one host that can reach the pooler. That gap PREDATES this round: the
unmodified `smdc` baseline fails at the same first record write with the same
403 (verified 2026-08-06). It is recorded as a proposed decision, not worked
around.

What this case needs is narrower and available: the database, the lifecycle
constraints, and a bucket this host can write. So it proves the thing the
review said was unproven.

THE DEFECT. Sequence 0 is written before the application-closed transition, so
a crash between them leaves a `started` row beside a valid record. In exactly
that state the row's `terminal_record_key` and `terminal_record_checksum` are
both NULL — the application sets them in the transition that just failed — and
the record BODY cannot carry either, because a record cannot contain its own
key or the checksum of its own bytes. Migration 013 requires a non-null key for
`application_closed`. So every reconciliation pass attempted an illegal
transition, caught it as a per-row error, and left the attempt `started`
forever. Registration never saw it.

The W8 battery's case wrote sequence 0 and then called
`writer.mark_application_closed` ITSELF, passing the key and checksum it
happened to be holding — proving the DDL accepts that transition and nothing
about whether the reconciler can perform it. This drives the real reconciler.

ADDITIVE only: it creates rows under a time-stamped run id and leaves them
terminal, as evidence. It deletes nothing.
"""

import datetime
import json
import logging
import os
import sys

import boto3

from database.modules.utils import rapid_db_connect as dbc
from observability.attempts import (AttemptIdentity, AttemptWriter,
                                    ExecutionBinding, LifecycleState,
                                    Provenance)
from pipeline.reconciler.service import ReconcilerService
from pipeline.runtime import termination
from pipeline.runtime.boundaries import S3ObjectStore
from pipeline.runtime.boundaries import checksum as body_checksum

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-8s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("fixc.crash-boundary")

RUN = "fixc-crash-" + datetime.datetime.now(
    datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
SCHEDULER_JOB_ID = f"{RUN}-job"

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  ({detail})", flush=True)


def now():
    return datetime.datetime.now(datetime.timezone.utc)


class SucceededBatch:
    """Answers for exactly this probe's synthetic job id.

    The row carries a job id Batch has never heard of, so a real
    `describe_jobs` returns nothing and the attempt goes down the unresolved
    path rather than the one under test.
    """

    def __init__(self, job_id):
        self.job_id = job_id

    def describe_jobs(self, jobs):
        return {"jobs": [{"jobId": job_id, "status": "SUCCEEDED",
                          "createdAt": 1, "startedAt": 2, "stoppedAt": 3,
                          "container": {"exitCode": 0}}
                         for job_id in jobs if job_id == self.job_id]}


def row_of(conn, attempt_id, column):
    from psycopg2 import sql
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT {} FROM attempts WHERE attempt_id = %s")
                    .format(sql.Identifier(column)), (attempt_id,))
        found = cur.fetchone()
    conn.rollback()
    return found[0] if found else None


def main():
    records_bucket = os.environ["RAPID_RECORDS_BUCKET"]
    diagnostics_bucket = os.environ.get("RAPID_DIAGNOSTICS_BUCKET",
                                        records_bucket)
    prefix = os.environ.get("RAPID_RECORDS_PREFIX", "attempts")

    s3 = boto3.client("s3")
    store = S3ObjectStore(records_bucket, client=s3)

    print(f">> run id: {RUN}")
    print(f">> records: s3://{records_bucket}/{prefix}")

    with dbc.connection("rapid-fixc-crash-boundary", lane="transaction") as conn:
        executor = dbc.ConnectionExecutor(conn)
        writer = AttemptWriter(executor.execute)

        binding = ExecutionBinding(
            job_definition_arn=(
                f"arn:aws:batch:us-east-1:{boto3.client('sts')
                                           .get_caller_identity()['Account']}"
                ":job-definition/rapid-science:1"),
            job_definition_rev=1,
            image_digest="sha256:" + "0" * 64,
            release_identity="fixc-crash-boundary",
            manifest_checksum="0" * 16)

        logical_job_id = f"{RUN}/crash/1"
        writer.create_logical_job(logical_job_id, RUN, binding)
        attempt_id = writer.create_submitted(
            AttemptIdentity(run_id=RUN, logical_job_id=logical_job_id,
                            exposure_id=999104, sca=4),
            created_at=now(), submitted_at=now(),
            scheduler_job_id=SCHEDULER_JOB_ID, binding=binding)
        conn.commit()

        writer.mark_started(
            attempt_id, now(),
            Provenance(source_sha="0" * 40,
                       container_digest="sha256:" + "0" * 64,
                       job_definition_rev=1, config_digest="0" * 64),
            application_attempt_index=1,
            config_snapshot_key=f"{prefix}/config/{RUN}/snap.json")
        conn.commit()

        # THE CRASH: sequence 0 is written; the application-closed transition
        # never happens.
        key = termination.terminal_record_key(prefix, RUN, logical_job_id,
                                              attempt_id, 0)
        record = {"schema_version": termination.RECORD_SCHEMA_VERSION,
                  "attempt_id": attempt_id, "run_id": RUN,
                  "logical_job_id": logical_job_id,
                  "rapid_outcome": "success",
                  "product_disposition": "published",
                  "application_intended_exit": 0, "record_sequence": 0}
        written = termination.write_terminal_record(store, key, record)

        state_before = row_of(conn, attempt_id, "lifecycle_state")
        key_before = row_of(conn, attempt_id, "terminal_record_key")
        checksum_before = row_of(conn, attempt_id, "terminal_record_checksum")

        check("1/the-crash-state-is-what-the-review-describes",
              state_before == LifecycleState.STARTED.value
              and key_before is None and checksum_before is None,
              f"state={state_before} key={key_before} "
              f"checksum={checksum_before}")

        # THE REAL RECONCILER, against the real row and the real constraints.
        service = ReconcilerService(
            conn=conn, batch_client=SucceededBatch(SCHEDULER_JOB_ID),
            records_store=store, diagnostics_store=store, s3_client=s3,
            records_prefix=prefix, diagnostics_bucket=diagnostics_bucket)
        summary = service.poll_once()
        conn.commit()

        state_after = row_of(conn, attempt_id, "lifecycle_state")
        materialized = row_of(conn, attempt_id, "reconciler_materialized")
        key_after = row_of(conn, attempt_id, "terminal_record_key")
        checksum_after = row_of(conn, attempt_id, "terminal_record_checksum")

        check("2/the-attempt-no-longer-stays-started-forever",
              state_after not in (LifecycleState.STARTED.value,),
              f"{state_before} -> {state_after}, summary={summary}")

        # THE KEY THE MATERIALIZATION SUPPLIED is sequence 0's — the record it
        # read and validated — and that is what makes the transition legal
        # where the row and the body both held NULL. The row does NOT end
        # citing it: `mark_terminal_after_start` runs immediately afterwards in
        # the same closure and advances the citation to the reconciler's own
        # sequence-1 closure record, which is by then the highest-sequence and
        # therefore authoritative account.
        #
        # THIS ASSERTION WAS WRONG BEFORE THE CODE WAS (round-3 finding #1).
        # It previously expected the sequence-0 checksum to survive beside the
        # advanced sequence-1 key, reasoning that sequence 1 folds the
        # predecessor's facts in verbatim. It does — but folding FACTS does not
        # make the two records' BYTES equal, and a checksum hashes bytes. The
        # probe was therefore pinning an incoherent pair that the registrar,
        # which fetches the cited key and hashes exactly those bytes, refused.
        # What must hold is COHERENCE: the triple the row ends with must
        # validate against itself. Asserted the way a consumer checks it.
        check("3/the-row-ends-citing-the-authoritative-record",
              key_after == termination.terminal_record_key(
                  prefix, RUN, logical_job_id, attempt_id, 1),
              f"row cites {key_after!r}")

        try:
            cited_bytes = store.get(key_after)
            cited_computed = body_checksum(cited_bytes)
        except Exception as exc:  # noqa: BLE001
            check("4/the-citation-triple-is-coherent", False, str(exc)[:90])
        else:
            check("4/the-citation-triple-is-coherent",
                  checksum_after is not None
                  and checksum_after == cited_computed,
                  f"row cites checksum {checksum_after!r} for key "
                  f"{key_after!r}, whose bytes hash to {cited_computed!r}"
                  + ("" if checksum_after != written["checksum"]
                     else " — this is sequence 0's checksum, stranded beside "
                          "the advanced sequence-1 key"))

        check("5/the-projection-is-marked-as-the-reconcilers",
              materialized is True,
              f"reconciler_materialized={materialized}")

        # The closure record the pass published, so the account is complete.
        closure_key = termination.terminal_record_key(
            prefix, RUN, logical_job_id, attempt_id, 1)
        try:
            body = json.loads(store.get(closure_key))
        except Exception as exc:  # noqa: BLE001
            check("6/a-closure-record-was-published", False, str(exc)[:90])
        else:
            check("6/a-closure-record-was-published",
                  body.get("record_author") == "reconciler"
                  and body.get("rapid_outcome") == "success",
                  f"author={body.get('record_author')} "
                  f"outcome={body.get('rapid_outcome')} "
                  f"classification={body.get('reconciliation_classification')}")

    failed = [name for name, ok in results if not ok]
    print()
    print(f">> {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print(f"!! FAILED: {', '.join(failed)}", file=sys.stderr)
        return 1
    print("FIXC-CRASH-BOUNDARY-OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
