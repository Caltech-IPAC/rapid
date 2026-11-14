"""A reference selected by coverage, against the schema: the loop's snapshot and plan.

Contract: the loop's snapshot reads each official L2 row's centre and
corners beside its program, so a pass holds a field whose selected frames
are estimated short of ``min_coverage_fraction`` (reported under ``held``,
neither planned nor failed) and plans it once a new frame lifts the
estimate, naming both frames.
"""

from __future__ import annotations

import json
from datetime import datetime

from rapidpipe.foundation import change, create_job, create_run, loop, publish
from rapidpipe.products.coverage import make_refimage_wcs
from rapidpipe.products.spatial import overlapping_fields, tessellation_field
from tests.db.foundation_helpers import write_attempt

SURVEY, DATASET, FID, SCA = "S", "D", 8, 8
CENTRE = (150.0293, 2.01472)                 # tile 3034797's centre
FIELD = 3034797
PIX = 0.11 / 3600.0
SETTINGS = {"reference": {"selection": {"selection_type": "coverage", "min_frames": 2, "max_frames": 50,
                                        "min_coverage_depth": 1, "min_coverage_fraction": 0.5,
                                        "max_coverage_depth": 4, "max_coverage_fraction": 1.0,
                                        "coverage_estimate_tolerance": 0.05},
                          "mosaic": {"naxis1": 9100, "naxis2": 9100, "cdelt1": -PIX, "cdelt2": PIX,
                                     "rotation": 0.0, "ra_center": "", "dec_center": ""}},
            "packet": {"gaia_match": False, "ned_match": False, "lvs_match": False}}


def live_run(conn) -> int:
    run = create_run(conn, "tester", "coverage", "abc123", SETTINGS, SURVEY, DATASET)
    change(conn, [{"open": {"survey": SURVEY, "dataset": DATASET, "run": run}}], "tester", "live")
    return run


def l2_key(expid: int) -> dict:
    return {"dataset": DATASET, "expid": expid, "sca": SCA, "fid": FID}


def publish_l2(conn, tmp_path, run: int, expid: int, mjdobs: float, size: float = 0.3) -> int:
    """Publish and bind one L2 row of a ``size``-degree frame centred on the field's centre; return its job."""
    ra, dec = CENTRE
    n = int(size / PIX)
    w = make_refimage_wcs(ra, dec, n, n, -PIX, PIX, 0.0)
    sky = w.pixel_to_world([-0.5, n - 0.5, n - 0.5, -0.5], [-0.5, -0.5, n - 0.5, n - 0.5])
    corners = {f"{a}{i + 1}": float(v[i]) for a, v in (("ra", sky.ra.deg), ("dec", sky.dec.deg)) for i in range(4)}
    row = {"rid": expid, **l2_key(expid), "field": tessellation_field(ra, dec), "hp6": 0, "hp9": 0,
           "dateobs": datetime(2027, 10, 7), "mjdobs": mjdobs, "exptime": 60.0,
           "filename": f"file:///l2/{expid}.fits", "checksum": "0" * 32, "crval1": ra, "crval2": dec,
           "crpix1": n / 2 + 0.5, "crpix2": n / 2 + 0.5, "cd11": -PIX, "cd12": 0.0, "cd21": 0.0, "cd22": PIX,
           "ra": ra, "dec": dec, "ra0": ra, "dec0": dec, **corners,
           "overlapfields": overlapping_fields(ra, dec, n / 2 + 0.5, n / 2 + 0.5, -PIX, 0.0, 0.0, PIX, n, n)}
    job = create_job(conn, run, "import", {}, expects=[{"product": "l2files", "key": l2_key(expid), "binding": None}])
    publish(conn, job, 1, write_attempt(tmp_path / f"l2-{expid}", {"l2files": [row]}))
    return job


def test_the_loop_holds_a_field_short_of_coverage_and_plans_it_once_a_frame_lifts_it(connect, tmp_path):
    conn = connect()
    run = live_run(conn)
    first = publish_l2(conn, tmp_path, run, 1, 1.0, size=0.1)
    selection = loop.Selection.parse("F146", str(SCA), "all")

    subs, counts = loop.plan(loop.snapshot(conn, run), [], selection)
    assert [s for s in subs if s["stage"] == "reference"] == [] and counts["failed"] == []
    ((held,),) = [[h for h in counts["held"] if h["field"] == FIELD]]
    assert held["reason"].startswith("estimated coverage ") and "< 0.5" in held["reason"]

    second = publish_l2(conn, tmp_path, run, 2, 2.0)
    subs, counts = loop.plan(loop.snapshot(conn, run), [], selection)
    (ref,) = [s for s in subs if s["stage"] == "reference" and s["scope"]["field"] == FIELD]
    assert [(i["key"]["expid"], i["source_job"]) for i in ref["inputs"]] == [(1, first), (2, second)]
    assert [h for h in counts["held"] if h["field"] == FIELD] == []
