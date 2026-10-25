"""Running one job on AWS Batch.

Foundation plan v5, Part 5 item 1: the launcher is reduced to submitting a
job under a run. The job row and its recorded inputs are written first by
``rapidpipe.foundation.create_job``; :func:`submit` starts one Batch job
whose command is ``job run --job <id>`` after the image's ``rapidpipe``
entrypoint, and records the Batch job id on the job row
(``jobs.batch_job_id``) when given the connection, the in-container counterpart of ``rapidpipe job local``: it
runs the stage's ``run(job)`` and publishes (``rapidpipe.foundation.run_job``).
A retry is a resubmission of the same job: ``job run`` stops on a
published job and otherwise takes the next attempt. Nothing retries here.

Deployment configuration comes from the environment and the caller, never
a committed value: ``RAPIDPIPE_BATCH_JOB_QUEUE``, the job definition (name,
``name:revision`` or ARN) as an argument, ``RAPIDPIPE_OUTPUTS_ROOT`` (an
``s3://`` URL, passed into the container), ``RAPID_DB_SECRET_ID`` passed in
when set (the job definition's ``RAPID_PARAMETER_PATH`` names the
database otherwise, ``rapidpipe.db.connection``), and optionally
``RAPIDPIPE_BATCH_JOB_NAME_PREFIX`` (default ``rapid``). ``boto3`` is
imported only by :func:`batch_client`, the one indirection a test replaces.

This subpackage may import ``rapidpipe.stages`` and the layers below it,
and is never imported by a stage module.
"""

from __future__ import annotations

import os
from typing import Any

from rapidpipe.db.connection import parameter_tree_environment
from rapidpipe.foundation import attempts, publish_kafka
from rapidpipe.foundation.local import failure_reason
from rapidpipe.stages.contract import STAGE_NAMES

__all__ = ["LaunchError", "batch_client", "job_environment", "memory_mib", "spot", "submit"]


class LaunchError(Exception):
    """A job that could not be started: an unknown stage or a missing setting."""


def batch_client() -> Any:
    import boto3

    return boto3.client("batch")


def _stage(stage: str) -> str:
    if stage not in STAGE_NAMES:
        raise LaunchError(f"unknown stage {stage!r}; known stages: {', '.join(STAGE_NAMES)}")
    return stage


def submit(job_id: int, stage: str, *, job_definition: str, conn: Any = None, client: Any = None,
           memory_mib: int | None = None, name_suffix: str = "",
           environment_extra: dict[str, str] | None = None, queue: str | None = None,
           one_attempt: bool = False) -> str:
    """Submit job ``job_id`` (of ``stage``) to Batch as ``job run --job <id>``; return the Batch job id.

    ``queue`` is the job queue, ``RAPIDPIPE_BATCH_JOB_QUEUE`` without one. With
    ``one_attempt`` (a Spot submission, :func:`spot`) the submission overrides the job
    definition's retry strategy to one attempt, so nothing retries on Spot; the loop's
    reconcile sends the next attempt to the prompt queue.
    With ``conn``, the id is recorded as the row's ``batch_job_id`` and its submission
    in ``jobs.attempts`` (``rapidpipe.foundation.attempts``), and committed; a
    resubmission replaces the id, so the row names its latest Batch job, and a
    ``failed`` row becomes ``launched`` again, so a loop started at once counts it
    pending, not failed, before its container starts. With
    ``memory_mib`` (the run's ``[batch.memory_mib]`` value for the stage, :func:`memory_mib`),
    the job asks for that memory instead of the job definition's. ``name_suffix`` ends
    the Batch job's name (the loop's rerun marks its one automatic rerun so).
    ``environment_extra`` (:func:`job_environment`) is added to the job's environment.
    When the Batch request itself raises, the row (with ``conn``) is marked
    ``failed`` with the error as its ``refusal`` and the error propagates; a
    failure recording the id after an accepted request is not caught here.
    """
    queue = queue or os.environ.get("RAPIDPIPE_BATCH_JOB_QUEUE")
    if not queue:
        raise LaunchError("RAPIDPIPE_BATCH_JOB_QUEUE is not set")
    root = os.environ.get("RAPIDPIPE_OUTPUTS_ROOT", "")
    if not root.startswith("s3://"):
        raise LaunchError(f"RAPIDPIPE_OUTPUTS_ROOT {root!r} is not an s3:// URL a Batch job can write")
    values = {**(environment_extra or {}), "RAPIDPIPE_OUTPUTS_ROOT": root}
    if os.environ.get("RAPID_DB_SECRET_ID"):
        values["RAPID_DB_SECRET_ID"] = os.environ["RAPID_DB_SECRET_ID"]
    environment = [{"name": k, "value": v} for k, v in values.items()]
    prefix = os.environ.get("RAPIDPIPE_BATCH_JOB_NAME_PREFIX") or "rapid"
    overrides = {"command": ["job", "run", "--job", str(job_id)], "environment": environment}
    if memory_mib is not None:
        overrides["resourceRequirements"] = [{"type": "MEMORY", "value": str(int(memory_mib))}]
    request = {"jobName": f"{prefix}-{stage}-{job_id}{name_suffix}", "jobQueue": queue,
               "jobDefinition": job_definition, "containerOverrides": overrides}
    if one_attempt:
        request["retryStrategy"] = {"attempts": 1}
    try:
        response = (client or batch_client()).submit_job(**request)
    except Exception as exc:
        if conn is not None:                # never left ``launched`` with nothing on Batch
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("UPDATE jobs SET state = 'failed', ended = now(), refusal = %s"
                            " WHERE id = %s AND state <> 'published'", (failure_reason(exc), job_id))
            conn.commit()
        raise
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute("UPDATE jobs SET batch_job_id = %s, state = CASE WHEN state = 'failed'"
                        " THEN 'launched' ELSE state END WHERE id = %s", (response["jobId"], job_id))
            attempts.record(cur, job_id, response["jobId"], {
                "queue": queue, "submitted": attempts.now(), "why": "rerun" if name_suffix else "first"})
        conn.commit()
    return response["jobId"]


_environments: dict[tuple, dict[str, str]] = {}


def job_environment(settings: dict, ssm: Any = None) -> dict[str, str]:
    """What a job would read from SSM, read here once per process and passed in its environment.

    The database endpoint and secret name of ``RAPID_PARAMETER_PATH``'s tree
    (``rapidpipe.db.connection.parameter_tree_environment``) unless this
    process has its own ``PGHOST``, and the run's ``[kafka]`` endpoint
    (``rapidpipe.foundation.publish_kafka.environment``) when the settings
    have the table. A job given both reads no SSM parameter. ``ssm`` is a
    test's SSM client.
    """
    path = os.environ.get("RAPID_PARAMETER_PATH")
    kafka = settings.get("kafka")
    key = (None if os.environ.get("PGHOST") else path, tuple(sorted((kafka or {}).items())))
    if key not in _environments:
        found = parameter_tree_environment(key[0], ssm_client=ssm) if key[0] else {}
        if kafka:
            found.update(publish_kafka.environment(kafka, ssm))
        _environments[key] = found
    return _environments[key]


def spot(settings: dict) -> dict | None:
    """The run's ``[batch.spot]`` table (``queue``, ``backlog``, ``hedge_factor``); ``None`` when it names
    no Spot queue, and then every submission goes to the prompt queue as before."""
    table = settings.get("batch", {}).get("spot") or {}
    return table if table.get("queue") else None


def memory_mib(settings: dict, stage: str) -> int | None:
    """The run's ``[batch.memory_mib]`` value for ``stage``; ``None`` (the job definition's) without one."""
    return settings.get("batch", {}).get("memory_mib", {}).get(stage)
