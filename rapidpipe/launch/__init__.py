"""Running one job on AWS Batch, or one old-contract stage as a local subprocess.

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

:func:`run_locally` runs a stage still on the old ``--run/--unit/--attempt``
contract (``admit``, ``finalize``) as a
subprocess, until its port to ``run(job)``.

This subpackage may import ``rapidpipe.stages`` and the layers below it,
and is never imported by a stage module.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any, Sequence

from rapidpipe.stages.contract import STAGE_NAMES

__all__ = ["LaunchError", "batch_client", "memory_mib", "run_locally", "submit"]


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
           memory_mib: int | None = None) -> str:
    """Submit job ``job_id`` (of ``stage``) to Batch as ``job run --job <id>``; return the Batch job id.

    With ``conn``, the id is recorded as the row's ``batch_job_id`` and committed; a
    resubmission replaces it, so the row names its latest Batch job. With
    ``memory_mib`` (the run's ``[batch.memory_mib]`` value for the stage, :func:`memory_mib`),
    the job asks for that memory instead of the job definition's.
    """
    queue = os.environ.get("RAPIDPIPE_BATCH_JOB_QUEUE")
    if not queue:
        raise LaunchError("RAPIDPIPE_BATCH_JOB_QUEUE is not set")
    root = os.environ.get("RAPIDPIPE_OUTPUTS_ROOT", "")
    if not root.startswith("s3://"):
        raise LaunchError(f"RAPIDPIPE_OUTPUTS_ROOT {root!r} is not an s3:// URL a Batch job can write")
    environment = [{"name": "RAPIDPIPE_OUTPUTS_ROOT", "value": root}]
    if os.environ.get("RAPID_DB_SECRET_ID"):
        environment.append({"name": "RAPID_DB_SECRET_ID", "value": os.environ["RAPID_DB_SECRET_ID"]})
    prefix = os.environ.get("RAPIDPIPE_BATCH_JOB_NAME_PREFIX") or "rapid"
    overrides = {"command": ["job", "run", "--job", str(job_id)], "environment": environment}
    if memory_mib is not None:
        overrides["resourceRequirements"] = [{"type": "MEMORY", "value": str(int(memory_mib))}]
    response = (client or batch_client()).submit_job(
        jobName=f"{prefix}-{stage}-{job_id}", jobQueue=queue, jobDefinition=job_definition,
        containerOverrides=overrides)
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute("UPDATE jobs SET batch_job_id = %s WHERE id = %s", (response["jobId"], job_id))
        conn.commit()
    return response["jobId"]


def memory_mib(settings: dict, stage: str) -> int | None:
    """The run's ``[batch.memory_mib]`` value for ``stage``; ``None`` (the job definition's) without one."""
    return settings.get("batch", {}).get("memory_mib", {}).get(stage)


def run_locally(stage: str, stage_argv: Sequence[str], *, python: str = sys.executable) -> int:
    """Run the stage as ``python -m rapidpipe.stages.<stage>``; return its exit code."""
    argv = [python, "-m", f"rapidpipe.stages.{_stage(stage)}", *stage_argv]
    return subprocess.run(argv).returncode
