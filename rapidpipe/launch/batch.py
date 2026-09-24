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
- ``RAPIDPIPE_BATCH_JOB_DEFINITION_SCRATCH`` /
  ``RAPIDPIPE_BATCH_JOB_DEFINITION_PRODUCTION`` -- the Batch job
  definition name or ARN for a run of that kind (:func:`job_definition_for`).
- ``RAPIDPIPE_OUTPUTS_ROOT_SCRATCH`` / ``RAPIDPIPE_OUTPUTS_ROOT_PRODUCTION``
  -- an ``s3://bucket/prefix`` under which ``runs/<run>/<stage>/<unit>/
  <attempt>`` lives for a run of that kind (the scratch bucket and the
  project bucket) (:func:`outputs_root_for`).
- ``RAPIDPIPE_BATCH_JOB_DEFINITION`` / ``RAPIDPIPE_OUTPUTS_ROOT`` -- the
  scratch fallback only. A production run fails closed: it never falls
  back to an unsuffixed variable (supervisor step 3, 2026-09-24,
  amendment A4), so a missing production setting cannot send project
  outputs to a scratch location.
- ``RAPIDPIPE_BATCH_JOB_NAME_PREFIX`` -- optional, default ``rapid``.

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
from pathlib import Path
from typing import Any, Iterable, Sequence

from rapidpipe.products.manifest import Manifest, ManifestError
from rapidpipe.products.storage import fetch_object, join, parse_location
from rapidpipe.runs.local import disposition_for
from rapidpipe.runs.repository import (
    RunNotFound,
    add_unit,
    allocate_attempt,
    attempt_output_location,
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

_TRANSIENT_FAILURE_CODE = 75

#: Unit states :func:`~rapidpipe.runs.repository.select_attempt` refuses
#: to touch (mirrors ``rapidpipe.runs.repository._TERMINAL_UNIT_STATES``,
#: not imported directly since it is private to that module).
_TERMINAL_UNIT_STATES = ("complete", "failed", "cancelled")

logger = logging.getLogger(__name__)


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


#: A ClientError-shaped exception's ``response["Error"]["Code"]`` values
#: that mean "the object is not there" -- the job wrote no manifest (or no
#: execution record), as opposed to reconcile being unable to tell.
#: Mirrors ``rapidpipe.stages.contract``'s ``_NOT_FOUND_ERROR_CODES``;
#: duplicated rather than imported because this module never imports a
#: stage module (module docstring, "the package's fixed dependency
#: direction").
_NOT_FOUND_ERROR_CODES = ("404", "NoSuchKey")


def _client_error_code(exc: BaseException) -> str | None:
    """The ``Error.Code`` of a ClientError-shaped exception, or ``None``.

    Matches by shape (a ``response`` attribute holding that structure), not
    by ``isinstance``, so a test's stand-in exception is recognised the
    same way as a real ``botocore.exceptions.ClientError`` without this
    module importing botocore. Mirrors
    ``rapidpipe.stages.contract._client_error_code``.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    if not isinstance(error, dict):
        return None
    code = error.get("Code")
    return code if isinstance(code, str) else None


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
) -> BatchSubmission:
    """Allocate an attempt and submit it to Batch as one job.

    Steps: :func:`~rapidpipe.runs.repository.add_unit`, then
    :func:`~rapidpipe.runs.repository.allocate_attempt` (its exceptions --
    the run fence and the attempt allowance -- propagate uncommitted);
    the output location is ``<outputs_root>/runs/<run>/<stage>/<unit>/
    <attempt>``, where ``outputs_root`` and ``job_definition`` default,
    when ``None``, to :func:`outputs_root_for` and
    :func:`job_definition_for` of the run's kind (read from its ``runs``
    row before anything is written); the Batch job's ``containerOverrides.command`` starts at
    ``stage`` (the image's entrypoint is ``rapidpipe``, per the stage
    contract's "Invocation" form). The job id Batch returns is then
    recorded on the attempt row with
    :func:`~rapidpipe.runs.repository.record_scheduler_job`. Commits
    after each repository call, as
    :func:`rapidpipe.runs.local.run_stage_locally` does.
    """
    if outputs_root is None or job_definition is None:
        kind = _run_kind(conn, run_id)
        outputs_root = outputs_root or outputs_root_for(kind)
        job_queue = _require_env("RAPIDPIPE_BATCH_JOB_QUEUE")
        job_definition = job_definition or job_definition_for(kind)
    else:
        job_queue = _require_env("RAPIDPIPE_BATCH_JOB_QUEUE")
    job_name_prefix = os.environ.get("RAPIDPIPE_BATCH_JOB_NAME_PREFIX", "rapid")

    add_unit(conn, run_id, stage, unit_kind, unit_id)
    conn.commit()

    attempt_id = allocate_attempt(conn, run_id, stage, unit_id, outputs_root=outputs_root)
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
    reason other than the object being absent (``404``/``NoSuchKey``) --
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
            if _client_error_code(exc) in _NOT_FOUND_ERROR_CODES:
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
            if _client_error_code(exc) in _NOT_FOUND_ERROR_CODES:
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
    - ``FAILED`` with no container exit code (a ``statusReason`` and no
      container exit -- e.g. the task was killed before it could exit):
      ``killed``, exit code ``None``.
    - a job id ``describe_jobs`` does not return at all: ``lost`` (the
      scheduler lost the job).
    - ``SUBMITTED``/``PENDING``/``RUNNABLE``/``STARTING``/``RUNNING``:
      untouched, reported with ``disposition=None``.

    Every recorded disposition passes ``scheduler_job_id`` through to
    :func:`~rapidpipe.runs.repository.record_attempt_result`.

    Reconcile never finishes the run, even when every unit is now
    terminal: that is an explicit
    :func:`~rapidpipe.runs.repository.finish_run` call only.

    Before returning, also repairs any attempt of the run left
    'succeeded' but unselected by a pre-existing split between
    ``record_attempt_result`` and ``select_attempt`` (the two now commit
    together, so this only ever cleans up old damage; see
    :func:`_repair_stranded_succeeded_attempts`).
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
        # No attempt is unresolved, but a run with none left can still
        # hold a pre-existing stranded 'succeeded' attempt from before
        # the fix above (a unit like that has no disposition IS NULL
        # attempt to be found by the query just above, so this repair
        # pass is the only path back to it).
        return _repair_stranded_succeeded_attempts(conn, run_id=run_id)

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
                # 'succeeded' and selected/complete together (supervisor
                # step 3, 2026-09-24, WP-F -- a split commit here left a
                # unit stranded 'succeeded' with no selected attempt,
                # invisible to the ``disposition IS NULL`` query below
                # that finds unresolved attempts; see the repair pass at
                # the end of this function for units already stranded
                # that way).
                record_attempt_result(
                    conn, attempt_id, 0, "succeeded", output_location,
                    _execution_record_with_defaults(conn, run_id, execution_record),
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

    results.extend(_repair_stranded_succeeded_attempts(conn, run_id=run_id))

    return results


def _repair_stranded_succeeded_attempts(conn, *, run_id: str) -> list[Reconciled]:
    """Select any attempt of ``run_id`` left 'succeeded' but unselected.

    Guards against a pre-existing split between :func:`record_attempt_result`
    and :func:`select_attempt`: a process death, or a failed second commit,
    between the two left the attempt permanently ``succeeded`` with its
    unit neither selected nor terminal -- and invisible to this function's
    main pass, which only looks at attempts with ``disposition IS NULL``
    (supervisor step 3, 2026-09-24, WP-F). The main pass above no longer
    creates new instances of this (it commits both writes together), so
    this repair pass only ever has pre-existing damage to clean up, and
    should shrink to nothing as old runs finish reconciling.

    Selecting is the same call the main pass makes, so it enforces the
    same invariants (``select_attempt`` requires ``disposition ==
    'succeeded'``, refuses an already-selected or terminal unit) --
    nothing here bypasses them, it only reaches attempts the main pass's
    query does not.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.id, a.scheduler_job_id
            FROM attempts a
            JOIN units u ON u.id = a.unit
            WHERE a.run = %s
              AND a.disposition = 'succeeded'
              AND u.selected_attempt IS NULL
              AND u.state NOT IN %s
            """,
            (run_id, _TERMINAL_UNIT_STATES),
        )
        rows = cur.fetchall()

    repaired: list[Reconciled] = []
    for attempt_id, job_id in rows:
        select_attempt(conn, attempt_id)
        conn.commit()
        logger.warning(
            "reconcile: repaired stranded attempt %s (run %s) -- was "
            "'succeeded' with no selected attempt; selected it now",
            attempt_id, run_id)
        repaired.append(Reconciled(
            attempt_id=attempt_id, job_id=job_id or "", batch_status="SUCCEEDED",
            disposition="succeeded", selected=True))

    return repaired


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
