"""Running one job's stage: submitted to AWS Batch, or as a local subprocess.

Foundation plan v5, Part 5 item 1: the launcher is reduced to submitting a
job under a run. The job row and its recorded inputs are written first by
``rapidpipe.foundation.create_job``; this module only starts the stage's
execution, ``rapidpipe stage <name> <argv>``, and the stage publishes its
own outputs last (Part 1 statement 6). Batch retries a job under its job
definition's retry strategy; there is no retry here, and no attempt
bookkeeping.

Deployment configuration comes from the environment only, never a
committed value: ``RAPIDPIPE_BATCH_JOB_QUEUE`` and
``RAPIDPIPE_BATCH_JOB_DEFINITION`` (name or ARN), and optionally
``RAPIDPIPE_BATCH_JOB_NAME_PREFIX`` (default ``rapid``). ``boto3`` is
imported only by :func:`batch_client`, the one indirection a test replaces.

This subpackage may import ``rapidpipe.stages`` and the layers below it,
and is never imported by a stage module.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any, Sequence

from rapidpipe.stages.contract import STAGE_NAMES

__all__ = ["LaunchError", "batch_client", "run_locally", "submit"]


class LaunchError(Exception):
    """A job that could not be started: an unknown stage or a missing setting."""


def batch_client() -> Any:
    import boto3

    return boto3.client("batch")


def _stage(stage: str) -> str:
    if stage not in STAGE_NAMES:
        raise LaunchError(f"unknown stage {stage!r}; known stages: {', '.join(STAGE_NAMES)}")
    return stage


def submit(job_id: int, stage: str, stage_argv: Sequence[str], *, client: Any = None) -> str:
    """Submit job ``job_id``'s stage to Batch; return the Batch job id."""
    settings = {}
    for name in ("RAPIDPIPE_BATCH_JOB_QUEUE", "RAPIDPIPE_BATCH_JOB_DEFINITION"):
        settings[name] = os.environ.get(name)
        if not settings[name]:
            raise LaunchError(f"{name} is not set")
    prefix = os.environ.get("RAPIDPIPE_BATCH_JOB_NAME_PREFIX") or "rapid"
    response = (client or batch_client()).submit_job(
        jobName=f"{prefix}-{_stage(stage)}-{job_id}",
        jobQueue=settings["RAPIDPIPE_BATCH_JOB_QUEUE"],
        jobDefinition=settings["RAPIDPIPE_BATCH_JOB_DEFINITION"],
        containerOverrides={"command": ["rapidpipe", "stage", stage, *stage_argv]})
    return response["jobId"]


def run_locally(stage: str, stage_argv: Sequence[str], *, python: str = sys.executable) -> int:
    """Run the stage as ``python -m rapidpipe.stages.<stage>``; return its exit code."""
    argv = [python, "-m", f"rapidpipe.stages.{_stage(stage)}", *stage_argv]
    return subprocess.run(argv).returncode
