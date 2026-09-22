"""Submit run units to AWS Batch, resolve their inputs, and reconcile results.

Implements the stage contract's "Invocation" ("the Batch wrapper allocates
one per Batch attempt") and "Exit codes" (75 retries, 70 stops; "The
launcher also records terminations without a stage exit code and
unexpected codes"), and the runs page's "Attempts" (``lost`` means the
scheduler lost the job) over ``rapidpipe.runs.repository``.

This module composes ``rapidpipe.runs.repository``, ``rapidpipe.products``
and ``rapidpipe.db``; it never imports a stage module or ``rapidpipe.cli``
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
- ``RAPIDPIPE_BATCH_JOB_DEFINITION`` -- the Batch job definition name or ARN.
- ``RAPIDPIPE_OUTPUTS_ROOT`` -- an ``s3://bucket/prefix`` under which
  ``runs/<run>/<stage>/<unit>/<attempt>`` lives; chosen by whoever
  configures the run's lane (a project or personal bucket).
- ``RAPIDPIPE_BATCH_JOB_NAME_PREFIX`` -- optional, default ``rapid``.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from rapidpipe.products.manifest import Manifest, ManifestError
from rapidpipe.products.storage import fetch_object, join, parse_location
from rapidpipe.runs.local import disposition_for
from rapidpipe.runs.repository import (
    add_unit,
    allocate_attempt,
    record_attempt_result,
    record_scheduler_job,
    select_attempt,
)

#: Batch job status values ``describe_jobs`` never finishes on; reported
#: back with ``disposition=None`` and otherwise untouched.
_UNRESOLVED_BATCH_STATUSES = ("SUBMITTED", "PENDING", "RUNNABLE", "STARTING", "RUNNING")

#: describe_jobs accepts at most 100 job ids per call (a Batch API limit).
_DESCRIBE_JOBS_BATCH_SIZE = 100

_TRANSIENT_FAILURE_CODE = 75


class LaunchError(Exception):
    """Base class for every exception this module raises."""


class MissingEnvironmentVariable(LaunchError):
    """A required ``RAPIDPIPE_*`` environment variable is not set."""


class DependencyIncomplete(LaunchError):
    """The requested upstream unit has no selected attempt yet.

    Raised by :func:`resolve_inputs_from_stage` -- the launcher's
    dependency enforcement in its first, honest form: one unit, one
    upstream stage (design brief; runs page, "Units": "A unit is pending
    until its declared input set is complete and its upstream attempts
    are selected.").
    """


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise MissingEnvironmentVariable(
            f"{name} is not set; rapidpipe.launch.batch requires it "
            "(README, \"Running on Batch\")")
    return value


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
    client: Any = None,
) -> BatchSubmission:
    """Allocate an attempt and submit it to Batch as one job.

    Steps: :func:`~rapidpipe.runs.repository.add_unit`, then
    :func:`~rapidpipe.runs.repository.allocate_attempt` (its exceptions --
    the run fence and the attempt allowance -- propagate uncommitted);
    the output location is ``<outputs_root>/runs/<run>/<stage>/<unit>/
    <attempt>``; the Batch job's ``containerOverrides.command`` starts at
    ``stage`` (the image's entrypoint is ``rapidpipe``, per the stage
    contract's "Invocation" form). The job id Batch returns is then
    recorded on the attempt row with
    :func:`~rapidpipe.runs.repository.record_scheduler_job`. Commits
    after each repository call, as
    :func:`rapidpipe.runs.local.run_stage_locally` does.
    """
    outputs_root = outputs_root or _require_env("RAPIDPIPE_OUTPUTS_ROOT")
    job_queue = _require_env("RAPIDPIPE_BATCH_JOB_QUEUE")
    job_definition = _require_env("RAPIDPIPE_BATCH_JOB_DEFINITION")
    job_name_prefix = os.environ.get("RAPIDPIPE_BATCH_JOB_NAME_PREFIX", "rapid")

    add_unit(conn, run_id, stage, unit_kind, unit_id)
    conn.commit()

    attempt_id = allocate_attempt(conn, run_id, stage, unit_id)
    conn.commit()

    outputs_location_obj = parse_location(outputs_root)
    output_location = join(
        outputs_location_obj, f"runs/{run_id}/{stage}/{unit_id}/{attempt_id}")

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

    batch = client if client is not None else batch_client()
    response = batch.submit_job(
        jobName=job_name,
        jobQueue=job_queue,
        jobDefinition=job_definition,
        containerOverrides={
            "command": command,
            "environment": [
                {"name": "RAPIDPIPE_RUN_ID", "value": run_id},
                {"name": "RAPIDPIPE_ATTEMPT_ID", "value": attempt_id},
            ],
        },
    )
    job_id = response["jobId"]

    record_scheduler_job(conn, attempt_id, job_id)
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
        try:
            fetch_object(location, "manifest.json", dest_path, client=s3_client)
        except Exception:  # noqa: BLE001 - any fetch failure means no valid manifest
            return None
        try:
            return Manifest.read(dest_path)
        except (ManifestError, OSError, ValueError):
            return None


def _fetch_execution_record(
    output_location: str, attempt_id: str, *, s3_client: Any,
) -> dict[str, Any]:
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
        try:
            fetch_object(location, relative, dest_path, client=s3_client)
        except Exception:  # noqa: BLE001 - missing/unreadable record -> {}
            return {}
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
    ``exec/<attempt>.json`` (or its absence) leaves unset.

    Mirrors ``rapidpipe.runs.local.run_stage_locally``'s own fallbacks for
    the same three columns (``schema_version``, ``source_revision``,
    ``settings_hash``): a job with no execution record at all (a job that
    never reached the point of writing one, or a ``lost``/no-manifest
    outcome this function synthesizes an empty record for) still needs a
    row that satisfies the schema.
    """
    record = dict(execution_record) if execution_record else {}
    record.setdefault("schema_version", _run_schema_version(conn, run_id))
    record.setdefault("source_revision", "unknown")
    record.setdefault("settings_hash", "unknown")
    return record


def _last_container_exit_code(job: dict[str, Any]) -> int | None:
    attempts = job.get("attempts") or []
    if not attempts:
        return None
    last = attempts[-1]
    container = last.get("container") or {}
    return container.get("exitCode")


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
    - ``FAILED`` with a container exit code: :func:`disposition_for` maps
      it (75 -> ``transient``, else ``failed``).
    - ``FAILED`` with no container exit code (a ``statusReason`` and no
      container exit -- e.g. the task was killed before it could exit):
      ``killed``, exit code ``None``.
    - a job id ``describe_jobs`` does not return at all: ``lost`` (the
      scheduler lost the job).
    - ``SUBMITTED``/``PENDING``/``RUNNABLE``/``STARTING``/``RUNNING``:
      untouched, reported with ``disposition=None``.

    Every recorded disposition passes ``scheduler_job_id`` through to
    :func:`~rapidpipe.runs.repository.record_attempt_result`.
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
            manifest = _fetch_manifest_if_valid(output_location, s3_client=s3_client)
            if manifest is not None:
                execution_record = _fetch_execution_record(
                    output_location, attempt_id, s3_client=s3_client)
                record_attempt_result(
                    conn, attempt_id, 0, "succeeded", output_location,
                    _execution_record_with_defaults(conn, run_id, execution_record),
                    scheduler_job_id=job_id)
                conn.commit()
                select_attempt(conn, attempt_id)
                conn.commit()
                results.append(Reconciled(
                    attempt_id=attempt_id, job_id=job_id, batch_status=status,
                    disposition="succeeded", selected=True))
            else:
                # Exit zero alone is not success (runs page, "Attempts").
                record_attempt_result(
                    conn, attempt_id, 0, "failed", output_location,
                    _execution_record_with_defaults(conn, run_id),
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
            else:
                # No container exit code recorded (e.g. a statusReason
                # from being killed before it could exit): the launcher
                # records this as a termination without a stage exit
                # code (stage contract, "Exit codes").
                disposition = "killed"
            record_attempt_result(
                conn, attempt_id, exit_code, disposition, output_location,
                _execution_record_with_defaults(conn, run_id),
                scheduler_job_id=job_id)
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
            _execution_record_with_defaults(conn, run_id),
            scheduler_job_id=job_id)
        conn.commit()
        results.append(Reconciled(
            attempt_id=attempt_id, job_id=job_id, batch_status=status,
            disposition="killed", selected=False))

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
