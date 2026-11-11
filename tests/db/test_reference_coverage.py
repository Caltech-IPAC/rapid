"""A reference selected by coverage, against the schema: the declined state, covfrac and covdepth, and the loop.

Contracts: a job whose manifest declines publishes nothing (no rows, no
binding, no ``published_attempt``), records state ``declined``, its
manifest and the reason, and is never published again, and ``job run``
stops before its stage; a reference's ``covfrac`` and ``covdepth`` load
from its manifest's row, null when it carries none; the loop's snapshot
reads a declined reference job's frames, so the pass holds its field with
the reason, never counts it failed or replans the same frames, and plans it
once a new frame is selected.
"""

from __future__ import annotations

import json
from datetime import datetime

from rapidpipe.foundation import change, create_job, create_run, job_show, loop, publish, run_job
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
REASON = "measured coverage 0.4800 < 0.5"


def query(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.commit()
    return rows


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


def declined_attempt(root) -> str:
    url = write_attempt(root, {})
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({**json.loads(manifest.read_text()), "declined": REASON,
                                    "reference": {"coverage": {"measured": 0.48}}}))
    return url


def reference_job(conn, run: int, sources: dict[int, int]) -> int:
    return create_job(conn, run, "reference", {"field": FIELD, "fid": FID, "sca": SCA},
                      inputs=[{"product": "l2files", "key": l2_key(e), "source_job": j} for e, j in sources.items()],
                      expects=[{"product": "refimages", "key": {"dataset": DATASET, "field": FIELD, "fid": FID},
                                "binding": None}])


def test_a_declined_job_publishes_nothing_records_why_and_is_never_published_again(connect, tmp_path):
    conn = connect()
    run = live_run(conn)
    l2 = publish_l2(conn, tmp_path, run, 1, 1.0)
    job = reference_job(conn, run, {1: l2})
    prefix = declined_attempt(tmp_path / "ref" / "1")

    result = publish(conn, job, 1, prefix)
    assert (result["published"], result["declined"], result["group"]) == (False, REASON, None)
    shown = job_show(conn, job)
    assert (shown["state"], shown["refusal"], shown["published_attempt"]) == ("declined", REASON, None)
    assert shown["manifest"]["prefix"] == prefix and shown["manifest"]["declined"] == REASON
    assert shown["ended"] is not None and shown["bindings"] == []
    assert query(conn, "SELECT count(*) FROM refimages WHERE job = %s", (job,)) == [(0,)]

    again = publish(conn, job, 2, write_attempt(tmp_path / "ref" / "2", {}))
    assert not again["published"] and again["declined"] == REASON
    assert job_show(conn, job)["state"] == "declined"

    def never(context):
        raise AssertionError("a declined job's stage is not run again")

    rerun = run_job(connect, job, (tmp_path / "outputs").as_uri(), never)
    assert (rerun["published"], rerun["declined"]) == (False, REASON)


def test_a_references_covfrac_and_covdepth_load_from_its_manifest_and_are_null_without(connect, tmp_path):
    conn = connect()
    run = live_run(conn)
    base = {"dataset": DATASET, "fid": FID, "filename": "file:///ref.fits", "checksum": "0" * 32,
            "status": 1, "infobits": 0, "nframes": 2, "cov5percent": 40.0}
    for n, (field, covfrac, covdepth) in enumerate(((FIELD, 0.62, 1.0), (FIELD + 1, None, None))):
        job = create_job(conn, run, "reference", {"field": field, "fid": FID, "sca": SCA})
        publish(conn, job, 1, write_attempt(tmp_path / f"ref-{n}", {"refimages": [
            {**base, "rfid": 900 + n, "field": field, "covfrac": covfrac, "covdepth": covdepth}]}))
    rows = query(conn, "SELECT field, covfrac, covdepth FROM refimages WHERE rfid IN (900, 901) ORDER BY rfid")
    assert [(f, None if c is None else round(c, 4), d) for f, c, d in rows] == [
        (FIELD, 0.62, 1.0), (FIELD + 1, None, None)]


def test_the_loop_holds_a_declined_field_not_failed_until_a_new_frame_is_selected(connect, tmp_path):
    conn = connect()
    run = live_run(conn)
    first = publish_l2(conn, tmp_path, run, 1, 1.0)
    job = reference_job(conn, run, {1: first})
    publish(conn, job, 1, declined_attempt(tmp_path / "ref"))
    selection = loop.Selection.parse("F146", str(SCA), "all")

    subs, counts = loop.plan(loop.snapshot(conn, run), [], selection)
    assert [s for s in subs if s["stage"] == "reference"] == [] and counts["failed"] == []
    assert counts["held"] == [{"field": FIELD, "fid": FID, "reason": f"job {job} declined these frames: {REASON}"}]
    assert not any(counts["pending"].values())

    second = publish_l2(conn, tmp_path, run, 2, 2.0)
    subs, counts = loop.plan(loop.snapshot(conn, run), [], selection)
    (ref,) = [s for s in subs if s["stage"] == "reference"]
    assert [(i["key"]["expid"], i["source_job"]) for i in ref["inputs"]] == [(1, first), (2, second)]
    assert counts["held"] == [] and counts["failed"] == []
