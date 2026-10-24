"""Helpers for the tests of rapidpipe.foundation (test_publish, test_change).

Not a test module. Writes an attempt's declared outputs as Parquet plus a
manifest under a ``file://`` prefix, records live runs, and watches for a
second connection to block on the first one's row lock.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from rapidpipe.foundation import change, create_job, create_run

SURVEY, DATASET = "S", "D"
FIELD_KEY = {"dataset": DATASET, "survey": SURVEY, "field": 1}


def write_attempt(root: Path, tables: dict[str, list[dict]]) -> str:
    """Write each table's rows as ``<table>.parquet`` and a manifest under ``root``; return its URL."""
    root.mkdir(parents=True)
    outputs = []
    for table, rows in tables.items():
        path = root / f"{table}.parquet"
        pq.write_table(pa.Table.from_pylist(rows), path)
        outputs.append({"table": table, "path": path.name,
                        "checksum": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()})
    (root / "manifest.json").write_text(json.dumps({"outputs": outputs}))
    return root.as_uri()


def objects_rows(n: int = 3) -> list[dict]:
    return [{"aid": aid, "dataset": DATASET, "survey": SURVEY, "field": 1,
             "ra0": 10.0 + aid, "dec0": -5.0} for aid in range(1, n + 1)]


def memberships_rows(source_job: int, n: int = 3) -> list[dict]:
    return [{"object_job": source_job, "aid": aid, "source_job": source_job, "sid": aid,
             "dra": 0.0, "ddec": 0.0, "flux": 100.0} for aid in range(1, n + 1)]


def new_run(conn, live: bool = False, why: str = "test") -> int:
    run = create_run(conn, "tester", why, "abc123", {"associate": {"radius": 1.0}}, SURVEY, DATASET)
    if live:
        change(conn, [{"open": {"survey": SURVEY, "dataset": DATASET, "run": run}}], "tester", "live")
    return run


def new_job(conn, run: int, expects=(), **inputs) -> int:
    return create_job(conn, run, "difference", {"field": 1}, expects=expects, **inputs)


def open_bindings(conn, **where) -> list[tuple]:
    """The open bindings as (id, product, key, job, run), optionally narrowed by product."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, product, key, job, run FROM official WHERE closed_at IS NULL"
                    " AND (%(product)s IS NULL OR product = %(product)s) ORDER BY id",
                    {"product": where.get("product")})
        rows = cur.fetchall()
    conn.commit()
    return rows


def wait_until_blocked(observer, pid: int, timeout: float = 10.0) -> None:
    """Return once backend ``pid`` is waiting on another backend's lock."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with observer.cursor() as cur:
            cur.execute("SELECT cardinality(pg_blocking_pids(%s))", (pid,))
            blocked = cur.fetchone()[0] > 0
        observer.commit()
        if blocked:
            return
        time.sleep(0.05)
    raise AssertionError(f"backend {pid} never blocked")
