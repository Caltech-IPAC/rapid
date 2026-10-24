"""Read packet sets back and write their validation report (foundation plan v5, Part 2, the reader).

Outside the pipeline: it reads what the packet stage published, never
writes to the database, and exits non-zero on any failure. For each packet
set (a ``packets_official`` job; ``--job`` narrows to some), it checks:

- schema: every record in the set's Avro container validates against the
  schema version the index names, and the container holds exactly the
  index's records;
- cutouts: ``cutoutDifference``, ``cutoutScience`` and ``cutoutReference``
  each open as FITS, 129 x 129 (``2 * packet.stamp_half_width + 1`` from the
  run's settings), and every stamp pixel that falls off the chip holds the
  edge fill, 0.0 (the stage's ``STAMP_FILL_VALUE``); the chip is the trigger
  difference image's shape, read from its header;
- forced: every ``prvDiaForcedSources`` entry equals the ``forced_official``
  row of the same set with its ``diaForcedSourceId`` (fluxes compared at
  the record's float32 precision; a null flux is a null);
- history: every ``prvDiaSources`` entry's ``midpointMjd`` precedes the
  index row's ``trigger_mjd``;
- index: every ``packets_official`` row's ``(filename, byte_offset,
  byte_length)`` decodes, with the container's header, to the record of its
  ``sid`` and ``aid`` at its ``trigger_mjd``, its ``source_job`` is an
  ``input`` row of the packet job (its recorded inputs), and its object
  is one the trigger is a member of: the trigger's membership of
  ``(object_job, aid)``, the difference job's (since 2026-10-24), or for
  an earlier set, its object set among the recorded inputs.

Usage::

    python scripts/read_packets.py [--job N ...] [--out report.md]

with the database named as for every rapidpipe command
(``rapidpipe.db.connection``: the ``PG*`` variables, else ``RAPID_PARAMETER_PATH``). The report is Markdown: one table row
per packet set with the counts, then every failure, one per line.
"""

from __future__ import annotations

import argparse
import io
import math
import sys
from collections import Counter
from pathlib import Path

import fastavro
import fastavro.validation
import numpy as np
import psycopg2.extras
from astropy.io import fits

from rapidpipe.db.connection import connect
from rapidpipe.foundation.prefix import get
from rapidpipe.science.alerts.assemble import load_schema
from rapidpipe.science.alerts.cutouts import STAMP_FILL_VALUE

CUTOUTS = ("cutoutDifference", "cutoutScience", "cutoutReference")
#: prvDiaForcedSources field -> forced column; the float32 fields compare at float32.
FORCED_FIELDS = {"diaObjectId": "aid", "expId": "expid", "detector": "sca", "ra": "ra",
                 "dec": "dec", "band": "band", "midpointMjd": "mjdobs",
                 "timeProcessedMjd": "time_proc"}
FORCED_FLOAT32 = {"psfFlux": "psf_flux", "psfFluxErr": "psf_fluxerr",
                  "scienceFlux": "science_flux", "scienceFluxErr": "science_fluxerr"}


def _query(conn, sql: str, params=()) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.commit()
    return rows


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) or isinstance(b, float):
        return float(a) == float(b) or (math.isnan(float(a)) and math.isnan(float(b)))
    return a == b


def _float32(value):
    return None if value is None else float(np.float32(value))


def check_cutout(blob: bytes | None, size: int, col: int, row: int,
                 chip: tuple[int, int]) -> tuple[str | None, bool]:
    """(failure or None, whether the stamp reaches off the chip) for one cutout.

    ``col``, ``row`` are the 0-based chip pixel of the stamp's centre;
    ``chip`` is the chip's (rows, columns).
    """
    if blob is None:
        return "missing", False
    try:
        with fits.open(io.BytesIO(blob)) as hdul:
            data = np.asarray(hdul[0].data)
    except Exception as error:  # noqa: BLE001 - any unreadable stamp is the failure reported
        return f"does not open as FITS ({error})", False
    if data.shape != (size, size):
        return f"shape {data.shape}, expected ({size}, {size})", False
    half = size // 2
    rows = np.arange(row - half, row + half + 1)[:, None]
    cols = np.arange(col - half, col + half + 1)[None, :]
    off = (rows < 0) | (rows >= chip[0]) | (cols < 0) | (cols >= chip[1])
    if off.any() and not np.all(data[off] == STAMP_FILL_VALUE):
        return f"{int(np.sum(data[off] != STAMP_FILL_VALUE))} off-chip pixels not {STAMP_FILL_VALUE}", True
    return None, bool(off.any())


def read_set(conn, job: int) -> tuple[dict, list[str]]:
    """Check one packet set; return its counts and its failures."""
    failures: list[str] = []
    counts = Counter()
    index = _query(conn, "SELECT * FROM packets_official WHERE job = %s ORDER BY sid", (job,))
    forced = {r["forced_id"]: r for r in _query(
        conn, "SELECT * FROM forced_official WHERE job = %s", (job,))}
    inputs = {(r["product"], r["source_job"]) for r in _query(
        conn, "SELECT product, source_job FROM job_inputs WHERE job = %s AND kind = 'input'",
        (job,))}
    member_of = {(r["object_job"], r["aid"], r["sid"]) for r in _query(
        conn, "SELECT m.object_job, m.aid, m.sid FROM memberships m"
              " WHERE m.job = (SELECT source_job FROM packets WHERE job = %s LIMIT 1)", (job,))}
    (settings,) = _query(conn, "SELECT r.settings FROM jobs j JOIN runs r ON r.id = j.run"
                               " WHERE j.id = %s", (job,))
    size = 2 * int(settings["settings"]["packet"]["stamp_half_width"]) + 1
    key = index[0]
    chip_rows = _query(conn, """
        SELECT d.filename FROM job_inputs i JOIN diffimages d ON d.job = i.source_job
        WHERE i.job = %s AND i.kind = 'input' AND i.product = 'diffimages'
          AND d.dataset = %s AND d.expid = %s AND d.sca = %s AND d.fid = %s""",
                       (job, key["dataset"], key["expid"], key["sca"], key["fid"]))
    if len(chip_rows) != 1:
        failures.append(f"job {job}: {len(chip_rows)} recorded trigger difference images, expected 1")
        chip = None
    else:
        header = fits.getheader(get(chip_rows[0]["filename"]))
        chip = (int(header["NAXIS2"]), int(header["NAXIS1"]))

    containers: dict[str, tuple[bytes, int]] = {}
    for row in index:
        filename = row["filename"]
        where = f"job {job} sid {row['sid']}"
        if filename not in containers:
            path: Path = get(filename)
            data = path.read_bytes()
            schema = load_schema(row["schema_version"])
            records = list(fastavro.reader(io.BytesIO(data)))
            for record in records:
                if not fastavro.validation.validate(record, schema, raise_errors=False):
                    failures.append(f"job {job}: record {record.get('diaSourceId')} in {path.name}"
                                    f" is not valid {row['schema_version']}")
            counts["records"] += len(records)
            counts["avro_bytes"] += len(data)
            first = next(fastavro.block_reader(io.BytesIO(data)))
            containers[filename] = (data, first.offset)
            n_index = sum(1 for r in index if r["filename"] == filename)
            if n_index != len(records):
                failures.append(f"job {job}: {path.name} holds {len(records)} records,"
                                f" the index {n_index}")
        data, header_end = containers[filename]
        counts["index_rows"] += 1

        try:
            chunk = data[:header_end] + data[row["byte_offset"]:row["byte_offset"] + row["byte_length"]]
            decoded = list(fastavro.reader(io.BytesIO(chunk)))
        except Exception as error:  # noqa: BLE001 - an undecodable locator is the failure reported
            failures.append(f"{where}: locator does not decode ({error})")
            continue
        if len(decoded) != 1:
            failures.append(f"{where}: locator holds {len(decoded)} records, expected 1")
            continue
        record = decoded[0]
        source = record["diaSource"]
        if (record["diaSourceId"], record["diaObject"]["diaObjectId"], source["midpointMjd"]) != (
                row["sid"], row["aid"], row["trigger_mjd"]):
            failures.append(f"{where}: locator decodes to source {record['diaSourceId']} object"
                            f" {record['diaObject']['diaObjectId']} at {source['midpointMjd']}")
        if ("sources", row["source_job"]) not in inputs:
            failures.append(f"{where}: sources job {row['source_job']} is not a recorded input")
        if (("objects", row["object_job"]) not in inputs
                and (row["object_job"], row["aid"], row["sid"]) not in member_of):
            failures.append(f"{where}: object ({row['object_job']}, {row['aid']}) is neither a recorded"
                            " input nor one its trigger is a member of")

        for name in CUTOUTS:
            col = int(round(source["x"] + 1.0)) - 1
            line = int(round(source["y"] + 1.0)) - 1
            failure, at_edge = (check_cutout(record[name], size, col, line, chip) if chip
                                else (None, False))
            counts["cutouts"] += 1
            counts["edge_cutouts"] += at_edge
            if failure:
                failures.append(f"{where}: {name} {failure}")

        for prv in record["prvDiaSources"] or []:
            counts["prv_sources"] += 1
            if not prv["midpointMjd"] < row["trigger_mjd"]:
                failures.append(f"{where}: prvDiaSource {prv['diaSourceId']} at"
                                f" {prv['midpointMjd']} does not precede {row['trigger_mjd']}")

        for entry in record["prvDiaForcedSources"] or []:
            counts["forced_values"] += 1
            stored = forced.get(entry["diaForcedSourceId"])
            if stored is None:
                failures.append(f"{where}: forced {entry['diaForcedSourceId']} has no"
                                " forced_official row")
                continue
            differ = [field for field, column in FORCED_FIELDS.items()
                      if not _same(entry[field], stored[column])]
            differ += [field for field, column in FORCED_FLOAT32.items()
                       if not _same(entry[field], _float32(stored[column]))]
            if differ:
                failures.append(f"{where}: forced {entry['diaForcedSourceId']} differs in"
                                f" {', '.join(differ)}")
    counts["forced_rows"] = len(forced)
    return {"job": job, "key": f"{key['expid']}-{key['sca']}-{key['fid']}", **counts}, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--job", type=int, action="append",
                        help="a packet set's job (repeatable); default every packets_official set")
    parser.add_argument("--out", type=Path, help="write the report here as well as to stdout")
    args = parser.parse_args(argv)

    columns = ("job", "key", "index_rows", "records", "avro_bytes", "cutouts", "edge_cutouts",
               "prv_sources", "forced_values", "forced_rows")
    lines = ["| " + " | ".join(columns) + " | failures |", "|" + "---|" * (len(columns) + 1)]
    all_failures: list[str] = []
    with connect(application_name="read_packets") as conn:
        jobs = args.job or [r["job"] for r in _query(
            conn, "SELECT DISTINCT job FROM packets_official ORDER BY job")]
        for job in jobs:
            if not _query(conn, "SELECT 1 FROM packets_official WHERE job = %s LIMIT 1", (job,)):
                all_failures.append(f"job {job}: no packets_official rows")
                continue
            counts, failures = read_set(conn, job)
            lines.append("| " + " | ".join(str(counts.get(c, 0)) for c in columns)
                         + f" | {len(failures)} |")
            all_failures += failures
    verdict = "CLEAN" if not all_failures else f"{len(all_failures)} FAILURES"
    report = "\n".join([f"Packet reader: {len(jobs)} set(s), {verdict}", "", *lines, "",
                        *(f"- {f}" for f in all_failures)]) + "\n"
    print(report, end="")
    if args.out:
        args.out.write_text(report)
    return 0 if not all_failures else 1


if __name__ == "__main__":
    sys.exit(main())
