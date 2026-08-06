"""Live proof for the runtime's ownership and application-closed paths.

NOT a unit test. The unit suite stubs the database entirely and is the
everyday gate; this drives the REAL resolver function, the REAL amended
lifecycle constraints, and the REAL grants against rapid-db through the
pooler, as ``rapid_pipeline``, exactly as a Batch job would.

What only a live run can prove, and why each is here:

* ``resolve_attempt`` is a database FUNCTION with a transaction-scoped
  advisory lock and a post-lock recheck. A stub returning an id proves the
  writer calls it; only the real call proves the function accepts our
  argument types and casts, which is where W1's live run found a genuine
  overload-resolution failure.
* The lifecycle CHECK matrix is enforced by the database. The unit suite
  asserts we send the right fields; only the database can confirm the row it
  produces is a legal state.
* The rowcount contract this worker added (charge 4) is only meaningful
  against a driver that reports one. A stub can be made to return any number;
  psycopg2's ``cursor.rowcount`` after a real UPDATE is the thing being
  relied on.
* The application-closed state is new in migration 013. Reaching it live is
  the first proof its constraint set admits what the runtime writes.

Run inside the pipeline image on a host whose role can read the pipeline
secret, with DBSERVER/DBPORT/DBNAME and RAPID_DB_SECRET_ID set:

    python3 -m pipeline.runtime.test.live_ownership

ADDITIVE test data only, under a run id stamped with the current UTC time so
it cannot collide with a previous run or with real work. Nothing is deleted
and nothing outside this run's own rows is updated; the rows are left in a
terminal state, as evidence.

Exit code is the result: 0 all proven, 1 something failed.
"""

import datetime
import logging
import os
import sys
import uuid

from database.modules.utils import rapid_db_connect as dbc
from observability.attempts import (
    AttemptWriter,
    ExecutionBinding,
    LifecycleState,
    ProductDisposition,
    RapidOutcome,
    Stage,
    StageOutcome,
)
from pipeline.runtime.environment import JobEnvironment
from pipeline.runtime.errors import RecordsError
from pipeline.runtime.ownership import (
    lifecycle_reader_for,
    normalize_attempt_index,
    resolve_ownership,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(name)s %(message)s",
)

failures = []


def check(name, expected, got):
    if expected == got:
        print(f"PASS  {name}  (expected={expected!r} got={got!r})")
    else:
        print(f"FAIL  {name}  (expected={expected!r} got={got!r})")
        failures.append(name)


def check_raises(name, exc_type, fn):
    try:
        fn()
    except exc_type as exc:
        print(f"PASS  {name}  (raised {type(exc).__name__})")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  {name}  (raised {type(exc).__name__}: {exc}, "
              f"expected {exc_type.__name__})")
        failures.append(name)
    else:
        print(f"FAIL  {name}  (nothing raised, expected {exc_type.__name__})")
        failures.append(name)


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    run_id = f"w2-live-{stamp}"
    logical_job_id = f"{run_id}-{uuid.uuid4().hex[:8]}"
    scheduler_job_id = f"live-{uuid.uuid4()}"

    print("=== live proof: runtime ownership + application-closed ===")
    print(f">> run_id            {run_id}")
    print(f">> logical_job_id    {logical_job_id}")
    print(f">> scheduler_job_id  {scheduler_job_id}")
    print(f">> DBSERVER={os.getenv('DBSERVER')} DBPORT={os.getenv('DBPORT')} "
          f"DBNAME={os.getenv('DBNAME')}")

    # The ARN's account field is spelled with a non-numeric placeholder, not
    # twelve zeros: .githooks/pre-push blocks any 12-digit run in a pushed
    # file with no allowlist, and it is right to match on shape rather than on
    # the real value — a guard that only catches the account it already knows
    # is not a guard. This is test data for a column the runtime only copies,
    # so nothing here needs to parse as a real ARN.
    binding = ExecutionBinding(
        job_definition_arn=("arn:aws:batch:us-east-1:ACCOUNTID:"
                            "job-definition/rapid-prompt:7"),
        image_digest="sha256:" + "e" * 64,
        manifest_checksum="sha256:" + "f" * 64,
        job_definition_rev=7,
        release_identity="rapid-2026.08.1",
    )

    job_env = JobEnvironment(
        manifest_uri=f"s3://rapid-manifests/{run_id}/manifest.json",
        batch_id=run_id,
        manifest_checksum=binding.manifest_checksum,
        scheduler_job_id=scheduler_job_id,
        attempt_index=1,
        queue_name="rapid-prompt",
        array_index=0,
    )

    with dbc.connection("rapid-w2-live", lane=dbc.LANE_TRANSACTION) as conn:
        executor = dbc.ConnectionExecutor(conn)
        writer = AttemptWriter(executor)
        reader = lifecycle_reader_for(executor)

        # -- the executor's amended return contract (charge 4) --------------
        # Proven against psycopg2's real cursor.rowcount, not a stub: an
        # UPDATE naming an attempt_id that cannot exist must report 0.
        rows = executor.execute(
            "UPDATE attempts SET scheduler_job_id = scheduler_job_id"
            " WHERE attempt_id = %s", [-1])
        check("executor reports a real rowcount for a no-match UPDATE", 0, rows)

        select_rows = executor.execute("SELECT 1", [])
        check("executor still returns rows for a result set", [(1,)],
              [tuple(r) for r in select_rows])

        # -- the logical job and its execution binding ----------------------
        writer.create_logical_job(logical_job_id, run_id, binding,
                                  scheduler_job_id=scheduler_job_id)
        print(">> logical job recorded")

        # -- ownership through the REAL resolver function -------------------
        ownership = resolve_ownership(
            writer, job_env, run_id=run_id, logical_job_id=logical_job_id,
            identity_extra={"exposure_id": 12345, "sca": 7},
            lifecycle_reader=reader)
        print(f">> resolved: {ownership}")
        check("attempt id is a real bigint", True,
              isinstance(ownership.attempt_id, int)
              and ownership.attempt_id > 0)
        check("attempt 1 is recorded as claiming", True,
              ownership.claimed_precreated)
        check("row is submitted after resolution",
              LifecycleState.SUBMITTED.value, reader(ownership.attempt_id))

        # Resolving again with the same identity must return the SAME row.
        # This is the resolver's whole purpose — a late-starting runtime and a
        # scheduler retry converging on one row — and only the real function's
        # advisory lock plus partial unique indexes can demonstrate it.
        again = resolve_ownership(
            writer, job_env, run_id=run_id, logical_job_id=logical_job_id,
            identity_extra={"exposure_id": 12345, "sca": 7},
            lifecycle_reader=reader)
        check("re-resolving the same identity returns one row",
              ownership.attempt_id, again.attempt_id)

        # A retry (index 2) must get its OWN row, with the binding copied.
        retry_env = JobEnvironment(
            manifest_uri=job_env.manifest_uri, batch_id=job_env.batch_id,
            manifest_checksum=job_env.manifest_checksum,
            scheduler_job_id=scheduler_job_id, attempt_index=2,
            queue_name=job_env.queue_name, array_index=0)
        retry = resolve_ownership(
            writer, retry_env, run_id=run_id, logical_job_id=logical_job_id,
            identity_extra={"exposure_id": 12345, "sca": 7},
            lifecycle_reader=reader)
        check("a retry resolves to a different row", True,
              retry.attempt_id != ownership.attempt_id)
        check("the retry is not recorded as claiming", False,
              retry.claimed_precreated)

        binding_rows = executor.execute(
            "SELECT binding_job_definition_arn, binding_image_digest,"
            "       binding_manifest_checksum"
            "  FROM attempts WHERE attempt_id = %s", [retry.attempt_id])
        check("the retry row carries the execution binding, copied at creation",
              (binding.job_definition_arn, binding.image_digest,
               binding.manifest_checksum),
              tuple(binding_rows[0]))

        # -- started, with the configuration digest bound in the same write --
        from pipeline.runtime import termination
        from observability.attempts import Provenance

        config_digest = "sha256:" + uuid.uuid4().hex * 2
        provenance = Provenance(
            source_sha="w2live" + uuid.uuid4().hex[:34],
            container_digest=binding.image_digest,
            job_definition_rev="7",
            config_digest=config_digest,
        )
        started = termination.start_attempt(
            writer, ownership.attempt_id, provenance, config_digest,
            "records/config-snapshots/sha256/deadbeef.json",
            scheduler_job_id=scheduler_job_id,
            application_attempt_index=1)
        check("row is started", LifecycleState.STARTED.value,
              reader(ownership.attempt_id))

        digest_rows = executor.execute(
            "SELECT config_digest FROM attempts WHERE attempt_id = %s",
            [ownership.attempt_id])
        check("the configuration digest was bound by the started transition",
              config_digest, digest_rows[0][0])

        # -- a stage span, against the real table ---------------------------
        writer.record_stage(ownership.attempt_id, Stage(
            stage_name="difference", started_at=now, duration_ms=1234.5,
            outcome=StageOutcome.SUCCESS))
        stage_rows = executor.execute(
            "SELECT stage_name, outcome FROM attempt_stages"
            " WHERE attempt_id = %s", [ownership.attempt_id])
        check("the stage span was written", ("difference", "success"),
              tuple(stage_rows[0]))

        # -- the application-closed transition (new in migration 013) -------
        record_key = termination.terminal_record_key(
            "records", run_id, logical_job_id, ownership.attempt_id, 0)
        writer.mark_application_closed(
            ownership.attempt_id,
            ended_at=datetime.datetime.now(datetime.timezone.utc),
            application_intended_exit=0,
            rapid_outcome=RapidOutcome.SUCCESS,
            product_disposition=ProductDisposition.PUBLISHED,
            terminal_record_key=record_key,
            terminal_record_sequence=0,
            terminal_record_checksum="sha256:" + "a" * 64)
        check("row reaches application_closed",
              LifecycleState.APPLICATION_CLOSED.value,
              reader(ownership.attempt_id))

        closed_rows = executor.execute(
            "SELECT application_intended_exit, scheduler_observed_exit,"
            "       rapid_outcome, product_disposition, terminal_record_key"
            "  FROM attempts WHERE attempt_id = %s", [ownership.attempt_id])
        intended, observed, outcome, disposition, key = closed_rows[0]
        check("application-intended exit is 0 (fail-loud: recorded, clean exit)",
              0, intended)
        check("scheduler-observed exit is ABSENT — the reconciler's column",
              None, observed)
        check("outcome recorded", "success", outcome)
        check("disposition recorded", "published", disposition)
        check("the row cites its terminal record", record_key, key)

        # -- the charge-4 check, live --------------------------------------
        # A transition naming an attempt that does not exist must raise
        # rather than silently succeed. This is the defect being closed,
        # proven against the real driver's rowcount.
        from observability.attempts import AttemptNotFound

        check_raises(
            "a transition against a nonexistent attempt raises",
            AttemptNotFound,
            lambda: writer.mark_application_closed(
                -1, ended_at=now, application_intended_exit=0,
                rapid_outcome=RapidOutcome.SUCCESS,
                product_disposition=ProductDisposition.NONE,
                terminal_record_key="k"))

        # -- the runtime refuses to start work from a closed row ------------
        check_raises(
            "ownership refuses an already-closed attempt",
            RecordsError,
            lambda: resolve_ownership(
                writer, job_env, run_id=run_id,
                logical_job_id=logical_job_id,
                identity_extra={"exposure_id": 12345, "sca": 7},
                lifecycle_reader=reader))

        # -- numbering normalization, against the stored convention ---------
        stored_rows = executor.execute(
            "SELECT application_attempt_index, scheduler_attempt_index"
            "  FROM attempts WHERE attempt_id = %s", [ownership.attempt_id])
        app_index, sched_index = stored_rows[0]
        check("the application-observed index is stored one-based",
              normalize_attempt_index(1), app_index)
        check("the scheduler-observed index is untouched by the runtime",
              None, sched_index)

        # -- what the retry row proves by NOT being closeable ---------------
        #
        # The retry row is deliberately left in `submitted`, and that is a
        # finding rather than untidiness. The first version of this proof
        # tried to close it with `mark_terminal_without_start` and the
        # database refused:
        #
        #   attempts_state_terminal_without_start_check violated
        #
        # because that constraint requires `application_attempt_index IS
        # NULL`, and the resolver SETS that index when it creates the row.
        # The constraint is right and the call was wrong:
        # `terminal_without_start` means the application never touched this
        # attempt, so a row the runtime resolved can never legally reach it —
        # the schema makes "the application was here" and "the application
        # never ran" mutually exclusive, which is exactly the property the
        # two-writer design depends on.
        #
        # A resolved-but-never-started attempt is therefore closed by the
        # RECONCILER, through `terminal_after_start` or
        # `missing_or_contradictory`, from scheduler state the application
        # does not have. Leaving the row open is the correct end state for an
        # application-side proof: its non-terminal state beside its sibling is
        # itself the reconciliation signal (the proposal's words), and W6/W8
        # own closing it.
        # ConnectionExecutor rolls back on error (one statement, one
        # transaction), so the connection stays usable for the check that
        # follows — itself worth proving, since a poisoned connection after a
        # constraint violation is how a batch of writes silently stops.
        check_raises(
            "the database refuses terminal_without_start on a resolved row",
            Exception,
            lambda: writer.mark_terminal_without_start(
                retry.attempt_id,
                ended_at=datetime.datetime.now(datetime.timezone.utc),
                scheduler_state="FAILED",
                error_category="scheduler_provisioning"))
        check("the retry row is unchanged, and left for the reconciler",
              LifecycleState.SUBMITTED.value, reader(retry.attempt_id))

    print()
    if failures:
        print(f"!! {len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1
    print("LIVE-OWNERSHIP-OK — all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
