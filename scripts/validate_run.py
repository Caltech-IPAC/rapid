"""Check that one run is one coherent account in the tables, the Parquet and the outbox.

Outside the pipeline, like ``read_packets.py``: it reads the database and
the job prefixes, never writes, prints one line per check (``PASS`` with
its counts, or ``FAIL`` with the first counter-example) and exits non-zero
on any failure. The checks are the 2026-10-21 night's (``docs/
foundation-smdc-step.md``, Validation), one function each, scoped to the
jobs of ``--run``:

- ``lineage``: every row of the run's jobs, every ``input`` row they
  recorded (those a stage recorded at its read included) and every binding naming them resolves to
  a published job's row of its key; every ``expects`` binding names its
  product and key.
- ``tables``: every published job's declared Parquet outputs hold as many
  rows as its table rows, match their manifest checksums, and no table holds
  rows of the job that it did not declare.
- ``catalog``: the object catalog's contract (plan amendment 2026-10-24):
  every official source of the run that the association accepts (its
  ``[associate]`` flags and S/N), wherever on its image it falls, is a
  member of an object in its own field or a neighbouring one (the field
  rule, runbook part 12 step 1); every membership's source is within the match radius of
  its object's founding position; no two objects of the run founded by
  different jobs are within the radius of each other, in one field or
  either side of a boundary (no duplicate founders); ``memberships_official`` holds no membership of a source set
  that is not official; every ``objects_official`` row's sums are its
  official members' aggregated directly.
- ``packets``: every packet set of the run decodes, by its index, to the
  records of its sid, object and trigger, its source set is its recorded
  input and its trigger a member of the object, every history detection
  precedes the trigger, ``nDiaSources`` counts the object's members in the
  source sets the job recorded reading, at or before the trigger, every cutout
  opens at the run's stamp size and every forced entry is its set's
  ``forced.parquet`` row; then ``read_packets.py`` on the bound sets.
- ``forced``: a packet set's forced rows of an (object, image) pair that a
  recorded predecessor set also holds are a predecessor's measurement,
  value for value, ``time_proc`` included: reused, never re-measured. Two
  packet sets in flight at once each measure a pair neither could read
  (runs 72 and 73, 2026-10-24), so a later set may record both and copies
  one: it must equal one of them. (The
  night's check took every official set as one measurement; a run may also
  hold a replacement set, which measures afresh what it replaces.)
- ``twice`` (only with ``--twice A,B``): two executions of one key are both
  published with equal source rows but for job and pid, and at most one is
  bound.
- ``counts``: no job of the run is still launched or running; the jobs per
  stage and state, the rows per table and the open bindings are printed.
  With ``--scas``, a pending job whose scope names another SCA is a second
  loop's under the same run and is reported, not counted (two loops on one
  run raced this check, 2026-10-24); a job naming no SCA always counts.

The night's checks 7 to 10 (one group makes a night official, a
replacement, undo, two groups at once) are operations, checked as they
ran; they have no read-only form over a run and are not here.

``tables`` and ``packets`` check their jobs and sets eight at a time
(:data:`WORKERS`), each worker thread on its own connection, and reduce the
results in job order; a set's decoded records are dropped once it is
checked. The checks themselves are the serial ones: every Parquet output is
read and hashed, every packet decoded.

Usage::

    python scripts/validate_run.py --run N [--only lineage,catalog] [--twice A,B]

with the database named as for every rapidpipe command
(``rapidpipe.db.connection``: the ``PG*`` variables, else
``RAPID_PARAMETER_PATH``).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fastavro
import numpy as np
import psycopg2.extras
import pyarrow.parquet as pq
from astropy.io import fits

from rapidpipe.db.connection import connect
from rapidpipe.foundation.prefix import get
from rapidpipe.products import storage
from rapidpipe.products.diffimage import INFORMATIONAL_SOURCE_FLAGS
from rapidpipe.products.spatial import within
from rapidpipe.science.spatial import field_neighbours

sys.path.insert(0, str(Path(__file__).resolve().parent))
import read_packets as rpk  # noqa: E402 - a sibling script, not a package

KEYS = {
    "exposures": ("dataset", "expid"),
    "l2files": ("dataset", "expid", "sca", "fid"),
    "diffimages": ("dataset", "expid", "sca", "fid"),
    "sources": ("dataset", "expid", "sca", "fid"),
    "packets": ("dataset", "expid", "sca", "fid"),
    "refimages": ("dataset", "field", "fid"),
    "psfs": ("dataset", "fid", "sca", "kind"),
    "objects": ("dataset", "survey", "field"),
    "catalogs": ("dataset", "name", "version"),
}
TABLES = ("exposures", "l2files", "refimages", "refimimages", "refimcatalogs", "diffimages", "psfs",
          "sources", "objects", "memberships", "packets", "forced", "external", "catalogs")
#: the child tables' other columns naming a job
JOB_COLUMNS = {"memberships": ("source_job", "object_job"), "packets": ("object_job", "source_job"),
               "external": ("source_job", "catalog_job")}
FORCED_COMPARED = ("forced_id", "psf_flux", "psf_fluxerr", "science_flux", "science_fluxerr", "flags",
                   "time_proc", "x", "y", "ra", "dec", "pid")

#: Jobs or packet sets checked at once by ``tables`` and ``packets``.
WORKERS = 8

_local = threading.local()
_opening = threading.Lock()
_opened = contextlib.ExitStack()      # every thread's connection, closed when main returns
_s3 = None


def q(sql: str, params=()) -> list[dict]:
    """Rows of ``sql`` on this thread's connection, opened on first use."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        with _opening:                  # connect() reads its secret through boto3, whose session is not thread-safe
            conn = _local.conn = _opened.enter_context(connect(application_name="validate_run"))
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall() if cur.description else []
    conn.commit()
    return [dict(r) for r in rows]


def fetch(url: str) -> Path:
    """``prefix.get`` through the one S3 client every thread shares (boto3 clients are thread-safe,
    creating them is not)."""
    return get(url, client=_s3)


def each(check, items: list) -> list:
    """``check`` over ``items``, :data:`WORKERS` at a time, the results in ``items``' order."""
    with ThreadPoolExecutor(WORKERS) as pool:
        return list(pool.map(check, items))


def keyed(product: str, alias: str = "t") -> str:
    return "jsonb_build_object(" + ", ".join(f"'{c}', {alias}.{c}" for c in KEYS[product]) + ")"


def report(name: str, failures: list[str], detail: str, t0: float) -> bool:
    verdict = "PASS" if not failures else f"FAIL ({len(failures)})"
    print(f"{name}: {verdict} in {time.perf_counter() - t0:.1f} s: {failures[0] if failures else detail}",
          flush=True)
    return not failures


def read_output(job: int, table: str):
    (row,) = q("SELECT manifest FROM jobs WHERE id = %s", (job,))
    prefix = row["manifest"]["prefix"].rstrip("/") + "/"
    (out,) = [o for o in row["manifest"]["outputs"] if o["table"] == table]
    return pq.read_table(fetch(prefix + out["path"]))


def run_jobs(run: int, stage: str | None = None) -> list[int]:
    return [r["id"] for r in q("SELECT id FROM jobs WHERE run = %s AND state = 'published'"
                                + (" AND stage = %s" if stage else "") + " ORDER BY id",
                                (run, stage) if stage else (run,))]


def check_lineage(run: int) -> bool:
    t0, bad = time.perf_counter(), []
    for table in TABLES:
        for col in ("job", *JOB_COLUMNS.get(table, ())):
            (r,) = q(f"SELECT count(*) n FROM {table} t JOIN jobs rj ON rj.id = t.job AND rj.run = %s"
                     f" LEFT JOIN jobs j ON j.id = t.{col} WHERE j.state IS DISTINCT FROM 'published'",
                     (run,))
            if r["n"]:
                bad.append(f"{table}.{col}: {r['n']} rows of the run naming an unpublished job")
    n_inputs = 0
    for product in KEYS:
        rows = q(f"""SELECT i.job, i.kind, i.key, i.source_job FROM job_inputs i JOIN jobs rj ON rj.id = i.job
                     WHERE rj.run = %s AND i.kind = 'input' AND i.product = %s
                       AND NOT EXISTS (SELECT 1 FROM {product} t JOIN jobs sj ON sj.id = t.job
                                       AND sj.state = 'published'
                                       WHERE t.job = i.source_job AND i.key = {keyed(product)})""",
                 (run, product))
        bad += [f"job {r['job']} {r['kind']} {product} {r['key']} from job {r['source_job']}:"
                " no published row" for r in rows]
        (r,) = q("SELECT count(*) n FROM job_inputs i JOIN jobs rj ON rj.id = i.job WHERE rj.run = %s"
                 " AND i.kind = 'input' AND i.product = %s", (run, product))
        n_inputs += r["n"]
    (r,) = q("SELECT count(*) n FROM job_inputs i JOIN jobs rj ON rj.id = i.job WHERE rj.run = %s"
             " AND i.kind = 'input' AND NOT i.product = ANY(%s)", (run, list(KEYS)))
    if r["n"]:
        bad.append(f"{r['n']} input rows of an unknown product")
    rows = q("""SELECT i.job, i.product, i.key, i.binding FROM job_inputs i JOIN jobs rj ON rj.id = i.job
                LEFT JOIN official o ON o.id = i.binding
                WHERE rj.run = %s AND i.kind = 'expects' AND i.binding IS NOT NULL
                  AND (o.id IS NULL OR o.product <> i.product OR o.key <> i.key)""", (run,))
    bad += [f"job {r['job']} expects binding {r['binding']}, which is not {r['product']} {r['key']}"
            for r in rows]
    n_bindings = 0
    for product in KEYS:
        rows = q(f"""SELECT o.id, o.job, o.key FROM official o JOIN jobs rj ON rj.id = o.job
                     WHERE rj.run = %s AND o.product = %s AND NOT EXISTS (
                       SELECT 1 FROM {product} t JOIN jobs j ON j.id = t.job AND j.state = 'published'
                       WHERE t.job = o.job AND o.key = {keyed(product)})""", (run, product))
        bad += [f"binding {r['id']}: {product} {r['key']} of job {r['job']} has no published row"
                for r in rows]
    (r,) = q("SELECT count(*) n FROM official o JOIN jobs j ON j.id = o.job WHERE j.run = %s", (run,))
    n_bindings = r["n"]
    (r,) = q("SELECT count(*) n FROM official o JOIN jobs j ON j.id = o.job WHERE j.run = %s"
             " AND NOT o.product = ANY(%s)", (run, list(KEYS)))
    if r["n"]:
        bad.append(f"{r['n']} bindings of an unknown product")
    return report("lineage", bad, f"0 orphans: {n_inputs} input rows and {n_bindings}"
                  " bindings resolve to published rows", t0)


def job_table_failures(j: dict) -> tuple[list[str], int, int]:
    """One published job's declared outputs against its manifest and its table rows."""
    bad, n_outputs, n_rows = [], 0, 0
    prefix = j["manifest"]["prefix"].rstrip("/") + "/"
    per_table, parquet = Counter(), {}
    for out in j["manifest"]["outputs"]:
        path = fetch(prefix + out["path"])
        per_table[out["table"]] += pq.read_metadata(path).num_rows
        parquet[out["table"]] = path
        n_outputs += 1
        if out["checksum"] != "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest():
            bad.append(f"job {j['id']} {out['path']}: the checksum differs from the manifest's")
    reconcile = j["manifest"].get("reconcile")
    if reconcile and reconcile["reconciled"]:
        # Founded objects the publisher attached elsewhere are not rows; their memberships moved.
        stored = {r["aid"] for r in q("SELECT aid FROM objects WHERE job = %s", (j["id"],))}
        gone = set(pq.read_table(parquet["objects"], columns=["aid"]).column("aid").to_pylist()) - stored
        members = pq.read_table(parquet["memberships"], columns=["object_job", "aid"]).to_pylist()
        moved = sum(1 for m in members if m["object_job"] == j["id"] and m["aid"] in gone)
        if len(gone) != reconcile["reconciled"]:
            bad.append(f"job {j['id']}: {len(gone)} founded objects not stored, reconcile says"
                       f" {reconcile['reconciled']}")
        per_table["objects"] -= len(gone)
        per_table["memberships"] += reconcile["attached_memberships"] - moved
    for table in TABLES:
        (r,) = q(f"SELECT count(*) n FROM {table} WHERE job = %s", (j["id"],))
        if table in per_table and r["n"] != per_table[table]:
            bad.append(f"job {j['id']} {table}: Parquet {per_table[table]} rows, table {r['n']}")
        elif table not in per_table and r["n"]:
            bad.append(f"job {j['id']} {table}: {r['n']} rows and no declared output")
        n_rows += r["n"]
    return bad, n_outputs, n_rows


def check_tables(run: int) -> bool:
    t0, bad, n_outputs, n_rows = time.perf_counter(), [], 0, 0
    jobs = q("SELECT id, manifest FROM jobs WHERE run = %s AND state = 'published' ORDER BY id", (run,))
    for b, n_out, n_row in each(job_table_failures, jobs):
        bad += b
        n_outputs += n_out
        n_rows += n_row
    return report("tables", bad, f"{len(jobs)} published jobs, {n_outputs} declared outputs and"
                  f" {n_rows} rows: counts equal, checksums match", t0)


def check_catalog(run: int) -> bool:
    t0, bad = time.perf_counter(), []
    (row,) = q("SELECT settings -> 'associate' AS a FROM runs WHERE id = %s", (run,))
    radius, flags, snr = (float(row["a"]["match_radius"]), int(row["a"]["source_flags"]),
                          float(row["a"]["min_snr"]))
    unmatched = q("""SELECT s.job, s.sid FROM sources_official s
                     WHERE s.run = %s AND s.flags & ~%s = %s
                     AND s.fluxerr > 0 AND s.fluxfit / s.fluxerr >= %s
                     AND NOT EXISTS (SELECT 1 FROM memberships m WHERE m.job = s.job AND m.sid = s.sid)""",
                  (run, INFORMATIONAL_SOURCE_FLAGS, flags, snr))
    bad += [f"source ({r['job']}, {r['sid']}) is accepted and a member of nothing" for r in unmatched[:5]]
    members = q("""SELECT m.job, m.sid, m.object_job, m.aid, s.ra, s.dec, s.field, o.ra0, o.dec0,
                          o.field AS object_field
                   FROM memberships m JOIN jobs j ON j.id = m.job AND j.run = %s
                   JOIN sources s ON s.job = m.source_job AND s.sid = m.sid
                   JOIN objects o ON o.job = m.object_job AND o.aid = m.aid""", (run,))
    far = [m for m in members if not within(m["ra"], m["dec"], m["ra0"], m["dec0"], radius)]
    bad += [f"membership ({m['job']}, {m['sid']}) of object ({m['object_job']}, {m['aid']}) is outside"
            " the radius" for m in far[:5]]
    near = {f: {f, *field_neighbours(f)} for f in {m["field"] for m in members}}
    astray = [m for m in members if m["object_field"] not in near[m["field"]]]
    bad += [f"membership ({m['job']}, {m['sid']}) in field {m['field']} is of object"
            f" ({m['object_job']}, {m['aid']}) in field {m['object_field']}, not it or a neighbour"
            for m in astray[:5]]
    objects = q("SELECT o.job, o.aid, o.field, o.ra0, o.dec0 FROM objects o JOIN jobs j ON j.id = o.job"
                " WHERE j.run = %s ORDER BY o.ra0", (run,))
    duplicates = 0
    for i, a in enumerate(objects):     # sorted by ra0: a pair within the radius is within it in RA too
        for b in objects[i + 1:]:
            if b["ra0"] - a["ra0"] > radius / max(math.cos(math.radians(abs(a["dec0"]) + radius)), 1e-9):
                break
            if a["job"] != b["job"] and within(a["ra0"], a["dec0"], b["ra0"], b["dec0"], radius):
                duplicates += 1
                if duplicates <= 5:
                    bad.append(f"objects ({a['job']}, {a['aid']}) and ({b['job']}, {b['aid']}),"
                               " founded by two jobs, are within the radius")
    (r,) = q("""SELECT count(*) n FROM memberships_official m WHERE m.run = %s AND NOT EXISTS (
                SELECT 1 FROM official o WHERE o.closed_at IS NULL AND o.product = 'sources'
                AND o.job = m.job)""", (run,))
    if r["n"]:
        bad.append(f"{r['n']} official memberships of a source set that is not official")
    (r,) = q("""SELECT count(*) n FROM memberships m JOIN jobs j ON j.id = m.job AND j.run = %s
                WHERE NOT EXISTS (SELECT 1 FROM official o WHERE o.closed_at IS NULL
                AND o.product = 'sources' AND o.job = m.job)""", (run,))
    superseded = r["n"]
    sums = q("""SELECT o.job, o.aid, o.nsources, o.sumra, o.sumdec, o.sumflux,
                       count(*) n, sum(s.ra - o.ra0) ra, sum(s.dec - o.dec0) dec, sum(s.fluxfit) flux
                FROM objects_official o
                JOIN memberships_official m ON m.object_job = o.job AND m.aid = o.aid
                JOIN sources s ON s.job = m.source_job AND s.sid = m.sid
                WHERE o.run = %s GROUP BY 1, 2, 3, 4, 5, 6""", (run,))
    off = [x for x in sums if x["nsources"] != x["n"]
           or not math.isclose(x["sumflux"], x["flux"], rel_tol=1e-6)
           or not math.isclose(x["sumra"], x["ra"], rel_tol=1e-6, abs_tol=1e-12)
           or not math.isclose(x["sumdec"], x["dec"], rel_tol=1e-6, abs_tol=1e-12)]
    bad += [f"object ({x['job']}, {x['aid']}): sums {x['nsources']} members, the direct aggregation"
            f" {x['n']}" for x in off[:5]]
    return report("catalog", bad, f"{len(objects)} objects in {len({o['field'] for o in objects})} fields,"
                  f" {len(members)} memberships within the radius ({superseded} of source sets not official,"
                  " out of the view), every accepted source a member of an object in its own field or a"
                  f" neighbour; no duplicate founders; {len(sums)} official"
                  " objects' sums = their members'", t0)


def members_by_aid(job: int, pairs: list[tuple[int, int]]) -> dict:
    """(object_job, aid) -> {(source job, sid): mjdobs}: the objects' members in the source sets
    packet job ``job`` recorded reading."""
    read = [r["source_job"] for r in q("SELECT source_job FROM job_inputs WHERE job = %s"
                                       " AND kind = 'input' AND product = 'sources'", (job,))]
    rows = q("SELECT DISTINCT m.object_job, m.aid, m.source_job, m.sid, s.mjdobs FROM memberships m"
             " JOIN sources s ON s.job = m.source_job AND s.sid = m.sid"
             " WHERE m.job = ANY(%s) AND (m.object_job, m.aid) IN"
             " (SELECT * FROM unnest(%s::bigint[], %s::bigint[]))",
             (read, [p[0] for p in pairs], [p[1] for p in pairs]))
    out = defaultdict(dict)
    for r in rows:
        out[(r["object_job"], r["aid"])][(r["source_job"], r["sid"])] = r["mjdobs"]
    return out


def packet_set_failures(job: int) -> tuple[list[str], Counter]:
    bad, counts = [], Counter()
    index = q("SELECT * FROM packets WHERE job = %s ORDER BY sid", (job,))
    inputs = q("SELECT product, source_job FROM job_inputs WHERE job = %s AND kind = 'input'", (job,))
    (named,) = q("SELECT array_agg(DISTINCT i.source_job) s FROM job_inputs i JOIN job_inputs e"
                 " ON e.job = i.job AND e.kind = 'expects' AND e.product = 'packets' AND e.key = i.key"
                 " WHERE i.job = %s AND i.kind = 'input' AND i.product = 'sources'", (job,))
    sources_in = named["s"] or []
    if len(sources_in) != 1:
        return [f"packet job {job}: {len(sources_in)} source sets of its key among its inputs"], counts
    forced = {r["forced_id"]: r for r in read_output(job, "forced").to_pylist()}
    (run_row,) = q("SELECT r.settings FROM jobs j JOIN runs r ON r.id = j.run WHERE j.id = %s", (job,))
    size = 2 * int(run_row["settings"]["packet"]["stamp_half_width"]) + 1
    members = members_by_aid(job, sorted({(r["object_job"], r["aid"]) for r in index}))
    containers = {}
    for row in index:
        where = f"packet job {job} sid {row['sid']}"
        if row["source_job"] != sources_in[0]:
            bad.append(f"{where}: the index names source set {row['source_job']}, its input {sources_in[0]}")
        if row["filename"] not in containers:
            data = fetch(row["filename"]).read_bytes()
            containers[row["filename"]] = (data, next(fastavro.block_reader(io.BytesIO(data))).offset)
        data, header_end = containers[row["filename"]]
        try:
            chunk = data[:header_end] + data[row["byte_offset"]:row["byte_offset"] + row["byte_length"]]
            (record,) = list(fastavro.reader(io.BytesIO(chunk)))
        except Exception as error:  # noqa: BLE001 - an undecodable locator is the failure reported
            bad.append(f"{where}: the locator does not decode to one record ({error})")
            continue
        counts["packets"] += 1
        if (record["diaSourceId"], record["diaObject"]["diaObjectId"],
                record["diaSource"]["midpointMjd"]) != (row["sid"], row["aid"], row["trigger_mjd"]):
            bad.append(f"{where}: the locator decodes to another record")
        trigger = row["trigger_mjd"]
        for prv in record["prvDiaSources"] or []:
            counts["history"] += 1
            if not prv["midpointMjd"] < trigger:
                bad.append(f"{where}: history detection at {prv['midpointMjd']} is not before {trigger}")
        mem = members.get((row["object_job"], row["aid"]), {})
        if (row["source_job"], row["sid"]) not in mem:
            bad.append(f"{where}: the trigger is not a member of object {row['aid']}")
        n_expected = sum(1 for mjd in mem.values() if mjd <= trigger)
        if record["diaObject"]["nDiaSources"] != n_expected:
            bad.append(f"{where}: nDiaSources {record['diaObject']['nDiaSources']}, memberships at or"
                       f" before the trigger {n_expected}")
        for name in rpk.CUTOUTS:
            try:
                with fits.open(io.BytesIO(record[name])) as hdul:
                    shape = np.asarray(hdul[0].data).shape
                if shape != (size, size):
                    bad.append(f"{where}: {name} shape {shape}")
            except Exception as error:  # noqa: BLE001 - an unreadable stamp is the failure reported
                bad.append(f"{where}: {name} does not open ({error})")
            counts["cutouts"] += 1
        for entry in record["prvDiaForcedSources"] or []:
            counts["forced"] += 1
            stored = forced.get(entry["diaForcedSourceId"])
            if stored is None:
                bad.append(f"{where}: forced {entry['diaForcedSourceId']} is not in its forced.parquet")
                continue
            differ = [f for f, c in rpk.FORCED_FIELDS.items() if not rpk._same(entry[f], stored[c])]
            differ += [f for f, c in rpk.FORCED_FLOAT32.items()
                       if not rpk._same(entry[f], rpk._float32(stored[c]))]
            if differ:
                bad.append(f"{where}: forced {entry['diaForcedSourceId']} differs in {', '.join(differ)}")
    if counts["packets"] != len(index):
        bad.append(f"packet job {job}: {counts['packets']} decoded of {len(index)} index rows")
    return bad, counts


def check_packets(run: int) -> bool:
    t0, bad, total = time.perf_counter(), [], Counter()
    sets = run_jobs(run, "packet")
    for b, c in each(packet_set_failures, sets):
        bad += b
        total += c
    read = [r["job"] for r in q("SELECT DISTINCT job FROM packets_official WHERE job = ANY(%s) ORDER BY job",
                                (sets,))]
    reader = "no bound set with packets for read_packets.py"
    if read:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = rpk.main([a for job in read for a in ("--job", str(job))])
        reader = f"read_packets.py exit {code}: {out.getvalue().splitlines()[0]}"
        if code:
            bad.append(reader)
    return report("packets", bad, f"{len(sets)} sets, {total['packets']} packets, {total['history']} history"
                  f" detections, {total['cutouts']} cutouts, {total['forced']} forced entries, each"
                  f" against its own recorded inputs; {reader}", t0)


def check_forced(run: int) -> bool:
    t0, bad, n_rows, n_reused = time.perf_counter(), [], 0, 0
    rows = {}
    for job in run_jobs(run, "packet"):
        rows[job] = {(r["aid"], r["rid"]): tuple(r[f] for f in FORCED_COMPARED)
                     for r in read_output(job, "forced").to_pylist()}
        n_rows += len(rows[job])
    for job, own in rows.items():
        held = defaultdict(list)                  # pair -> the recorded predecessors' measurements of it
        for r in q("SELECT source_job FROM job_inputs WHERE job = %s AND kind = 'input'"
                   " AND product = 'packets'", (job,)):
            pred = rows.get(r["source_job"])
            if pred is None:
                pred = {(x["aid"], x["rid"]): tuple(x[f] for f in FORCED_COMPARED)
                        for x in read_output(r["source_job"], "forced").to_pylist()}
            for pair in own.keys() & pred.keys():
                held[pair].append((r["source_job"], pred[pair]))
        for pair, preds in held.items():
            n_reused += 1
            if not any(all(rpk._same(a, b) for a, b in zip(own[pair], m)) for _, m in preds):
                bad.append(f"object {pair[0]} image rid {pair[1]}: set {job} measured it again,"
                           f" differently from its predecessors {[j for j, _ in preds]}")
    return report("forced", bad, f"{n_rows} forced rows in {len(rows)} sets; {n_reused} rows shared with a"
                  " recorded predecessor, every one a predecessor's measurement", t0)


def check_twice(a: int, b: int) -> bool:
    t0, bad = time.perf_counter(), []
    jobs = {r["id"]: r for r in q("SELECT id, stage, scope, state, refusal FROM jobs WHERE id = ANY(%s)",
                                  ([a, b],))}
    for j in (a, b):
        if j not in jobs or jobs[j]["state"] != "published":
            bad.append(f"job {j} is {jobs.get(j, {}).get('state', 'absent')}")
    if not bad and (jobs[a]["stage"], jobs[a]["scope"]) != (jobs[b]["stage"], jobs[b]["scope"]):
        bad.append(f"jobs {a} and {b} differ in stage or scope")
    cols = [r["column_name"] for r in q("SELECT column_name FROM information_schema.columns"
                                        " WHERE table_name = 'sources' ORDER BY ordinal_position")]
    keep = ", ".join(c for c in cols if c not in ("job", "pid"))
    ra = q(f"SELECT {keep} FROM sources WHERE job = %s ORDER BY sid", (a,))
    rb = q(f"SELECT {keep} FROM sources WHERE job = %s ORDER BY sid", (b,))
    differ = sum(1 for x, y in zip(ra, rb) if not all(rpk._same(x[c], y[c]) for c in x))
    if len(ra) != len(rb) or differ:
        bad.append(f"jobs {a} and {b}: {len(ra)} and {len(rb)} source rows, {differ} differ")
    bound = [r["job"] for r in q("SELECT DISTINCT job FROM official WHERE job = ANY(%s) AND closed_at IS NULL",
                                 ([a, b],))]
    if len(bound) > 1:
        bad.append(f"both {a} and {b} hold open bindings")
    return report("twice", bad, f"{a} and {b} published, {len(ra)} equal source rows; bound"
                  f" {bound or 'neither'}", t0)


def job_sca(scope: dict) -> int | None:
    """The SCA a job's scope names: its ``sca``, or its SOC stem's ``wfiNN``; ``None`` for a field's."""
    if "sca" in scope:
        return int(scope["sca"])
    m = re.search(r"_wfi(\d{2})_", scope.get("file", ""))
    return int(m[1]) if m else None


def pending_outside(pending: list[dict], scas: set[int] | None) -> tuple[list[dict], list[dict]]:
    """``pending`` jobs split into those the check counts and those of another loop's SCAs:
    with ``scas``, a job whose scope names an SCA outside them is another loop's; a job
    naming no SCA (a field's) is always counted."""
    if scas is None:
        return pending, []
    mine, others = [], []
    for job in pending:
        sca = job_sca(job["scope"])
        (others if sca is not None and sca not in scas else mine).append(job)
    return mine, others


def check_counts(run: int, scas: set[int] | None = None) -> bool:
    t0 = time.perf_counter()
    jobs = q("SELECT stage, state, count(*) n FROM jobs WHERE run = %s GROUP BY 1, 2 ORDER BY 1, 2", (run,))
    pending, others = pending_outside(q("SELECT stage, state, scope FROM jobs WHERE run = %s"
                                        " AND state IN ('launched', 'running')", (run,)), scas)
    bad = [f"{n} {stage} jobs still {state}"
           for (stage, state), n in sorted(Counter((j["stage"], j["state"]) for j in pending).items())]
    rows = {t: q(f"SELECT count(*) n FROM {t} x JOIN jobs j ON j.id = x.job WHERE j.run = %s", (run,))[0]["n"]
            for t in TABLES}
    (o,) = q("SELECT count(*) n FROM official o JOIN jobs j ON j.id = o.job WHERE j.run = %s"
             " AND o.closed_at IS NULL", (run,))
    detail = (f"jobs {json.dumps({f'{r['stage']} {r['state']}': r['n'] for r in jobs})}; rows"
              f" {json.dumps({t: n for t, n in rows.items() if n})}; {o['n']} open bindings"
              + (f"; {len(others)} pending jobs of SCAs outside {sorted(scas)} not counted" if scas else ""))
    return report("counts", bad, detail, t0)


def main(argv: list[str] | None = None) -> int:
    global _s3
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", type=int, required=True)
    parser.add_argument("--only", help="comma-separated checks; default every one but twice")
    parser.add_argument("--twice", help="A,B: two executions of one key, for the twice check")
    parser.add_argument("--scas", help="comma-separated SCAs: the counts check ignores pending jobs of other"
                                       " SCAs (a second loop's under the same run)")
    args = parser.parse_args(argv)
    scas = {int(x) for x in args.scas.split(",")} if args.scas else None
    checks = {"lineage": lambda: check_lineage(args.run), "tables": lambda: check_tables(args.run),
              "catalog": lambda: check_catalog(args.run), "packets": lambda: check_packets(args.run),
              "forced": lambda: check_forced(args.run), "counts": lambda: check_counts(args.run, scas)}
    if args.twice:
        a, b = (int(x) for x in args.twice.split(","))
        checks["twice"] = lambda: check_twice(a, b)
    names = args.only.split(",") if args.only else list(checks)
    if not args.twice and "twice" not in names:
        print("twice: skipped (no --twice A,B)")
    unknown = [n for n in names if n not in checks]
    if unknown:
        parser.error(f"unknown or unarmed checks {unknown}")
    t0 = time.perf_counter()
    _s3 = storage.s3_client()
    with _opened:
        results = [checks[n]() for n in names]
    print(f"run {args.run}: {sum(results)}/{len(results)} checks PASS in {time.perf_counter() - t0:.1f} s")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
