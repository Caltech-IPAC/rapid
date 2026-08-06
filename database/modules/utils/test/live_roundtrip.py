"""Live round-trip proof for the connection helper and the attempt writer.

NOT a unit test — the unit suites (``test_rapid_db_connect.py``,
``observability/test/test_attempts.py``) stub the database boundary
entirely and are the everyday gate. This script is the deliberate
opposite: it drives the real helper against the real pooler on rapid-db
as ``rapid_pipeline``, and the real ``AttemptWriter`` through the full
amended lifecycle, so the parts a stub cannot check — grants, the
resolver function, the CHECK matrix, the pooler's transaction lane — are
proven against the thing itself.

Run it on rapid-admin (team policy: never the laptop), inside the
pipeline image, with DBSERVER/DBPORT/DBNAME and RAPID_DB_SECRET_ID set:

    python3 -m database.modules.utils.test.live_roundtrip

It writes ADDITIVE test data only — one logical job and its attempt rows,
under a run id stamped with the current UTC time so it can never collide
with a previous run or with real work. Nothing is deleted or updated
outside the rows this run creates; the rows are left in a terminal state,
as evidence.

Exit code is the result: 0 all proven, 1 something failed.
"""

import datetime
import logging
import os
import sys
import uuid

from database.modules.utils import rapid_db_connect as dbc
from observability.attempts import (
    AttemptIdentity,
    AttemptWriter,
    ExecutionBinding,
    LifecycleState,
    ProductDisposition,
    Provenance,
    RapidOutcome,
    Stage,
    StageOutcome,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("live_roundtrip")

failures = []


def check(name, expected, got):
    if expected == got:
        print(f"PASS  {name}  (expected={expected!r} got={got!r})")
    else:
        print(f"FAIL  {name}  (expected={expected!r} got={got!r})")
        failures.append(name)


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    run_id = f"w1-live-{stamp}"
    logical_job_id = f"{run_id}-{uuid.uuid4().hex[:8]}"
    scheduler_job_id = f"live-{uuid.uuid4()}"

    print("=== live round-trip: connection helper + attempt writer ===")
    print(f">> run_id            {run_id}")
    print(f">> logical_job_id    {logical_job_id}")
    print(f">> scheduler_job_id  {scheduler_job_id}")
    print(f">> DBSERVER={os.getenv('DBSERVER')} DBPORT={os.getenv('DBPORT')} "
          f"DBNAME={os.getenv('DBNAME')}")
    print(f">> RAPID_DB_SECRET_ID={os.getenv('RAPID_DB_SECRET_ID')}")

    # --- the connection helper, through the pooler -------------------------
    # application_name carries the component and its lane, which is what
    # makes this connection attributable in pg_stat_activity and in the
    # pooler's admin interface.
    with dbc.connection("w1-live-roundtrip", lane=dbc.LANE_TRANSACTION) as conn:
        executor = dbc.ConnectionExecutor(conn)

        # Prove the connection is real and is the identity we intended.
        who = executor("SELECT current_user, current_setting('application_name')", [])
        print(f">> connected as {who[0][0]!r}, application_name={who[0][1]!r}")
        check("helper/connected-as-rapid_pipeline", "rapid_pipeline", who[0][0])
        check("helper/application_name-carries-lane",
              "w1-live-roundtrip[transaction]", who[0][1])

        # The pooler is in the path, not bypassed: port 6432 is pgbouncer.
        check("helper/through-the-pooler-port", "6432", os.getenv("DBPORT"))

        # Parameterization: a value that would be catastrophic if
        # interpolated comes back as a plain string.
        hostile = "'; DROP TABLE attempts; --"
        rows = executor("SELECT %s::text", [hostile])
        check("helper/parameterized-value-not-interpreted", hostile, rows[0][0])

        # Dynamic identifiers go through sql.Identifier.
        stmt = dbc.select_columns("attempts", ["attempt_id"], where_column="run_id")
        rows = executor(stmt, [run_id])
        check("helper/identifier-quoted-select-runs", [], rows)

        # --- the attempt writer, full amended lifecycle --------------------
        writer = AttemptWriter(executor)
        check("writer/declares-schema-version-2", 2, writer.schema_version)

        binding = ExecutionBinding(
            job_definition_arn=(
                "arn:aws:batch:us-east-1:ACCOUNT:job-definition/"
                "rapid-science:7"),
            job_definition_rev=7,
            image_digest="sha256:" + "0" * 64,
            release_identity=f"w1-live-{stamp}",
            manifest_checksum="sha256:" + "1" * 64,
        )

        writer.create_logical_job(logical_job_id, run_id, binding,
                                  scheduler_job_id=scheduler_job_id)
        rows = executor(
            "SELECT image_digest FROM logical_jobs WHERE logical_job_id = %s",
            [logical_job_id])
        check("writer/logical-job-recorded", binding.image_digest, rows[0][0])

        # Re-recording the same logical job must not rewrite the binding.
        writer.create_logical_job(logical_job_id, run_id, binding)
        rows = executor(
            "SELECT count(*) FROM logical_jobs WHERE logical_job_id = %s",
            [logical_job_id])
        check("writer/logical-job-creation-idempotent", 1, rows[0][0])

        identity = AttemptIdentity(run_id=run_id, logical_job_id=logical_job_id,
                                   exposure_id=None, sca=None)

        # Acquisition goes through the resolver — never a bare INSERT.
        attempt_id = writer.resolve_attempt(
            identity, created_at=now, submitted_at=now,
            scheduler_job_id=scheduler_job_id,
            application_attempt_index=1)
        print(f">> resolved attempt_id {attempt_id}")
        check("resolver/returned-an-attempt-id", True, isinstance(attempt_id, int))

        rows = executor(
            "SELECT lifecycle_state, binding_image_digest,"
            "       application_attempt_index"
            " FROM attempts WHERE attempt_id = %s", [attempt_id])
        check("resolver/created-in-submitted", "submitted", rows[0][0])
        check("resolver/copied-execution-binding", binding.image_digest, rows[0][1])
        check("resolver/set-application-index", 1, rows[0][2])

        # Idempotent re-resolution: the same call must find the same row.
        again = writer.resolve_attempt(
            identity, created_at=now, submitted_at=now,
            scheduler_job_id=scheduler_job_id,
            application_attempt_index=1)
        check("resolver/re-resolution-is-idempotent", attempt_id, again)

        # A scheduler retry (index 2) gets its OWN row, with the binding.
        retry_id = writer.resolve_attempt(
            identity, created_at=now, submitted_at=now,
            scheduler_job_id=scheduler_job_id,
            application_attempt_index=2)
        check("resolver/retry-is-a-new-row", True, retry_id != attempt_id)
        rows = executor(
            "SELECT binding_image_digest FROM attempts WHERE attempt_id = %s",
            [retry_id])
        check("resolver/retry-row-copied-binding", binding.image_digest, rows[0][0])

        # --- started -------------------------------------------------------
        provenance = Provenance(
            source_sha="w1-live-source-sha",
            container_digest="sha256:" + "0" * 64,
            job_definition_rev="7",
            config_digest="sha256:" + "2" * 64,
        )
        writer.mark_started(attempt_id, started_at=now, provenance=provenance,
                            scheduler_job_id=scheduler_job_id,
                            application_attempt_index=1)
        rows = executor("SELECT lifecycle_state FROM attempts WHERE attempt_id = %s",
                        [attempt_id])
        check("lifecycle/started", "started", rows[0][0])

        writer.record_stage(attempt_id, Stage(
            stage_name="w1_live_roundtrip_stage", started_at=now,
            duration_ms=12.5, outcome=StageOutcome.SUCCESS))
        rows = executor(
            "SELECT count(*) FROM attempt_stages WHERE attempt_id = %s",
            [attempt_id])
        check("lifecycle/stage-recorded", 1, rows[0][0])

        # --- application-closed: the new state -----------------------------
        record_key = f"records/{run_id}/{attempt_id}/0.json"
        writer.mark_application_closed(
            attempt_id, ended_at=now, application_intended_exit=0,
            rapid_outcome=RapidOutcome.SUCCESS,
            product_disposition=ProductDisposition.NONE,
            terminal_record_key=record_key,
            terminal_record_sequence=0,
            terminal_record_checksum="sha256:" + "3" * 64)
        rows = executor(
            "SELECT lifecycle_state, application_intended_exit,"
            "       scheduler_observed_exit, terminal_record_key,"
            "       terminal_record_sequence"
            " FROM attempts WHERE attempt_id = %s", [attempt_id])
        check("lifecycle/application-closed", "application_closed", rows[0][0])
        check("lifecycle/application-intended-exit-written", 0, rows[0][1])
        # The defining absence of the state: the scheduler's exit is not
        # known yet, and the row says so with NULL rather than a sentinel.
        check("lifecycle/scheduler-exit-still-absent", None, rows[0][2])
        check("lifecycle/terminal-record-cited", record_key, rows[0][3])
        check("lifecycle/record-sequence-zero", 0, rows[0][4])

        # --- terminal after start: the reconciler's transition -------------
        writer.mark_terminal_after_start(
            attempt_id, ended_at=now,
            scheduler_observed_exit=0, scheduler_state="SUCCEEDED")
        rows = executor(
            "SELECT lifecycle_state, application_intended_exit,"
            "       scheduler_observed_exit, scheduler_state, rapid_outcome"
            " FROM attempts WHERE attempt_id = %s", [attempt_id])
        check("lifecycle/terminal-after-start", "terminal_after_start", rows[0][0])
        # COALESCE must have PRESERVED the application's own values.
        check("lifecycle/application-intent-preserved", 0, rows[0][1])
        check("lifecycle/scheduler-exit-recorded", 0, rows[0][2])
        check("lifecycle/scheduler-state-recorded", "SUCCEEDED", rows[0][3])
        check("lifecycle/application-outcome-not-overwritten", "success", rows[0][4])

        # --- the retry row is closed too, so nothing is left non-terminal --
        #
        # NOT as terminal_without_start, and the reason is worth recording:
        # that state forbids the application-observed attempt index, because
        # it means the application never ran. This row was created by
        # resolve_attempt WITH an application index — the runtime was present
        # and asked for it — so calling it "never started" would be a lie the
        # constraint correctly refuses (proven live: CheckViolation on
        # attempts_state_terminal_without_start_check).
        #
        # terminal_without_start is for a row the RECONCILER created from
        # scheduler history for an attempt that never ran, which carries only
        # the scheduler index. The distinction is exactly the one the
        # two-index split exists to preserve, so this closes the retry the
        # honest way instead: started, then closed as an abrupt loss.
        writer.mark_started(retry_id, started_at=now, provenance=provenance,
                            scheduler_job_id=scheduler_job_id,
                            application_attempt_index=2)
        writer.mark_abrupt_loss(
            retry_id, ended_at=now, scheduler_state="FAILED",
            error_category="scheduler_reclaimed",
            scheduler_observed_exit=137,
            terminal_record_key=f"records/{run_id}/{retry_id}/1.json",
            terminal_record_sequence=1)
        rows = executor(
            "SELECT lifecycle_state, error_category, scheduler_observed_exit,"
            "       application_intended_exit"
            " FROM attempts WHERE attempt_id = %s", [retry_id])
        check("lifecycle/retry-row-terminal", "terminal_after_start", rows[0][0])
        check("lifecycle/allowlisted-error-category-accepted",
              "scheduler_reclaimed", rows[0][1])
        check("lifecycle/abrupt-loss-records-scheduler-exit", 137, rows[0][2])
        # The honest absence: the application never stated an intent, and the
        # row says NULL rather than a fabricated code.
        check("lifecycle/abrupt-loss-leaves-intent-absent", None, rows[0][3])

        # A genuinely never-started attempt — reconciler-created from
        # scheduler history, scheduler index only, no application index —
        # IS terminal_without_start, which is the state's real occupant.
        never_started_id = writer.resolve_attempt(
            AttemptIdentity(run_id=run_id, logical_job_id=logical_job_id),
            created_at=now, submitted_at=now,
            scheduler_job_id=f"{scheduler_job_id}-never",
            scheduler_attempt_index=1)
        writer.mark_terminal_without_start(
            never_started_id, ended_at=now, scheduler_state="FAILED",
            error_category="scheduler_provisioning")
        rows = executor(
            "SELECT lifecycle_state, error_category,"
            "       application_attempt_index"
            " FROM attempts WHERE attempt_id = %s", [never_started_id])
        check("lifecycle/never-started-is-terminal-without-start",
              "terminal_without_start", rows[0][0])
        check("lifecycle/never-started-category", "scheduler_provisioning",
              rows[0][1])
        check("lifecycle/never-started-has-no-application-index", None, rows[0][2])

        # --- errors are RAISED, never flagged ------------------------------
        # The contract's headline property, proven against the live database:
        # a constraint violation arrives as an exception, not as a member
        # flag a caller might forget to read.
        raised = None
        try:
            executor(
                "INSERT INTO attempts (schema_version, run_id, logical_job_id,"
                " lifecycle_state, created_at, submitted_at, error_category)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [2, run_id, logical_job_id, "terminal_without_start",
                 now, now, "not_a_real_category"])
        except Exception as exc:  # noqa: BLE001 - proving it propagates
            raised = type(exc).__name__
        check("contract/invalid-category-raises", True, raised is not None)
        print(f">> the disallowed category raised {raised}")

        check("contract/executor-has-no-exit_code-member", False,
              hasattr(executor, "exit_code"))

        # Final state of everything this run created.
        print("\n>> rows created by this run:")
        rows = executor(
            "SELECT attempt_id, schema_version, lifecycle_state,"
            "       application_attempt_index, application_intended_exit,"
            "       scheduler_observed_exit"
            " FROM attempts WHERE run_id = %s ORDER BY attempt_id", [run_id])
        for r in rows:
            print(f"   attempt_id={r[0]} v={r[1]} state={r[2]} "
                  f"app_idx={r[3]} app_exit={r[4]} sched_exit={r[5]}")

    print(f"\n=== live round-trip: {len(failures)} failure(s) ===")
    if failures:
        for name in failures:
            print(f"   FAILED: {name}")
        return 1
    print("LIVE-ROUNDTRIP-OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
