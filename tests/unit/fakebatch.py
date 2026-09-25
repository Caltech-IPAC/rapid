"""A minimal in-memory stand-in for a boto3 Batch client, for tests that
must run whether or not boto3 is installed.

Only the operations ``rapidpipe.launch.batch`` calls are implemented:
``describe_job_definitions`` (a released run's revision must be ACTIVE;
``job_definitions`` maps ``name:revision`` to a status, and one not in it
is not found), ``submit_job``, ``describe_jobs`` (returning whatever
statuses the test configured, omitting any job id the test never
registered -- standing in for a job Batch itself has forgotten about),
and ``terminate_job``. ``calls`` records each operation's name and its
key argument, in order, so a test can assert on call shape.
"""

from __future__ import annotations

import itertools
from typing import Any


class FakeBatch:
    """An in-memory Batch client, keyed by job id."""

    def __init__(self):
        self._jobs: dict[str, dict[str, Any]] = {}
        self._id_counter = itertools.count(1)
        self.calls: list[tuple[str, Any]] = []
        self.submitted: list[dict[str, Any]] = []
        self.terminated: list[dict[str, Any]] = []
        self.job_definitions: dict[str, str] = {}

    def describe_job_definitions(self, *, jobDefinitions: list[str], **_: Any) -> dict[str, Any]:
        self.calls.append(("describe_job_definitions", tuple(jobDefinitions)))
        found = []
        for name in jobDefinitions:
            if name in self.job_definitions:
                job_name, _, revision = name.partition(":")
                found.append({"jobDefinitionName": job_name, "revision": int(revision),
                              "status": self.job_definitions[name]})
        return {"jobDefinitions": found}

    def submit_job(
        self, *, jobName: str, jobQueue: str, jobDefinition: str,
        containerOverrides: dict[str, Any], **_: Any,
    ) -> dict[str, str]:
        job_id = f"job-{next(self._id_counter)}"
        self.calls.append(("submit_job", jobName))
        self.submitted.append({
            "jobName": jobName, "jobQueue": jobQueue,
            "jobDefinition": jobDefinition,
            "containerOverrides": containerOverrides,
        })
        # Registered with no status yet -- a test that wants describe_jobs
        # to see this job calls set_status() to give it one.
        self._jobs[job_id] = {"jobId": job_id, "jobName": jobName}
        return {"jobId": job_id, "jobName": jobName}

    def set_status(
        self, job_id: str, status: str, *,
        status_reason: str | None = None,
        container_exit_code: int | None = None,
    ) -> None:
        """Test setup: give a submitted (or arbitrary) job id a status.

        ``container_exit_code``, when given, is recorded as
        ``attempts[-1].container.exitCode`` -- the shape
        :func:`rapidpipe.launch.batch.reconcile` reads a FAILED job's exit
        code from. Omitting it stands in for a termination Batch never
        saw a container exit code for.
        """
        job = self._jobs.setdefault(job_id, {"jobId": job_id})
        job["status"] = status
        if status_reason is not None:
            job["statusReason"] = status_reason
        if container_exit_code is not None:
            job["attempts"] = [{"container": {"exitCode": container_exit_code}}]
        elif "attempts" not in job:
            job["attempts"] = []

    def forget(self, job_id: str) -> None:
        """Remove a job id entirely, standing in for one describe_jobs no
        longer returns -- the scheduler lost it."""
        self._jobs.pop(job_id, None)

    def describe_jobs(self, *, jobs: list[str], **_: Any) -> dict[str, Any]:
        self.calls.append(("describe_jobs", tuple(jobs)))
        found = [self._jobs[job_id] for job_id in jobs if job_id in self._jobs]
        return {"jobs": found}

    def terminate_job(self, *, jobId: str, reason: str, **_: Any) -> dict:
        self.calls.append(("terminate_job", jobId))
        self.terminated.append({"jobId": jobId, "reason": reason})
        return {}
