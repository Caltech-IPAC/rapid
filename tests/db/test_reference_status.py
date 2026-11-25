"""A reference graded status 0 fails its job, and a measured reference PSF binds with a good one.

Contracts, against database/schema.sql, with ``job local`` and the fake
tools: a status 0 reference (a fatal InfoBit) exits 65, leaves its job
``failed`` with its fatal bits in the refusal and loads no row of any
table; the loop then plans no difference against it, does not plan it
again in the same run, and reports it under ``failed``. A status 1
reference binds as before. With ``[reference.psfcat] enabled`` under
``"measured"`` the measured PSF's 18 ``reference`` ``psfs`` rows (set
``refimage-<field>-<filter>``) bind in the reference's own change group,
a status 0 reference loads none, and a measurement that fell back to the
run's set leaves their keys out of the binding while the reference binds.
"""

from __future__ import annotations

import json

from rapidpipe.cli import main as cli
from rapidpipe.foundation import change, create_job, create_run, loop, publish
from rapidpipe.stages import reference
from tests.db.foundation_helpers import write_attempt
from tests.db.test_reference_job import l2_row, query
from tests.support import fakereftools
from tests.support.fakereftools import FRAMES, RTID, write_frames
from tests.unit.test_reference_stage import FakeCatalog, measured_psf, settings, too_few, write_psf

SURVEY, DATASET, FID, SCA = "S", "D", 8, 1
MEASURED_SET = "refimage-4711398-F146"
REFERENCE_SET = "S1"


def live_run(conn, body: dict) -> int:
    run = create_run(conn, "tester", "reference status", "abc123", body, SURVEY, DATASET)
    change(conn, [{"open": {"survey": SURVEY, "dataset": DATASET, "run": run}}], "tester", "live")
    return run


def l2_inputs(conn, run, tmp_path) -> list[dict]:
    """Two official L2 rows, as the loop's snapshot reads them, and the inputs naming them."""
    paths = write_frames(tmp_path / "l2")[:2]
    keys = [{"dataset": DATASET, "expid": 100 + n, "sca": SCA, "fid": FID} for n in range(2)]
    l2_job = create_job(conn, run, "import", {}, expects=[
        {"product": "l2files", "key": key, "binding": None} for key in keys])
    publish(conn, l2_job, 1, write_attempt(tmp_path / "import", {"l2files": [
        l2_row(100 + n, 7000 + n, path, FRAMES[n][4]) for n, path in enumerate(paths)]}))
    return [{"product": "l2files", "source_job": l2_job, "key": key} for key in keys]


def psf_rows(conn, run, tmp_path) -> tuple[int, int]:
    """A science row of the scope's filter and SCA and a set's reference row, official; their jobs."""
    (tmp_path / "psfs").mkdir()
    jobs = []
    for kind, name in (("science", "science.fits"), ("reference", "refimage_psf_fid8.fits")):
        path = tmp_path / "psfs" / name
        md5 = write_psf(path, sigma=1.2 if kind == "science" else 1.4)
        key = {"dataset": DATASET, "fid": FID, "sca": SCA, "kind": kind}
        row = {**key, "filename": path.as_uri(), "checksum": md5, "status": 1}
        if kind == "reference":
            key["psf_set"] = row["psf_set"] = REFERENCE_SET
        else:
            row["psf_set"] = None
        job = create_job(conn, run, "import", {}, expects=[{"product": "psfs", "key": key, "binding": None}])
        publish(conn, job, 1, write_attempt(tmp_path / f"psf-{kind}", {"psfs": [row]}))
        jobs.append(job)
    return jobs[0], jobs[1]


def job_local(run, inputs, expects, capsys) -> tuple[int, dict]:
    code = cli.main(["job", "local", "--run", str(run), "--stage", "reference",
                     "--scope", json.dumps({"field": RTID, "fid": FID, "sca": SCA}),
                     "--inputs", json.dumps(inputs), "--expects", json.dumps(expects)])
    return code, json.loads(capsys.readouterr().out)


def ref_expects() -> list[dict]:
    return [{"product": "refimages", "key": {"dataset": DATASET, "field": RTID, "fid": FID}, "binding": None}]


def psf_expects() -> list[dict]:
    return [{"product": "psfs", "binding": None,
             "key": {"dataset": DATASET, "fid": FID, "sca": s, "kind": "reference", "psf_set": MEASURED_SET}}
            for s in range(1, 19)]


def fakes(monkeypatch, tmp_path, measure=None, made=None):
    monkeypatch.setattr(reference, "toolkit", lambda: reference.Toolkit(
        runner=fakereftools.FakeReferenceToolRunner(), reference_psf=measure or measured_psf(),
        psf_catalog=made or FakeCatalog()))
    monkeypatch.setenv("RAPIDPIPE_WORK", str(tmp_path / "work"))
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", (tmp_path / "outputs").as_uri())


def reference_job(conn, run) -> int:
    ((job,),) = query(conn, "SELECT id FROM jobs WHERE run = %s AND stage = 'reference'", (run,))
    return job


def test_a_status_0_reference_fails_65_loads_nothing_and_is_never_differenced(connect, tmp_path,
                                                                             monkeypatch, capsys):
    conn = connect()
    body = {**settings(infobits={"t08": 10}), "difference": {"reference_psf": "measured", "reference_psf_set": ""}}
    run = live_run(conn, body)
    inputs = l2_inputs(conn, run, tmp_path)
    fakes(monkeypatch, tmp_path)
    code = cli.main(["job", "local", "--run", str(run), "--stage", "reference",   # 4 sources under t08 = 10: bit 8
                     "--scope", json.dumps({"field": RTID, "fid": FID, "sca": SCA}),
                     "--inputs", json.dumps(inputs), "--expects", json.dumps(ref_expects())])
    capsys.readouterr()
    job = reference_job(conn, run)
    ((state, exit_code, refusal),) = query(conn, "SELECT state, exit_code, refusal FROM jobs WHERE id = %s", (job,))
    assert (code, state, exit_code) == (65, "failed", 65) and "reference status 0: fatal InfoBits 8 " in refusal
    for table in ("refimages", "refimimages", "refimcatalogs", "refimmeta"):
        assert query(conn, f"SELECT count(*) FROM {table} WHERE job = %s", (job,)) == [(0,)]
    assert query(conn, "SELECT count(*) FROM official WHERE job = %s", (job,)) == [(0,)]

    snap = loop.snapshot(conn, run)
    assert snap.refs == {} and len(snap.l2) == 2
    subs, counts = loop.plan(snap, [], loop.Selection.parse("F146", str(SCA), "all"))
    assert subs == []
    assert [(f["job"], f["stage"]) for f in counts["failed"]] == [(job, "reference")]


def test_a_status_1_reference_binds_as_before(connect, tmp_path, monkeypatch, capsys):
    conn = connect()
    run = live_run(conn, settings(infobits={"t08": 1}))
    inputs = l2_inputs(conn, run, tmp_path)
    fakes(monkeypatch, tmp_path)
    code, result = job_local(run, inputs, ref_expects(), capsys)
    assert code == 0 and result["group"] is not None and result["refusal"] is None
    assert query(conn, "SELECT status, job FROM refimages_official") == [(1, result["job"])]


def test_a_measured_psf_binds_its_18_rows_in_the_references_group(connect, tmp_path, monkeypatch, capsys):
    conn = connect()
    body = {**settings(psfcat={"enabled": True}, infobits={"t08": 1}),
            "difference": {"reference_psf": "measured", "reference_psf_set": ""}}
    run = live_run(conn, body)
    inputs = l2_inputs(conn, run, tmp_path)
    science_job, _ = psf_rows(conn, run, tmp_path)
    science = {"product": "psfs", "source_job": science_job,
               "key": {"dataset": DATASET, "fid": FID, "sca": SCA, "kind": "science"}}
    fakes(monkeypatch, tmp_path)
    code, result = job_local(run, [*inputs, science], [*ref_expects(), *psf_expects()], capsys)
    assert code == 0 and result["refusal"] is None
    rows = query(conn, "SELECT sca, psf_set, filename, checksum FROM psfs_official WHERE kind = 'reference'"
                       " AND job = %s ORDER BY sca", (result["job"],))
    assert [r[0] for r in rows] == list(range(1, 19)) and {r[1] for r in rows} == {MEASURED_SET}
    assert len({(r[2], r[3]) for r in rows}) == 1 and rows[0][2].endswith("/ref/refimage_psf.fits")
    assert query(conn, "SELECT DISTINCT opened_group FROM official WHERE job = %s AND closed_at IS NULL",
                 (result["job"],)) == [(result["group"],)]
    assert query(conn, "SELECT cattype FROM refimcatalogs_official ORDER BY cattype") == [(1,), (2,)]


def test_a_status_0_measured_reference_loads_no_psf_rows(connect, tmp_path, monkeypatch, capsys):
    conn = connect()
    body = {**settings(psfcat={"enabled": True}, infobits={"t08": 10}),
            "difference": {"reference_psf": "measured", "reference_psf_set": ""}}
    run = live_run(conn, body)
    inputs = l2_inputs(conn, run, tmp_path)
    science_job, _ = psf_rows(conn, run, tmp_path)
    science = {"product": "psfs", "source_job": science_job,
               "key": {"dataset": DATASET, "fid": FID, "sca": SCA, "kind": "science"}}
    fakes(monkeypatch, tmp_path)
    code = cli.main(["job", "local", "--run", str(run), "--stage", "reference",
                     "--scope", json.dumps({"field": RTID, "fid": FID, "sca": SCA}),
                     "--inputs", json.dumps([*inputs, science]),
                     "--expects", json.dumps([*ref_expects(), *psf_expects()])])
    capsys.readouterr()
    job = reference_job(conn, run)
    assert code == 65 and query(conn, "SELECT state FROM jobs WHERE id = %s", (job,)) == [("failed",)]
    assert query(conn, "SELECT count(*) FROM psfs WHERE job = %s", (job,)) == [(0,)]
    assert query(conn, "SELECT count(*) FROM official WHERE job = %s", (job,)) == [(0,)]


def test_a_fallback_leaves_the_measured_keys_out_and_binds_the_reference(connect, tmp_path, monkeypatch, capsys):
    conn = connect()
    body = {**settings(psfcat={"enabled": True}, infobits={"t08": 1}),
            "difference": {"reference_psf": "measured", "reference_psf_set": REFERENCE_SET}}
    run = live_run(conn, body)
    inputs = l2_inputs(conn, run, tmp_path)
    science_job, set_job = psf_rows(conn, run, tmp_path)
    psfs = [{"product": "psfs", "source_job": science_job,
             "key": {"dataset": DATASET, "fid": FID, "sca": SCA, "kind": "science"}},
            {"product": "psfs", "source_job": set_job,
             "key": {"dataset": DATASET, "fid": FID, "sca": SCA, "kind": "reference", "psf_set": REFERENCE_SET}}]
    fakes(monkeypatch, tmp_path, measure=too_few)
    code, result = job_local(run, [*inputs, *psfs], [*ref_expects(), *psf_expects()], capsys)
    assert code == 0 and result["refusal"] is None and result["group"] is not None
    assert query(conn, "SELECT product FROM official WHERE job = %s AND closed_at IS NULL", (result["job"],)) == [
        ("refimages",)]
    assert query(conn, "SELECT count(*) FROM psfs WHERE job = %s", (result["job"],)) == [(0,)]
    (manifest,) = query(conn, "SELECT manifest FROM jobs WHERE id = %s", (result["job"],))[0]
    assert manifest["refpsf"]["fallback"] is True and manifest["unbound"] == ["psfs"]
