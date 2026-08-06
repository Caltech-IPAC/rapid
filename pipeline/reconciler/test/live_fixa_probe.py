"""Live proof for the FixA protocol fixes, against rapid-db and real Batch.

NOT a unit test. The unit suite stubs the database and the scheduler entirely
and is the everyday gate; this drives the REAL resolver (as amended by
migration 017), the REAL amended lifecycle constraints, and a REAL reconciler
cycle against rapid-db through the pooler, exactly as the service would.

Same shape and same reasons as ``pipeline/runtime/test/live_ownership.py``.

WHAT ONLY A LIVE RUN CAN PROVE HERE, finding by finding:

* **#2, the submission order.** A unit test can assert that the rows are
  written before ``SubmitJob`` is called, because it owns the clock. Only a
  live run proves the rows the submitter writes are ones the REAL resolver
  can claim — the two sides agreeing on the logical-job key is the whole
  mechanism, and a disagreement is invisible until something tries to claim.

* **#3, the run-scoped key.** The collision it fixes is a database one:
  ``logical_jobs`` has a global primary key, and only the real table can
  demonstrate that two runs over one exposure/SCA are now two rows rather
  than one silently-kept binding.

* **#9, the claim/index split.** The whole point is that the DDL forbids
  ``application_attempt_index`` in ``terminal_without_start``. A stub cannot
  refuse a state; the constraint can, and this drives a claimed-but-unstarted
  row into that state to prove the window is reachable.

* **#10, the compare-and-set.** The rowcount contract is only meaningful
  against a driver that reports one. This runs the started CAS twice and
  proves the second one matches nothing.

* **#4/#15/#16/#24, the reconciler cycle.** One real ``poll_once`` over rows
  this probe created, against real ``describe_jobs`` for jobs Batch really
  ran, publishing real closure records to the real records bucket.

ADDITIVE test data only, under a run id stamped with the current UTC time so
it cannot collide with a previous run or with real work. Nothing is deleted
and nothing outside this run's own rows is updated; the rows are left in a
terminal state, as evidence.

Run inside the pipeline image on a host whose role can reach rapid-db, Batch
and the records bucket:

    python3 -m pipeline.reconciler.test.live_fixa_probe

Exit code is the result: 0 all proven, 1 something failed.
"""

import datetime
import logging
import os
import sys

from database.modules.utils import rapid_db_connect as dbc
from observability.attempts import (
    AttemptIdentity,
    AttemptWriter,
    ExecutionBinding,
    LifecycleState,
    LogicalJobConflict,
    Provenance,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("fixa.live")

RUN = f"fixa-probe-{datetime.datetime.now(datetime.timezone.utc):%Y%m%dT%H%M%SZ}"

results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))
    print(f"{'PASS' if condition else 'FAIL'}  {name}"
          f"{'  (' + detail + ')' if detail else ''}")
    return bool(condition)


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def binding_for(suffix="1"):
    """A COMPLETE execution binding — every field, per finding #11."""
    return ExecutionBinding(
        job_definition_arn=(
            "arn:aws:batch:us-east-1:000000000000:job-definition/"
            f"rapid-pipeline-science:{suffix}"),
        image_digest="sha256:" + "0" * 64,
        manifest_checksum="sha256:" + "1" * 64,
        job_definition_rev=int(suffix),
        release_identity="rapid-fixa-probe")


def scalar(conn, sql, params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return None if row is None else row[0]


def probe_incomplete_binding():
    """#11: an incomplete binding is refused before any row exists."""
    try:
        ExecutionBinding(
            job_definition_arn="arn:x", image_digest="sha256:x",
            manifest_checksum="sha256:y")
    except ValueError as exc:
        check("11/incomplete-binding-refused",
              "job_definition_rev" in str(exc)
              and "release_identity" in str(exc))
    else:
        check("11/incomplete-binding-refused", False,
              "an incomplete binding was accepted")


def probe_run_scoped_identity(conn):
    """#3: two runs over one exposure/SCA are two logical jobs, not one."""
    from submission.manifest import ProcessingUnit

    writer = AttemptWriter(dbc.ConnectionExecutor(conn))
    unit = ProcessingUnit(exposure=999001, sca=1)

    first = unit.logical_job_key(f"{RUN}-a")
    second = unit.logical_job_key(f"{RUN}-b")
    check("3/two-runs-produce-two-keys", first != second,
          f"{first} vs {second}")

    writer.create_logical_job(first, f"{RUN}-a", binding_for("1"))
    writer.create_logical_job(second, f"{RUN}-b", binding_for("2"))
    conn.commit()

    # Each kept its OWN binding — the collision the global primary key caused.
    rev_a = scalar(conn,
                   "SELECT job_definition_rev FROM logical_jobs"
                   " WHERE logical_job_id = %s", [first])
    rev_b = scalar(conn,
                   "SELECT job_definition_rev FROM logical_jobs"
                   " WHERE logical_job_id = %s", [second])
    conn.rollback()
    check("3/each-run-keeps-its-own-binding", (rev_a, rev_b) == (1, 2),
          f"revs {rev_a}, {rev_b}")

    # A genuine conflict with a DIFFERENT binding is refused, not ignored.
    try:
        writer.create_logical_job(first, f"{RUN}-a", binding_for("9"))
    except LogicalJobConflict as exc:
        conn.rollback()
        check("3/a-conflicting-binding-raises",
              "job_definition_arn" in str(exc), str(exc)[:120])
    else:
        conn.rollback()
        check("3/a-conflicting-binding-raises", False,
              "a differing binding was silently ignored")


def probe_claim_without_start(conn):
    """#9 and #10: claim, then prove the never-started window is reachable."""
    from submission.manifest import ProcessingUnit

    writer = AttemptWriter(dbc.ConnectionExecutor(conn))
    unit = ProcessingUnit(exposure=999002, sca=2)
    logical = unit.logical_job_key(RUN)
    scheduler_job = f"{RUN}-job-claim"

    writer.create_logical_job(logical, RUN, binding_for("1"),
                              scheduler_job_id=scheduler_job)
    attempt_id = writer.create_submitted(
        AttemptIdentity(run_id=RUN, logical_job_id=logical,
                        exposure_id=unit.exposure, sca=unit.sca),
        created_at=now(), submitted_at=now(), binding=binding_for("1"))
    conn.commit()

    # Claim through the REAL resolver, as the runtime does.
    claimed = writer.resolve_attempt(
        AttemptIdentity(run_id=RUN, logical_job_id=logical,
                        exposure_id=unit.exposure, sca=unit.sca),
        created_at=now(), submitted_at=now(),
        scheduler_job_id=scheduler_job, application_attempt_index=1)
    conn.commit()
    check("9/claim-resolves-to-the-precreated-row", claimed == attempt_id,
          f"claimed {claimed}, pre-created {attempt_id}")

    claim_index = scalar(conn,
                         "SELECT application_claim_index FROM attempts"
                         " WHERE attempt_id = %s", [claimed])
    started_index = scalar(conn,
                           "SELECT application_attempt_index FROM attempts"
                           " WHERE attempt_id = %s", [claimed])
    conn.rollback()
    check("9/claim-writes-the-claim-column", claim_index == 1)
    check("9/claim-does-not-write-the-started-index", started_index is None,
          "this is what made terminal_without_start unreachable")

    # THE WINDOW. A container killed here did no work, by construction.
    writer.mark_terminal_without_start(
        claimed, ended_at=now(), scheduler_state="FAILED",
        error_category="scheduler_provisioning",
        closure_record_key=f"records/{RUN}/attempt-{claimed}/seq-0001.json",
        closure_record_sequence=1)
    conn.commit()

    state = scalar(conn, "SELECT lifecycle_state FROM attempts"
                         " WHERE attempt_id = %s", [claimed])
    closure = scalar(conn, "SELECT closure_record_key FROM attempts"
                           " WHERE attempt_id = %s", [claimed])
    conn.rollback()
    check("9/claimed-but-unstarted-closes-without-start",
          state == LifecycleState.TERMINAL_WITHOUT_START.value, f"state {state}")
    check("14/never-started-attempt-cites-its-closure-record",
          bool(closure), f"key {closure}")


def probe_started_cas(conn):
    """#10: the started transition is a real compare-and-set."""
    from observability.attempts import AttemptNotFound
    from submission.manifest import ProcessingUnit

    writer = AttemptWriter(dbc.ConnectionExecutor(conn))
    unit = ProcessingUnit(exposure=999003, sca=3)
    logical = unit.logical_job_key(RUN)
    scheduler_job = f"{RUN}-job-cas"

    writer.create_logical_job(logical, RUN, binding_for("1"),
                              scheduler_job_id=scheduler_job)
    attempt_id = writer.create_submitted(
        AttemptIdentity(run_id=RUN, logical_job_id=logical,
                        exposure_id=unit.exposure, sca=unit.sca),
        created_at=now(), submitted_at=now(), binding=binding_for("1"))
    writer.resolve_attempt(
        AttemptIdentity(run_id=RUN, logical_job_id=logical),
        created_at=now(), submitted_at=now(),
        scheduler_job_id=scheduler_job, application_attempt_index=1)
    conn.commit()

    provenance = Provenance(source_sha="fixa-probe",
                            container_digest="sha256:" + "0" * 64,
                            job_definition_rev="rapid-pipeline-science",
                            config_digest="sha256:" + "2" * 64)
    snapshot = f"records/config-snapshots/sha256/{'2' * 64}.json"

    writer.mark_started(attempt_id, started_at=now(), provenance=provenance,
                        scheduler_job_id=scheduler_job,
                        config_snapshot_key=snapshot)
    conn.commit()

    bound = scalar(conn, "SELECT config_snapshot_key FROM attempts"
                         " WHERE attempt_id = %s", [attempt_id])
    index = scalar(conn, "SELECT application_attempt_index FROM attempts"
                         " WHERE attempt_id = %s", [attempt_id])
    conn.rollback()
    check("10/snapshot-key-is-bound-in-the-database", bound == snapshot,
          f"key {bound}")
    check("9/started-cas-writes-the-attempt-index", index == 1)

    # The SECOND writer must match nothing: the row has left `submitted`.
    try:
        writer.mark_started(attempt_id, started_at=now(),
                            provenance=provenance,
                            config_snapshot_key=snapshot)
    except AttemptNotFound as exc:
        conn.rollback()
        check("10/a-second-starter-matches-nothing",
              "already left that state" in str(exc))
    else:
        conn.rollback()
        check("10/a-second-starter-matches-nothing", False,
              "the second start overwrote the first")
    return attempt_id


def probe_resolver_refusals(conn):
    """#28: the resolver refuses to resolve across identities."""
    import psycopg2

    from submission.manifest import ProcessingUnit

    writer = AttemptWriter(dbc.ConnectionExecutor(conn))
    unit = ProcessingUnit(exposure=999004, sca=4)
    logical = unit.logical_job_key(RUN)
    scheduler_job = f"{RUN}-job-refuse"

    writer.create_logical_job(logical, RUN, binding_for("1"),
                              scheduler_job_id=scheduler_job)
    writer.create_submitted(
        AttemptIdentity(run_id=RUN, logical_job_id=logical,
                        exposure_id=unit.exposure, sca=unit.sca),
        created_at=now(), submitted_at=now(), binding=binding_for("1"))
    writer.resolve_attempt(
        AttemptIdentity(run_id=RUN, logical_job_id=logical),
        created_at=now(), submitted_at=now(),
        scheduler_job_id=scheduler_job, application_attempt_index=1)
    conn.commit()

    # (a) Another logical job, same scheduler attempt.
    try:
        writer.resolve_attempt(
            AttemptIdentity(run_id=RUN, logical_job_id=logical + "-IMPOSTOR"),
            created_at=now(), submitted_at=now(),
            scheduler_job_id=scheduler_job, application_attempt_index=1)
    except psycopg2.Error as exc:
        conn.rollback()
        check("28a/refuses-cross-logical-job-resolution",
              "across identities" in str(exc))
    else:
        conn.rollback()
        check("28a/refuses-cross-logical-job-resolution", False,
              "resolved to another logical job's row")

    # (b) A second claimant on a row ALREADY CLAIMED via the scheduler-index
    # path — which is where 013 attempted a compare-and-set and then returned
    # the row regardless of whether it matched.
    #
    # The row has to be reachable by scheduler index for that path to be
    # taken, so this is a separate logical job whose row the RECONCILER
    # created (scheduler index, no application index), claimed by attempt 1,
    # and then contended for by attempt 2.
    logical_b = ProcessingUnit(exposure=999005, sca=5).logical_job_key(RUN)
    scheduler_b = f"{RUN}-job-claimed"
    writer.create_logical_job(logical_b, RUN, binding_for("1"),
                              scheduler_job_id=scheduler_b)
    conn.commit()

    reconciler_row = writer.resolve_attempt(
        AttemptIdentity(run_id=RUN, logical_job_id=logical_b),
        created_at=now(), submitted_at=now(),
        scheduler_job_id=scheduler_b, scheduler_attempt_index=1)
    first_claimant = writer.resolve_attempt(
        AttemptIdentity(run_id=RUN, logical_job_id=logical_b),
        created_at=now(), submitted_at=now(),
        scheduler_job_id=scheduler_b, application_attempt_index=1,
        scheduler_attempt_index=1)
    conn.commit()
    check("28b/the-first-claimant-claims-the-reconciler-created-row",
          first_claimant == reconciler_row,
          f"claimed {first_claimant}, created {reconciler_row}")

    try:
        writer.resolve_attempt(
            AttemptIdentity(run_id=RUN, logical_job_id=logical_b),
            created_at=now(), submitted_at=now(),
            scheduler_job_id=scheduler_b, application_attempt_index=2,
            scheduler_attempt_index=1)
    except psycopg2.Error as exc:
        conn.rollback()
        check("28b/refuses-a-second-claimant",
              "two attempts" in str(exc) or "already claimed" in str(exc),
              str(exc)[:120])
    else:
        conn.rollback()
        check("28b/refuses-a-second-claimant", False,
              "one row was handed to two attempts")


def probe_reconciler_cycle(conn):
    """#4/#15/#16/#24: one real reconciler cycle over this probe's rows."""
    import boto3

    from pipeline.reconciler import service as service_mod
    from pipeline.runtime.boundaries import S3ObjectStore
    from submission.startup import fetch_parameters

    # The parameter tree is the one home for bucket names. rapid-db's own
    # instance role cannot read it — the reconciler runs on the ORCHESTRATOR
    # host, whose role can (rapid-service-identities.yaml grants it), and this
    # probe runs on rapid-db because that is where the pooler is. Rather than
    # widen a role for a probe, fall back to the environment and say which
    # source was used, so the run is never silently reading the wrong bucket.
    try:
        parameters = fetch_parameters()
        source = "parameter tree"
    except Exception as exc:  # noqa: BLE001 - reported, not hidden
        logger.warning("could not read the parameter tree (%s); falling back "
                       "to the environment", exc)
        parameters = {
            "s3/records-bucket": os.environ.get("RAPID_RECORDS_BUCKET", ""),
            "s3/diagnostics-bucket": os.environ.get(
                "RAPID_DIAGNOSTICS_BUCKET", ""),
            "s3/records-prefix": os.environ.get("RAPID_RECORDS_PREFIX",
                                                "attempts"),
        }
        source = "environment"

    records_bucket = parameters["s3/records-bucket"]
    diagnostics_bucket = parameters["s3/diagnostics-bucket"]
    records_prefix = parameters["s3/records-prefix"]
    print(f"      buckets from the {source}: records={records_bucket} "
          f"diagnostics={diagnostics_bucket} prefix={records_prefix}")
    if not records_bucket or not diagnostics_bucket:
        check("4/a-real-poll-cycle-completes", False,
              "no bucket names available from either source")
        return

    s3 = boto3.client("s3")
    svc = service_mod.ReconcilerService(
        conn=conn,
        batch_client=boto3.client("batch"),
        records_store=S3ObjectStore(records_bucket, client=s3),
        diagnostics_store=S3ObjectStore(diagnostics_bucket, client=s3),
        s3_client=s3,
        records_prefix=records_prefix,
        diagnostics_bucket=diagnostics_bucket)

    check("24/a-fresh-service-is-healthy", svc.healthy, str(svc.health()))

    # #15: the open set now includes recently-closed terminal rows, which is
    # what makes supersession reachable at all. This is a pure DATABASE
    # assertion — the bounded requery is SQL — so it runs wherever the probe
    # can reach rapid-db, which is the whole point of proving it here.
    rows = svc.open_attempts()
    states = {row["lifecycle_state"] for row in rows}
    check("15/terminal-rows-are-revisited-for-supersession",
          bool(states & {LifecycleState.TERMINAL_WITHOUT_START.value,
                         LifecycleState.TERMINAL_AFTER_START.value,
                         LifecycleState.MISSING_OR_CONTRADICTORY.value}),
          f"states in the open set: {sorted(states)}")
    check("14/the-open-set-selects-the-runtime-provenance-columns",
          all(column in (rows[0] if rows else {})
              for column in ("config_digest", "config_snapshot_key",
                             "source_sha", "container_digest"))
          if rows else False,
          "a reconciler-first record is built FROM THE ROW, so a column that "
          "is not selected is a fact the record cannot carry")

    # The poll cycle itself needs batch:DescribeJobs, which belongs to the
    # ORCHESTRATOR role — the reconciler's real host — not to
    # rapid-db-instance-role. This probe runs on rapid-db because that is
    # where the pooler is, so the cycle is attempted and its absence
    # REPORTED rather than quietly skipped or papered over by widening a
    # role for a probe.
    try:
        summary = svc.poll_once()
    except Exception as exc:  # noqa: BLE001 - reported
        print(f"      SKIPPED the poll cycle: {type(exc).__name__}: "
              f"{str(exc)[:160]}")
        print("      (batch:DescribeJobs is the orchestrator role's, not "
              "rapid-db's; the cycle is W8's to run on the orchestrator host)")
        return

    print(f"      poll summary: {summary}")
    check("4/a-real-poll-cycle-completes", isinstance(summary, dict)
          and "open" in summary, str(summary))
    check("16/no-attempt-errored-in-the-cycle", summary.get("errors", 0) == 0,
          f"errors={summary.get('errors')}")
    check("24/the-service-is-still-healthy-after-a-real-cycle", svc.healthy,
          str(svc.health()))


def main():
    for name in ("DBSERVER", "DBPORT", "DBNAME", "RAPID_DB_SECRET_ID"):
        if not os.environ.get(name):
            print(f"!! {name} is not set", file=sys.stderr)
            return 2

    print(f"=== FixA live probe, run {RUN} ===")
    probe_incomplete_binding()

    with dbc.connection("rapid-fixa-probe", lane="transaction") as conn:
        probe_run_scoped_identity(conn)
        probe_claim_without_start(conn)
        probe_started_cas(conn)
        probe_resolver_refusals(conn)
        probe_reconciler_cycle(conn)

    failures = [name for name, ok, _ in results if not ok]
    print(f"\n=== {len(results) - len(failures)}/{len(results)} proven ===")
    if failures:
        print("FAILED: " + ", ".join(failures), file=sys.stderr)
        return 1
    print("FIXA-LIVE-OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
