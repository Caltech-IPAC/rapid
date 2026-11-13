"""The loop's snapshot reads each official L2 row's program from its exposure's official exposures row.

Contract: :func:`rapidpipe.foundation.loop.snapshot` gives every official
``l2files`` row of the dataset the ``program_id`` of the official
``exposures`` row of its (dataset, expid), or ``None`` while that exposure
has none; a ``--programs`` selection over a two-program dataset then plans
a reference from the selected program's frames only, and, once the field
has an official reference, differences only the selected program's rows.
Run by foundation.yml against database/schema.sql.
"""

from __future__ import annotations

import dataclasses
import datetime

from rapidpipe.foundation import loop
from tests.db.proof.fixture import CURRENT, DATASET, World

SETTINGS = {"reference": {"selection": {"min_frames": 1}},
            "packet": {"gaia_match": False, "ned_match": False, "lvs_match": False, "ss_match": False}}
#: F184, the filters seed's fid 1; SCA 1; tessellation field 7.
FID, SCA, FIELD = 1, 1, 7


def _key(expid: int) -> dict:
    return {"dataset": DATASET, "expid": expid, "sca": SCA, "fid": FID}


def _l2file(expid: int) -> dict:
    """An L2 row of exposure ``expid`` centred in :data:`FIELD`."""
    ra, dec = 10.0, -5.0
    return {**_key(expid), "field": FIELD, "hp6": 0, "hp9": 0,
            "dateobs": datetime.datetime(2026, 10, 1) + datetime.timedelta(days=expid),
            "mjdobs": 61000.0 + expid, "exptime": 60.0, "filename": f"l2-{expid}.fits",
            "checksum": "0" * 32, "status": 1, "crval1": ra, "crval2": dec, "crpix1": 1.0,
            "crpix2": 1.0, "cd11": 1e-5, "cd12": 0.0, "cd21": 0.0, "cd22": 1e-5, "ra": ra,
            "dec": dec, "overlapfields": [FIELD],
            **{f"{c}{n}": v for n in range(5) for c, v in (("ra", ra), ("dec", dec))}}


def _intake(w: World, run: int, expid: int, program: int | None) -> None:
    """Publish exposure ``expid``'s L2 row, and its exposures row of ``program`` unless ``None``."""
    outputs = {"l2files": [_l2file(expid)]}
    expects = [{"product": "l2files", "key": _key(expid), "binding": CURRENT}]
    if program is not None:
        outputs["exposures"] = [{"dataset": DATASET, "expid": expid, "exptime": 60.0, "program_id": program,
                                 "dateobs": datetime.datetime(2026, 10, 1)}]
        expects.append({"product": "exposures", "key": {"dataset": DATASET, "expid": expid},
                        "binding": CURRENT})
    w.job(run, "import", {"expid": expid}, outputs, expects=expects)


def test_the_snapshot_reads_program_id_and_a_two_program_dataset_plans_only_the_selected_one(connect, tmp_path):
    w = World(connect(), tmp_path)
    run = w.run("programs", SETTINGS, live=True)
    _intake(w, run, 1, 1049)
    _intake(w, run, 2, 1018)
    _intake(w, run, 3, None)                 # its exposures row not yet official
    snap = loop.snapshot(w.conn, run)
    assert {k[0]: v["program"] for k, v in snap.l2.items()} == {1: 1049, 2: 1018, 3: None}

    sel = loop.Selection.parse("F184", str(SCA), "all", programs="1049")
    subs, _ = loop.plan(snap, [], sel)
    refs = [s for s in subs if s["stage"] == "reference"]
    assert len(refs) == 1 and [i["key"]["expid"] for i in refs[0]["inputs"]] == [1]
    everyone, _ = loop.plan(snap, [], loop.Selection.parse("F184", str(SCA), "all"))
    assert [i["key"]["expid"] for s in everyone if s["stage"] == "reference" for i in s["inputs"]] == [1, 2, 3]

    w.job(run, "import", {"reference": FIELD}, {"refimages": [{
        "dataset": DATASET, "field": FIELD, "fid": FID, "filename": "ref.fits", "checksum": "0" * 32,
        "status": 1, "nframes": 2, "cov5percent": 50.0}]},
        expects=[{"product": "refimages", "key": {"dataset": DATASET, "field": FIELD, "fid": FID},
                  "binding": CURRENT}])
    snap = dataclasses.replace(loop.snapshot(w.conn, run), psfs={(FID, SCA, "science"): (1, 1)})
    subs, _ = loop.plan(snap, [], sel)
    assert [s["stage"] for s in subs] == ["difference"] and subs[0]["scope"]["expid"] == 1
