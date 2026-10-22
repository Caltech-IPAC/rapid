"""Running one job's stage in this process, then publishing it (``job local``, ``job run``).

Foundation plan v5, Part 1 statement 6: a stage writes its files under an
immutable prefix that contains its job id and attempt,
``<outputs root>/<run>/<stage>/<job>/<attempt>/``, and publishes last.
This module is the stage contract for the foundation's stages, the
decision PR 278 left to the first port: a stage is one function,
``run(job: JobContext) -> None``, that reads what the context names, writes
its files and ``manifest.json`` (:func:`rapidpipe.foundation.publish`'s
format) under ``job.path`` and returns. :func:`run_job` runs it and calls
:func:`~rapidpipe.foundation.publish.publish`; the stage never publishes.

Retry is a new attempt of the same job: :func:`run_job` reads the job
first and stops before touching a file if it is already published, then
claims the next unused attempt directory under the job's prefix. Nothing
retries by itself.

The outputs root is a URL, ``file://`` or ``s3://``
(:mod:`rapidpipe.foundation.prefix`). Under ``s3://`` the stage writes into
a local working directory and :func:`run_job` uploads it to the attempt's
prefix before publishing and removes it once the upload has succeeded (a
stage or upload that fails leaves it, for a person); the next attempt number is one more than the
highest present, so two concurrent attempts of one job are not supported
there (Batch runs one at a time).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import resource
import shutil
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from psycopg2.extras import RealDictCursor

from rapidpipe.exitcodes import ExitCode
from rapidpipe.foundation.prefix import attempts, local_path, put
from rapidpipe.foundation.publish import publish

__all__ = ["JobContext", "local_path", "run_job"]


@dataclass(frozen=True)
class JobContext:
    """What a stage's ``run(job)`` is given.

    ``inputs`` are the job's recorded ``job_inputs`` rows as dicts (``kind``,
    ``product``, ``key``, ``source_job``, ``binding``), the authority for
    what the stage reads; ``settings`` is its run's settings body;
    ``connect()`` is a context manager yielding a connection for short
    reads, closed on exit. ``prefix`` is the
    attempt's URL and ``path`` the local directory the stage writes: the
    prefix itself for ``file://``, ``workdir`` for ``s3://``.
    """

    job: int
    run: int
    stage: str
    scope: dict
    survey: str
    dataset: str
    settings: dict
    inputs: list[dict]
    attempt: int
    prefix: str
    connect: Callable[[], Any]
    workdir: Path | None = None

    @property
    def path(self) -> Path:
        return self.workdir if self.workdir is not None else local_path(self.prefix)

    def write_manifest(self, outputs: Iterable[tuple[str, str]], **fields: Any) -> None:
        """Write ``manifest.json`` declaring each ``(table, relative path)`` with its checksum.

        ``fields`` are recorded beside ``outputs`` (an import's ``provenance``).
        """
        declared = []
        for table, relative in outputs:
            digest = hashlib.sha256()
            with (self.path / relative).open("rb") as stream:
                for block in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(block)
            declared.append({"table": table, "path": relative,
                             "checksum": f"sha256:{digest.hexdigest()}"})
        (self.path / "manifest.json").write_text(json.dumps({**fields, "outputs": declared}, indent=1))


def _claim_attempt(job_root: Path) -> int:
    """Create the next unused ``<job_root>/<n>`` directory and return ``n``."""
    job_root.mkdir(parents=True, exist_ok=True)
    attempt = 1 + max((int(p.name) for p in job_root.iterdir() if p.name.isdigit()), default=0)
    while True:
        try:
            (job_root / str(attempt)).mkdir()
            return attempt
        except FileExistsError:
            attempt += 1


def run_job(connect: Callable[[], Any], job_id: int, outputs_root: str,
            stage_run: Callable[[JobContext], None]) -> dict[str, Any]:
    """Run job ``job_id``'s next attempt with ``stage_run`` and publish it.

    ``connect()`` is a context manager yielding a connection
    (``rapidpipe.db.connection.connect``); the stage gets the same factory.

    Returns :func:`~rapidpipe.foundation.publish.publish`'s result with
    ``peak_rss_kib``: the peak resident memory of this process (``self``)
    and of its largest finished child process (``children``, the toolkit's
    binaries), in KiB. A job already published returns at once with
    ``published`` false and no file touched. The job row records
    ``running``, ``started``, the image digest (``RAPID_IMAGE_DIGEST``,
    which the job definition sets; none for a checkout) and the instance
    type (:func:`instance_type`) before the stage,
    ``ended`` and ``exit_code`` after it; a stage or publication that raises
    leaves the job ``failed`` and the exception propagates.
    """
    with connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT j.state, j.stage, j.scope, j.run, j.published_attempt, j.refusal,"
                        " r.settings, r.survey, r.dataset"
                        " FROM jobs j JOIN runs r ON r.id = j.run WHERE j.id = %s", (job_id,))
            job = cur.fetchone()
            if job is None:
                raise LookupError(f"job {job_id} does not exist")
            if job["state"] == "published":
                conn.commit()
                return {"job": job_id, "attempt": job["published_attempt"], "published": False,
                        "group": None, "refusal": job["refusal"], "live": None}
            cur.execute("SELECT kind, product, key, source_job, binding FROM job_inputs"
                        " WHERE job = %s ORDER BY kind, product, source_job", (job_id,))
            inputs = [dict(r) for r in cur.fetchall()]
        conn.commit()
        job_prefix = f"{outputs_root.rstrip('/')}/{job['run']}/{job['stage']}/{job_id}"
        if urlparse(job_prefix).scheme == "file":
            job_root = local_path(job_prefix)
            attempt = _claim_attempt(job_root)
            prefix, workdir = (job_root / str(attempt)).as_uri() + "/", None
        else:
            attempt = 1 + max(attempts(job_prefix), default=0)
            prefix = f"{job_prefix}/{attempt}/"
            workdir = Path(tempfile.mkdtemp(prefix=f"rapidpipe-job-{job_id}-{attempt}-"))
        _record(conn, job_id, "state = 'running', started = now(), ended = NULL, exit_code = NULL,"
                " image_digest = %s, instance_type = %s",
                (os.environ.get("RAPID_IMAGE_DIGEST"), instance_type()))

    context = JobContext(
        job=job_id, run=job["run"], stage=job["stage"], scope=job["scope"],
        survey=job["survey"], dataset=job["dataset"], settings=job["settings"],
        inputs=inputs, attempt=attempt, prefix=prefix, connect=connect, workdir=workdir)
    try:
        stage_run(context)  # no connection held while the stage computes
        put(context.path, prefix)
        if workdir is not None:
            shutil.rmtree(workdir)
        with connect() as conn:
            result = publish(conn, job_id, attempt, context.prefix)
            _record(conn, job_id, "ended = now(), exit_code = 0")
    except BaseException:
        with connect() as conn:
            _record(conn, job_id, "state = 'failed', ended = now(), exit_code = %s",
                    (int(ExitCode.STAGE_ERROR),))
        raise
    peak = {who: resource.getrusage(scope).ru_maxrss for who, scope in
            (("self", resource.RUSAGE_SELF), ("children", resource.RUSAGE_CHILDREN))}
    return {**result, "peak_rss_kib": peak}


#: The EC2 instance metadata service (IMDSv2): a session token, then one read.
_IMDS = "http://169.254.169.254/latest"


def instance_type(timeout: float = 1.0) -> str | None:
    """The EC2 instance type this process runs on, from IMDSv2; ``None`` when unreachable.

    One ``PUT`` for a token and one ``GET`` of ``meta-data/instance-type``,
    each with ``timeout`` seconds; off EC2, or in a container the metadata
    service's hop limit does not reach, either fails, the type is unknown and
    the error is logged.
    """
    try:
        token = urllib.request.urlopen(urllib.request.Request(
            f"{_IMDS}/api/token", method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"}), timeout=timeout).read()
        reply = urllib.request.urlopen(urllib.request.Request(
            f"{_IMDS}/meta-data/instance-type",
            headers={"X-aws-ec2-metadata-token": token.decode()}), timeout=timeout).read()
    except (OSError, ValueError) as exc:
        logging.getLogger(__name__).warning("instance type unknown: %s", exc)
        return None
    return reply.decode().strip() or None


def _record(conn, job_id: int, assignments: str, params: tuple = ()) -> None:
    """Update the job row's execution columns; a published job keeps its state."""
    conn.rollback()
    state_guard = " AND state <> 'published'" if "state" in assignments else ""
    with conn.cursor() as cur:
        cur.execute(f"UPDATE jobs SET {assignments} WHERE id = %s{state_guard}", (*params, job_id))
    conn.commit()
