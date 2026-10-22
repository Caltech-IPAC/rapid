"""The publisher: load one completed attempt, then bind it if it may (Part 1 statement 6).

A stage writes its files under an immutable prefix that includes its job
id and attempt, and declares its catalog outputs in ``<prefix>/manifest.json``::

    {"outputs": [{"table": "objects", "path": "objects.parquet",
                  "checksum": "sha256:<64 hex digits>"}, ...]}

``table`` is one of :data:`PRODUCT_TABLES`; ``path`` is relative to the
prefix; ``checksum`` is the file's SHA-256 in the ``sha256:`` form of
``rapidpipe.products.manifest``. Each Parquet file's columns are the
table's column names, without ``job``, which the publisher sets.

The prefix is a URL, ``file://`` or ``s3://``; each file is read through
:func:`rapidpipe.foundation.prefix.get`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from psycopg2 import sql
from psycopg2.extras import Json, execute_values

from rapidpipe.foundation.official import Refused, apply
from rapidpipe.foundation.prefix import get

#: The tables a manifest may declare outputs for.
PRODUCT_TABLES = frozenset({
    "exposures", "l2files", "refimages", "refimimages", "refimcatalogs", "diffimages",
    "psfs", "sources", "objects", "memberships", "packets", "forced", "external", "catalogs",
})


class PublishError(Exception):
    """An attempt whose manifest or declared outputs could not be verified; nothing was published."""


def publish(conn, job_id: int, attempt: int, prefix: str) -> dict[str, Any]:
    """Publish ``attempt`` of job ``job_id`` from ``prefix`` in one transaction.

    In order: lock the job row; if the job is already published, stop
    (a retry never touches a file); verify every declared output; load
    each into its table with ``job`` set; record the attempt, manifest and
    state; then, inside a savepoint, bind the job's ``expects`` keys if its
    run is live for its survey and dataset, recording a refusal on the job
    instead when the binding is refused. ``ANALYZE`` every table it wrote
    to after commit: the loaded tables, and ``official`` when it bound.

    Returns ``{"job", "attempt", "published", "group", "refusal", "live"}``:
    ``published`` is whether this call published, ``group`` the automatic
    binding's change group or ``None``, ``live`` whether the job's run was
    live when it bound (``None`` when the job expects nothing, or was
    already published).
    """
    root = prefix.rstrip("/") + "/"
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT state, run, published_attempt, refusal FROM jobs"
                        " WHERE id = %s FOR UPDATE", (job_id,))
            row = cur.fetchone()
            if row is None:
                raise LookupError(f"job {job_id} does not exist")
            state, run, published_attempt, refusal = row
            if state == "published":
                conn.commit()
                return {"job": job_id, "attempt": published_attempt, "published": False,
                        "group": None, "refusal": refusal, "live": None}
            manifest = _verified_manifest(root)
            tables = []
            for output in manifest["outputs"]:
                _load(cur, output["table"], get(root + output["path"]), job_id)
                tables.append(output["table"])
            cur.execute("UPDATE jobs SET state = 'published', published_attempt = %s,"
                        " manifest = %s WHERE id = %s",
                        (attempt, Json({"prefix": prefix, **manifest}), job_id))
            group, refusal, live = _bind(cur, job_id, run)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    with conn.cursor() as cur:
        for table in sorted(set(tables) | ({"official"} if group is not None else set())):
            cur.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(table)))
    conn.commit()
    return {"job": job_id, "attempt": attempt, "published": True, "group": group,
            "refusal": refusal, "live": live}


def _verified_manifest(root: str) -> dict:
    try:
        manifest = json.loads(get(root + "manifest.json").read_text())
    except FileNotFoundError:
        raise PublishError(f"{root}manifest.json does not exist") from None
    for output in manifest["outputs"]:
        if output["table"] not in PRODUCT_TABLES:
            raise PublishError(f"{output['table']!r} is not a product table")
        try:
            path = get(root + output["path"])
        except FileNotFoundError:
            path = None
        if path is None or not path.is_file():
            raise PublishError(f"declared output {root}{output['path']} does not exist")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
        if output["checksum"] != f"sha256:{digest.hexdigest()}":
            raise PublishError(f"declared output {path} does not match its checksum")
    return manifest


def _load(cur, table: str, path: Path, job_id: int) -> None:
    data = pq.read_table(path)
    if "job" in data.column_names:
        raise PublishError(f"{path} carries a job column; the publisher sets it")
    columns = data.column_names
    rows = [(*values, job_id) for values in zip(*(data.column(c).to_pylist() for c in columns))]
    if not rows:
        return
    statement = sql.SQL("INSERT INTO {} ({}) VALUES %s").format(
        sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, [*columns, "job"])))
    execute_values(cur, statement, rows, page_size=1000)


def _bind(cur, job_id: int, run: int) -> tuple[int | None, str | None, bool | None]:
    """The automatic binding, inside a savepoint; returns (group id, refusal, run live)."""
    cur.execute("SELECT product, key, binding FROM job_inputs"
                " WHERE job = %s AND kind = 'expects' ORDER BY product", (job_id,))
    expects = cur.fetchall()
    if not expects:
        return None, None, None
    cur.execute("SAVEPOINT automatic_binding")
    cur.execute("SELECT 1 FROM official o JOIN runs r ON r.id = o.run"
                " AND r.survey = o.survey AND r.dataset = o.dataset"
                " WHERE o.run = %s AND o.closed_at IS NULL", (run,))
    live = cur.fetchone() is not None
    try:
        if not live:
            raise Refused(f"run {run} is not live for its survey and dataset")
        group = [{"close": binding, "open": {"product": product, "key": key, "job": job_id}}
                 for product, key, binding in expects]
        group_id = apply(cur, group, "publisher", f"automatic binding of job {job_id}")
    except Refused as refused:
        cur.execute("ROLLBACK TO SAVEPOINT automatic_binding")
        cur.execute("UPDATE jobs SET refusal = %s WHERE id = %s", (str(refused), job_id))
        return None, str(refused), live
    cur.execute("RELEASE SAVEPOINT automatic_binding")
    return group_id, None, live
