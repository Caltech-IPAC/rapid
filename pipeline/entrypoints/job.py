"""
File:    job.py

The single dispatching entrypoint. Every RAPID Batch job runs this.

    python -m pipeline.entrypoints.job --class prompt
    python -m pipeline.entrypoints.job --class bulk

Two commands, fixed in the two job definitions, differing only in the workload
class. Everything else about what a job does comes from its manifest, and
adding a job type is a manifest vocabulary change rather than an
infrastructure change.

**The class is the discriminator, and it is fixed by the definition.** The
prompt job definition's command names the prompt class; the bulk definition's
names bulk. That is the adopted commands-differ contract, and it is what the
image can trust about itself: a container knows which definition started it
because the command says so, and cannot be told otherwise at submit time. The
queue, by contrast, is a submit-time parameter Batch does not bind to the
definition — which is why `AWS_BATCH_JQ_NAME` is checked against the route
matrix rather than believed.

**Order is the design.** The sequence below is not arrangeable:

    1. read the per-invocation environment       (fail-loud, no defaults)
    2. load the manifest, verify its checksum
    3. validate the full route                   (job type x class x queue)
    4. resolve attempt ownership                 (claim or create, one resolver)
    5. persist the configuration snapshot        (create-once, content-addressed)
    6. mark started, binding the digest          (one CAS, no half-bound state)
    7. run the stage sequence
    8. terminate                                 (disposition, bundle, record, close)

Steps 1-3 happen before any row is touched: a submission whose route is invalid
must not produce an attempt at all. Step 5 precedes step 6 because a crash
between them is the terminal-without-start case, where no work occurred by
construction; reversing them would produce a row bound to a snapshot that does
not exist. Step 7 cannot begin before step 6 because the started CAS is what
makes the attempt reconstructible.

**Exit codes carry no application meaning.** A classified application failure
exits 0 — scheduler-SUCCEEDED with application-failure is the combination the
attempt schema was built for, and the record says which it was. Nonzero is
reserved for the unrecordable: the records path unreachable, or an exception
escaping the protocol itself. Nothing here tests `>= 64`, writes a `.done`
file, or greps a log.
"""

import argparse
import contextlib
import datetime
import os
import sys
import traceback

from pipeline.runtime import environment, logging_setup, science_config
from pipeline.runtime.boundaries import S3ObjectStore
from pipeline.runtime.errors import (
    ConfigError,
    RecordsError,
    RuntimeErrorBase,
    serialize_error,
)
from pipeline.runtime.ownership import lifecycle_reader_for, resolve_ownership
from pipeline.runtime.process import redact
from pipeline.runtime.stages import StageRecorder, run_stage
from pipeline.runtime.termination import (
    EXIT_RECORDED,
    EXIT_UNRECORDABLE,
    persist_configuration_snapshot,
    start_attempt,
    terminate,
)
from pipeline.runtime.workdir import WorkingDirectory
from pipeline.stages.context import StageContext
from pipeline.stages.sequences import sequence_for
from submission.manifest import Manifest
from submission.routes import (
    JOB_TYPE_REGISTRATION,
    WORKLOAD_CLASSES,
    RouteError,
)
from submission.startup import configuration_digest, fetch_parameters

# The parameter-tree keys the entrypoint itself needs, as distinct from the
# ones stages read. Named here so a missing one is a startup failure with a
# clear message rather than a KeyError inside the termination protocol.
PARAM_RECORDS_BUCKET = "s3/records-bucket"
PARAM_DIAGNOSTICS_BUCKET = "s3/diagnostics-bucket"
PARAM_RECORDS_PREFIX = "s3/records-prefix"

_logger = logging_setup.get_logger("entrypoint")


def parse_arguments(argv=None) -> argparse.Namespace:
    """Parse the one argument the command line carries.

    `--class` is required and has no default. A default would let a job
    definition whose command lost its discriminator run as whichever class the
    default named, which is precisely the misconfiguration the route matrix
    exists to catch.
    """
    parser = argparse.ArgumentParser(
        prog="rapid-job",
        description="The RAPID Batch payload entrypoint. The workload class "
                    "is fixed by the job definition's command; everything "
                    "else comes from the submission manifest.")
    parser.add_argument(
        "--class", dest="workload_class", required=True,
        choices=list(WORKLOAD_CLASSES),
        help="the workload class this job definition runs (fixed per "
             "definition, never overridden at submit time)")
    return parser.parse_args(argv)


def load_manifest(job_env, s3_client) -> "Manifest":
    """Fetch the manifest and verify it is the one the submitter recorded.

    A checksum mismatch means this job is reading a different manifest than
    the one that sized its array, so its array-index binding cannot be
    trusted — every child would resolve to the wrong unit. `ConfigError`,
    which the route-rejection path treats the same as a bad route.
    """
    import boto3  # noqa: F401 - kept local; the client is injected

    uri = job_env.manifest_uri
    if not uri.startswith("s3://"):
        raise ConfigError(
            f"RAPID_MANIFEST_URI={uri!r} is not an s3:// URI", uri=uri)

    bucket, _, key = uri[len("s3://"):].partition("/")
    if not bucket or not key:
        raise ConfigError(
            f"RAPID_MANIFEST_URI={uri!r} does not name a bucket and a key",
            uri=uri)

    try:
        body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as exc:  # noqa: BLE001 - translated
        raise ConfigError(
            f"could not read the submission manifest at {uri}: {exc}",
            uri=uri) from exc

    manifest = Manifest.from_json(body.decode("utf-8"))

    if manifest.checksum() != job_env.manifest_checksum:
        raise ConfigError(
            f"manifest checksum mismatch: the job definition's environment "
            f"records {job_env.manifest_checksum}, the object at {uri} "
            f"checksums to {manifest.checksum()}. This job would bind its "
            f"array index against a manifest other than the one that sized "
            f"its array.",
            uri=uri, expected=job_env.manifest_checksum,
            actual=manifest.checksum())

    return manifest


def validate_route(manifest, workload_class, job_env, parameters):
    """Reject a submission whose route is not in the matrix.

    Three facts checked as one tuple: the manifest's job type, the class this
    entrypoint was invoked with, and the queue Batch actually placed the job
    on. Any mismatch raises `RouteError`, which the caller records as
    `config_invalid` and exits without doing work.
    """
    return manifest.validate_for(workload_class,
                                 queue_name=job_env.queue_name,
                                 queue_names=parameters)


def build_provenance(config_digest: str, parameters: dict):
    """The runtime's own observation of what it is executing.

    Distinct from the submission-time execution binding, which the submitter
    authored at logical-job scope and the resolver copied onto the row. These
    four are what *this process* can see about itself; a disagreement between
    `container_digest` here and the binding's `image_digest` is a
    reconciliation signal rather than a duplicate.

    All four are required at `started` by the DDL. They come from the image
    (set as ENV at build time, so a container that does not know what it is
    cannot start) rather than from the mutable parameter tree.
    """
    from observability.attempts import Provenance

    missing = [name for name in ("RAPID_SOURCE_SHA", "RAPID_IMAGE_DIGEST",
                                 "RAPID_JOB_DEFINITION_REV")
               if not (os.environ.get(name) or "").strip()]
    if missing:
        raise ConfigError(
            "the image does not carry its own identity; missing: "
            + ", ".join(missing)
            + ". These are set as ENV in the Containerfile at build time and "
            "on the job definition; a started attempt without provenance is "
            "not a representable state.",
            missing=",".join(missing))

    return Provenance(
        source_sha=os.environ["RAPID_SOURCE_SHA"].strip(),
        container_digest=os.environ["RAPID_IMAGE_DIGEST"].strip(),
        job_definition_rev=os.environ["RAPID_JOB_DEFINITION_REV"].strip(),
        config_digest=config_digest)


def run_sequence(context, job_type: str, recorder) -> None:
    """Run one job type's stages in order.

    Each stage is one `run_stage` call, so each gets its span, its own bundle
    log file, and — on failure — a stage record carrying the error category
    before the exception propagates to the termination protocol.
    """
    sequence = sequence_for(job_type)
    context.logger.info(
        "stage sequence for %s (%d stages): %s", job_type, len(sequence),
        ", ".join(name for name, _fn in sequence))

    for name, function in sequence:
        run_stage(name, lambda fn=function: fn(context),
                  recorder=recorder, workdir=context.workdir,
                  logger=context.logger)


def dispatch_registration(context) -> None:
    """The registration job type.

    Registration is not a staged pipeline — it is one pass over completed work
    — so it is reached here rather than through a stage sequence. It is routed
    (the route matrix carries it: prompt class, transaction lane) so that a
    registration manifest is accepted and validated like any other, and it
    runs as a job with a full attempt record.

    It consumes *reconciled* outcomes only — attempts the reconciler has
    moved to a terminal state and published a closure record for. That gate is
    the reason a container killed after writing its own record cannot have its
    products registered: until the reconciler has seen the scheduler's verdict,
    the attempt is not a candidate.

    Registration opens its own database connection rather than reusing the
    entrypoint's. The entrypoint's connection belongs to the attempt-record
    writer and is scoped to this attempt's own lifecycle; registration is a
    pass over *other* attempts and is a distinct component at the pooler door,
    with its own `application_name` for attribution.

    What this replaced: four `__main__`-only scripts that downloaded each job's
    stdout log from S3 and regex-grepped `terminating_exitcode` out of it,
    wrote `.done` sentinels on failure paths as well as success ones, and
    hardcoded process exit 0. Deleted at the cutover fence.
    """
    from database.modules.utils.rapid_db_connect import connection
    from pipeline.registration import candidates, register_batch

    logger = context.logger
    with connection("rapid-registration", lane="transaction") as conn:
        rows = candidates(conn)
        logger.info("registration: %d reconciled attempt(s) to consider",
                    len(rows))
        run = register_batch(conn, rows)

    context.record(registration=run.as_dict())

    if run.failed:
        # This job's own failures are its own outcome — the counting pattern
        # that closes the hardcoded-exit-0 defect. Raised so it is classified
        # and recorded like any other application failure, rather than
        # returned as a status nobody checks. `records_error` is the honest
        # category: what failed was writing this pass's account of other
        # attempts' products.
        raise RecordsError(
            f"{run.failed} of {len(rows)} registration(s) failed; "
            f"registered {run.registered}, skipped {run.skipped}, "
            f"deferred {run.deferred}",
            failed=run.failed, considered=len(rows))


def main(argv=None) -> int:
    """The entrypoint. Returns the process exit code.

    Structured so that everything before the attempt row exists raises out to
    the pre-attempt handler — which has nowhere to record an outcome and so
    exits nonzero with the reason on the safety stream — and everything after
    it goes through the termination protocol.
    """
    arguments = parse_arguments(argv)
    logging_setup.configure()

    try:
        return _run(arguments.workload_class)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - the last resort
        # Nothing below this point can record anything: either the attempt row
        # does not exist yet, or the records path itself failed. Fail loud on
        # the safety stream (CloudWatch via awslogs) and exit nonzero, so Batch
        # reports FAILED rather than SUCCEEDED over a job with no account.
        serialized = serialize_error(exc, redactor=redact)
        _logger.error("unrecordable failure (%s): %s",
                      serialized.error_category, serialized.message)
        _logger.error("%s", redact(traceback.format_exc()))
        return EXIT_UNRECORDABLE


def _run(workload_class: str) -> int:
    """Startup, dispatch, termination."""
    import boto3

    # 1. The per-invocation environment. Fail-loud, no defaults.
    job_env = environment.read_environment()
    _logger.info("environment: %s", environment.describe(job_env))

    s3_client = boto3.client("s3")

    # 2-3. The manifest and the full route. Both before any row is touched: a
    # submission with an invalid route must not produce an attempt.
    manifest = load_manifest(job_env, s3_client)
    parameters = fetch_parameters()
    route = validate_route(manifest, workload_class, job_env, parameters)
    unit = manifest.unit_for_index(job_env.array_index or 0)

    _logger.info("route validated: job_type=%s class=%s queue=%s lane=%s "
                 "unit=%s", manifest.job_type, workload_class,
                 job_env.queue_name, route.db_lane, unit.key)

    config_digest = configuration_digest(parameters)
    provenance = build_provenance(config_digest, parameters)

    records_bucket = _required(parameters, PARAM_RECORDS_BUCKET)
    diagnostics_bucket = _required(parameters, PARAM_DIAGNOSTICS_BUCKET)
    records_prefix = _required(parameters, PARAM_RECORDS_PREFIX)

    # Before the first connection, and after the tree is read: the endpoint is
    # a tree fact, and the connection helper reads it from the environment.
    export_database_environment(parameters)

    workdir = WorkingDirectory.create(job_env.attempt_key)
    # Rebind the logging identifiers now that they are known: every line from
    # here on carries the scheduler job id and attempt index, which is what
    # makes one array child's lines separable in the shared safety stream.
    adapter = logging_setup.configure(job_id=job_env.scheduler_job_id,
                                      attempt_id=job_env.attempt_index)
    logger = logging_setup.get_logger("job", adapter=adapter)

    with _database(route, job_env) as (writer, execute):
        # 4. Attempt ownership, through W1's one resolver.
        #
        # The logical-job key is RUN-SCOPED and comes from the one function
        # both sides share (review finding #3): the submitter writes the row
        # under this key and the resolver claims it by matching on it, so a
        # second copy of the format string here is how the two would stop
        # agreeing.
        ownership = resolve_ownership(
            writer, job_env,
            run_id=manifest.batch_id,
            logical_job_id=unit.logical_job_key(manifest.batch_id),
            identity_extra={"exposure_id": unit.exposure, "sca": unit.sca,
                            "sky_tile": getattr(unit.facts, "rtid", None)},
            lifecycle_reader=lifecycle_reader_for(execute))

        records_store = S3ObjectStore(records_bucket, client=s3_client)
        diagnostics_store = S3ObjectStore(diagnostics_bucket, client=s3_client)

        # 5. The configuration snapshot, before any work. Create-once and
        #    content-addressed, so a thousand array children persist one.
        digest, snapshot_key_value, _created = persist_configuration_snapshot(
            records_store, records_prefix, parameters)

        # 6. Started, binding the digest in the same compare-and-set.
        startup = start_attempt(
            writer, ownership.attempt_id, provenance, digest,
            snapshot_key_value,
            scheduler_job_id=job_env.scheduler_job_id,
            application_attempt_index=ownership.attempt_index)

        recorder = StageRecorder()
        context = StageContext(
            workdir=workdir, unit=unit, job_type=manifest.job_type,
            science=science_config.load(), parameters=parameters,
            logger=logger, s3=s3_client, started_at=startup.started_at)

        outcome, disposition, error = _execute(context, manifest.job_type,
                                               recorder, logger)

        # 8. The termination protocol, in its stated order.
        result = terminate(
            writer=writer, store=diagnostics_store,
            record_store=records_store, ownership=ownership,
            job_env=job_env, workdir=workdir, records_prefix=records_prefix,
            outcome=outcome, product_disposition=disposition,
            started_at=startup.started_at, config_digest=digest,
            snapshot_key_value=snapshot_key_value,
            stages=recorder.as_list(), provenance=provenance, error=error)

    logger.info("terminated: outcome=%s disposition=%s record=%s exit=%d",
                result.outcome, result.product_disposition,
                result.record_key, result.intended_exit)
    return result.intended_exit


def _execute(context, job_type, recorder, logger):
    """Run the payload; return `(outcome, disposition, error)`.

    A caught application failure is recorded and the job exits 0 — the
    representable combination. Only failures in the records path itself
    propagate, and those are raised by the termination protocol rather than
    here.
    """
    from observability.attempts import ProductDisposition, RapidOutcome

    try:
        if job_type == JOB_TYPE_REGISTRATION:
            dispatch_registration(context)
        else:
            run_sequence(context, job_type, recorder)
    except RuntimeErrorBase as exc:
        # A classified application failure: the taxonomy already knows what
        # kind it is, and the record will carry the category.
        logger.error("application failure (%s): %s", exc.error_category, exc)
        return (RapidOutcome.FAILURE.value, ProductDisposition.NONE.value,
                serialize_error(exc, redactor=redact))
    except Exception as exc:  # noqa: BLE001 - classified as internal_error
        logger.exception("unexpected exception in the payload")
        return (RapidOutcome.FAILURE.value, ProductDisposition.NONE.value,
                serialize_error(exc, redactor=redact))

    # `failed` is a property returning the list of failed stage records, not a
    # method — calling it raised TypeError on every successful non-registration
    # job. Unreached so far only because the sole canaried job type is
    # `registration`, which raises before this line.
    failed_stages = recorder.failed
    if failed_stages:
        # No stage raised, but one recorded a failure — a partial outcome, and
        # a real third state rather than a rounded-off success.
        return (RapidOutcome.PARTIAL.value,
                ProductDisposition.PUBLISHED.value, None)

    return (RapidOutcome.SUCCESS.value, ProductDisposition.PUBLISHED.value,
            None)


# The connection helper (W1) reads the database endpoint from these
# environment variables, deliberately: it is used by hosts and services that
# are not Batch jobs and have no parameter tree. The payload's endpoint is
# operational configuration and lives in the tree — the co-design moved it
# there precisely so it would stop depending "on someone remembering
# job-definition env entries (the rev-5/rev-6 failure class)". This mapping is
# the bridge, and it is one place rather than a job-definition Environment
# block per definition.
DB_PARAMETER_ENV = {
    "db/server": "DBSERVER",
    "db/port": "DBPORT",
    "db/name": "DBNAME",
    "db/secret-id": "RAPID_DB_SECRET_ID",
}


def export_database_environment(parameters: dict) -> dict:
    """Map the tree's `db/*` entries onto the connection helper's contract.

    Returns what was set, for the log. Raises `ConfigError` naming every
    missing key at once rather than one per run — the same reasoning as the
    per-invocation environment contract, and for the same reason: each
    discovery otherwise costs a container start.

    Values are exported into `os.environ` because the helper reads them
    there. That is a process-global write, which is worth doing once at
    startup in the entrypoint and nowhere else — a stage that wanted a
    different database would be a stage with a bug.
    """
    missing = [key for key in DB_PARAMETER_ENV if not parameters.get(key)]
    if missing:
        raise ConfigError(
            "the pipeline parameter tree does not carry the database "
            "endpoint; missing: " + ", ".join(sorted(missing))
            + ". The payload's endpoint is operational configuration and "
            "lives in the tree, not in job-definition environment entries.",
            missing=",".join(sorted(missing)))

    exported = {}
    for key, variable in DB_PARAMETER_ENV.items():
        os.environ[variable] = parameters[key]
        exported[variable] = parameters[key]
    # The secret ID is an identifier, not a credential, so it is safe to log;
    # the credential itself is fetched from Secrets Manager under the job role
    # and never passes through here.
    _logger.info("database endpoint from the parameter tree: %s:%s/%s "
                 "(secret %s)", exported["DBSERVER"], exported["DBPORT"],
                 exported["DBNAME"], exported["RAPID_DB_SECRET_ID"])
    return exported


@contextlib.contextmanager
def _database(route, job_env):
    """One connection for the attempt's lifetime, on the route's lane.

    The lane is the job type's contract (route matrix), not a caller's choice.
    `application_name` carries the scheduler job id so a connection held too
    long is attributable to a job in `pg_stat_activity` without a join.
    """
    from database.modules.utils.rapid_db_connect import (
        ConnectionExecutor,
        connection,
    )
    from observability.attempts import AttemptWriter

    application_name = f"rapid-payload:{job_env.scheduler_job_id}"
    with connection(application_name, lane=route.db_lane) as conn:
        execute = ConnectionExecutor(conn)
        yield AttemptWriter(execute), execute


def _required(parameters: dict, name: str) -> str:
    """One parameter the entrypoint itself cannot run without."""
    value = parameters.get(name)
    if not value:
        raise ConfigError(
            f"the pipeline parameter tree does not carry {name!r}; the "
            f"termination protocol writes through it and has no default",
            parameter=name)
    return value


if __name__ == "__main__":
    sys.exit(main())
