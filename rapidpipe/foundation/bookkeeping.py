"""Runs and jobs: creating them, and showing them as dicts.

A run is a recipe someone launched for a reason (Part 1 statement 1); a
job is one execution of one stage under a run, with the inputs chosen for
it at launch recorded in ``job_inputs`` in two kinds (statement 4):

``inputs``
    the sets the job reads: dicts with ``product``, ``key`` and
    ``source_job`` (the set's job), optionally ``binding`` (the binding it
    was read through). What a stage reads at execution instead (the
    difference job's object catalog, the packet job's members and window,
    amendment 2026-10-24) the publisher records as more ``input`` rows from
    the manifest's ``reads``.
``expects``
    per output key, the open binding the job expects to replace: dicts
    with ``product``, ``key`` and ``binding`` (``None`` for none). The
    publisher binds exactly these keys.

Keys are dicts whose names are the product table's column names
(``schema.sql``'s header).
"""

from __future__ import annotations

from typing import Any, Iterable

from psycopg2.extras import Json, RealDictCursor

from rapidpipe.foundation.official import NotFound


def create_run(conn, who: str, why: str, revision: str, settings: dict,
               survey: str, dataset: str, layers: dict | None = None) -> int:
    """Record a run and return its id.

    ``layers`` is how ``settings`` was resolved (``rapidpipe.foundation.layers``),
    stored beside it; ``None`` for a body given whole.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO runs (who, why, revision, settings, layers, survey, dataset)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (who, why, revision, Json(settings), None if layers is None else Json(layers),
                 survey, dataset))
            run_id = cur.fetchone()[0]
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return run_id


def create_job(conn, run_id: int, stage: str, scope: dict,
               inputs: Iterable[dict] = (), expects: Iterable[dict] = ()) -> int:
    """Record a job in state ``launched`` with its inputs; return its id."""
    rows = (
        [("input", i["product"], i["key"], i.get("source_job"), i.get("binding")) for i in inputs]
        + [("expects", e["product"], e["key"], None, e["binding"]) for e in expects])
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO jobs (run, stage, scope) VALUES (%s, %s, %s) RETURNING id",
                        (run_id, stage, Json(scope)))
            job_id = cur.fetchone()[0]
            for kind, product, key, source_job, binding in rows:
                cur.execute(
                    "INSERT INTO job_inputs (job, kind, product, key, source_job, binding)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (job_id, kind, product, Json(key), source_job, binding))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return job_id


def run_show(conn, run_id: int) -> dict[str, Any]:
    """The run's row, its open live binding (``live``, or ``None``) and its jobs."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM runs WHERE id = %s", (run_id,))
        run = cur.fetchone()
        if run is None:
            raise NotFound(f"run {run_id} does not exist")
        cur.execute("SELECT id FROM official WHERE run = %s AND closed_at IS NULL", (run_id,))
        live = cur.fetchone()
        cur.execute("SELECT id, stage, state, published_attempt, refusal FROM jobs"
                    " WHERE run = %s ORDER BY id", (run_id,))
        jobs = [dict(r) for r in cur.fetchall()]
    return {**run, "live": live["id"] if live else None, "jobs": jobs}


def job_show(conn, job_id: int) -> dict[str, Any]:
    """The job's row, its recorded inputs and every binding of its rows, open or closed."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
        job = cur.fetchone()
        if job is None:
            raise NotFound(f"job {job_id} does not exist")
        cur.execute("SELECT kind, product, key, source_job, binding FROM job_inputs"
                    " WHERE job = %s ORDER BY kind, product", (job_id,))
        inputs = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM official WHERE job = %s ORDER BY id", (job_id,))
        bindings = [dict(r) for r in cur.fetchall()]
    return {**job, "inputs": inputs, "bindings": bindings}
