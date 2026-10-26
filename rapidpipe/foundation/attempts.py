"""A job's Batch submissions, each recorded on its row, and the run's report of them.

``jobs.attempts`` is a JSON object keyed by Batch job id, one entry per
submission of the job (a Batch job may hold Batch's own retries; its entry
names the last). Three writers add to an entry, each its own fields:

- the submitter (``rapidpipe.launch.submit``): ``queue``, ``submitted``,
  ``why`` (``first`` or ``rerun``);
- the job itself (``rapidpipe.foundation.local.run_job``, in a Batch
  container): ``started``, ``instance_type``, ``availability_zone``,
  ``batch_attempt``, then ``ended`` and ``outcome`` (``published`` or
  ``failed``);
- the loop's reconcile, for a Batch job that ended with its row
  unpublished: ``stopped``, ``status``, ``status_reason``, ``exit_code``;
  for one it terminates as a straggler: ``hedged`` and ``status_reason``.

Times are ISO 8601 UTC strings. :func:`report` reads the entries of a run.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

#: Batch's status reason when EC2 took a Spot host back (``Host EC2 (instance i-...) terminated.``).
RECLAIM = "Host EC2"
#: The concurrency :func:`report` times the fill to (the Spot policy's reopen trigger,
#: rapid_systems ``docs/reference/spot-and-retry-policy.md``).
FILL_VCPUS = 1000


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def record(cur, job_id: int, batch_job_id: str, fields: dict[str, Any]) -> None:
    """Merge ``fields`` into the job's entry for ``batch_job_id`` (on the caller's transaction)."""
    cur.execute("UPDATE jobs SET attempts = attempts || jsonb_build_object(%s::text,"
                " coalesce(attempts -> %s, '{}'::jsonb) || %s::jsonb) WHERE id = %s",
                (batch_job_id, batch_job_id, json.dumps(fields), job_id))


def _t(text: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(text) if text else None


def report(entries: list[dict], spot_queue: str | None, vcpus: int = 1) -> dict:
    """A run's submissions measured: per queue, reclaims, hedges, lost job-hours, fill time.

    ``entries`` are every job's ``attempts`` values with ``job`` added. A
    reclaim is a Spot entry whose status reason is :data:`RECLAIM`'s; lost
    job-hours are the started-to-end hours of entries not ``published``;
    ``fill_seconds`` is from the first submission to the moment
    :data:`FILL_VCPUS` vCPU were running at once (``vcpus`` per job, the job
    definition's), ``None`` while that has not happened.
    """
    per_queue: dict[str, int] = {}
    reclaims = hedges = 0
    lost = 0.0
    edges = []
    for e in entries:
        queue = e.get("queue") or "unknown"
        per_queue[queue] = per_queue.get(queue, 0) + 1
        if spot_queue and queue == spot_queue and (e.get("status_reason") or "").startswith(RECLAIM):
            reclaims += 1
        if e.get("hedged"):
            hedges += 1
        started = _t(e.get("started"))
        end = _t(e.get("ended") or e.get("stopped") or e.get("hedged"))
        if started and end and e.get("outcome") != "published":
            lost += (end - started).total_seconds()
        if started:
            edges.append((started, vcpus))
            if end:
                edges.append((end, -vcpus))
    first = min((_t(e["submitted"]) for e in entries if e.get("submitted")), default=None)
    fill, running = None, 0
    for at, step in sorted(edges, key=lambda x: (x[0], x[1])):
        running += step
        if running >= FILL_VCPUS:
            fill = round((at - first).total_seconds()) if first else None
            break
    return {"submissions": per_queue, "reclaims": reclaims, "hedges": hedges,
            "lost_job_hours": round(lost / 3600, 3), "fill_seconds": fill, "fill_vcpus": FILL_VCPUS}


def run_entries(conn, run: int) -> list[dict]:
    """Every entry of the run's jobs, ``job`` and ``batch_job`` added."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, attempts FROM jobs WHERE run = %s AND attempts <> '{}'::jsonb", (run,))
        rows = cur.fetchall()
    conn.commit()
    return [{**entry, "job": job, "batch_job": bid} for job, attempts in rows
            for bid, entry in (attempts or {}).items()]
