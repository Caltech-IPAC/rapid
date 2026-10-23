"""Check that one run is one coherent account in the tables, the Parquet and the outbox.

Outside the pipeline, like ``read_packets.py``: it reads the database and
the job prefixes, never writes, prints one line per check (``PASS`` with
its counts, or ``FAIL`` with the first counter-example) and exits non-zero
on any failure. The checks are the 2026-10-21 night's (``docs/
foundation-smdc-step.md``, Validation), one function each, scoped to the
jobs of ``--run``:

- ``lineage``: every row of the run's jobs, every ``input`` and
  ``excludes`` row they recorded and every binding naming them resolves to
  a published job's row of its key; every ``expects`` binding names its
  product and key.
- ``tables``: every published job's declared Parquet outputs hold as many
  rows as its table rows, match their manifest checksums, and no table holds
  rows of the job that it did not declare.
- ``chain``: for each object set of the run that is bound or final (no
  other set of the run names it as its predecessor), the members of its chain minus exclusions
  are ``members_official`` row for row (when the set is bound), every
  object's stored sums reproduce its members' statistics, and every set
  holds its predecessor's objects.
- ``packets``: every packet set of the run decodes, by its index, to the
  records of its sid, object and trigger, its object and source sets are
  its recorded inputs, every history detection precedes the trigger,
  ``nDiaSources`` counts the memberships at or before it, every cutout
  opens at the run's stamp size and every forced entry is its set's
  ``forced.parquet`` row; then ``read_packets.py`` on the bound sets.
- ``forced``: a packet set's forced rows of an (object, image) pair that a
  recorded predecessor set also holds are the predecessor's measurement,
  value for value, ``time_proc`` included: reused, never re-measured. (The
  night's check took every official set as one measurement; a run may also
  hold a replacement set, which measures afresh what it replaces.)
- ``twice`` (only with ``--twice A,B``): two executions of one key are both
  published with equal source rows but for job and pid, and at most one is
  bound.
- ``counts``: no job of the run is still launched or running; the jobs per
  stage and state, the rows per table and the open bindings are printed.

The night's checks 7 to 10 (one group makes a night official, a
replacement, undo, two groups at once) are operations, checked as they
ran; they have no read-only form over a run and are not here.

``tables`` and ``packets`` check their jobs and sets eight at a time
(:data:`WORKERS`), each worker thread on its own connection, and reduce the
results in job order; a set's decoded records are dropped once it is
checked. The checks themselves are the serial ones: every Parquet output is
read and hashed, every packet decoded.

Usage::

    python scripts/validate_run.py --run N [--only lineage,chain] [--twice A,B] [--depth D]

with the database named as for every rapidpipe command
(``rapidpipe.db.connection``: the ``PG*`` variables, else
``RAPID_PARAMETER_PATH``). ``--depth`` asserts the chain depth of the
run's one bound object set (or, with none bound, its one final set).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
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
from rapidpipe.science.statistics.sums import Sums, ra_offset

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
JOB_COLUMNS = {"memberships": ("source_job",), "packets": ("object_job", "source_job"),
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
                     WHERE rj.run = %s AND i.kind IN ('input', 'excludes') AND i.product = %s
                       AND NOT EXISTS (SELECT 1 FROM {product} t JOIN jobs sj ON sj.id = t.job
                                       AND sj.state = 'published'
                                       WHERE t.job = i.source_job AND i.key = {keyed(product)})""",
                 (run, product))
        bad += [f"job {r['job']} {r['kind']} {product} {r['key']} from job {r['source_job']}:"
                " no published row" for r in rows]
        (r,) = q("SELECT count(*) n FROM job_inputs i JOIN jobs rj ON rj.id = i.job WHERE rj.run = %s"
                 " AND i.kind IN ('input', 'excludes') AND i.product = %s", (run, product))
        n_inputs += r["n"]
    (r,) = q("SELECT count(*) n FROM job_inputs i JOIN jobs rj ON rj.id = i.job WHERE rj.run = %s"
             " AND i.kind IN ('input', 'excludes') AND NOT i.product = ANY(%s)", (run, list(KEYS)))
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
    return report("lineage", bad, f"0 orphans: {n_inputs} input and excludes rows and {n_bindings}"
                  " bindings resolve to published rows", t0)


def job_table_failures(j: dict) -> tuple[list[str], int, int]:
    """One published job's declared outputs against its manifest and its table rows."""
    bad, n_outputs, n_rows = [], 0, 0
    prefix = j["manifest"]["prefix"].rstrip("/") + "/"
    per_table = Counter()
    for out in j["manifest"]["outputs"]:
        path = fetch(prefix + out["path"])
        per_table[out["table"]] += pq.read_metadata(path).num_rows
        n_outputs += 1
        if out["checksum"] != "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest():
            bad.append(f"job {j['id']} {out['path']}: the checksum differs from the manifest's")
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


def chain_of(job: int) -> list[int]:
    return [r["job"] for r in q(
        "WITH RECURSIVE chain (job, depth) AS (SELECT %s::bigint, 1 UNION ALL"
        " SELECT i.source_job, c.depth + 1 FROM chain c JOIN job_inputs i ON i.job = c.job"
        " AND i.kind = 'input' AND i.product = 'objects' WHERE i.source_job IS NOT NULL)"
        " SELECT job FROM chain ORDER BY depth", (job,))]


def excluded_of(chain: list[int]) -> set[int]:
    return {r["source_job"] for r in q("SELECT DISTINCT source_job FROM job_inputs"
                                       " WHERE job = ANY(%s) AND kind = 'excludes'", (chain,))}


def expected_members(chain: list[int]) -> Counter:
    """(set job, aid, source job, sid) over the chain's memberships Parquet, minus exclusions."""
    excluded = excluded_of(chain)
    rows = Counter()
    for job in chain:
        t = read_output(job, "memberships").to_pydict()
        for aid, source_job, sid in zip(t["aid"], t["source_job"], t["sid"]):
            if source_job not in excluded:
                rows[(job, aid, source_job, sid)] += 1
    return rows


def sums_failures(set_job: int, members: Counter) -> tuple[list[str], int]:
    """Every object's stored sums against its members' statistics."""
    by_source = defaultdict(list)
    for (_, _, source_job, sid) in members:
        by_source[source_job].append(sid)
    src = {}
    for source_job, sids in by_source.items():
        for r in q("SELECT sid, ra, dec, fluxfit FROM sources WHERE job = %s AND sid = ANY(%s)",
                   (source_job, sids)):
            src[(source_job, r["sid"])] = (r["ra"], r["dec"], r["fluxfit"])
    by_aid = defaultdict(set)
    for (_, aid, source_job, sid) in members:
        by_aid[aid].add((source_job, sid))
    bad = []
    objects = q("SELECT aid, ra0, dec0, nsources, sumra, sumra2, sumdec, sumdec2, sumflux, sumflux2"
                " FROM objects WHERE job = %s", (set_job,))
    for o in objects:
        aid = o.pop("aid")
        s = Sums(**o)
        mem = [src[m] for m in sorted(by_aid.get(aid, ()))]
        if s.nsources != len(mem):
            bad.append(f"set {set_job} object {aid}: nsources {s.nsources}, members {len(mem)}")
            continue
        if not mem:
            continue
        ra, dec, flux = (np.array([m[i] for m in mem]) for i in range(3))
        dra = np.array([ra_offset(r, s.ra0) for r in ra])
        st = s.statistics()
        tests = (("meanra", st["meanra"], (s.ra0 + dra.mean()) % 360.0, 1e-12, 0.0),
                 ("meandec", st["meandec"], dec.mean(), 1e-12, 0.0),
                 ("stdevdec", st["stdevdec"], np.std(dec), 1e-13, 1e-9),
                 ("stdevra", st["stdevra"], np.std(dra * np.cos(np.radians(dec))), 1e-13, 1e-6),
                 ("meanflux", st["meanflux"], flux.mean(), 0.0, 1e-9),
                 ("stdevflux", st["stdevflux"], np.std(flux), 1e-9 * abs(flux).max(), 1e-9))
        for name, got, want, atol, rtol in tests:
            if not abs(got - want) <= atol + rtol * abs(want):
                bad.append(f"set {set_job} object {aid}: {name} {got!r} from the sums, {want!r} from"
                           f" {len(mem)} members")
                break
    return bad, len(objects)


def check_chain(run: int, depth: int | None) -> bool:
    t0, bad, notes = time.perf_counter(), [], []
    sets = [j for j in run_jobs(run) if q("SELECT 1 FROM objects WHERE job = %s LIMIT 1", (j,))
            or q("SELECT 1 FROM job_inputs WHERE job = %s AND kind = 'expects' AND product = 'objects'", (j,))]
    named = {r["source_job"] for r in q("SELECT source_job FROM job_inputs WHERE job = ANY(%s)"
                                        " AND kind = 'input' AND product = 'objects'", (sets,))}
    bound = {r["job"] for r in q("SELECT job FROM official WHERE product = 'objects' AND job = ANY(%s)"
                                 " AND closed_at IS NULL", (sets,))}
    checked = sorted(bound | {j for j in sets if j not in named})
    if not checked:
        return report("chain", [], "no object sets in the run", t0)
    deep = sorted(bound) or checked
    if depth is not None and len(deep) != 1:
        bad.append(f"--depth names one chain; the run has sets {deep}")
    for set_job in checked:
        chain = chain_of(set_job)
        if depth is not None and set_job in deep and len(chain) != depth:
            bad.append(f"set {set_job}: chain depth {len(chain)}, expected {depth}")
        expected = expected_members(chain)
        if set_job in bound:
            view = Counter((r["job"], r["aid"], r["source_job"], r["sid"]) for r in q(
                "SELECT job, aid, source_job, sid FROM members_official WHERE set_job = %s", (set_job,)))
            if view != expected:
                extra, missing = view - expected, expected - view
                bad.append(f"set {set_job}: members_official has {sum(extra.values())} rows the chain"
                           f" lacks (first {next(iter(extra), None)}) and lacks {sum(missing.values())}"
                           f" (first {next(iter(missing), None)})")
        sbad, n_objects = sums_failures(set_job, expected)
        bad += sbad
        previous = None
        for job in reversed(chain):
            aids = {r["aid"] for r in q("SELECT aid FROM objects WHERE job = %s", (job,))}
            if previous is not None and not previous[1] <= aids:
                bad.append(f"set {job} lacks {len(previous[1] - aids)} objects of its predecessor"
                           f" {previous[0]}")
            previous = (job, aids)
        notes.append(f"set {set_job} depth {len(chain)}, {sum(expected.values())} members"
                     f"{' = members_official' if set_job in bound else ' (unbound)'}, {n_objects} objects' sums")
    return report("chain", bad, "; ".join(notes) + "; every set holds its predecessor's objects", t0)


def members_by_aid(object_job: int) -> dict:
    """aid -> {(source job, sid): mjdobs} through the set's chain, minus exclusions."""
    chain = chain_of(object_job)
    rows = q("SELECT DISTINCT m.aid, m.source_job, m.sid, s.mjdobs FROM memberships m"
             " JOIN sources s ON s.job = m.source_job AND s.sid = m.sid"
             " WHERE m.job = ANY(%s) AND NOT m.source_job = ANY(%s)", (chain, list(excluded_of(chain))))
    out = defaultdict(dict)
    for r in rows:
        out[r["aid"]][(r["source_job"], r["sid"])] = r["mjdobs"]
    return out


def packet_set_failures(job: int) -> tuple[list[str], Counter]:
    bad, counts = [], Counter()
    index = q("SELECT * FROM packets WHERE job = %s ORDER BY sid", (job,))
    inputs = q("SELECT product, source_job FROM job_inputs WHERE job = %s AND kind = 'input'", (job,))
    objects_in = [i["source_job"] for i in inputs if i["product"] == "objects"]
    sources_in = [i["source_job"] for i in inputs if i["product"] == "sources"]
    if len(objects_in) != 1 or len(sources_in) != 1:
        return [f"packet job {job}: {len(objects_in)} object and {len(sources_in)} source inputs"], counts
    forced = {r["forced_id"]: r for r in read_output(job, "forced").to_pylist()}
    (run_row,) = q("SELECT r.settings FROM jobs j JOIN runs r ON r.id = j.run WHERE j.id = %s", (job,))
    size = 2 * int(run_row["settings"]["packet"]["stamp_half_width"]) + 1
    members = members_by_aid(objects_in[0])
    containers = {}
    for row in index:
        where = f"packet job {job} sid {row['sid']}"
        if (row["object_job"], row["source_job"]) != (objects_in[0], sources_in[0]):
            bad.append(f"{where}: the index names sets {row['object_job']} and {row['source_job']},"
                       f" its inputs {objects_in[0]} and {sources_in[0]}")
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
        mem = members.get(row["aid"], {})
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
        for r in q("SELECT source_job FROM job_inputs WHERE job = %s AND kind = 'input'"
                   " AND product = 'packets'", (job,)):
            pred = rows.get(r["source_job"])
            if pred is None:
                pred = {(x["aid"], x["rid"]): tuple(x[f] for f in FORCED_COMPARED)
                        for x in read_output(r["source_job"], "forced").to_pylist()}
            for pair in own.keys() & pred.keys():
                n_reused += 1
                if not all(rpk._same(a, b) for a, b in zip(own[pair], pred[pair])):
                    bad.append(f"object {pair[0]} image rid {pair[1]}: set {job} measured it again,"
                               f" differently from its predecessor {r['source_job']}")
    return report("forced", bad, f"{n_rows} forced rows in {len(rows)} sets; {n_reused} rows shared with a"
                  " recorded predecessor, every one its predecessor's measurement", t0)


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


def check_counts(run: int) -> bool:
    t0 = time.perf_counter()
    jobs = q("SELECT stage, state, count(*) n FROM jobs WHERE run = %s GROUP BY 1, 2 ORDER BY 1, 2", (run,))
    pending = [r for r in jobs if r["state"] in ("launched", "running")]
    bad = [f"{r['n']} {r['stage']} jobs still {r['state']}" for r in pending]
    rows = {t: q(f"SELECT count(*) n FROM {t} x JOIN jobs j ON j.id = x.job WHERE j.run = %s", (run,))[0]["n"]
            for t in TABLES}
    (o,) = q("SELECT count(*) n FROM official o JOIN jobs j ON j.id = o.job WHERE j.run = %s"
             " AND o.closed_at IS NULL", (run,))
    detail = (f"jobs {json.dumps({f'{r['stage']} {r['state']}': r['n'] for r in jobs})}; rows"
              f" {json.dumps({t: n for t, n in rows.items() if n})}; {o['n']} open bindings")
    return report("counts", bad, detail, t0)


def main(argv: list[str] | None = None) -> int:
    global _s3
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", type=int, required=True)
    parser.add_argument("--only", help="comma-separated checks; default every one but twice")
    parser.add_argument("--twice", help="A,B: two executions of one key, for the twice check")
    parser.add_argument("--depth", type=int, help="the chain depth expected of the run's one bound (else final) object set")
    args = parser.parse_args(argv)
    checks = {"lineage": lambda: check_lineage(args.run), "tables": lambda: check_tables(args.run),
              "chain": lambda: check_chain(args.run, args.depth), "packets": lambda: check_packets(args.run),
              "forced": lambda: check_forced(args.run), "counts": lambda: check_counts(args.run)}
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
