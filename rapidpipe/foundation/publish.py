"""The publisher: load one completed attempt, then bind it if it may (Part 1 statement 6).

A stage writes its files under an immutable prefix that includes its job
id and attempt, and declares its catalog outputs in ``<prefix>/manifest.json``::

    {"outputs": [{"table": "objects", "path": "objects.parquet",
                  "checksum": "sha256:<64 hex digits>"}, ...]}

``table`` is one of :data:`PRODUCT_TABLES`; ``path`` is relative to the
prefix; ``checksum`` is the file's SHA-256 in the ``sha256:`` form. Each Parquet file's columns are the
table's column names, without ``job``, which the publisher sets.

Two optional manifest keys, written by a stage that reads at execution
what Part 1 statement 4 otherwise names at launch (amendment of
2026-10-24): ``reads``, a list of ``{"product", "key", "source_job"}``,
recorded as the job's ``input`` rows in ``job_inputs`` when it publishes
(the difference job's object catalogs, the packet job's members and
forced-photometry window); and ``catalog``, ``{"dataset", "survey",
"fields", "radius"}`` on a difference job, whose founded ``objects`` and
``memberships`` (and, under ``xcatalog``, its ``xobjects`` and
``xmemberships``, recorded as ``xreconcile``) are reconciled under the locks of those fields (each
field that gained an object or a membership and its neighbours) against
the objects founded in them since its read before they load (:mod:`rapidpipe.foundation.catalog`),
the outcome recorded in the stored manifest as ``reconcile``.

A stage that ends normally having made nothing to publish declines: its
manifest names no outputs and carries ``declined``, the reason (a
reference whose coverage map is short of ``[reference.selection]
min_coverage_fraction``). The publisher loads no rows and binds nothing:
it records the manifest, the reason in ``refusal`` and state ``declined``
(no ``published_attempt``), and a declined job, like a published one, is
never published again.

A third, ``empty_ok``, lists the tables a stage declares may validly load
zero rows (a difference's ``sources`` when nothing passes the cut). The
automatic binding is refused for a job that expects a product whose table
loaded no rows, a table its manifest does not declare included, unless
``empty_ok`` names it: the rows stay published and unbound, the refusal on
the job, as for any refused binding.

The tables of :data:`CHILDREN` keep their rows in child tables that
inherit them (``schema.sql``): those of :data:`DATE_SCA_CHILDREN` one per
UT observation date and SCA, as `dev`'s sources and xsources do, those of
:data:`FIELD_CHILDREN` one per field, the row's object's, as `dev`'s
astroobjects and merges are split. Before the transaction that loads a set,
the publisher makes each child its rows fall in, by ``create_catalog_child``
or ``create_field_child``, in a short transaction of its own, so the lock
inheriting takes on the parent is never held while a set loads; then it
loads each child by bulk ``COPY``, as `dev`'s loaders do
(``pipeline/loadSECatIntoDBSourcesTable.py``), where every other table
takes ``INSERT``. A membership the reconcile attaches to an object in a
field whose child does not yet exist gets that child made inside the
loading transaction. The fields a job's rows went to are recorded in
``job_fields`` with them, so a reader finds a job's children without a parent.

The prefix is a URL, ``file://`` or ``s3://``; each file is read through
:func:`rapidpipe.foundation.prefix.get`.

The publisher returns its own lock and hold timings as ``metrics``, which
:func:`rapidpipe.foundation.local.run_job` adds to the job's ``manifest``
``metrics`` in its write after the commit, so ``job_metrics`` carries them
(publish-lock-metrics): ``publish_lock_fields``, the fields it locked;
``publish_lock_tries``, the lock loop's tries, 1 when the first took
them; ``publish_lock_wait_s``, the loop's start to the locks taken, its
waits between tries included and the verification before it and the
loads below not; ``publish_load_sources_s`` and ``publish_load_xsources_s``,
the ``COPY`` of the tables of :data:`DATE_SCA_CHILDREN`, which no field
lock protects and so load in the publishing transaction before the locks
are taken (publish-hold-trim: 58.0 % of run 234's hold), summed over the
tries that loaded them; ``publish_hold_s``, the locks taken to the commit's return, when they are
released; and that hold split by the steps run under the locks, each
``publish_hold_<step>_s``: ``between`` (reading the objects founded since
the read), ``attach`` (the reconcile's match), ``load_<table>`` (each
other output's ``INSERT`` or ``COPY``), ``record`` (``job_fields``,
``job_inputs`` and the job row), ``bind`` (the automatic binding) and
``commit``, which sum to ``publish_hold_s`` but for the few statements
between them. Seconds are wall seconds of ``time.perf_counter``. A job
found published by the loop records none.
"""

from __future__ import annotations

import csv
import hashlib
import io
import itertools
import json
import logging
import math
import random
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pyarrow.parquet as pq
from psycopg2 import sql
from psycopg2.extras import Json, execute_values

from rapidpipe.foundation import catalog as object_catalog
from rapidpipe.foundation.official import NotFound, Refused, apply
from rapidpipe.foundation.prefix import get

#: The tables a manifest may declare outputs for.
PRODUCT_TABLES = frozenset({
    "exposures", "l2files", "refimages", "refimimages", "refimcatalogs", "diffimages",
    "psfs", "sources", "xsources", "objects", "memberships", "xobjects", "xmemberships", "packets",
    "forced", "external", "catalogs",
})

#: The product tables whose rows live in child tables, one per UT observation date
#: (``mjdobs``) and SCA, loaded by ``COPY``.
DATE_SCA_CHILDREN = frozenset({"sources", "xsources"})

#: The product tables whose rows live in child tables, one per field (the row's
#: object's), loaded by ``COPY``.
FIELD_CHILDREN = frozenset({"objects", "memberships", "xobjects", "xmemberships", "packets", "forced"})

#: Every product table whose rows live in child tables.
CHILDREN = DATE_SCA_CHILDREN | FIELD_CHILDREN

#: The longest and the first wait, seconds, before a publisher that found a field's lock held
#: tries again (full jitter, doubling): it waits with no pool connection, so the cap bounds
#: only how late it sees the lock free.
LOCK_RETRY_CAP_S = 10.0
LOCK_RETRY_INITIAL_S = 0.5


def lock_wait(tries: int) -> float:
    """Seconds to wait before try ``tries + 1`` of a field's lock: full jitter under a doubling
    capped at :data:`LOCK_RETRY_CAP_S`. The doubling stops at the cap, so a publisher that has
    tried a thousand times still waits (run 229: ``0.5 * 2 ** 1024`` overflowed a float and
    failed 945 finished differences after about 85 minutes of tries)."""
    doublings = min(tries, math.ceil(math.log2(LOCK_RETRY_CAP_S / LOCK_RETRY_INITIAL_S)))
    return random.uniform(0, min(LOCK_RETRY_CAP_S, LOCK_RETRY_INITIAL_S * 2 ** doublings))


#: The job states the publisher never publishes again: its rows loaded, or declined with none.
FINAL = ("published", "declined")


class PublishError(Exception):
    """An attempt whose manifest or declared outputs could not be verified; nothing was published."""


def publish(conn, job_id: int, attempt: int, prefix: str) -> dict[str, Any]:
    """Publish ``attempt`` of job ``job_id`` from ``prefix`` in one transaction.

    In order: if the job is already published, stop (a retry never
    touches a file); verify every declared output and make the child
    tables its rows need, and commit that; lock the job row and stop if
    it was published meanwhile; when no lock of the fields its catalogs
    name is held, load its ``sources`` and ``xsources`` into their
    children by ``COPY`` and take those locks, all or none; when any is
    held roll back, those loads with it, and try again after a jittered
    wait (never waiting on a lock while holding a connection); reconcile
    its catalogs; load each other output into its table, or its children
    by ``COPY``, with ``job`` set; record the attempt, manifest,
    state and ``ended`` (the commit's moment, which the caller moves to
    its own end); then, inside a savepoint, bind the job's ``expects``
    keys if its run is live for its survey and dataset, recording a
    refusal on the job instead when the binding is refused.

    It runs no ``ANALYZE``: autovacuum keeps the tables' statistics. Each
    ``ANALYZE`` takes its table's self-conflicting lock until its
    transaction ends, so a post-commit ``ANALYZE`` serialised every
    publisher behind the first table they shared (run 215: 794 sessions
    queued, 376 published differences held on their hosts to the attempt
    timeout); the stages' reads of a child plan the same with or without
    its statistics (publish-analyze's plan check).

    A manifest that carries ``declined`` stops after the job row's lock:
    the job is marked ``declined`` with its manifest and the reason in
    ``refusal``, and nothing is loaded or bound.

    Returns ``{"job", "attempt", "published", "group", "refusal", "live"}``:
    ``published`` is whether this call published, ``group`` the automatic
    binding's change group or ``None``, ``live`` whether the job's run was
    live when it bound (``None`` when the job expects nothing, or was
    already published); a call that published adds ``metrics``, its lock
    and hold timings (the module's docstring), seconds to 3 decimals. A declined job's adds
    ``declined``, the reason.
    """
    root = prefix.rstrip("/") + "/"
    manifest = rows = None
    children: dict[str, dict[Any, str]] = {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
            if row is None:
                raise NotFound(f"job {job_id} does not exist")
            if row[0] not in FINAL:
                manifest = _verified_manifest(root)
                rows = {output["table"]: _rows(get(root + output["path"]))
                        for output in manifest["outputs"]}
                children = _children(cur, rows)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    locked = sorted({f for key, _, _ in RECONCILED if key in (manifest or {})
                     for f in object_catalog.fields(manifest[key])})
    hold: dict[str, float] = {}
    unlocked: dict[str, float] = {}
    try:
        started = time.perf_counter()
        for tries in itertools.count():
            with conn.cursor() as cur:
                cur.execute("SELECT state, run, published_attempt, refusal FROM jobs"
                            " WHERE id = %s FOR UPDATE", (job_id,))
                state, run, published_attempt, refusal = cur.fetchone()
                if state in FINAL:
                    conn.commit()
                    return {"job": job_id, "attempt": published_attempt, "published": False,
                            "group": None, "refusal": refusal, "live": None,
                            **({"declined": refusal} if state == "declined" else {})}
                if manifest.get("declined"):
                    cur.execute("UPDATE jobs SET state = 'declined', manifest = %s, refusal = %s,"
                                " ended = clock_timestamp() WHERE id = %s",
                                (Json({"prefix": prefix, **manifest}), manifest["declined"], job_id))
                    conn.commit()
                    return {"job": job_id, "attempt": attempt, "published": False, "group": None,
                            "refusal": manifest["declined"], "live": None, "declined": manifest["declined"]}
                if not object_catalog.held(cur, run, locked):
                    for output in manifest["outputs"]:
                        table = output["table"]
                        if table in DATE_SCA_CHILDREN:
                            with _held(unlocked, f"load_{table}"):
                                _copy(cur, table, children.setdefault(table, {}), rows[table],
                                      job_id)
                    if object_catalog.try_lock(cur, run, locked):
                        break
            conn.rollback()
            time.sleep(lock_wait(tries))
        taken = time.perf_counter()
        if tries:
            logging.getLogger(__name__).info("job %d: the locks of its %d fields taken at try %d",
                                             job_id, len(locked), tries + 1)
        with conn.cursor() as cur:
            reads = manifest.get("reads", [])
            if "catalog" in manifest or "xcatalog" in manifest:
                _reconcile(cur, job_id, run, manifest, reads, rows, hold)
            tables = []
            for output in manifest["outputs"]:
                table = output["table"]
                tables.append(table)
                if table in DATE_SCA_CHILDREN:
                    continue
                with _held(hold, f"load_{table}"):
                    if table in CHILDREN:
                        _copy(cur, table, children.setdefault(table, {}), rows[table], job_id)
                    else:
                        _insert(cur, table, rows[table], job_id)
            recording = time.perf_counter()
            filed = sorted({(table, int(r["field"])) for table in FIELD_CHILDREN & set(tables)
                            for r in rows[table]})
            if filed:
                execute_values(cur, "INSERT INTO job_fields (job, parent, field) VALUES %s",
                               [(job_id, table, field) for table, field in filed])
            if reads:
                execute_values(cur, "INSERT INTO job_inputs (job, kind, product, key, source_job)"
                               " VALUES %s", [(job_id, "input", r["product"], Json(r["key"]),
                                               r["source_job"]) for r in reads])
            # ended with the state, so a host lost before the caller's own write (a Spot reclaim)
            # still leaves the published row an end; the caller's later write moves it to its own.
            cur.execute("UPDATE jobs SET state = 'published', published_attempt = %s,"
                        " manifest = %s, ended = clock_timestamp() WHERE id = %s",
                        (attempt, Json({"prefix": prefix, **manifest}), job_id))
            loaded = {table: len(r) for table, r in rows.items()}
            hold["record"] = time.perf_counter() - recording
            with _held(hold, "bind"):
                group, refusal, live = _bind(cur, job_id, run, loaded,
                                             set(manifest.get("empty_ok", [])))
        with _held(hold, "commit"):
            conn.commit()
    except BaseException:
        conn.rollback()
        raise
    released = time.perf_counter()
    metrics = {"publish_lock_fields": len(locked), "publish_lock_tries": tries + 1,
               "publish_lock_wait_s": taken - started - sum(unlocked.values()),
               "publish_hold_s": released - taken,
               **{f"publish_{name}_s": seconds for name, seconds in unlocked.items()},
               **{f"publish_hold_{name}_s": seconds for name, seconds in hold.items()}}
    return {"job": job_id, "attempt": attempt, "published": True, "group": group,
            "refusal": refusal, "live": live,
            "metrics": {k: round(v, 3) if k.endswith("_s") else v for k, v in metrics.items()}}


@contextmanager
def _held(hold: dict[str, float], name: str) -> Iterator[None]:
    """Add the block's wall seconds to ``hold[name]``."""
    start = time.perf_counter()
    try:
        yield
    finally:
        hold[name] = hold.get(name, 0.0) + time.perf_counter() - start


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


def _rows(path: Path) -> list[dict]:
    data = pq.read_table(path)
    if "job" in data.column_names:
        raise PublishError(f"{path} carries a job column; the publisher sets it")
    return data.to_pylist()


def _children(cur, rows: dict[str, list[dict]]) -> dict[str, dict[Any, str]]:
    """Make the child tables the rows of :data:`CHILDREN` fall in.

    Parameters
    ----------
    cur : psycopg2 cursor
        A cursor in the transaction that makes them, committed by the caller.
    rows : dict of str to list of dict
        The rows of each declared output, by table.

    Returns
    -------
    dict of str to dict
        For each such table with rows, its child key (:func:`_child_key`) to the child's name.
    """
    children: dict[str, dict[Any, str]] = {}
    for table in sorted(CHILDREN & rows.keys()):
        for key in sorted({_child_key(table, r) for r in rows[table]}):
            children.setdefault(table, {})[key] = _make_child(cur, table, key)
    return children


def _child_key(table: str, row: dict) -> Any:
    """``(floor(mjdobs), sca)`` for :data:`DATE_SCA_CHILDREN`, the field for :data:`FIELD_CHILDREN`."""
    if table in FIELD_CHILDREN:
        if row.get("field") is None:
            raise PublishError(f"a {table} row carries no field; its child cannot be named")
        return int(row["field"])
    return math.floor(row["mjdobs"]), int(row["sca"])


def _make_child(cur, table: str, key: Any) -> str:
    """Make (or find) the child of ``table`` for ``key`` and return its name."""
    if table in FIELD_CHILDREN:
        cur.execute("SELECT create_field_child(%s, %s)", (table, key))
    else:
        cur.execute("SELECT create_catalog_child(%s, %s, %s)", (table, float(key[0]), key[1]))
    return cur.fetchone()[0]


def _copy(cur, table: str, children: dict[Any, str], rows: list[dict], job_id: int) -> None:
    """Load rows into their child tables by ``COPY``, one statement per child.

    Parameters
    ----------
    cur : psycopg2 cursor
        A cursor in the publishing transaction.
    table : str
        One of :data:`CHILDREN`.
    children : dict
        Child key to the child's name, as made by ``_children``; a child a row
        needs that is not there (a membership the reconcile attached to an
        object in another field) is made in this transaction and added.
    rows : list of dict
        The table's rows, every one with the same columns, ``job`` not among them.
    job_id : int
        The publishing job, set on every row.

    Returns
    -------
    None
    """
    if not rows:
        return
    columns = list(rows[0])
    by_child: dict[str, list[dict]] = {}
    for r in rows:
        key = _child_key(table, r)
        if key not in children:
            children[key] = _make_child(cur, table, key)
        by_child.setdefault(children[key], []).append(r)
    for child, child_rows in sorted(by_child.items()):
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        for r in child_rows:
            # \N is NULL, so an empty string stays one; a float keeps every digit repr gives it.
            writer.writerow([*("\\N" if r[c] is None else repr(r[c]) if isinstance(r[c], float)
                               else r[c] for c in columns), job_id])
        buffer.seek(0)
        statement = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT csv, NULL '\\N')").format(
            sql.Identifier(child), sql.SQL(", ").join(map(sql.Identifier, [*columns, "job"])))
        cur.copy_expert(statement, buffer)


def _insert(cur, table: str, rows: list[dict], job_id: int) -> None:
    if not rows:
        return
    columns = list(rows[0])
    statement = sql.SQL("INSERT INTO {} ({}) VALUES %s").format(
        sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, [*columns, "job"])))
    execute_values(cur, statement, [(*(r[c] for c in columns), job_id) for r in rows],
                   page_size=1000)


#: The manifest keys of the catalogs a difference job reconciles, the objects table each names,
#: and the key its outcome is recorded under.
RECONCILED = (("catalog", "objects", "reconcile"), ("xcatalog", "xobjects", "xreconcile"))


def _reconcile(cur, job_id: int, run: int, manifest: dict, reads: list[dict],
               rows: dict[str, list[dict]], hold: dict[str, float]) -> None:
    """Attach the job's founded objects and xobjects to those founded since its read.

    Called holding the locks of every field either catalog names
    (:func:`publish` takes them all at once); each catalog's outcome is
    recorded on the manifest (``reconcile``, ``xreconcile``), and the
    seconds of its read and its match added to ``hold`` (``between``, ``attach``).
    """
    present = [(key, table, out) for key, table, out in RECONCILED if key in manifest]
    for key, table, out in present:
        objects_table, memberships_table, ident, sid = object_catalog.CATALOGS[table]
        read_jobs = [r["source_job"] for r in reads if r["product"] == table]
        with _held(hold, "between"):
            others = object_catalog.between(cur, job_id, run, manifest[key], read_jobs, table)
        with _held(hold, "attach"):
            rows[objects_table], rows[memberships_table], manifest[out] = object_catalog.attach(
                job_id, rows.get(objects_table, []), rows.get(memberships_table, []), others,
                float(manifest[key]["radius"]), ident, sid)


def _bind(cur, job_id: int, run: int, loaded: dict[str, int],
          empty_ok: set[str]) -> tuple[int | None, str | None, bool | None]:
    """The automatic binding, inside a savepoint; returns (group id, refusal, run live).

    ``loaded`` is the rows each table loaded; an expected product with none,
    not in ``empty_ok``, refuses the binding.
    """
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
        empty = sorted({product for product, _, _ in expects
                        if not loaded.get(product) and product not in empty_ok})
        if empty:
            raise Refused(f"job {job_id} loaded no {', '.join(empty)} rows and its stage"
                          " did not declare them empty_ok")
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
