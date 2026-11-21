"""scripts/run_products_csv.py's rows: one per published difference job of the run, with its L2 file and product directories.

Contracts: a row carries its L2 file's stem and pointing and the directories
of its reference, its own difference job and its packet job; the packet job
is the officially bound one of the image's, never a packet job of another
image that read the same sources as xmembers; a difference image with no
packet job has an empty packet directory; a failed difference job and
another run's are not rows; a run of another dataset is refused. Run by
foundation.yml against database/schema.sql.
"""

from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path

import pytest

from tests.db.foundation_helpers import DATASET, new_run

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_SPEC = importlib.util.spec_from_file_location("run_products_csv", SCRIPTS / "run_products_csv.py")
run_products_csv = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_products_csv)

CORNERS = {f"{c}{i}": 1.0 for i in range(5) for c in ("ra", "dec")}


def _job(cur, run: int, stage: str, scope: dict, attempt: int | None = 1) -> int:
    """A job row, published at ``attempt`` or failed when ``attempt`` is None."""
    cur.execute("INSERT INTO jobs (run, stage, scope, state, published_attempt) VALUES (%s, %s, %s, %s, %s)"
                " RETURNING id", (run, stage, json.dumps(scope), "failed" if attempt is None else "published",
                                  attempt))
    return cur.fetchone()[0]


def _input(cur, job: int, product: str, key: dict, source_job: int) -> None:
    cur.execute("INSERT INTO job_inputs (job, kind, product, key, source_job) VALUES (%s, 'input', %s, %s, %s)",
                (job, product, json.dumps(key), source_job))


def _l2(cur, job: int, expid: int, sca: int, stem: str) -> None:
    row = {"job": job, "dataset": DATASET, "expid": expid, "sca": sca, "fid": 1, "field": 7, "hp6": 0,
           "hp9": 0, "dateobs": "2026-11-04 12:00:00", "mjdobs": 61348.5, "exptime": 139.8,
           "filename": f"file:///out/1/intake/{job}/1/{stem}_cal.fits.gz", "checksum": "c",
           "crval1": 0.0, "crval2": 0.0, "crpix1": 0.0, "crpix2": 0.0, "cd11": 0.0, "cd12": 0.0,
           "cd21": 0.0, "cd22": 0.0, "ra": 268.5 + expid, "dec": -29.0, **CORNERS}
    cur.execute(f"INSERT INTO l2files ({', '.join(row)}) VALUES ({', '.join(['%s'] * len(row))})",
                tuple(row.values()))


def _diff(cur, job: int, expid: int, sca: int) -> None:
    row = {"job": job, "dataset": DATASET, "expid": expid, "sca": sca, "fid": 1, "field": 7,
           "infobitssci": 0, "infobitsref": 0, "filename": "d.fits", **CORNERS}
    cur.execute(f"INSERT INTO diffimages ({', '.join(row)}) VALUES ({', '.join(['%s'] * len(row))})",
                tuple(row.values()))


def _bind(cur, product: str, key: dict, job: int) -> None:
    cur.execute("INSERT INTO official (product, key, job, opened_group, opened_by, opened_why)"
                " VALUES (%s, %s, %s, nextval('change_groups'), 'tester', 'test')",
                (product, json.dumps(key), job))


def _image(cur, run: int, ref: int, expid: int, sca: int, stem: str, attempt: int | None = 1) -> int:
    """An intake job's L2 row and a difference job of it against ``ref``; return the difference job."""
    key = {"dataset": DATASET, "expid": expid, "sca": sca, "fid": 1}
    intake = _job(cur, run, "intake", {"file": stem})
    _l2(cur, intake, expid, sca, stem)
    diff = _job(cur, run, "difference", {"expid": expid, "sca": sca, "fid": 1}, attempt)
    _input(cur, diff, "l2files", key, intake)
    _input(cur, diff, "refimages", {"dataset": DATASET, "field": 7, "fid": 1}, ref)
    if attempt is not None:
        _diff(cur, diff, expid, sca)
    return diff


def test_rows_name_each_published_difference_with_its_reference_and_bound_packet(connect, tmp_path):
    conn = connect()
    run, other_run = new_run(conn), new_run(conn)
    with conn, conn.cursor() as cur:
        ref = _job(cur, other_run, "reference", {"field": 7, "fid": 1}, attempt=2)
        diff = _image(cur, run, ref, 1, 8, "r0001_a_wfi08", attempt=3)
        lone = _image(cur, run, ref, 2, 9, "r0002_a_wfi09")
        _image(cur, run, ref, 3, 8, "r0003_a_wfi08", attempt=None)
        _image(cur, other_run, ref, 4, 8, "r0004_a_wfi08")
        key = {"dataset": DATASET, "expid": 1, "sca": 8, "fid": 1}
        bound = _job(cur, run, "packet", {"expid": 1, "sca": 8, "fid": 1})
        _input(cur, bound, "sources", key, diff)
        _bind(cur, "packets", key, bound)
        rerun = _job(cur, run, "packet", {"expid": 1, "sca": 8, "fid": 1})
        _input(cur, rerun, "sources", key, diff)
        # A later packet job of image 2 that read image 1's sources as xmembers.
        reader = _job(cur, run, "packet", {"expid": 2, "sca": 9, "fid": 1})
        _input(cur, reader, "sources", key, diff)

    rows = run_products_csv.product_rows(conn, DATASET, run)

    assert [r["l2_stem"] for r in rows] == ["r0001_a_wfi08", "r0002_a_wfi09"]
    first, second = rows
    assert (first["sca"], first["ra"], first["dec"], first["field"], first["filter"]) == (8, 269.5, -29.0, 7, "F184")
    assert (first["mjdobs"], first["dateobs"], first["exptime"]) == (61348.5, "2026-11-04T12:00:00",
                                                                     pytest.approx(139.8))
    assert first["reference_dir"] == f"{other_run}/reference/{ref}/2"
    assert first["difference_dir"] == f"{run}/difference/{diff}/3"
    assert first["packet_dir"] == f"{run}/packet/{bound}/1"
    assert second["difference_dir"] == f"{run}/difference/{lone}/1"
    assert second["packet_dir"] == ""

    out = tmp_path / "run.csv"
    run_products_csv.write_csv(rows, out)
    with out.open() as fh:
        read = list(csv.DictReader(fh))
    assert list(read[0]) == run_products_csv.COLUMNS
    assert read[1]["packet_dir"] == ""


def test_latest_published_packet_job_when_none_is_bound(connect):
    conn = connect()
    run = new_run(conn)
    with conn, conn.cursor() as cur:
        ref = _job(cur, run, "reference", {"field": 7, "fid": 1})
        diff = _image(cur, run, ref, 1, 8, "r0001_a_wfi08")
        key = {"dataset": DATASET, "expid": 1, "sca": 8, "fid": 1}
        first = _job(cur, run, "packet", {"expid": 1, "sca": 8, "fid": 1})
        latest = _job(cur, run, "packet", {"expid": 1, "sca": 8, "fid": 1}, attempt=2)
        failed = _job(cur, run, "packet", {"expid": 1, "sca": 8, "fid": 1}, attempt=None)
        # A later packet job of another image that read image 1's sources as xmembers.
        reader = _job(cur, run, "packet", {"expid": 2, "sca": 9, "fid": 1})
        for job in (first, latest, failed, reader):
            _input(cur, job, "sources", key, diff)

    (row,) = run_products_csv.product_rows(conn, DATASET, run)

    assert row["packet_dir"] == f"{run}/packet/{latest}/2"


def test_a_run_of_another_dataset_is_refused(connect):
    conn = connect()
    run = new_run(conn)

    with pytest.raises(ValueError, match="its dataset is 'D'"):
        run_products_csv.product_rows(conn, "other", run)
