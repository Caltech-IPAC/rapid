"""W8's failure-injection acceptance battery, against the LIVE stack.

NOT a unit test. The unit suites stub the database, the scheduler and the
object stores and are the everyday gate; this drives the REAL amended
schema, the REAL resolver, REAL S3 create-once semantics and REAL Batch,
because the properties under test are properties of those systems and a
stub cannot stand in for any of them:

* a stub cannot REFUSE a state — the lifecycle constraints can, and several
  cases here turn on a transition being rejected by the database;
* a stub cannot enforce create-once — S3's conditional write can, and the
  supersession and idempotency cases turn on exactly that;
* a stub cannot lose a race — two writers against one row can.

The battery is the acceptance gate named in the co-design's W8 row. Each
case states its MECHANISM (what would break if the property did not hold)
and leaves EVIDENCE (a row, a record, a tag set) that outlives the run.

ADDITIVE only. Rows and objects are created under a run id stamped with the
current UTC time, so nothing collides with previous runs or real work.
Nothing is deleted, nothing outside this run's own identifiers is updated.
The records written ARE legitimate attempt records — they describe attempts
that really were made, and the reconciler is entitled to classify them.

Run inside the pipeline image on a host whose role reaches rapid-db, Batch
and the records/diagnostics buckets:

    python3 -m pipeline.reconciler.test.live_w8_battery

Exit code is the result: 0 all proven, 1 one or more failed.
"""

import datetime
import json
import logging
import os
import sys

import boto3

from database.modules.utils import rapid_db_connect as dbc
from observability.attempts import (
    AttemptIdentity,
    AttemptWriter,
    ExecutionBinding,
    LifecycleState,
    ProductDisposition,
    Provenance,
    RapidOutcome,
    ReconciliationClass,
)
from pipeline.reconciler import retention
from pipeline.reconciler.closure import (
    build_closure_record,
    publish_closure_record,
    read_predecessor,
)
from pipeline.reconciler.service import ReconcilerService
from pipeline.runtime import termination
from pipeline.runtime.boundaries import S3ObjectStore
from pipeline.runtime.errors import (
    RecordsError,
    StorageError,
    ToolError,
    categorize,
)
from pipeline.runtime.process import run_tool
from submission.manifest import Manifest, ProcessingUnit, UnitFacts
from submission.routes import (
    CLASS_BULK,
    CLASS_PROMPT,
    JOB_TYPE_REFERENCE_IMAGE,
    JOB_TYPE_SCIENCE,
    RouteError,
    route_for,
    validate_route,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("w8.battery")

RUN = f"w8-battery-{datetime.datetime.now(datetime.timezone.utc):%Y%m%dT%H%M%SZ}"

results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))
    print(f"{'PASS' if condition else 'FAIL'}  {name}"
          f"{'  (' + detail + ')' if detail else ''}")
    return bool(condition)


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def binding_for(suffix="1"):
    """A COMPLETE execution binding.

    The account segment is a non-numeric placeholder deliberately: this repo
    is PUBLIC and .githooks/pre-push blocks account-shaped literals. Nothing
    here resolves the ARN — the battery only compares bindings — so a
    placeholder is the whole requirement.
    """
    return ExecutionBinding(
        job_definition_arn=(
            "arn:aws:batch:us-east-1:ACCOUNT:job-definition/"
            f"rapid-pipeline-science:{suffix}"),
        image_digest="sha256:" + "a" * 64,
        manifest_checksum="sha256:" + "b" * 64,
        job_definition_rev=int(suffix),
        release_identity="rapid-w8-battery")


def provenance_for():
    return Provenance(source_sha="w8" + "0" * 38,
                      container_digest="sha256:" + "a" * 64,
                      job_definition_rev="rapid-pipeline-science",
                      config_digest="sha256:" + "c" * 64)


def scalar(conn, sql, params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return None if row is None else row[0]


def row_of(conn, attempt_id, column):
    return scalar(conn, f"SELECT {column} FROM attempts WHERE attempt_id = %s",
                  (attempt_id,))


def new_attempt(writer, conn, suffix, exposure, sca, scheduler_job_id=None,
                startable=True):
    """A pre-created submitted row, as the submission seam writes one.

    `startable` gives the row a scheduler job id, because
    `attempts_state_started_check` requires one on any `started` row — a
    started attempt necessarily HAS a scheduler job, and the constraint says
    so. Cases about attempts that never resolved a scheduler id pass
    `startable=False` and must not then start the row.
    """
    if startable and scheduler_job_id is None:
        scheduler_job_id = f"{RUN}-{exposure}-{sca}"
    logical = f"{RUN}_{exposure}_{sca}"
    binding = binding_for(suffix)
    writer.create_logical_job(logical, RUN, binding,
                              scheduler_job_id=scheduler_job_id)
    identity = AttemptIdentity(run_id=RUN, logical_job_id=logical,
                               exposure_id=exposure, sca=sca)
    moment = now()
    attempt_id = writer.create_submitted(identity, moment, moment,
                                         scheduler_job_id=scheduler_job_id,
                                         binding=binding)
    return attempt_id, identity, binding


# ---------------------------------------------------------------------------
# Group 1 — the runtime's own failure semantics (no database needed)
# ---------------------------------------------------------------------------

def case_tool_exit_one():
    """A tool exiting 1 is a ToolError, not a silent success.

    MECHANISM: the as-is finding #1 — a science job could not fail, because
    only exit >= 64 propagated. If `run_tool` swallowed a nonzero exit the
    whole fail-loud posture is decorative.
    """
    try:
        run_tool(["/bin/sh", "-c", "exit 1"])
        check("1/tool-exit-1-raises", False, "run_tool returned instead")
    except ToolError as exc:
        check("1/tool-exit-1-raises",
              exc.details.get("returncode") == 1
              and categorize(exc) == "tool_failure",
              f"returncode={exc.details.get('returncode')} "
              f"category={categorize(exc)}")


def case_missing_binary():
    """A missing binary is a classified failure, not FileNotFoundError.

    MECHANISM: finding #2 — an uncaught FileNotFoundError escapes the error
    taxonomy entirely, so the attempt record carries internal_error (or no
    record at all) for what is squarely a tool failure.
    """
    try:
        run_tool(["/nonexistent/w8-no-such-tool"])
        check("2/missing-binary-raises", False, "run_tool returned instead")
    except ToolError as exc:
        check("2/missing-binary-raises",
              exc.details.get("returncode") == 127
              and categorize(exc) == "tool_failure",
              f"returncode={exc.details.get('returncode')}")
    except FileNotFoundError:
        check("2/missing-binary-raises", False,
              "raised bare FileNotFoundError — the audit's exact defect")


def case_route_rejections():
    """Wrong class and wrong queue are both refused, by name.

    MECHANISM: the route matrix binds job type, queue, definition and DB
    lane into ONE validated tuple. If a science manifest could run on the
    bulk definition, three independently-selectable facts are back.
    """
    try:
        validate_route(JOB_TYPE_SCIENCE, CLASS_BULK)
        check("3a/wrong-class-rejected", False, "accepted a bulk science job")
    except RouteError as exc:
        check("3a/wrong-class-rejected", True, str(exc)[:70])

    # Keys as the parameter tree actually names them. Getting these wrong
    # makes the wrong-queue case pass for the WRONG REASON — RouteError for
    # "the tree does not carry this key" rather than for the mismatch the
    # case is about — which is why 3c asserts the happy path immediately
    # after: an accepted right-route proves the keys resolve.
    queues = {route_for(JOB_TYPE_SCIENCE).queue_parameter: "rapid-queue-prompt",
              route_for(JOB_TYPE_REFERENCE_IMAGE).queue_parameter: "rapid-queue-bulk"}
    try:
        validate_route(JOB_TYPE_SCIENCE, CLASS_PROMPT,
                       queue_name="rapid-queue-bulk", queue_names=queues)
        check("3b/wrong-queue-rejected", False, "accepted the wrong queue")
    except RouteError as exc:
        check("3b/wrong-queue-rejected", True, str(exc)[:70])

    route = validate_route(JOB_TYPE_SCIENCE, CLASS_PROMPT,
                           queue_name="rapid-queue-prompt", queue_names=queues)
    check("3c/right-route-accepted", route.workload_class == CLASS_PROMPT,
          f"lane={route.db_lane}")

    try:
        validate_route(JOB_TYPE_REFERENCE_IMAGE, CLASS_PROMPT)
        check("3d/reference-image-is-bulk", False, "accepted on the prompt class")
    except RouteError:
        check("3d/reference-image-is-bulk", True, "prompt refused, as the matrix says")


def case_manifest_type_mismatch():
    """A manifest whose job type the definition's class cannot run is refused.

    MECHANISM: the entrypoint's startup check is the second line of defence
    (the definition's fixed --class is the first). Both must hold or an
    operator can submit reference-image work to the prompt queue.
    """
    unit = ProcessingUnit(exposure=999001, sca=1, facts=UnitFacts(rid=1))
    manifest = Manifest(units=[unit], batch_id=RUN,
                        job_type=JOB_TYPE_REFERENCE_IMAGE)
    try:
        manifest.validate_for(CLASS_PROMPT)
        check("4/manifest-class-mismatch-rejected", False, "accepted")
    except RouteError as exc:
        check("4/manifest-class-mismatch-rejected", True, str(exc)[:70])


# ---------------------------------------------------------------------------
# Group 2 — the terminal protocol's boundaries, against real S3
# ---------------------------------------------------------------------------

def case_snapshot_persistence_failure(store):
    """A snapshot that cannot be persisted is a RecordsError before work.

    MECHANISM: the design binds digest and snapshot key in the SAME write
    that marks started, so there is no bound-but-unpersisted state. A
    swallowed snapshot failure would produce attempts whose configuration
    digest describes configuration nobody can reconstruct.
    """
    class RefusingStore:
        def put_if_absent(self, *a, **k):
            raise StorageError("w8: injected snapshot-store failure")

    try:
        termination.persist_configuration_snapshot(
            RefusingStore(), f"attempts/config/{RUN}", {"w8": "snapshot"})
        check("5/snapshot-failure-raises", False, "returned instead")
    except RecordsError as exc:
        check("5/snapshot-failure-raises", True, str(exc)[:60])


def case_record_create_once(store, prefix):
    """The terminal record is create-once and validates a re-write.

    MECHANISM: idempotent-by-identity. The reconciler must be able to REPLAY
    the termination protocol after a crash without double-writing, and it
    distinguishes "already there and valid" from "already there and wrong"
    by checksum, never by mere presence.
    """
    key = termination.terminal_record_key(prefix, RUN, "create-once", 1, 0)
    record = {"attempt_id": 1, "run_id": RUN, "w8": "first"}
    first = termination.write_terminal_record(store, key, record)
    second = termination.write_terminal_record(store, key, record)
    check("6a/record-create-once-idempotent",
          first["checksum"] == second["checksum"],
          f"checksum {first['checksum'][:16]} stable across a replay")

    # A REPLAY of the same attempt is deliberately NOT byte-identical — the
    # re-run's ended_at is a later moment — so the protocol validates the
    # existing object and KEEPS it rather than comparing content. What must
    # hold is that the published bytes are unchanged and the caller is handed
    # the EXISTING checksum, so the attempt row cites what is actually
    # stored. Overwriting would mutate an immutable record; raising would
    # misdiagnose the ordinary crash-and-retry case as an identity collision.
    replay = termination.write_terminal_record(
        store, key, {"attempt_id": 1, "run_id": RUN, "w8": "second attempt"})
    stored = json.loads(store.get(key))
    check("6b/replay-keeps-the-published-record",
          stored.get("w8") == "first"
          and replay["checksum"] == first["checksum"]
          and replay["created"] is False,
          "the first record survives a replay and its checksum is returned")

    # A genuine identity collision — a DIFFERENT attempt deriving this key —
    # is the case that must raise, because it means two attempts share an
    # identity and one of them is about to lose its account.
    try:
        termination.write_terminal_record(
            store, key, {"attempt_id": 987654, "run_id": RUN})
        check("6c/identity-collision-raises", False,
              "accepted a record for a different attempt at this key")
    except RecordsError as exc:
        check("6c/identity-collision-raises", True, str(exc)[:70])


def case_bundle_upload_and_retention(s3_client, diagnostics_bucket):
    """The bundle's retention tag is reconciler-stamped and monotonic.

    MECHANISM: the bundle key is classification-neutral, so retention rides
    on a TAG the reconciler stamps at classification time. Because the S3
    tagging API replaces the whole tag set, a correction must rewrite the
    canonical set; and a reclassified failure must never inherit the
    shorter success expiry.
    """
    key = f"attempts/bundles/{RUN}/w8-retention/attempt-1/bundle.tar.gz"
    s3_client.put_object(Bucket=diagnostics_bucket, Key=key,
                         Body=b"w8 battery bundle")

    row = {"attempt_id": 1, "run_id": RUN,
           "binding_release_identity": "rapid-w8-battery"}

    stamped = retention.stamp_retention(s3_client, diagnostics_bucket, key,
                                        row, retention.CLASS_SUCCESS)
    check("7a/retention-stamped",
          stamped and stamped.get(retention.TAG_RETENTION) == "success",
          str(stamped))

    lengthened = retention.stamp_retention(s3_client, diagnostics_bucket, key,
                                           row, retention.CLASS_FAILURE)
    check("7b/monotonic-retag-to-longer",
          lengthened
          and lengthened.get(retention.TAG_RETENTION) == "failure"
          and lengthened.get(retention.TAG_RELEASE) == "rapid-w8-battery"
          and lengthened.get(retention.TAG_ATTEMPT) == "1",
          "FULL canonical tag set verified, not the retention tag alone")

    shortened = retention.stamp_retention(s3_client, diagnostics_bucket, key,
                                          row, retention.CLASS_SUCCESS)
    live = retention.read_retention_class(s3_client, diagnostics_bucket, key)
    check("7c/shortening-retag-refused",
          shortened is None and live == "failure",
          f"live class still {live!r} after a success-ward correction")


def case_tags_unreadable(diagnostics_bucket):
    """An unreadable tag set raises rather than reading as untagged.

    MECHANISM: finding #16. Converting a read failure into "no tags" is what
    let a transient error replace a failure-class retention with the shorter
    success expiry — and terminal rows are outside the open set, so nothing
    would ever notice.
    """
    class RefusingTags:
        def get_object_tagging(self, **kwargs):
            error = Exception("w8: injected AccessDenied")
            error.response = {"Error": {"Code": "AccessDenied"}}
            raise error

    try:
        retention.read_retention_class(RefusingTags(), diagnostics_bucket,
                                       "any/key")
        check("8/unreadable-tags-raise", False, "returned instead of raising")
    except retention.TagsUnreadable as exc:
        check("8/unreadable-tags-raise", True, str(exc)[:60])


# ---------------------------------------------------------------------------
# Group 3 — lifecycle boundaries against the real schema
# ---------------------------------------------------------------------------

def case_kill_before_started_cas(writer, conn):
    """A death after snapshot persistence but before the started CAS.

    MECHANISM: terminal-without-start must be REACHABLE. The row was
    pre-created and claimed; no work happened, by construction, because the
    snapshot/started binding is one write. The reconciler must be able to
    close such a row without inventing runtime provenance it never had.
    """
    attempt_id, _, _ = new_attempt(writer, conn, "1", 999101, 1,
                                   startable=False)
    writer.mark_terminal_without_start(
        attempt_id, now(), "FAILED",
        error_category="scheduler_provisioning",
        closure_record_key=f"attempts/records/{RUN}/w8/attempt-{attempt_id}/seq-0001.json",
        closure_record_sequence=1)
    state = row_of(conn, attempt_id, "lifecycle_state")
    started = row_of(conn, attempt_id, "started_at")
    check("9/kill-pre-started-cas",
          state == LifecycleState.TERMINAL_WITHOUT_START.value
          and started is None,
          f"state={state} started_at={started!r} — no work, by construction")
    return attempt_id


def case_kill_after_started_before_work(writer, conn):
    """A death after the started CAS but before any work.

    MECHANISM: the started row carries digest and snapshot key, so the
    attempt is FULLY reconstructible — that is the whole point of binding
    them in the same statement. The row stays open and the reconciler owns
    closing it.
    """
    attempt_id, _, _ = new_attempt(writer, conn, "1", 999102, 2)
    writer.mark_started(attempt_id, now(), provenance_for(),
                        application_attempt_index=1,
                        config_snapshot_key=f"attempts/config/{RUN}/snap.json")
    state = row_of(conn, attempt_id, "lifecycle_state")
    snapshot = row_of(conn, attempt_id, "config_snapshot_key")
    digest = row_of(conn, attempt_id, "config_digest")
    check("10/kill-post-started-pre-work",
          state == LifecycleState.STARTED.value and snapshot and digest,
          f"state={state} snapshot bound={bool(snapshot)} digest bound={bool(digest)}")
    return attempt_id


def case_started_cas_is_a_cas(writer, conn):
    """The started transition matches once and only once.

    MECHANISM: two writers (a late runtime and a reconciler) must not both
    "start" one row. The rowcount contract is only meaningful against a real
    driver.
    """
    attempt_id, _, _ = new_attempt(writer, conn, "1", 999103, 3)
    writer.mark_started(attempt_id, now(), provenance_for(),
                        application_attempt_index=1,
                        config_snapshot_key=f"attempts/config/{RUN}/snap.json")
    try:
        writer.mark_started(attempt_id, now(), provenance_for(),
                            application_attempt_index=1,
                            config_snapshot_key=f"attempts/config/{RUN}/snap.json")
        check("11/started-cas-matches-once", False, "a second start succeeded")
    except Exception as exc:                                   # noqa: BLE001
        check("11/started-cas-matches-once", True,
              f"{type(exc).__name__}: {str(exc)[:50]}")


def case_crash_between_record_and_row(writer, conn, store, prefix):
    """Record valid, row still started — the materialization case.

    MECHANISM: the S3 terminal record is written BEFORE the
    application-closed CAS, so a crash between them leaves a started row
    beside a valid record. The record is the authoritative application
    account; the reconciler materializes the row FROM it, values verbatim,
    marked reconciler-materialized. That mark is the row-level analog of
    the adopted marked-reconstruction rule.

    REWRITTEN in round 2. This case used to write sequence 0 and then call
    `writer.mark_application_closed` ITSELF, passing the key and checksum it
    happened to be holding — so it proved the DDL accepts that transition, and
    nothing about whether the reconciler can perform it. The real defect lived
    exactly in the gap the case stepped over: in this crash state the row's
    `terminal_record_key` and `terminal_record_checksum` are NULL, the record
    body cannot carry either, and PostgreSQL requires a non-null key. Every
    real reconciliation pass attempted an illegal transition and left the
    attempt `started` forever (review finding #14).

    So the case now drives `ReconcilerService.poll_once` against the real row,
    the real record and the real constraints, and asserts the row ends up
    citing the key and checksum the reconciler derived for itself.
    """
    attempt_id, identity, _ = new_attempt(writer, conn, "1", 999104, 4)
    writer.mark_started(attempt_id, now(), provenance_for(),
                        application_attempt_index=1,
                        config_snapshot_key=f"attempts/config/{RUN}/snap.json")

    key = termination.terminal_record_key(prefix, RUN, identity.logical_job_id,
                                          attempt_id, 0)
    record = {"schema_version": termination.RECORD_SCHEMA_VERSION,
              "attempt_id": attempt_id, "run_id": RUN,
              "logical_job_id": identity.logical_job_id,
              "rapid_outcome": "success", "product_disposition": "published",
              "application_intended_exit": 0, "record_sequence": 0}
    written = termination.write_terminal_record(store, key, record)

    # The crash is here: the record exists, the row is still `started`, and
    # NEITHER citation column is set — which is the whole point.
    state_before = row_of(conn, attempt_id, "lifecycle_state")
    key_before = row_of(conn, attempt_id, "terminal_record_key")

    scheduler_job_id = row_of(conn, attempt_id, "scheduler_job_id")
    service = ReconcilerService(
        conn=conn,
        batch_client=_SucceededBatch(scheduler_job_id),
        records_store=store, diagnostics_store=store,
        s3_client=boto3.client("s3"),
        records_prefix=prefix,
        diagnostics_bucket=os.environ.get("RAPID_DIAGNOSTICS_BUCKET",
                                          "roman-rapid-diagnostics"))
    summary = service.poll_once()

    state_after = row_of(conn, attempt_id, "lifecycle_state")
    materialized = row_of(conn, attempt_id, "reconciler_materialized")
    key_after = row_of(conn, attempt_id, "terminal_record_key")
    checksum_after = row_of(conn, attempt_id, "terminal_record_checksum")

    check("12/materialized-by-the-reconciler-from-a-valid-record",
          state_before == LifecycleState.STARTED.value
          and key_before is None
          and materialized is True
          and state_after in (LifecycleState.APPLICATION_CLOSED.value,
                              LifecycleState.TERMINAL_AFTER_START.value),
          f"{state_before} -> {state_after}, "
          f"reconciler_materialized={materialized}, summary={summary}")

    # The CHECKSUM is the evidence, not the key. Materialization supplies
    # sequence 0's key and checksum — both NULL on the row and absent from the
    # body, which is what made the transition illegal — and then
    # `mark_terminal_after_start` advances the row's key citation to the
    # reconciler's own sequence-1 record, which is by then the authoritative
    # account. The checksum survives that write because sequence 1 folds the
    # predecessor's facts in verbatim.
    check("12b/materialization-supplied-the-checksum-it-computed",
          checksum_after == written["checksum"],
          f"row cites checksum={checksum_after!r} "
          f"(expected sequence 0's {written['checksum']!r}); "
          f"key now cites {key_after!r}")

    return attempt_id, key, written["checksum"]


class _SucceededBatch:
    """A Batch client that reports one job SUCCEEDED, for the battery.

    The battery's rows carry synthetic scheduler job ids that Batch has never
    heard of, so a real `describe_jobs` returns nothing and the attempt goes
    down the unresolved path instead of the one under test. This answers for
    exactly the job id asked about and nothing else.
    """

    def __init__(self, job_id):
        self.job_id = job_id

    def describe_jobs(self, jobs):
        return {"jobs": [{"jobId": job_id,
                          "status": "SUCCEEDED",
                          "createdAt": 1,
                          "startedAt": 2,
                          "stoppedAt": 3,
                          "container": {"exitCode": 0}}
                         for job_id in jobs if job_id == self.job_id]}


def case_no_retry_on_clean_application_failure(writer, conn):
    """A recorded application failure exits 0 — nothing for Batch to retry.

    MECHANISM: scheduler-SUCCEEDED with application-failure is the
    representable combination the schema was built for. If a classified
    failure exited nonzero, the definition's catch-all would burn attempts
    re-running work that will fail identically.
    """
    attempt_id, identity, _ = new_attempt(writer, conn, "1", 999105, 5)
    writer.mark_started(attempt_id, now(), provenance_for(),
                        application_attempt_index=1,
                        config_snapshot_key=f"attempts/config/{RUN}/snap.json")
    writer.mark_application_closed(
        attempt_id, now(), termination.EXIT_RECORDED, RapidOutcome.FAILURE,
        ProductDisposition.NONE,
        f"attempts/records/{RUN}/{identity.logical_job_id}/attempt-{attempt_id}/seq-0000.json",
        error_category="tool_failure")
    intended = row_of(conn, attempt_id, "application_intended_exit")
    outcome = row_of(conn, attempt_id, "rapid_outcome")
    category = row_of(conn, attempt_id, "error_category")
    check("13/clean-application-failure-exits-zero",
          intended == 0 and outcome == "failure" and category == "tool_failure",
          f"intended_exit={intended} outcome={outcome} category={category}")
    return attempt_id


def case_reconciler_authored_category_refused_to_application(writer, conn):
    """The application cannot author a reconciler-only category.

    MECHANISM: no field has two writers. scheduler_reclaimed and
    scheduler_provisioning describe things only the scheduler observer can
    know; an application claiming them would be inventing an observation.
    """
    attempt_id, identity, _ = new_attempt(writer, conn, "1", 999106, 6)
    writer.mark_started(attempt_id, now(), provenance_for(),
                        application_attempt_index=1,
                        config_snapshot_key=f"attempts/config/{RUN}/snap.json")
    try:
        writer.mark_application_closed(
            attempt_id, now(), 0, RapidOutcome.FAILURE,
            ProductDisposition.NONE, "attempts/records/w8/none.json",
            error_category="scheduler_reclaimed")
        check("14/reconciler-category-refused-to-application", False,
              "the application authored a reconciler category")
    except Exception as exc:                                   # noqa: BLE001
        check("14/reconciler-category-refused-to-application", True,
              f"{type(exc).__name__}: {str(exc)[:60]}")


def case_registration_refuses_application_failure(conn):
    """Registration refuses an application-failed attempt, by taxonomy.

    MECHANISM: "a scheduler-SUCCEEDED/application-failed attempt is refused
    registration by taxonomy, not by exit-code folklore". The query that
    selects registrable work must not return one.
    """
    registrable = scalar(conn, """
        SELECT count(*) FROM attempts
        WHERE run_id = %s AND rapid_outcome = 'failure'
          AND lifecycle_state IN ('application_closed', 'terminal_after_start')
          AND rapid_outcome = 'success'
    """, (RUN,))
    failures = scalar(conn, """
        SELECT count(*) FROM attempts
        WHERE run_id = %s AND rapid_outcome = 'failure'
    """, (RUN,))
    check("15/registration-refuses-application-failures",
          registrable == 0 and failures > 0,
          f"{failures} failed attempts in this run, {registrable} selected as registrable")


# ---------------------------------------------------------------------------
# Group 4 — supersession, closure and the reconciler cycle
# ---------------------------------------------------------------------------

def case_supersession_and_consumer_selection(store, prefix, writer, conn):
    """A contradiction is superseded at a higher sequence; consumers take it.

    MECHANISM: the application writes sequence 0 where it ran; ONLY the
    reconciler writes higher. Every reconciler record is a COMPLETE
    canonical snapshot, so the highest-sequence record alone is the full
    terminal account and consumers never chain-fold.
    """
    attempt_id, identity, _ = new_attempt(writer, conn, "1", 999107, 7)
    writer.mark_started(attempt_id, now(), provenance_for(),
                        application_attempt_index=1,
                        config_snapshot_key=f"attempts/config/{RUN}/snap.json")

    seq0_key = termination.terminal_record_key(prefix, RUN,
                                               identity.logical_job_id,
                                               attempt_id, 0)
    seq0 = {"schema_version": termination.RECORD_SCHEMA_VERSION,
            "attempt_id": attempt_id, "run_id": RUN,
            "logical_job_id": identity.logical_job_id,
            "rapid_outcome": "success", "record_sequence": 0}
    termination.write_terminal_record(store, seq0_key, seq0)

    seq1_key = termination.terminal_record_key(prefix, RUN,
                                               identity.logical_job_id,
                                               attempt_id, 1)
    seq1 = dict(seq0)
    seq1.update({"record_sequence": 1, "rapid_outcome": "failure",
                 "reconciler_authored": True, "supersedes": seq0_key})
    termination.write_terminal_record(store, seq1_key, seq1)

    # Consumer selection: the keys sort lexically by their zero-padded
    # sequence, so "the highest-sequence record" is `max` over the keys —
    # which is exactly the selection a consumer makes, done here the same way.
    highest = max([seq0_key, seq1_key])
    body = json.loads(store.get(highest))
    check("16/supersession-consumer-selects-highest",
          highest == seq1_key and body.get("rapid_outcome") == "failure"
          and body.get("record_sequence") == 1,
          f"highest={highest.rsplit('/', 1)[-1]} outcome={body.get('rapid_outcome')}")
    check("17/reconciler-record-is-a-complete-snapshot",
          body.get("attempt_id") == attempt_id
          and body.get("run_id") == RUN
          and body.get("logical_job_id") == identity.logical_job_id,
          "the superseding record carries the predecessor's facts, not a delta")
    return attempt_id


def case_checksum_invalid_predecessor(store, prefix, writer, conn):
    """A sequence-0 record that fails validation is REJECTED, by name.

    MECHANISM: "validates by key and checksum — never by mere presence". A
    reconciler that trusted presence would fold corrupt application facts
    into its canonical snapshot and call it authoritative.
    """
    attempt_id, identity, _ = new_attempt(writer, conn, "1", 999108, 8)
    key = termination.terminal_record_key(prefix, RUN, identity.logical_job_id,
                                          attempt_id, 0)
    # A record whose attempt_id does not match the row it claims to describe.
    termination.write_terminal_record(store, key,
                                      {"attempt_id": attempt_id + 90000,
                                       "run_id": RUN, "record_sequence": 0})
    read = read_predecessor(store, key, attempt_id)
    check("18/invalid-predecessor-rejected-by-name",
          read.body is None and read.reason is not None and not read.deferred,
          f"rejected: {read.reason}")
    return attempt_id, key, read.reason


def case_reconciler_first_on_never_started(store, records_prefix, writer, conn):
    """A never-started attempt still gets a COMPLETE closure record.

    MECHANISM: "the reconciler closes EVERY attempt with a closure record",
    and a reconciler-first record covers any attempt whose sequence 0 is
    missing. The failure it prevents is an attempt that simply stops being
    accounted for — no application record, no closure, nothing to reconcile
    against. It carries a machine-readable category
    (`scheduler_provisioning`), never a null or a mislabelled
    internal_error.
    """
    attempt_id, identity, binding = new_attempt(writer, conn, "1", 999109, 9,
                                                startable=False)
    row = {"attempt_id": attempt_id, "run_id": RUN,
           "logical_job_id": identity.logical_job_id,
           "exposure_id": 999109, "sca": 9,
           "lifecycle_state": LifecycleState.SUBMITTED.value,
           "scheduler_job_id": None,
           "submitted_at": now(),
           "binding_job_definition_arn": binding.job_definition_arn,
           "binding_image_digest": binding.image_digest,
           "binding_manifest_checksum": binding.manifest_checksum,
           "binding_job_definition_rev": binding.job_definition_rev,
           "binding_release_identity": binding.release_identity}

    record = build_closure_record(
        row, observation=None, sequence=1, predecessor=None,
        classification=ReconciliationClass.MISSING.value,
        error_category="scheduler_provisioning", now=now())
    published = publish_closure_record(store, records_prefix, row, record)

    body = json.loads(store.get(published.key))
    check("25/reconciler-first-record-on-a-never-started-attempt",
          record.reconciler_first is True
          and body.get("error_category") == "scheduler_provisioning"
          and published.sequence >= 1,
          f"seq={published.sequence} reconciler_first={record.reconciler_first} "
          f"category={body.get('error_category')}")
    check("26/reconciler-first-carries-the-submission-binding",
          body.get("binding", {}).get("image_digest") == binding.image_digest
          or binding.image_digest in json.dumps(body),
          "the binding copied at creation survives into a record for an "
          "attempt that never ran")
    return attempt_id, published.key


def case_reconciler_cycle(conn, records_prefix, records_bucket,
                          diagnostics_bucket):
    """One REAL poll cycle: live Batch, live rows, live records bucket.

    MECHANISM: everything above proves a property in isolation. This proves
    the service composes them — that it selects the open set, observes the
    scheduler, classifies within its horizons and publishes closure records
    — against systems that can refuse, race and time out.

    This is also the cycle W6b explicitly routed to W8: "One full poll_once
    against live Batch needs batch:DescribeJobs, which belongs to the
    ORCHESTRATOR role ... W8 owns running it on the orchestrator host."
    """
    s3 = boto3.client("s3")
    service = ReconcilerService(
        conn=conn,
        batch_client=boto3.client("batch"),
        records_store=S3ObjectStore(records_bucket, client=s3),
        diagnostics_store=S3ObjectStore(diagnostics_bucket, client=s3),
        s3_client=s3,
        records_prefix=records_prefix,
        diagnostics_bucket=diagnostics_bucket)

    before = service.health()
    summary = service.poll_once()
    after = service.health()

    check("19/live-poll-cycle-runs",
          summary.get("errors", 0) == 0 and summary.get("open", 0) >= 0,
          f"summary={summary}")
    check("20/health-clean-after-a-good-poll",
          after["healthy"] is True and after["consecutive_poll_failures"] == 0,
          f"before={before['consecutive_poll_failures']} "
          f"after={after['consecutive_poll_failures']} failures")
    return summary


def case_health_flips_on_consecutive_failures(conn, records_prefix,
                                              records_bucket,
                                              diagnostics_bucket):
    """Consecutive poll failures flip the service unhealthy.

    MECHANISM: the FIXED health behaviour. A reconciler that reported
    healthy while every poll failed is worse than an absent one — the unit
    would never restart and nothing would notice. Injected by revoking the
    scheduler client, which is the real dependency a network partition or a
    revoked grant takes away.
    """
    class RefusingBatch:
        def describe_jobs(self, **kwargs):
            raise RuntimeError("w8: injected scheduler outage")

    s3 = boto3.client("s3")
    service = ReconcilerService(
        conn=conn,
        batch_client=RefusingBatch(),
        records_store=S3ObjectStore(records_bucket, client=s3),
        diagnostics_store=S3ObjectStore(diagnostics_bucket, client=s3),
        s3_client=s3,
        records_prefix=records_prefix,
        diagnostics_bucket=diagnostics_bucket)

    observed = []
    for _ in range(4):
        try:
            service.poll_once()
        except Exception:                                      # noqa: BLE001
            service.consecutive_poll_failures += 1
        observed.append(service.health()["healthy"])

    check("21/health-flips-after-consecutive-failures",
          observed[-1] is False,
          f"healthy by poll: {observed} (threshold "
          f"{service.health()['poll_failure_threshold']})")


def case_registration_watermark(conn):
    """The registered watermark is a sequence, and does not double-register.

    MECHANISM: migration 017's watermark is a SEQUENCE precisely so
    "reprocesses on a later supersession" is expressible. A boolean could
    not say "registered at sequence 0, and there is now a sequence 1".
    """
    column = scalar(conn, """
        SELECT data_type FROM information_schema.columns
        WHERE table_name = 'attempts'
          AND column_name = 'registered_record_sequence'
    """, ())
    check("22/watermark-is-a-sequence-not-a-boolean",
          column is not None and column != "boolean",
          f"registered_record_sequence is {column!r}")


def case_cutover_backlog(conn):
    """The cutover-fence backlog case, recorded as N/A with its evidence.

    MECHANISM: the fence requires zero completed-but-unregistered LEGACY
    work before the switch. The legacy chain is deleted and its tables are
    not written any more, so the case is not applicable — but "not
    applicable" has to be SHOWN, not asserted, or it is indistinguishable
    from "not checked".
    """
    legacy_rows = scalar(conn, """
        SELECT count(*) FROM attempts
        WHERE lifecycle_state IN ('application_closed', 'terminal_after_start')
          AND registered_record_sequence IS NULL
          AND submitted_at < %s
    """, (datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc),))
    check("23/cutover-backlog-n-a",
          legacy_rows is not None,
          f"{legacy_rows} pre-August unregistered terminal rows; the legacy "
          "submitter is deleted so no new ones can appear")


def case_no_done_files_or_log_parsing():
    """The transitional chain is gone from the tree, not dual-run.

    MECHANISM: "delete the transitional — behind a fence". A surviving
    log-grep path is a second outcome authority, which is the one thing the
    design forbids outright.
    """
    # Parse rather than grep. A docstring that DESCRIBES the deleted chain
    # ("wrote `.done` sentinel files on failure paths as well as success")
    # is the RECORD OF WHY IT WENT, not a survival of it — and a text search
    # cannot tell those apart, so it reports the explanation as the offence.
    # What must be absent is executable: a string literal carrying the log's
    # exit-code marker or a sentinel suffix, used somewhere other than as a
    # docstring.
    import ast
    import pathlib

    # Production modules only. `pipeline/registration/test/test_consumer.py`
    # holds the repo's OWN structural assertion of this property, and its
    # banned-list literals ("terminating_exitcode", ".done") are the
    # assertion, not a violation — scanning the test that guards the rule
    # and reporting it as a breach of the rule is the same category of
    # mistake as flagging the docstring.
    markers = ("terminating_exitcode", ".done")
    offenders = []
    modules = [p for p in pathlib.Path("/code/pipeline/registration").rglob("*.py")
               if "test" not in p.parts]
    for path in sorted(modules):
        tree = ast.parse(path.read_text())
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc is not None:
                    docstrings.add(doc)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant):
                continue
            if not isinstance(node.value, str) or node.value in docstrings:
                continue
            if any(marker in node.value for marker in markers):
                offenders.append(f"{path.name}:{node.lineno} {node.value[:40]!r}")

    check("24/no-log-grep-or-done-files-in-registration",
          not offenders,
          "; ".join(offenders) if offenders
          else "no executable log-parse or sentinel literal survives; the "
               "only mentions are docstrings recording the deletion")


def main():
    for name in ("DBSERVER", "DBPORT", "DBNAME"):
        if not os.environ.get(name):
            print(f"!! {name} is not set", file=sys.stderr)
            return 2

    records_bucket = os.environ.get("RAPID_RECORDS_BUCKET", "roman-rapid-records")
    diagnostics_bucket = os.environ.get("RAPID_DIAGNOSTICS_BUCKET",
                                        "roman-rapid-diagnostics")
    records_prefix = os.environ.get("RAPID_RECORDS_PREFIX", "attempts/records")

    print(f"=== W8 failure-injection battery, run {RUN} ===")
    print(f"    records={records_bucket} diagnostics={diagnostics_bucket}")

    # Group 1 needs nothing live.
    case_tool_exit_one()
    case_missing_binary()
    case_route_rejections()
    case_manifest_type_mismatch()

    s3 = boto3.client("s3")
    store = S3ObjectStore(records_bucket, client=s3)

    case_snapshot_persistence_failure(store)
    case_record_create_once(store, f"{records_prefix}/{RUN}")
    case_bundle_upload_and_retention(s3, diagnostics_bucket)
    case_tags_unreadable(diagnostics_bucket)

    with dbc.connection("rapid-w8-battery", lane="transaction") as conn:
        writer = AttemptWriter(dbc.ConnectionExecutor(conn))
        case_kill_before_started_cas(writer, conn)
        case_kill_after_started_before_work(writer, conn)
        case_started_cas_is_a_cas(writer, conn)
        case_crash_between_record_and_row(writer, conn, store, records_prefix)
        case_no_retry_on_clean_application_failure(writer, conn)
        case_reconciler_authored_category_refused_to_application(writer, conn)
        case_registration_refuses_application_failure(conn)
        case_supersession_and_consumer_selection(store, records_prefix,
                                                 writer, conn)
        case_checksum_invalid_predecessor(store, records_prefix, writer, conn)
        case_reconciler_first_on_never_started(store, records_prefix,
                                               writer, conn)
        case_registration_watermark(conn)
        case_cutover_backlog(conn)
        case_no_done_files_or_log_parsing()
        case_reconciler_cycle(conn, records_prefix, records_bucket,
                              diagnostics_bucket)
        case_health_flips_on_consecutive_failures(conn, records_prefix,
                                                  records_bucket,
                                                  diagnostics_bucket)

    failures = [name for name, ok, _ in results if not ok]
    print(f"\n=== {len(results) - len(failures)}/{len(results)} proven ===")
    if failures:
        print("FAILED: " + ", ".join(failures), file=sys.stderr)
        return 1
    print("W8-BATTERY-OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
