"""``job local --stage difference`` publishes an epoch's difference image and source set.

Contract: under a live run, a difference job whose inputs are a published
``l2files`` row, a ``refimages`` row with its mosaic catalog and two
``psfs`` rows (science and reference, from two jobs) differences them
(fake tools); its ``diffimages`` row and ``sources`` set bind to the job's
expected keys, and the official views show the row and its sources under
the one ``pid`` the database drew.
"""

from __future__ import annotations

import json
from datetime import datetime

from rapidpipe.cli import main as cli
from rapidpipe.foundation import change, create_job, create_run, publish
from rapidpipe.selftest.support.fakedifftools import (
    CRVAL,
    FakePsfCatalog,
    FakeToolRunner,
    fake_sip_to_pv,
    write_epoch,
)
from rapidpipe.stages import difference
from tests.db.foundation_helpers import write_attempt
from tests.unit.test_difference_stage import settings

SURVEY, DATASET, FID, SCA, EXPID, FIELD = "S", "D", 1, 7, 84613, 5321


def published(conn, run, tmp_path, name: str, tables: dict) -> int:
    job = create_job(conn, run, "import", {})
    publish(conn, job, 1, write_attempt(tmp_path / name, tables))
    return job


def query(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.commit()
    return rows


def test_job_local_publishes_a_bound_difference_image_and_source_set(connect, tmp_path,
                                                                     monkeypatch, capsys):
    conn = connect()
    run = create_run(conn, "tester", "difference", "abc123", settings(), SURVEY, DATASET)
    change(conn, [{"open": {"survey": SURVEY, "dataset": DATASET, "run": run}}], "tester", "live")
    files = write_epoch(tmp_path / "inputs")
    ra, dec = CRVAL
    corners = {f"{a}{i}": (ra if a == "ra" else dec) for i in range(5) for a in ("ra", "dec")}
    l2_job = published(conn, run, tmp_path, "l2", {"l2files": [{
        "rid": 7001, "dataset": DATASET, "expid": EXPID, "sca": SCA, "fid": FID, "field": FIELD,
        "hp6": 0, "hp9": 0, "dateobs": datetime(2027, 10, 7), "mjdobs": 61273.0087,
        "exptime": 140.0, "filename": files["science"].as_uri(), "checksum": "0" * 32,
        "crval1": ra, "crval2": dec, "crpix1": 32.0, "crpix2": 32.0, "cd11": -3e-5, "cd12": 0.0,
        "cd21": 0.0, "cd22": 3e-5, "ra": ra, "dec": dec, **corners}]})
    ref_job = published(conn, run, tmp_path, "ref", {
        "refimages": [{"rfid": 90011, "dataset": DATASET, "field": FIELD, "fid": FID,
                       "filename": files["reference"].as_uri(), "checksum": "0" * 32,
                       "status": 1, "infobits": 0}],
        "refimcatalogs": [{"rfid": 90011, "cattype": 1, "filename": files["catalog"].as_uri(),
                           "checksum": "0" * 32}]})
    psf_jobs = [published(conn, run, tmp_path, role, {"psfs": [{
        "dataset": DATASET, "fid": FID, "sca": SCA, "filename": files[role].as_uri(),
        "checksum": "0" * 32, "status": 1}]}) for role in ("science_psf", "reference_psf")]

    runner, catalog = FakeToolRunner(), FakePsfCatalog()
    monkeypatch.setattr(difference, "toolkit", lambda: difference.Toolkit(
        runner=runner, sip_to_pv=fake_sip_to_pv, psf_catalog=catalog))
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", (tmp_path / "outputs").as_uri())
    key = {"dataset": DATASET, "expid": EXPID, "sca": SCA, "fid": FID}
    inputs = [{"product": "l2files", "key": key, "source_job": l2_job},
              {"product": "refimages", "key": {"dataset": DATASET, "field": FIELD, "fid": FID},
               "source_job": ref_job},
              *({"product": "psfs", "key": {"dataset": DATASET, "fid": FID, "sca": SCA},
                 "source_job": job} for job in psf_jobs)]
    code = cli.main(["job", "local", "--run", str(run), "--stage", "difference",
                     "--scope", json.dumps({"expid": EXPID, "sca": SCA, "fid": FID}),
                     "--inputs", json.dumps(inputs),
                     "--expects", json.dumps([{"product": p, "key": key, "binding": None}
                                              for p in ("diffimages", "sources")])])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["published"] and result["group"] is not None

    ((job, pid, rid, rfid),) = query(conn, "SELECT job, pid, rid, rfid FROM diffimages_official")
    assert (job, rid, rfid) == (result["job"], 7001, 90011)
    counts = query(conn, "SELECT count(*), count(DISTINCT pid), min(pid) FROM sources_official")
    assert counts[0][0] >= 1 and counts[0][1:] == (1, pid)
