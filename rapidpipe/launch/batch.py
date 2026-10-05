"""Submit run units to AWS Batch, resolve their inputs, and reconcile results.

Implements the stage contract's "Invocation" (every submission is a fresh
attempt; a Batch job runs its container once) and "Exit codes" (75 and
the approved infrastructure failures are retried by the launcher, 70
stops; "The launcher also records terminations without a stage exit code
and unexpected codes"), and the runs page's "Attempts" (``lost`` means
the scheduler lost the job) over ``rapidpipe.runs.repository``.

Batch does not retry: the rebuild's job definitions carry
``RetryStrategy: Attempts: 1`` (rapid_systems
``cloudformation/rapid-batch.yaml``), so one Batch job is one RAPID
attempt with one output location. A retryable outcome -- exit 75, or one
of :data:`RETRYABLE_INFRASTRUCTURE_REASONS` -- is recorded ``transient``,
which returns the unit to ``ready`` while the run's
``max_attempts_per_unit`` allows, and the run walk submits it again as a
fresh attempt.

This module composes ``rapidpipe.runs.repository``, ``rapidpipe.products``
and ``rapidpipe.db``; it never imports a stage module or the command-line tool
(the package's fixed dependency direction, ``rapid_docs``'
stage-contract.md, "The package"). ``boto3`` is never imported at module
level: :func:`batch_client` is the one indirection point a test
monkeypatches, mirroring ``rapidpipe.products.storage.s3_client``.

Deployment configuration -- the Batch job queue and definition, and the
S3 root new attempts' outputs are written under -- comes from the
environment only, never a committed value (README, "Running on Batch";
specification.md, "Repositories": "Account identifiers, bucket names and
hostnames are injected at deploy time, never committed to `rapid`."):

- ``RAPIDPIPE_BATCH_JOB_QUEUE`` -- the Batch job queue name or ARN.
- ``RAPIDPIPE_BATCH_RECLAIM_QUEUE`` -- optional: the queue for every
  further attempt of a unit once one of its attempts, in the run or a run
  it was seeded from, was lost to a Spot reclaim (:func:`is_host_reclaim`),
  so a reclaimed unit moves to on-demand capacity and never retries on
  Spot. Unset, or equal to
  ``RAPIDPIPE_BATCH_JOB_QUEUE``, every attempt goes to the job queue and
  no reclaim lookup is made (:func:`queue_for_unit`).
- ``RAPIDPIPE_BATCH_JOB_DEFINITION_SCRATCH`` /
  ``RAPIDPIPE_BATCH_JOB_DEFINITION_PRODUCTION`` -- the Batch job
  definition name or ARN for a run of that kind (:func:`job_definition_for`).
- ``RAPIDPIPE_OUTPUTS_ROOT_SCRATCH`` / ``RAPIDPIPE_OUTPUTS_ROOT_PRODUCTION``
  -- an ``s3://bucket/prefix`` under which ``runs/<run>/<stage>/<unit>/
  <attempt>`` lives for a run of that kind (the scratch bucket and the
  project bucket) (:func:`outputs_root_for`).
- ``RAPIDPIPE_BATCH_JOB_DEFINITION`` / ``RAPIDPIPE_OUTPUTS_ROOT`` -- the
  scratch fallback only. A production run fails closed: it never falls
  back to an unsuffixed variable, so a missing production setting cannot
  send project outputs to a scratch location.
- ``RAPIDPIPE_BATCH_JOB_NAME_PREFIX`` -- optional, default ``rapid``.

A run created from a release (``runs.release``) never uses those
job-definition variables: :func:`submit_unit` submits to the release's own
``release_deployments.job_definition`` revision for the run's kind, after
``describe_job_definitions`` confirms it is ACTIVE, and refuses
(:class:`ReleaseDefinitionRefused`) rather than fall back to an
unversioned name (releases.md §The launcher reads the release).

The run's kind is fixed at creation (runs page, "Runs"), so
:func:`submit_unit` reads it from the ``runs`` row and picks the root and
definition from it; an explicit ``outputs_root``/``job_definition``
argument still wins.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from rapidpipe.exitcodes import ExitCode
from rapidpipe.products.manifest import Manifest, ManifestError
from rapidpipe.products.storage import fetch_object, is_not_found, join, parse_location
from rapidpipe.runs import inputs as run_inputs
from rapidpipe.runs.local import disposition_for
from rapidpipe.runs.repository import (
    RunNotFound,
    add_unit,
    allocate_attempt,
    attempt_output_location,
    record_attempt_locations,
    record_attempt_result,
    record_reconcile_note,
    record_scheduler_job,
    select_attempt,
)

#: Batch job status values ``describe_jobs`` never finishes on; reported
#: back with ``disposition=None`` and otherwise untouched.
_UNRESOLVED_BATCH_STATUSES = ("SUBMITTED", "PENDING", "RUNNABLE", "STARTING", "RUNNING")

#: describe_jobs accepts at most 100 job ids per call (a Batch API limit).
_DESCRIBE_JOBS_BATCH_SIZE = 100

_TRANSIENT_FAILURE_CODE = ExitCode.TRANSIENT_FAILURE

#: The approved infrastructure failures (stage contract, "Exit codes";
#: runs page, "Attempts"): a FAILED job with no container exit code whose
#: reason starts with one of these is recorded ``transient`` rather than
#: ``killed``, so the launcher retries it. ``statusReason`` is the job's or
#: its last attempt's (an EC2 host reclaimed under the job); ``reason`` is
#: the last attempt's container reason (an image pull or Docker daemon
#: failure before the stage started). The same three patterns the rebuild
#: job definitions' ``EvaluateOnExit`` rows carried while Batch retried.
RETRYABLE_INFRASTRUCTURE_REASONS: dict[str, tuple[str, ...]] = {
    "statusReason": ("Host EC2",),
    "reason": ("Cannot", "DockerTimeoutError"),
}

logger = logging.getLogger(__name__)


class LaunchError(Exception):
    """Base class for every exception this module raises."""


class MissingEnvironmentVariable(LaunchError):
    """A required ``RAPIDPIPE_*`` environment variable is not set."""


class ReleaseDefinitionRefused(LaunchError):
    """A released run's job definition cannot be used: the release has no
    single deployment for the run's kind, the revision is not ACTIVE, or
    an explicit ``job_definition`` contradicts it. Permanent, not
    retryable."""


class ProfileNotAllowed(LaunchError):
    """``profile=True`` was refused for a production run.

    Profiling is scratch-only (``--profile`` on ``run submit`` or ``run start``): a
    profile file is written into the attempt's own outputs prefix, which
    for a production run is the products bucket, not a scratch location.
    Permanent, not retryable; the CLI maps it to exit 64.
    """


class DependencyIncomplete(LaunchError):
    """The requested upstream unit has no selected attempt yet.

    Raised by :func:`resolve_inputs_from_stage` -- the launcher's
    dependency enforcement in its first, honest form: one unit, one
    upstream stage (design brief; runs page, "Units": "A unit is pending
    until its declared input set is complete and its upstream attempts
    are selected.").
    """


class ReconcileFetchFailed(LaunchError):
    """:func:`reconcile` could not fetch an attempt's manifest or
    execution record from S3, for a reason other than "the object is not
    there" (e.g. AccessDenied on the launcher's own role, a network
    failure, or any other unexpected error).

    Distinct from "the job wrote nothing" -- the job may well have
    succeeded. Raised internally by :func:`_fetch_manifest_if_valid` and
    :func:`_fetch_execution_record`; :func:`reconcile` catches it and
    leaves the attempt's disposition unresolved rather than recording a
    terminal ``failed`` for a launcher-side problem the job never had a
    chance to cause (runs page, "Attempts").
    """

    def __init__(self, exc: BaseException, *, key: str):
        self.key = key
        self.__cause__ = exc
        super().__init__(f"{type(exc).__name__}: {exc} (key={key!r})")


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise MissingEnvironmentVariable(
            f"{name} is not set; rapidpipe.launch.batch requires it "
            "(README, \"Running on Batch\")")
    return value


_RUN_KINDS = ("scratch", "production")


def _env_for_kind(base: str, kind: str) -> str:
    """``<base>_<KIND>``; for scratch only, else ``<base>``. Otherwise
    :class:`MissingEnvironmentVariable` naming what was looked for."""
    if kind not in _RUN_KINDS:
        raise ValueError(f"run kind must be one of {_RUN_KINDS}, got {kind!r}")
    specific = f"{base}_{kind.upper()}"
    if kind == "production":
        return _require_env(specific)
    value = os.environ.get(specific) or os.environ.get(base)
    if not value:
        raise MissingEnvironmentVariable(
            f"neither {specific} nor {base} is set; rapidpipe.launch.batch "
            "requires one (README, \"Running on Batch\")")
    return value


def outputs_root_for(kind: str) -> str:
    """The outputs root for a run of ``kind`` ('scratch' or 'production').

    ``RAPIDPIPE_OUTPUTS_ROOT_SCRATCH`` (falling back to
    ``RAPIDPIPE_OUTPUTS_ROOT``) or ``RAPIDPIPE_OUTPUTS_ROOT_PRODUCTION``
    (no fallback: production fails closed); raises
    :class:`MissingEnvironmentVariable` when unset.
    """
    return _env_for_kind("RAPIDPIPE_OUTPUTS_ROOT", kind)


def job_definition_for(kind: str) -> str:
    """The Batch job definition for a run of ``kind`` ('scratch' or 'production').

    ``RAPIDPIPE_BATCH_JOB_DEFINITION_SCRATCH`` (falling back to
    ``RAPIDPIPE_BATCH_JOB_DEFINITION``) or
    ``RAPIDPIPE_BATCH_JOB_DEFINITION_PRODUCTION`` (no fallback: production
    fails closed); raises :class:`MissingEnvironmentVariable` when unset.
    """
    return _env_for_kind("RAPIDPIPE_BATCH_JOB_DEFINITION", kind)


def _run_kind(conn, run_id: str) -> str:
    """The ``runs.kind`` of ``run_id``; :class:`RunNotFound` if there is none.

    A module-level function so the database-free unit tests can
    monkeypatch it alongside the repository calls.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT kind FROM runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
    if row is None:
        raise RunNotFound(f"run {run_id!r} does not exist")
    return row[0]


def run_kind(conn, run_id: str) -> str:
    """The ``runs.kind`` of ``run_id``; :class:`RunNotFound` if there is none."""
    return _run_kind(conn, run_id)


def _release_job_definition(conn, run_id: str) -> tuple[str, str] | None:
    """``(release tag, "name:revision")`` for a run created from a release,
    or ``None`` for a run with no release.

    The consumer is chosen by kind: the one ``release_deployments`` row
    whose consumer name ends in ``-production`` is a production run's, the
    one that does not is a scratch run's; anything but exactly one match
    is refused. A module-level function so the database-free unit tests
    can monkeypatch it.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT kind, release FROM runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
        if row is None:
            raise RunNotFound(f"run {run_id!r} does not exist")
        kind, release = row
        if release is None:
            return None
        cur.execute(
            "SELECT consumer, job_definition FROM release_deployments WHERE release = %s",
            (release,))
        deployments = cur.fetchall()
    matches = [job_definition for consumer, job_definition in deployments
               if consumer.endswith("-production") == (kind == "production")]
    if len(matches) != 1:
        raise ReleaseDefinitionRefused(
            f"run {run_id!r} is from release {release!r}, which records "
            f"{len(matches)} job definition(s) for a {kind} run "
            f"({[c for c, _ in deployments]}); refusing to submit")
    return release, matches[0]


def _require_active(batch: Any, release: str, job_definition: str) -> None:
    response = batch.describe_job_definitions(jobDefinitions=[job_definition])
    statuses = [d.get("status") for d in response.get("jobDefinitions", [])]
    if "ACTIVE" not in statuses:
        raise ReleaseDefinitionRefused(
            f"release {release!r} job definition {job_definition!r} is "
            f"{statuses[0] if statuses else 'not found'}, not ACTIVE; refusing to "
            "submit (never falling back to the unversioned name)")


def batch_client() -> Any:
    """Return a fresh ``boto3`` Batch client.

    A module-level indirection point, mirroring
    ``rapidpipe.products.storage.s3_client``: tests monkeypatch
    ``rapidpipe.launch.batch.batch_client`` to return a fake, so importing
    this module -- and every function that accepts an explicit ``client``
    -- never requires boto3 to be installed.
    """
    import boto3

    return boto3.client("batch")


@dataclass(frozen=True)
class BatchSubmission:
    """What :func:`submit_unit` returns: the attempt and job it created."""

    attempt_id: str
    job_id: str
    job_name: str
    output_location: str


@dataclass(frozen=True)
class Reconciled:
    """One attempt's outcome after :func:`reconcile` looked at its job."""

    attempt_id: str
    job_id: str
    batch_status: str
    disposition: str | None
    selected: bool


def _job_name(prefix: str, stage: str, attempt_id: str) -> str:
    # Batch job names must match [A-Za-z0-9_-]{1,128}. unit_id may contain
    # "/" (e.g. "e20260821001234/SCA07"), so it is deliberately not part
    # of the name; the attempt id (a plain ULID) is unique enough on its
    # own and is what reconcile/cancel key off of regardless.
    return f"{prefix}-{stage}-{attempt_id}"


def submit_unit(
    conn,
    *,
    run_id: str,
    stage: str,
    unit_kind: str,
    unit_id: str,
    inputs_location: str,
    settings_location: str | None = None,
    outputs_root: str | None = None,
    job_definition: str | None = None,
    client: Any = None,
    s3_client: Any = None,
    profile: bool = False,
) -> BatchSubmission:
    """Allocate an attempt and submit it to Batch as one job.

    Steps: :func:`~rapidpipe.runs.repository.add_unit`, then
    :func:`~rapidpipe.runs.repository.allocate_attempt` (its exceptions --
    the run fence and the attempt allowance -- propagate uncommitted);
    the output location is ``<outputs_root>/runs/<run>/<stage>/<unit>/
    <attempt>``, where ``outputs_root`` and ``job_definition`` default,
    when ``None``, to :func:`outputs_root_for` and
    :func:`job_definition_for` of the run's kind (read from its ``runs``
    row before anything is written) -- except for a run created from a
    release, whose job definition is always the release's recorded
    ``name:revision`` for its kind (:func:`_release_job_definition`),
    checked ACTIVE with ``describe_job_definitions`` first; an explicit
    ``job_definition`` that differs from it is refused. The Batch job's
    ``containerOverrides.command`` starts at
    ``stage`` (the image's entrypoint is ``rapidpipe``, per the stage
    contract's "Invocation" form). The job id Batch returns is then
    recorded on the attempt row with
    :func:`~rapidpipe.runs.repository.record_scheduler_job`. The attempt's
    ``inputs_location`` and ``settings_location`` are recorded
    (:func:`~rapidpipe.runs.repository.record_attempt_locations`) in the
    allocation's own transaction (runs.md §Rules). Commits after each
    repository call, as
    :func:`rapidpipe.runs.local.run_stage_locally` does.

    Before anything is written, the input-set manifest at
    ``inputs_location`` is read (through ``s3_client``, default
    ``rapidpipe.products.storage.s3_client``); an absent or unreadable one
    raises :class:`rapidpipe.runs.inputs.InputsRefused` and nothing is
    submitted. After ``add_unit`` and before ``allocate_attempt``, every
    instance it names that is a registered product instance is bound in
    ``unit_inputs`` (:func:`rapidpipe.runs.inputs.bind_registered_inputs`,
    idempotent, so a retry rebinds nothing new) and committed with the
    unit (runs.md §Rules).

    ``profile=True`` sets ``RAPIDPIPE_PROFILE=1`` in the job's
    ``containerOverrides.environment``, refused with
    :class:`ProfileNotAllowed` for a production run before anything is
    written or submitted: a profile lands in the attempt's own outputs
    prefix, which for production is the products bucket, not a scratch
    location.
    """
    if profile and _run_kind(conn, run_id) == "production":
        raise ProfileNotAllowed(
            f"--profile is refused for a production run ({run_id}); "
            "profiling is for scratch runs, since profiles land in the "
            "attempt's own outputs prefix, which for production is the "
            "products bucket")
    released = _release_job_definition(conn, run_id)
    if released is not None:
        release, release_definition = released
        if job_definition is not None and job_definition != release_definition:
            raise ReleaseDefinitionRefused(
                f"run {run_id!r} is from release {release!r}, whose job definition "
                f"is {release_definition!r}, not {job_definition!r}; refusing")
        job_definition = release_definition
    if outputs_root is None or job_definition is None:
        kind = _run_kind(conn, run_id)
        outputs_root = outputs_root or outputs_root_for(kind)
        job_queue = queue_for_unit(conn, run_id, stage, unit_id)
        job_definition = job_definition or job_definition_for(kind)
    else:
        job_queue = queue_for_unit(conn, run_id, stage, unit_id)
    job_name_prefix = os.environ.get("RAPIDPIPE_BATCH_JOB_NAME_PREFIX", "rapid")
    batch = client if client is not None else batch_client()
    if released is not None:
        _require_active(batch, released[0], job_definition)

    # runs.md §Rules: read first, so a refusal leaves no unit and no
    # attempt behind.
    input_names = run_inputs.read_input_instances(inputs_location, s3_client=s3_client)

    add_unit(conn, run_id, stage, unit_kind, unit_id)
    run_inputs.bind_registered_inputs(conn, run_id, stage, unit_id, input_names)
    conn.commit()

    attempt_id = allocate_attempt(conn, run_id, stage, unit_id, outputs_root=outputs_root)
    # Frozen with the allocation, one commit: an attempt whose submission
    # then fails still records what it would have run with, which is what
    # a seeded re-run reads back (runs.md §Rules).
    record_attempt_locations(conn, attempt_id, inputs_location, settings_location)
    conn.commit()

    output_location = attempt_output_location(outputs_root, run_id, stage, unit_id, attempt_id)

    command = [
        "stage", stage,
        "--run", run_id,
        "--unit", unit_id,
        "--attempt", attempt_id,
        "--inputs", inputs_location,
        "--outputs", output_location,
    ]
    if settings_location is not None:
        command += ["--settings", settings_location]

    job_name = _job_name(job_name_prefix, stage, attempt_id)

    environment = [
        {"name": "RAPIDPIPE_RUN_ID", "value": run_id},
        {"name": "RAPIDPIPE_ATTEMPT_ID", "value": attempt_id},
    ]
    if profile:
        environment.append({"name": "RAPIDPIPE_PROFILE", "value": "1"})

    response = batch.submit_job(
        jobName=job_name,
        jobQueue=job_queue,
        jobDefinition=job_definition,
        containerOverrides={
            "command": command,
            "environment": environment,
        },
    )
    job_id = response["jobId"]

    record_scheduler_job(conn, attempt_id, job_id, output_location=output_location)
    conn.commit()

    return BatchSubmission(
        attempt_id=attempt_id,
        job_id=job_id,
        job_name=job_name,
        output_location=output_location,
    )


def resolve_inputs_from_stage(
    conn, *, run_id: str, unit_id: str, upstream_stage: str,
) -> str:
    """Return the selected attempt's output location for ``unit_id`` in
    ``upstream_stage``.

    The launcher's dependency enforcement in its first, honest form: one
    unit, one upstream stage. Refuses with :class:`DependencyIncomplete`
    when the upstream unit does not exist, has no selected attempt, or is
    not otherwise complete (runs page, "Units": "A unit is pending until
    its declared input set is complete and its upstream attempts are
    selected.").
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.state, u.selected_attempt, a.output_location
            FROM units u
            LEFT JOIN attempts a ON a.id = u.selected_attempt
            WHERE u.run = %s AND u.stage = %s AND u.unit_id = %s
            """,
            (run_id, upstream_stage, unit_id),
        )
        row = cur.fetchone()

    if row is None:
        raise DependencyIncomplete(
            f"unit (run={run_id!r}, stage={upstream_stage!r}, "
            f"unit_id={unit_id!r}) does not exist")

    state, selected_attempt, output_location = row
    if state != "complete" or selected_attempt is None:
        raise DependencyIncomplete(
            f"unit (run={run_id!r}, stage={upstream_stage!r}, "
            f"unit_id={unit_id!r}) is {state!r}, not complete with a "
            "selected attempt; refusing")

    return output_location


def _chunks(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _fetch_manifest_if_valid(output_location: str, *, s3_client: Any) -> Manifest | None:
    """Return the attempt's manifest, or ``None`` if it genuinely wrote
    none.

    Raises :class:`ReconcileFetchFailed` if an S3 fetch failed for any
    reason other than the object being absent (``NOT_FOUND_ERROR_CODES``) --
    e.g. AccessDenied -- since that means reconcile could not determine
    whether a manifest exists, not that it doesn't.
    """
    location = parse_location(output_location)

    if not location.is_s3():
        assert location.path is not None
        manifest_path = location.path / "manifest.json"
        if not manifest_path.exists():
            return None
        try:
            return Manifest.read(manifest_path)
        except (ManifestError, OSError, ValueError):
            return None

    with tempfile.TemporaryDirectory() as tmp:
        dest_path = Path(tmp) / "manifest.json"
        key = join(location, "manifest.json")
        try:
            fetch_object(location, "manifest.json", dest_path, client=s3_client)
        except Exception as exc:  # noqa: BLE001 - classified below
            if is_not_found(exc):
                return None
            raise ReconcileFetchFailed(exc, key=key) from exc
        try:
            return Manifest.read(dest_path)
        except (ManifestError, OSError, ValueError):
            return None


def _fetch_execution_record(
    output_location: str, attempt_id: str, *, s3_client: Any,
) -> dict[str, Any]:
    """Return the attempt's execution record, or ``{}`` if it genuinely
    wrote none.

    Raises :class:`ReconcileFetchFailed` on the same non-"not found"
    fetch failures :func:`_fetch_manifest_if_valid` does, and for the
    same reason.
    """
    location = parse_location(output_location)
    relative = f"exec/{attempt_id}.json"

    if not location.is_s3():
        assert location.path is not None
        record_path = location.path / relative
        if not record_path.exists():
            return {}
        try:
            return json.loads(record_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    with tempfile.TemporaryDirectory() as tmp:
        dest_path = Path(tmp) / "record.json"
        key = join(location, relative)
        try:
            fetch_object(location, relative, dest_path, client=s3_client)
        except Exception as exc:  # noqa: BLE001 - classified below
            if is_not_found(exc):
                return {}
            raise ReconcileFetchFailed(exc, key=key) from exc
        try:
            return json.loads(dest_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}


def _run_schema_version(conn, run_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT schema_version FROM runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
        return row[0] if row else None


def _execution_record_with_defaults(
    conn, run_id: str, execution_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fill the ``execution_records`` NOT NULL columns a Batch-written
    ``exec/<attempt>.json`` (or its absence, or a present-but-``null``
    field within it) leaves unset.

    Mirrors ``rapidpipe.runs.local.run_stage_locally``'s own fallbacks for
    the same three columns (``schema_version``, ``source_revision``,
    ``settings_hash``): a job with no execution record at all (a job that
    never reached the point of writing one, or a ``lost``/no-manifest
    outcome this function synthesizes an empty record for) still needs a
    row that satisfies the schema. A record a stage DID write can still
    hold explicit ``null`` for these fields (``_source_revision`` and the
    ``RAPIDPIPE_IMAGE_DIGEST``/``RAPID_IMAGE_DIGEST`` lookup both return
    ``None`` when they can't determine a value), so ``setdefault`` alone
    is not enough -- a present ``None`` must be replaced too.
    """
    record = dict(execution_record) if execution_record else {}
    if record.get("schema_version") is None:
        record["schema_version"] = _run_schema_version(conn, run_id)
    if record.get("source_revision") is None:
        record["source_revision"] = "unknown"
    if record.get("settings_hash") is None:
        record["settings_hash"] = "unknown"
    # The release the job carried, as the stage recorded it, passes
    # through unchanged to execution_records.release; the repository
    # records an absent, empty or "unreleased" value as NULL.
    if "release" in record:
        record["release"] = record["release"] or None
    return record


def _last_container_exit_code(job: dict[str, Any]) -> int | None:
    attempts = job.get("attempts") or []
    if not attempts:
        return None
    last = attempts[-1]
    container = last.get("container") or {}
    return container.get("exitCode")


def _status_reasons(job: dict[str, Any]) -> list[Any]:
    """The job's own ``statusReason`` and its last attempt's."""
    attempts = job.get("attempts") or []
    last = attempts[-1] if attempts else {}
    return [job.get("statusReason"), last.get("statusReason")]


def is_host_reclaim(job: dict[str, Any]) -> bool:
    """Whether ``job`` lost its EC2 host (a Spot reclaim): a ``statusReason``,
    the job's or its last attempt's, starts with one of
    :data:`RETRYABLE_INFRASTRUCTURE_REASONS`' ``statusReason`` prefixes."""
    prefixes = RETRYABLE_INFRASTRUCTURE_REASONS["statusReason"]
    return any(isinstance(r, str) and r.startswith(prefixes) for r in _status_reasons(job))


def is_retryable_infrastructure_failure(job: dict[str, Any]) -> bool:
    """Whether a FAILED ``job`` with no container exit code failed for one of
    :data:`RETRYABLE_INFRASTRUCTURE_REASONS`."""
    if is_host_reclaim(job):
        return True
    attempts = job.get("attempts") or []
    last = attempts[-1] if attempts else {}
    container_reasons = [(last.get("container") or {}).get("reason"),
                         (job.get("container") or {}).get("reason")]
    prefixes = RETRYABLE_INFRASTRUCTURE_REASONS["reason"]
    return any(isinstance(r, str) and r.startswith(prefixes) for r in container_reasons)


def _unit_was_reclaimed(conn, run_id: str, stage: str, unit_id: str) -> bool:
    """Whether any attempt of ``unit_id`` in ``run_id``'s ``stage``, or of
    the unit it was seeded from (``units.seeded_from_unit``, followed back
    through every seed, so a ``--retry-failed`` re-run keeps it), was
    recorded ``transient`` for a Spot reclaim (:func:`reconcile` marks it
    ``scheduler_metadata.batch.reclaim``).

    A module-level function so the database-free unit tests can
    monkeypatch it alongside the repository calls.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH RECURSIVE lineage (id, seed) AS (
                SELECT id, seeded_from_unit FROM units
                WHERE run = %s AND stage = %s AND unit_id = %s
                UNION
                SELECT u.id, u.seeded_from_unit
                FROM units u JOIN lineage l ON u.id = l.seed
            )
            SELECT EXISTS (
                SELECT 1
                FROM lineage l
                JOIN attempts a ON a.unit = l.id
                JOIN execution_records e ON e.attempt = a.id
                WHERE a.disposition = 'transient'
                  AND (e.scheduler_metadata -> 'batch' ->> 'reclaim') = 'true'
            )
            """,
            (run_id, stage, unit_id))
        (reclaimed,) = cur.fetchone()
    return bool(reclaimed)


def _search_queues() -> list[str]:
    """The queues a job of this deployment can be on: the job queue, then
    the reclaim queue when it is set and differs."""
    job_queue = _require_env("RAPIDPIPE_BATCH_JOB_QUEUE")
    reclaim_queue = os.environ.get("RAPIDPIPE_BATCH_RECLAIM_QUEUE") or job_queue
    return [job_queue] if reclaim_queue == job_queue else [job_queue, reclaim_queue]


def queue_for_unit(conn, run_id: str, stage: str, unit_id: str) -> str:
    """The Batch queue the next attempt of ``unit_id`` is submitted to.

    ``RAPIDPIPE_BATCH_JOB_QUEUE``, unless ``RAPIDPIPE_BATCH_RECLAIM_QUEUE``
    is set, differs from it, and an earlier attempt of this unit, in this
    run or a run it was seeded from, was lost to a Spot reclaim
    (:func:`_unit_was_reclaimed`): then the
    reclaim queue, for every further attempt, so a reclaimed unit runs on
    on-demand capacity and never retries on Spot. Any other transient
    (exit 75, a container-start failure, ``lost``) on a unit never
    reclaimed keeps the job queue. Unset or equal, no lookup is made.
    """
    queues = _search_queues()
    if len(queues) == 1:
        return queues[0]
    job_queue, reclaim_queue = queues
    return reclaim_queue if _unit_was_reclaimed(conn, run_id, stage, unit_id) else job_queue


def _epoch_ms_to_iso(value: Any) -> str | None:
    """A Batch epoch-milliseconds timestamp field as a UTC ISO 8601 string,
    or ``None`` for a missing or unparseable value."""
    if value is None:
        return None
    try:
        seconds = float(value) / 1000.0
    except (TypeError, ValueError):
        return None
    return (datetime.fromtimestamp(seconds, tz=timezone.utc)
            .isoformat().replace("+00:00", "Z"))


def _batch_scheduler_metadata(job: dict[str, Any]) -> dict[str, Any]:
    """The ``"batch"`` value :func:`reconcile` merges into an attempt's
    ``execution_records.scheduler_metadata`` (``rapid_docs`` runs.md
    addendum, direction/run-timings): Batch's own ``createdAt``/
    ``startedAt``/``stoppedAt`` (as UTC ISO 8601, converted from the
    epoch-millisecond fields ``describe_jobs`` returns), how many
    container attempts the job itself made, its last ``statusReason``,
    the job's own container's ``logStreamName``, and its job queue.
    Missing fields are omitted, never written as ``null``, so
    ``rapidpipe run timings`` (which reads this back) can tell "never
    recorded" from "recorded empty".
    """
    metadata: dict[str, Any] = {}
    for key, batch_field in (("created_at", "createdAt"), ("started_at", "startedAt"),
                             ("stopped_at", "stoppedAt")):
        iso = _epoch_ms_to_iso(job.get(batch_field))
        if iso is not None:
            metadata[key] = iso
    attempts = job.get("attempts")
    if attempts is not None:
        metadata["attempts"] = len(attempts)
    status_reason = job.get("statusReason")
    if status_reason is not None:
        metadata["status_reason"] = status_reason
    log_stream = (job.get("container") or {}).get("logStreamName")
    if log_stream is not None:
        metadata["log_stream"] = log_stream
    job_queue = job.get("jobQueue")
    if job_queue is not None:
        metadata["job_queue"] = job_queue
    return metadata


def _with_batch_scheduler_metadata(
        execution_record: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """``execution_record`` with Batch's own job timestamps merged into its
    ``scheduler_metadata`` under a ``"batch"`` key -- merged with, not
    replacing, whatever ``scheduler_metadata`` the record already carries
    (direction/run-timings; there is none today, since no stage writes
    one, but this must not assume that stays true).

    Also, when ``execution_record`` itself carries a ``"timing"`` key (the
    stage's own ``fetch_s``/``body_s``, written into ``exec/<attempt>.json``
    by ``rapidpipe.stages.contract.run_stage``, direction/logging-timing,
    and already read back here by :func:`_fetch_execution_record` for a
    SUCCEEDED job with a valid manifest -- ``_execution_record_with_defaults``
    passes it through unchanged), copies it into ``scheduler_metadata``
    under a ``"stage"`` key, merged the same way, so ``rapidpipe run
    timings`` can read ``fetch_s``/``body_s`` back without a second S3
    fetch of its own. There is no ``publish_s`` here: the stage writes its
    execution record before publishing (the same reason it has no
    ``"ended"``), so that phase is never in this dict; ``run timings``
    always prints ``-`` for it.
    """
    record = dict(execution_record)
    scheduler_metadata = dict(record.get("scheduler_metadata") or {})
    scheduler_metadata["batch"] = {
        **(scheduler_metadata.get("batch") or {}), **_batch_scheduler_metadata(job)}
    stage_timing = record.get("timing")
    if stage_timing:
        scheduler_metadata["stage"] = {
            **(scheduler_metadata.get("stage") or {}), **stage_timing}
    record["scheduler_metadata"] = scheduler_metadata
    return record


def reconcile(
    conn, *, run_id: str, client: Any = None, s3_client: Any = None,
) -> list[Reconciled]:
    """Reconcile every unresolved attempt of ``run_id`` against Batch.

    For every attempt of the run with ``disposition IS NULL`` and a
    ``scheduler_job_id``, calls ``describe_jobs`` in batches of at most
    100 job ids and maps each job's status to a disposition (stage
    contract, "Exit codes"; runs page, "Attempts"):

    - ``SUCCEEDED`` with a valid ``manifest.json`` at the output location:
      ``succeeded`` (exit 0), then selected.
    - ``SUCCEEDED`` without a valid manifest: ``failed``, exit 0 -- exit
      zero alone is not success.
    - ``SUCCEEDED`` where fetching the manifest or execution record from
      S3 failed for a reason other than the object being absent (e.g.
      AccessDenied on the launcher's own role): left unresolved,
      reported with ``disposition=None`` -- the job may well have
      succeeded, so this must not consume one of the unit's limited
      attempts. The reason is recorded on the attempt (``reconcile_note``)
      via :func:`~rapidpipe.runs.repository.record_reconcile_note`; a
      later :func:`reconcile` call retries it.
    - ``FAILED`` with a container exit code: :func:`disposition_for` maps
      it (75 -> ``transient``, else ``failed``).
    - ``FAILED`` with no container exit code and an approved
      infrastructure reason (:func:`is_retryable_infrastructure_failure`:
      an EC2 host reclaimed, an image pull or Docker timeout):
      ``transient``, exit code ``None``, so the launcher retries it as a
      fresh attempt.
    - ``FAILED`` with no container exit code otherwise (a ``statusReason``
      and no container exit -- e.g. the task was killed before it could
      exit): ``killed``, exit code ``None``.
    - a job id ``describe_jobs`` does not return at all: ``lost`` (the
      scheduler lost the job).
    - ``SUBMITTED``/``PENDING``/``RUNNABLE``/``STARTING``/``RUNNING``:
      untouched, reported with ``disposition=None``.

    Every recorded disposition passes ``scheduler_job_id`` through to
    :func:`~rapidpipe.runs.repository.record_attempt_result`.

    Reconcile never finishes the run, even when every unit is now
    terminal: that is an explicit
    :func:`~rapidpipe.runs.repository.finish_run` call only.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, output_location, scheduler_job_id
            FROM attempts
            WHERE run = %s AND disposition IS NULL AND scheduler_job_id IS NOT NULL
            """,
            (run_id,),
        )
        rows = cur.fetchall()

    if not rows:
        return []

    attempts_by_job_id = {job_id: (attempt_id, output_location)
                           for attempt_id, output_location, job_id in rows}
    job_ids = list(attempts_by_job_id)

    batch = client if client is not None else batch_client()

    jobs_by_id: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(job_ids, _DESCRIBE_JOBS_BATCH_SIZE):
        response = batch.describe_jobs(jobs=list(chunk))
        for job in response.get("jobs", []):
            jobs_by_id[job["jobId"]] = job

    results: list[Reconciled] = []
    for job_id, (attempt_id, output_location) in attempts_by_job_id.items():
        job = jobs_by_id.get(job_id)

        if job is None:
            # describe_jobs did not return this job id at all: the
            # scheduler lost it (runs page, "Attempts": "lost means the
            # scheduler lost the job").
            record_attempt_result(
                conn, attempt_id, None, "lost", output_location,
                _execution_record_with_defaults(conn, run_id),
                scheduler_job_id=job_id)
            conn.commit()
            results.append(Reconciled(
                attempt_id=attempt_id, job_id=job_id, batch_status="LOST",
                disposition="lost", selected=False))
            continue

        status = job.get("status", "")

        if status in _UNRESOLVED_BATCH_STATUSES:
            results.append(Reconciled(
                attempt_id=attempt_id, job_id=job_id, batch_status=status,
                disposition=None, selected=False))
            continue

        if status == "SUCCEEDED":
            try:
                manifest = _fetch_manifest_if_valid(output_location, s3_client=s3_client)
                execution_record = (
                    _fetch_execution_record(output_location, attempt_id, s3_client=s3_client)
                    if manifest is not None else None)
            except ReconcileFetchFailed as exc:
                # The Batch job SUCCEEDED; reconcile just could not read
                # its manifest or execution record (e.g. AccessDenied on
                # the launcher's own role) -- not evidence the job
                # failed. Leave the attempt unresolved (disposition
                # stays NULL, scheduler_job_id untouched) so a later
                # reconcile retries it once the launcher-side problem is
                # fixed, instead of consuming one of the unit's limited
                # attempts on a launcher-side error (runs page,
                # "Attempts").
                record_reconcile_note(conn, attempt_id, str(exc))
                conn.commit()
                results.append(Reconciled(
                    attempt_id=attempt_id, job_id=job_id, batch_status=status,
                    disposition=None, selected=False))
                continue

            if manifest is not None:
                # record_attempt_result and select_attempt run in the
                # same transaction, one commit, so a process death (or a
                # failed second commit) between them cannot happen: the
                # unit either stays exactly as it was, or is both
                # 'succeeded' and selected/complete together (a split
                # commit here left a unit stranded 'succeeded' with no
                # selected attempt, invisible to the ``disposition IS
                # NULL`` query below that finds unresolved attempts).
                record_attempt_result(
                    conn, attempt_id, 0, "succeeded", output_location,
                    _with_batch_scheduler_metadata(
                        _execution_record_with_defaults(conn, run_id, execution_record), job),
                    scheduler_job_id=job_id)
                select_attempt(conn, attempt_id)
                conn.commit()
                results.append(Reconciled(
                    attempt_id=attempt_id, job_id=job_id, batch_status=status,
                    disposition="succeeded", selected=True))
            else:
                # Exit zero alone is not success (runs page, "Attempts").
                record_attempt_result(
                    conn, attempt_id, 0, "failed", output_location,
                    _with_batch_scheduler_metadata(
                        _execution_record_with_defaults(conn, run_id), job),
                    scheduler_job_id=job_id)
                conn.commit()
                results.append(Reconciled(
                    attempt_id=attempt_id, job_id=job_id, batch_status=status,
                    disposition="failed", selected=False))
            continue

        if status == "FAILED":
            exit_code = _last_container_exit_code(job)
            if exit_code is not None:
                disposition = disposition_for(exit_code, manifest_ok=False)
            elif is_retryable_infrastructure_failure(job):
                # An approved infrastructure failure: Batch no longer
                # retries (Attempts: 1), so the launcher does, as a fresh
                # attempt (runs page, "Attempts").
                disposition = "transient"
            else:
                # No container exit code recorded (e.g. a statusReason
                # from being killed before it could exit): the launcher
                # records this as a termination without a stage exit
                # code (stage contract, "Exit codes").
                disposition = "killed"
            execution_record = _with_batch_scheduler_metadata(
                _execution_record_with_defaults(conn, run_id), job)
            if disposition == "transient" and is_host_reclaim(job):
                # What queue_for_unit reads to send the unit's further
                # attempts to the reclaim queue.
                execution_record["scheduler_metadata"]["batch"]["reclaim"] = True
            record_attempt_result(
                conn, attempt_id, exit_code, disposition, output_location,
                execution_record, scheduler_job_id=job_id)
            conn.commit()
            results.append(Reconciled(
                attempt_id=attempt_id, job_id=job_id, batch_status=status,
                disposition=disposition, selected=False))
            continue

        # Any other/unexpected status: treated the same as FAILED with no
        # container exit code -- an unexpected code the launcher records
        # as a termination it cannot classify further.
        record_attempt_result(
            conn, attempt_id, None, "killed", output_location,
            _with_batch_scheduler_metadata(
                _execution_record_with_defaults(conn, run_id), job),
            scheduler_job_id=job_id)
        conn.commit()
        results.append(Reconciled(
            attempt_id=attempt_id, job_id=job_id, batch_status=status,
            disposition="killed", selected=False))

    return results


#: ``run reconcile --resolve-jobless``'s default ``--older-than``.
DEFAULT_JOBLESS_AFTER_SECONDS = 600


def default_jobless_after_seconds() -> int:
    """The age :func:`resolve_jobless` applies when given none
    (:data:`DEFAULT_JOBLESS_AFTER_SECONDS`)."""
    return DEFAULT_JOBLESS_AFTER_SECONDS


def _jobs_named(batch: Any, job_queue: str, job_name: str) -> list[str]:
    """Every job id on ``job_queue`` whose name is ``job_name``, in any
    status: with a ``filters`` argument ``ListJobs`` ignores ``jobStatus``
    and returns every status, 100 per page (botocore's Batch model,
    ``ListJobs``)."""
    job_ids: list[str] = []
    kwargs: dict[str, Any] = {
        "jobQueue": job_queue,
        "filters": [{"name": "JOB_NAME", "values": [job_name]}],
    }
    while True:
        response = batch.list_jobs(**kwargs)
        job_ids += [job["jobId"] for job in response.get("jobSummaryList", [])
                    if job.get("jobName", job_name) == job_name]
        token = response.get("nextToken")
        if not token:
            return job_ids
        kwargs["nextToken"] = token


def resolve_jobless(
    conn, *, run_id: str, older_than_seconds: float | None = None, client: Any = None,
) -> list[Reconciled]:
    """Resolve ``run_id``'s job-less attempts (loop.md §Concurrency and
    recovery).

    A job-less attempt has ``disposition IS NULL`` and no
    ``scheduler_job_id``: its allocation committed but the Batch submission
    (or recording its job id) failed or has not happened yet, so
    :func:`reconcile` never looks at it and ``run cancel`` cannot terminate
    it. For each one, the Batch queue (``RAPIDPIPE_BATCH_JOB_QUEUE``, and
    ``RAPIDPIPE_BATCH_RECLAIM_QUEUE`` too when it is set and differs, since
    a reclaimed unit's attempt is submitted there) is searched, every
    status, for the name :func:`submit_unit` gives its job
    (:func:`_job_name` with ``RAPIDPIPE_BATCH_JOB_NAME_PREFIX``):

    - exactly one job: its id is recorded on the attempt
      (:func:`~rapidpipe.runs.repository.record_scheduler_job`) for the
      ordinary :func:`reconcile` to resolve -- ``REPAIRED``;
    - more than one: left as it is -- ``AMBIGUOUS`` (``job_id`` lists them);
    - none, and the attempt started more than ``older_than_seconds`` ago:
      recorded ``lost`` through
      :func:`~rapidpipe.runs.repository.record_attempt_result` (exit code
      ``None``, :func:`_execution_record_with_defaults`, ``reconcile_note``
      "no scheduler job after N s"), which returns the unit to ``ready``
      while attempts remain and otherwise ``failed`` -- ``NOJOB``;
    - none, and younger: left for a later call, not reported.

    ``older_than_seconds`` ``None`` is :data:`DEFAULT_JOBLESS_AFTER_SECONDS`.

    Before any write the row is re-read ``FOR UPDATE`` and skipped if a job
    id or a disposition arrived meanwhile. One commit per attempt, as
    :func:`reconcile` does.
    """
    if older_than_seconds is None:
        older_than_seconds = DEFAULT_JOBLESS_AFTER_SECONDS
    if older_than_seconds < 0:
        raise ValueError(f"older_than_seconds must be >= 0, got {older_than_seconds!r}")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, stage FROM attempts
            WHERE run = %s AND disposition IS NULL AND scheduler_job_id IS NULL
            ORDER BY started, id
            """,
            (run_id,))
        jobless = cur.fetchall()
    if not jobless:
        return []

    queues = _search_queues()
    prefix = os.environ.get("RAPIDPIPE_BATCH_JOB_NAME_PREFIX", "rapid")
    batch = client if client is not None else batch_client()
    note = f"no scheduler job after {older_than_seconds:g} s"
    results: list[Reconciled] = []
    for attempt_id, stage in jobless:
        job_name = _job_name(prefix, stage, attempt_id)
        # Distinct ids: a queue's name and its ARN are two strings for one
        # queue, and the same job found twice is not an ambiguity.
        found = list(dict.fromkeys(
            job_id for queue in queues for job_id in _jobs_named(batch, queue, job_name)))
        if len(found) > 1:
            results.append(Reconciled(
                attempt_id=attempt_id, job_id=",".join(found), batch_status="AMBIGUOUS",
                disposition=None, selected=False))
            continue
        with conn.cursor() as cur:
            cur.execute(
                "SELECT output_location, disposition, scheduler_job_id, "
                "started < now() - make_interval(secs => %s) "
                "FROM attempts WHERE id = %s FOR UPDATE",
                (float(older_than_seconds), attempt_id))
            output_location, disposition, job_id, old_enough = cur.fetchone()
        if disposition is not None or job_id is not None:
            conn.rollback()
            continue
        if found:
            record_scheduler_job(conn, attempt_id, found[0])
            conn.commit()
            results.append(Reconciled(
                attempt_id=attempt_id, job_id=found[0], batch_status="REPAIRED",
                disposition=None, selected=False))
            continue
        if not old_enough:
            conn.rollback()
            continue
        record_attempt_result(
            conn, attempt_id, None, "lost", output_location,
            _execution_record_with_defaults(conn, run_id),
            scheduler_job_id=None, reconcile_note=note)
        conn.commit()
        results.append(Reconciled(
            attempt_id=attempt_id, job_id="-", batch_status="NOJOB",
            disposition="lost", selected=False))
    return results


def cancel(conn, *, attempt_id: str, reason: str, client: Any = None) -> None:
    """Ask Batch to terminate the job for ``attempt_id``.

    The disposition is recorded by the next :func:`reconcile`, not here
    -- Batch reports the termination asynchronously, and reconcile is the
    one place attempt outcomes are written.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT scheduler_job_id FROM attempts WHERE id = %s", (attempt_id,))
        row = cur.fetchone()
    if row is None:
        from rapidpipe.runs.repository import AttemptNotFound

        raise AttemptNotFound(f"attempt {attempt_id!r} does not exist")
    (job_id,) = row
    if job_id is None:
        raise LaunchError(
            f"attempt {attempt_id!r} has no scheduler_job_id; nothing to cancel")

    batch = client if client is not None else batch_client()
    batch.terminate_job(jobId=job_id, reason=reason)
