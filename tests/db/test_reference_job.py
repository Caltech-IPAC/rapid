"""``job local --stage reference`` publishes a reference from the two L2 rows its job names.

Contract: under a live run, a reference job whose inputs are two published
``l2files`` rows coadds them (fake tools), and its ``refimages`` row binds
to the job's expected key; the official views show the row, its two
``refimimages`` members (the named L2 rows) and its mosaic catalog, all
under the one ``rfid`` the database drew.
"""

from __future__ import annotations

import json
from datetime import datetime

from rapidpipe.cli import main as cli
from rapidpipe.foundation import change, create_job, create_run, publish
from tests.support import fakereftools
from tests.support.fakereftools import FRAMES, RTID, write_frames
from rapidpipe.stages import reference
from tests.db.foundation_helpers import write_attempt
from tests.unit.test_reference_stage import settings

SURVEY, DATASET, FID, SCA = "S", "D", 8, 1


def l2_row(expid: int, rid: int, path, mjdobs: float) -> dict:
    ra, dec = fakereftools.TILE_CENTRE
    corners = {f"{a}{i}": (ra if a == "ra" else dec) for i in range(5) for a in ("ra", "dec")}
    return {"rid": rid, "dataset": DATASET, "expid": expid, "sca": SCA, "fid": FID,
            "field": RTID, "hp6": 0, "hp9": 0, "dateobs": datetime(2027, 10, 7),
            "mjdobs": mjdobs, "exptime": 140.0, "filename": path.as_uri(), "checksum": "0" * 32,
            "crval1": ra, "crval2": dec, "crpix1": 48.5, "crpix2": 48.5, "cd11": -3e-5,
            "cd12": 0.0, "cd21": 0.0, "cd22": 3e-5, "ra": ra, "dec": dec, **corners}


def query(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.commit()
    return rows


def test_job_local_publishes_a_bound_reference_from_two_named_l2_rows(connect, tmp_path,
                                                                     monkeypatch, capsys):
    conn = connect()
    run = create_run(conn, "tester", "reference", "abc123", settings(), SURVEY, DATASET)
    change(conn, [{"open": {"survey": SURVEY, "dataset": DATASET, "run": run}}], "tester", "live")
    paths = write_frames(tmp_path / "l2")[:2]
    l2_job = create_job(conn, run, "import", {})
    publish(conn, l2_job, 1, write_attempt(tmp_path / "import", {"l2files": [
        l2_row(100 + n, 7000 + n, path, FRAMES[n][4]) for n, path in enumerate(paths)]}))

    monkeypatch.setattr(reference, "toolkit", fakereftools.fake_toolkit)
    monkeypatch.setenv("RAPIDPIPE_WORK", str(tmp_path / "work"))
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", (tmp_path / "outputs").as_uri())
    inputs = [{"product": "l2files", "source_job": l2_job,
               "key": {"dataset": DATASET, "expid": 100 + n, "sca": SCA, "fid": FID}}
              for n in range(2)]
    key = {"dataset": DATASET, "field": RTID, "fid": FID}
    code = cli.main(["job", "local", "--run", str(run), "--stage", "reference",
                     "--scope", json.dumps({"field": RTID, "fid": FID, "sca": SCA}),
                     "--inputs", json.dumps(inputs),
                     "--expects", json.dumps([{"product": "refimages", "key": key,
                                               "binding": None}])])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["published"] and result["group"] is not None

    ((job, rfid, nframes),) = query(conn, "SELECT job, rfid, nframes FROM refimages_official")
    assert (job, nframes) == (result["job"], 2)
    assert query(conn, "SELECT rid FROM refimimages_official WHERE rfid = %s ORDER BY rid",
                 (rfid,)) == [(7000,), (7001,)]
    assert query(conn, "SELECT cattype FROM refimcatalogs_official WHERE rfid = %s",
                 (rfid,)) == [(1,)]
