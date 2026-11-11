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
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from psycopg2.extras import RealDictCursor

from rapidpipe.exitcodes import CommandExit, ExitCode
from rapidpipe.foundation import attempts as submissions
from rapidpipe.foundation import steps
from rapidpipe.foundation.official import NotFound
from rapidpipe.foundation.prefix import attempts, local_path, put
from rapidpipe.foundation.publish import FINAL, publish

__all__ = ["JobContext", "failure_reason", "local_path", "named_exit_code", "run_job"]


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

    def write_manifest(self, outputs: Iterable[tuple[str, str]], empty_ok: Iterable[str] = (),
                       **fields: Any) -> None:
        """Write ``manifest.json`` declaring each ``(table, relative path)`` with its checksum.

        ``empty_ok`` names the tables that may validly hold no rows (the
        publisher refuses to bind an expected product that loaded none
        otherwise); ``fields`` are recorded beside ``outputs`` (an import's
        ``provenance``).
        """
        declared = []
        for table, relative in outputs:
            digest = hashlib.sha256()
            with (self.path / relative).open("rb") as stream:
                for block in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(block)
            declared.append({"table": table, "path": relative,
                             "checksum": f"sha256:{digest.hexdigest()}"})
        if empty_ok:
            fields["empty_ok"] = sorted(empty_ok)
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


#: The longest reason a failed job's ``refusal`` records, in characters.
REFUSAL_CHARS = 500


def failure_reason(exc: BaseException) -> str:
    """``<Class>: <message>``, at most :data:`REFUSAL_CHARS` characters: what a failed job's row says."""
    return f"{type(exc).__name__}: {exc}"[:REFUSAL_CHARS]


def named_exit_code(exc: BaseException) -> int:
    """A :class:`~rapidpipe.exitcodes.CommandExit`'s code, 70 for anything else."""
    return exc.code if isinstance(exc, CommandExit) else int(ExitCode.STAGE_ERROR)


def run_job(connect: Callable[[], Any], job_id: int, outputs_root: str,
            stage_run: Callable[[JobContext], None],
            exit_code: Callable[[BaseException], int] = named_exit_code) -> dict[str, Any]:
    """Run job ``job_id``'s next attempt with ``stage_run`` and publish it.

    ``connect()`` is a context manager yielding a connection
    (``rapidpipe.db.connection.connect``); the stage gets the same factory.

    Returns :func:`~rapidpipe.foundation.publish.publish`'s result with
    ``peak_rss_kib``: the peak resident memory of this process (``self``)
    and of its largest finished child process (``children``, the toolkit's
    binaries), in KiB. Their sum as the stage ended is also written into the
    manifest's ``metrics`` as ``peak_rss_kib`` before publication, so
    ``job_metrics`` holds every job's, with the container's cgroup v2
    memory (:class:`CgroupMemory`): ``cgroup_anon_peak_bytes`` and
    ``cgroup_file_peak_bytes``, the largest ``memory.stat`` ``anon`` and
    ``file`` sampled through the stage, and ``cgroup_memory_peak_bytes``,
    ``memory.peak`` as the stage ended; each absent where its file is.
    Beside them, for the throughput report (throughput-metrics): the
    stage's named steps (:mod:`rapidpipe.foundation.steps`, ``step_<name>_s``
    and ``s3_read_bytes``), the upload of the attempt's files as
    ``step_s3_write_s`` and ``s3_write_bytes`` (``s3://`` only, the manifest
    itself not counted), ``startup_s`` (:func:`startup_seconds`: the process's
    start to this call, the imports before the job, outside
    ``container_seconds``), ``cgroup_cpu_s`` and ``cgroup_cpu_wall_s``
    (:class:`CgroupCpu`: the container's CPU seconds and the wall seconds of
    the same interval, from the stage's start to the manifest's upload) and
    ``host_vcpus`` (the CPUs this process may run on: Batch sets no cpuset,
    so the host's, never the job's reservation). A job already published, or declined (its stage
    ended with nothing to publish, :mod:`rapidpipe.foundation.publish`), returns at once with
    ``published`` false and no file touched; a declined one's result carries ``declined``. The job row records
    ``running``, ``started``, the image digest (``RAPID_IMAGE_DIGEST``,
    which the job definition sets; none for a checkout) and the instance
    type (:func:`instance_type`) before the stage, and its Batch attempt entry
    the instance id beside the type, clearing a reason an
    earlier attempt's failure left in ``refusal``,
    ``ended`` and ``exit_code`` after it; a stage or publication that raises
    leaves the job ``failed`` and the exception propagates. The row records
    ``exit_code(exc)``, the code the process will exit with (``rapidpipe
    job run`` passes its own classification, so a transient failure records
    75), and :func:`failure_reason` in ``refusal``: the exception's class and
    message, a :class:`~rapidpipe.exitcodes.CommandExit`'s included (a
    :class:`~rapidpipe.science.difference.tools.ToolFailed` names the tool,
    its exit code and its last output lines). When that write itself fails,
    the job id and the reason are logged and the stage's exception still
    propagates.
    """
    startup = startup_seconds()
    steps.reset()
    with connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT j.state, j.stage, j.scope, j.run, j.published_attempt, j.refusal,"
                        " r.settings, r.survey, r.dataset"
                        " FROM jobs j JOIN runs r ON r.id = j.run WHERE j.id = %s", (job_id,))
            job = cur.fetchone()
            if job is None:
                raise NotFound(f"job {job_id} does not exist")
            if job["state"] in FINAL:
                conn.commit()
                return {"job": job_id, "attempt": job["published_attempt"], "published": False,
                        "group": None, "refusal": job["refusal"], "live": None,
                        **({"declined": job["refusal"]} if job["state"] == "declined" else {})}
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
        machine = instance_type()
        _record(conn, job_id, "state = 'running', started = now(), ended = NULL, exit_code = NULL,"
                " refusal = NULL, image_digest = %s, instance_type = %s",
                (os.environ.get("RAPID_IMAGE_DIGEST"), machine))
        _record_attempt(conn, job_id, {"started": submissions.now(), "instance_type": machine,
                                       "instance_id": instance_id(),
                                       "availability_zone": availability_zone(),
                                       "batch_attempt": os.environ.get("AWS_BATCH_JOB_ATTEMPT")})

    context = JobContext(
        job=job_id, run=job["run"], stage=job["stage"], scope=job["scope"],
        survey=job["survey"], dataset=job["dataset"], settings=job["settings"],
        inputs=inputs, attempt=attempt, prefix=prefix, connect=connect, workdir=workdir)
    try:
        cpu = CgroupCpu()
        with CgroupMemory() as memory:
            stage_run(context)  # no connection held while the stage computes
        peak = _peak_rss_kib()
        manifest = context.path / "manifest.json"
        fixed = {"peak_rss_kib": peak["self"] + peak["children"], **memory.peaks(),
                 "host_vcpus": len(os.sched_getaffinity(0)),
                 **({"startup_s": round(startup, 3)} if startup is not None else {})}

        def record() -> None:
            _record_metrics(manifest, {**fixed, **steps.metrics(), **cpu.used()})

        if workdir is None:
            put(context.path, prefix, before_last=record)
        else:
            with steps.step("s3_write"):
                steps.add("s3_write_bytes", sum(f.stat().st_size for f in context.path.rglob("*")
                                                if f.is_file() and f != manifest))
                put(context.path, prefix, before_last=record)
        if workdir is not None:
            shutil.rmtree(workdir)
        with connect() as conn:
            result = publish(conn, job_id, attempt, context.prefix)
            _record(conn, job_id, "ended = now(), exit_code = 0")
            _record_attempt(conn, job_id, {"ended": submissions.now(),
                                           "outcome": "declined" if result.get("declined") else "published"})
    except BaseException as exc:
        code = exit_code(exc)
        reason = failure_reason(exc)
        try:
            with connect() as conn:
                _record(conn, job_id, "state = 'failed', ended = now(), exit_code = %s, refusal = %s",
                        (code, reason))
                _record_attempt(conn, job_id, {"ended": submissions.now(), "outcome": "failed", "exit_code": code})
        except Exception:  # noqa: BLE001 - the stage's own failure is the one to raise
            logging.getLogger(__name__).exception(
                "job %d: its failure (exit %d, %s) could not be recorded on its row", job_id, code, reason)
        raise
    return {**result, "peak_rss_kib": _peak_rss_kib()}


def _peak_rss_kib() -> dict[str, int]:
    """This process's peak resident memory and its largest finished child's, KiB."""
    return {who: resource.getrusage(scope).ru_maxrss for who, scope in
            (("self", resource.RUSAGE_SELF), ("children", resource.RUSAGE_CHILDREN))}


def _record_metrics(manifest: Path, metrics: dict[str, float]) -> None:
    """Add ``metrics`` (``peak_rss_kib``, the cgroup peaks, the steps) to the manifest's ``metrics``."""
    if not manifest.exists():
        return
    body = json.loads(manifest.read_text())
    body.setdefault("metrics", {}).update(metrics)
    manifest.write_text(json.dumps(body, indent=1))


#: The container's own cgroup v2 directory (Batch's containers see their own at the root).
CGROUP = Path("/sys/fs/cgroup")


class CgroupMemory:
    """The container cgroup's memory through a stage: ``memory.stat`` sampled every ``interval`` seconds.

    ``anon`` (the anonymous working set, what the ask must hold) and
    ``file`` (page cache, which the kernel reclaims under the limit) are
    current values, so a thread keeps their largest sample; a peak shorter
    than ``interval`` can be missed. ``memory.peak`` is the kernel's own
    high-water mark of their sum and more, read once at the end. Outside
    cgroup v2 (a laptop, cgroup v1) the files are absent and so are the peaks.
    """

    def __init__(self, root: Path | None = None, interval: float = 1.0):
        self.root, self.interval = root or CGROUP, interval
        self.anon = self.file = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample_until_stopped, daemon=True)

    def __enter__(self) -> "CgroupMemory":
        self.sample()
        self._thread.start()
        return self

    def __exit__(self, *exc) -> bool:
        self._stop.set()
        self._thread.join()
        self.sample()
        return False

    def _sample_until_stopped(self) -> None:
        while not self._stop.wait(self.interval):
            self.sample()

    def sample(self) -> None:
        """Read ``memory.stat`` once and keep the larger ``anon`` and ``file``."""
        try:
            fields = dict(line.split() for line in (self.root / "memory.stat").read_text().splitlines())
        except (OSError, ValueError):
            return
        for name in ("anon", "file"):
            if name in fields:
                setattr(self, name, max(int(fields[name]), getattr(self, name) or 0))

    def peaks(self) -> dict[str, int]:
        """The metrics this stage recorded: the anon and file peaks and ``memory.peak``, bytes."""
        out = {"cgroup_anon_peak_bytes": self.anon, "cgroup_file_peak_bytes": self.file}
        try:
            out["cgroup_memory_peak_bytes"] = int((self.root / "memory.peak").read_text())
        except (OSError, ValueError):
            pass
        return {k: v for k, v in out.items() if v is not None}


class CgroupCpu:
    """The container cgroup's CPU seconds from construction to :meth:`used`, beside the wall's.

    ``cpu.stat`` ``usage_usec`` counts every process in the container, the
    toolkit's binaries included. Outside cgroup v2 the file is absent and so
    are both numbers, never zero.
    """

    def __init__(self, root: Path | None = None):
        self.root = root or CGROUP
        self.start, self.wall = self._usage(), time.perf_counter()

    def _usage(self) -> int | None:
        try:
            fields = dict(line.split() for line in (self.root / "cpu.stat").read_text().splitlines())
            return int(fields["usage_usec"])
        except (OSError, ValueError, KeyError):
            return None

    def used(self) -> dict[str, float]:
        """``cgroup_cpu_s`` and ``cgroup_cpu_wall_s`` so far; empty where ``cpu.stat`` is not."""
        now = self._usage()
        if self.start is None or now is None:
            return {}
        return {"cgroup_cpu_s": round((now - self.start) / 1e6, 3),
                "cgroup_cpu_wall_s": round(time.perf_counter() - self.wall, 3)}


def startup_seconds(proc: Path = Path("/proc")) -> float | None:
    """Seconds from this process's start to now, from ``/proc``; ``None`` off Linux."""
    try:
        ticks = int((proc / "self" / "stat").read_text().rsplit(")", 1)[1].split()[19])
        uptime = float((proc / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return uptime - ticks / os.sysconf("SC_CLK_TCK")


#: The EC2 instance metadata service (IMDSv2): a session token, then one read.
_IMDS = "http://169.254.169.254/latest"


def instance_type(timeout: float = 1.0, item: str = "instance-type") -> str | None:
    """The EC2 instance type this process runs on, from IMDSv2; ``None`` when unreachable.

    One ``PUT`` for a token and one ``GET`` of ``meta-data/<item>``,
    each with ``timeout`` seconds; off EC2, or in a container the metadata
    service's hop limit does not reach, either fails, the value is unknown and
    the error is logged.
    """
    try:
        token = urllib.request.urlopen(urllib.request.Request(
            f"{_IMDS}/api/token", method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"}), timeout=timeout).read()
        reply = urllib.request.urlopen(urllib.request.Request(
            f"{_IMDS}/meta-data/{item}",
            headers={"X-aws-ec2-metadata-token": token.decode()}), timeout=timeout).read()
    except (OSError, ValueError) as exc:
        logging.getLogger(__name__).warning("%s unknown: %s", item, exc)
        return None
    return reply.decode().strip() or None


def availability_zone() -> str | None:
    """The availability zone this process runs in, read as :func:`instance_type` reads the type;
    only inside a Batch container (``AWS_BATCH_JOB_ID`` set), ``None`` elsewhere."""
    return instance_type(item="placement/availability-zone") if os.environ.get("AWS_BATCH_JOB_ID") else None


def instance_id() -> str | None:
    """The EC2 instance id, as :func:`availability_zone` reads the zone: the host the report groups
    co-tenants by; only inside a Batch container."""
    return instance_type(item="instance-id") if os.environ.get("AWS_BATCH_JOB_ID") else None


def _record_attempt(conn, job_id: int, fields: dict) -> None:
    """Add ``fields`` to this Batch job's entry in ``jobs.attempts``; nothing outside Batch."""
    batch_job_id = os.environ.get("AWS_BATCH_JOB_ID")
    if not batch_job_id:
        return
    conn.rollback()
    with conn.cursor() as cur:
        submissions.record(cur, job_id, batch_job_id, fields)
    conn.commit()


def _record(conn, job_id: int, assignments: str, params: tuple = ()) -> None:
    """Update the job row's execution columns; a published or declined job keeps its state."""
    conn.rollback()
    state_guard = " AND state NOT IN ('published', 'declined')" if "state" in assignments else ""
    with conn.cursor() as cur:
        cur.execute(f"UPDATE jobs SET {assignments} WHERE id = %s{state_guard}", (*params, job_id))
    conn.commit()
