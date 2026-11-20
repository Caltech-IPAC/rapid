"""Stopping a run, and cleaning up after a stopped one (``rapidpipe run stop``, ``run cleanup``).

:func:`stop` records the run as stopped (``runs.stopped``, with who and why)
and ends what it has on Batch. From that commit the run takes no further job:
every path that records, submits or starts one passes
:func:`rapidpipe.foundation.bookkeeping.admit`, whose share lock on the run's
row the stop's update waits for. So when the stop reads the run's jobs, every
submission made before it is on its row, and none follows. It then refuses the
jobs recorded and never submitted, and asks Batch to terminate every
submission Batch has not ended (``terminate_job`` cancels a queued job and
kills a started one). It does not wait for Batch: a second ``stop`` repeats
the request, harmlessly, and reports what is still active.

:func:`cleanup` is for a stopped run whose every job is shown to have ended
(:func:`not_ended`): each row ``published`` or ``failed``, and each Batch
submission either recorded as published by its own container or reported
ended by Batch, here or by an earlier pass of the loop. A job this cannot
show, a submission Batch no longer describes included, makes it refuse,
naming the job. It then lists the run's files: everything under
``<outputs root>/<run>/<stage>/<job>/<attempt>/`` for a job of the run with
that stage, except a published job's published attempt, which holds every
product a row or an official binding names. A file under the run's prefix
that is no job's attempt (``qa/``, a stage directory with another run's job
number) is counted as unrecognised and never removed. With ``delete`` each
attempt is checked again, under a lock on its job's row that the publisher
also takes, immediately before its files are removed; one that no longer
qualifies is kept and named.

The outputs root is a URL, ``file://`` or ``s3://``, as everywhere
(:mod:`rapidpipe.foundation.prefix`); ``client`` is a test's S3 client and
``batch_client`` a factory called only when a submission has to be read.
"""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlparse

from rapidpipe.foundation import attempts, loop
from rapidpipe.foundation.bookkeeping import stopped_message
from rapidpipe.foundation.official import NotFound, Refused
from rapidpipe.foundation.prefix import _bucket_key, local_path
from rapidpipe.products import storage

__all__ = ["cleanup", "not_ended", "stop"]

#: Batch's two final job states.
BATCH_ENDED = ("SUCCEEDED", "FAILED")


def stop(conn, run_id: int, who: str, why: str, batch_client: Callable[[], Any]) -> dict:
    """Stop run ``run_id``: record it, refuse its unsubmitted jobs, ask Batch to end the rest.

    Returns ``{"run", "stopped", "stopped_by", "stopped_why", "already"}`` (the
    first stop's record, ``already`` true when this call found the run stopped),
    ``refused`` (the ids of jobs recorded and never submitted or started, now
    ``failed``), ``terminated`` (``job``, ``stage``, ``batch_job`` and Batch's
    ``status`` for each submission asked to end), ``unknown`` (submissions with
    no recorded end that Batch does not describe) and ``running_here`` (jobs
    running outside Batch, which end by themselves or where they run).
    """
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE runs SET stopped = now(), stopped_by = %s, stopped_why = %s"
                        " WHERE id = %s AND stopped IS NULL", (who, why, run_id))
            already = cur.rowcount == 0
            cur.execute("SELECT stopped, stopped_by, stopped_why FROM runs WHERE id = %s", (run_id,))
            record = cur.fetchone()
            if record is None:
                raise NotFound(f"run {run_id} does not exist")
            cur.execute("UPDATE jobs SET state = 'failed', ended = now(), refusal = %s WHERE run = %s"
                        " AND state = 'launched' AND batch_job_id IS NULL AND attempts = '{}'::jsonb"
                        " RETURNING id", (f"Stopped: {stopped_message(run_id, *record)}", run_id))
            refused = sorted(r[0] for r in cur.fetchall())
            cur.execute("SELECT id FROM jobs WHERE run = %s AND state = 'running' AND attempts = '{}'::jsonb"
                        " ORDER BY id", (run_id,))
            running_here = [r[0] for r in cur.fetchall()]
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    active, unknown = _submissions(conn, run_id, batch_client)
    reason = f"run {run_id} stopped by {record[1]}: {record[2]}"[:250]
    client = batch_client() if active else None
    for item in active:
        client.terminate_job(jobId=item["batch_job"], reason=reason)
        with conn.cursor() as cur:
            attempts.record(cur, item["job"], item["batch_job"],
                            {"terminated": attempts.now(), "status_reason": reason})
        conn.commit()
    return {"run": run_id, "stopped": record[0], "stopped_by": record[1], "stopped_why": record[2],
            "already": already, "refused": refused, "terminated": active, "unknown": unknown,
            "running_here": running_here}


def _submissions(conn, run_id: int, batch_client: Callable[[], Any]) -> tuple[list[dict], list[dict]]:
    """The run's Batch submissions not shown ended, read from Batch: ``(active, unknown)``.

    A submission is shown ended when its container recorded ``published`` on its entry (it exits
    at once after) or Batch reported a final status, recorded on the entry here as the loop's
    reconcile records it. A container's own ``failed`` is no proof: Batch may start it again.
    ``active`` are those Batch reports in a state before its final two, ``unknown`` those it does
    not describe (it forgets a job about a week after its end; another account or region never
    knew it); each item is ``{"job", "stage", "batch_job", "status"}``.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id, stage, attempts FROM jobs WHERE run = %s AND attempts <> '{}'::jsonb"
                    " ORDER BY id", (run_id,))
        open_ = [{"job": job, "stage": stage, "batch_job": batch_id, "status": None}
                 for job, stage, entries in cur.fetchall() for batch_id, entry in sorted(entries.items())
                 if entry.get("outcome") != "published" and entry.get("status") not in BATCH_ENDED]
    conn.commit()
    if not open_:
        return [], []
    client, described = batch_client(), {}
    ids = [item["batch_job"] for item in open_]
    for i in range(0, len(ids), 100):
        described.update((b["jobId"], b) for b in client.describe_jobs(jobs=ids[i:i + 100])["jobs"])
    active, unknown = [], []
    for item in open_:
        found = described.get(item["batch_job"])
        if found is None:
            unknown.append(item)
        elif found["status"] in BATCH_ENDED:
            with conn.cursor() as cur:
                attempts.record(cur, item["job"], item["batch_job"], {
                    "stopped": loop._iso(found.get("stoppedAt")), "status": found["status"],
                    "status_reason": found.get("statusReason"),
                    "exit_code": found.get("container", {}).get("exitCode")})
            conn.commit()
        else:
            active.append({**item, "status": found["status"]})
    return active, unknown


def not_ended(conn, run_id: int, batch_client: Callable[[], Any]) -> list[dict]:
    """The run's jobs not shown to have ended, each ``{"job", "stage", "why"}``; empty when all have.

    First the loop's own reconcile, which marks ``failed`` a row whose latest Batch job ended
    without recording it; then every submission (:func:`_submissions`); then every row still
    ``launched`` or ``running``. ``why`` says what would end it.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM jobs WHERE run = %s AND state <> 'published' AND batch_job_id IS NOT NULL"
                    " LIMIT 1", (run_id,))
        on_batch = cur.fetchone() is not None
    conn.commit()
    if on_batch:
        loop.reconcile(conn, run_id, batch_client())
    active, unknown = _submissions(conn, run_id, batch_client)
    found = [{"job": i["job"], "stage": i["stage"],
              "why": f"its Batch job {i['batch_job']} is {i['status']}: 'rapidpipe run stop --run {run_id}'"
                     " asks Batch to end it again, and it takes Batch a minute or two"} for i in active]
    found += [{"job": i["job"], "stage": i["stage"],
               "why": f"its Batch job {i['batch_job']} has no recorded end and Batch does not describe it "
                      "(Batch forgets a job about a week after it ends, and never knew one of another "
                      "account or region: check the AWS identity and region this runs under)"}
              for i in unknown]
    named = {i["job"] for i in found}
    with conn.cursor() as cur:
        cur.execute("SELECT id, stage, state, attempts = '{}'::jsonb FROM jobs WHERE run = %s"
                    " AND state IN ('launched', 'running') ORDER BY id", (run_id,))
        for job, stage, state, local in cur.fetchall():
            if job in named:
                continue
            why = (f"its row is still '{state}' with no Batch job: it runs outside Batch and ends by "
                   f"itself, or is interrupted where it runs; if its process is gone, 'rapidpipe job run "
                   f"--job {job}' is refused under the stopped run and closes the row" if local else
                   f"its row is still '{state}' though its Batch jobs ended: 'rapidpipe job run --job "
                   f"{job}' is refused under the stopped run and closes the row")
            found.append({"job": job, "stage": stage, "why": why})
    conn.commit()
    return sorted(found, key=lambda i: i["job"])


def _list(prefix: str, client: Any = None) -> list[str]:
    """Every file under the URL ``prefix`` (ending ``/``), as paths relative to it."""
    if urlparse(prefix).scheme == "file":
        root = local_path(prefix)
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) \
            if root.is_dir() else []
    bucket, key = _bucket_key(prefix)
    s3 = client or storage.s3_client()
    found, token = [], None
    while True:
        page = s3.list_objects_v2(Bucket=bucket, Prefix=key,
                                  **({"ContinuationToken": token} if token else {}))
        found += [obj["Key"][len(key):] for obj in page.get("Contents", [])]
        if not page.get("IsTruncated"):
            return sorted(found)
        token = page["NextContinuationToken"]


def _remove(prefix: str, relatives: list[str], client: Any = None) -> list[str]:
    """Remove the named files under ``prefix``, and nothing else but the directories that leaves
    empty (``file://``); return those that could not be."""
    if urlparse(prefix).scheme == "file":
        root, failed = local_path(prefix), []
        for relative in relatives:
            try:
                (root / relative).unlink(missing_ok=True)
            except OSError:
                failed.append(relative)
        for directory in sorted({p for r in relatives for p in (root / r).parents if p == root or root in p.parents},
                                key=lambda p: len(p.parts), reverse=True):
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        return failed
    bucket, key = _bucket_key(prefix)
    s3, failed = client or storage.s3_client(), []
    for i in range(0, len(relatives), 1000):
        reply = s3.delete_objects(Bucket=bucket, Delete={
            "Objects": [{"Key": key + r} for r in relatives[i:i + 1000]], "Quiet": True})
        failed += [e["Key"][len(key):] for e in reply.get("Errors", [])]
    return failed


def cleanup(connect: Callable[[], Any], run_id: int, outputs_root: str, batch_client: Callable[[], Any],
            delete: bool = False, client: Any = None) -> dict:
    """List what a stopped, ended run left that no published job holds; with ``delete``, remove it.

    ``connect()`` is a context manager yielding a connection. Raises
    :class:`~rapidpipe.foundation.official.Refused` when the run is not stopped, when a job is not
    shown ended (each named with what would end it), or when the run's published jobs wrote under
    another root than ``outputs_root``.

    Returns ``{"run", "prefix", "attempts", "files", "protected", "unrecognised"}``: ``attempts`` one
    entry per attempt directory that qualifies (``job``, ``stage``, ``state``, ``attempt``, ``prefix``,
    ``files``), ``files`` their total, ``protected`` the published attempts and their file count, which
    are never listed, and ``unrecognised`` the count of files under the run's prefix that are no job's
    attempt, by their first path component. With ``delete``, also ``deleted`` (files removed), ``kept``
    (attempts that no longer qualified when their turn came, each with ``why``) and ``failed`` (files
    the storage refused to remove).
    """
    prefix = f"{outputs_root.rstrip('/')}/{run_id}/"
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT stopped FROM runs WHERE id = %s", (run_id,))
            run = cur.fetchone()
        conn.commit()
        if run is None:
            raise NotFound(f"run {run_id} does not exist")
        if run[0] is None:
            raise Refused(f"run {run_id} is not stopped: 'rapidpipe run stop --run {run_id} --why ...' "
                          "first, so that nothing more is written under it")
        waiting = not_ended(conn, run_id, batch_client)
        if waiting:
            raise Refused(f"{len(waiting)} job{'s' if len(waiting) != 1 else ''} of run {run_id} not shown "
                          "to have ended, nothing listed or removed: "
                          + "; ".join(f"job {i['job']} ({i['stage']}): {i['why']}" for i in waiting[:20])
                          + (f"; and {len(waiting) - 20} more" if len(waiting) > 20 else ""))
        with conn.cursor() as cur:
            cur.execute("SELECT id, stage, state, published_attempt, manifest ->> 'prefix' FROM jobs"
                        " WHERE run = %s", (run_id,))
            jobs = {r[0]: r[1:] for r in cur.fetchall()}
        conn.commit()
    elsewhere = sorted(job for job, (_, state, _, written) in jobs.items()
                       if state == "published" and not (written or "").startswith(prefix))
    if elsewhere:
        raise Refused(f"run {run_id}'s published job {elsewhere[0]} wrote under {jobs[elsewhere[0]][3]}, not "
                      f"under {prefix}: RAPIDPIPE_OUTPUTS_ROOT here is not the root the run wrote to")
    groups: dict[tuple[int, int], list[str]] = {}
    protected: dict[tuple[int, int], int] = {}
    unrecognised: dict[str, int] = {}
    for relative in _list(prefix, client):
        parts = relative.split("/")
        job = jobs.get(int(parts[1])) if len(parts) >= 4 and parts[1].isdigit() and parts[2].isdigit() else None
        if job is None or job[0] != parts[0]:
            unrecognised[parts[0]] = unrecognised.get(parts[0], 0) + 1
        elif job[1] == "published" and job[2] == int(parts[2]):
            protected[int(parts[1]), int(parts[2])] = protected.get((int(parts[1]), int(parts[2])), 0) + 1
        else:
            groups.setdefault((int(parts[1]), int(parts[2])), []).append(relative)
    listed = [{"job": job, "stage": jobs[job][0], "state": jobs[job][1], "attempt": attempt,
               "prefix": f"{prefix}{jobs[job][0]}/{job}/{attempt}/", "files": len(files)}
              for (job, attempt), files in sorted(groups.items())]
    result = {"run": run_id, "prefix": prefix, "attempts": listed, "files": sum(a["files"] for a in listed),
              "protected": {"attempts": len(protected), "files": sum(protected.values())},
              "unrecognised": unrecognised}
    if not delete:
        return result
    deleted, kept, failed = 0, [], []
    for (job, attempt), files in sorted(groups.items()):
        with connect() as conn:
            try:
                with conn.cursor() as cur:      # the publisher takes this row's lock before it publishes
                    cur.execute("SELECT j.stage, j.state, j.published_attempt, r.stopped FROM jobs j"
                                " JOIN runs r ON r.id = j.run WHERE j.id = %s AND j.run = %s FOR UPDATE OF j",
                                (job, run_id))
                    now = cur.fetchone()
                    why = ("its job is no longer a job of the run" if now is None or now[0] != jobs[job][0] else
                           "the run is no longer stopped" if now[3] is None else
                           f"its job is '{now[1]}', not ended" if now[1] not in loop.ENDED else
                           "it was published since the listing" if now[2] == attempt else None)
                    if why is None:
                        gone = _remove(prefix, files, client)
                        failed += gone
                        deleted += len(files) - len(gone)
                    else:
                        kept.append({"job": job, "attempt": attempt, "files": len(files), "why": why})
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
    return {**result, "deleted": deleted, "kept": kept, "failed": failed}
