"""A reference PSF set imported by ``scripts/import_psf_set.py`` and read by a difference job.

Contracts, against database/schema.sql: a set's import job opens every
key's binding in one change group; importing it again without ``--force``
skips every key and submits nothing, and with ``--force`` replaces the
bindings in one group that ``official undo`` reverts; two sets are official
side by side in one dataset, and the science PSF's key is untouched; two
runs naming different sets each bind their own row to a difference job, and
a run naming a set with no official row plans none and says why; a
difference job under ``"precomputed"`` reads the bound row, writes it as
``refimage_psf.fits`` and records its ``psfid``.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from rapidpipe.cli import main as cli
from rapidpipe.foundation import change, create_run, loop, undo
from tests.db.test_difference_job import published, query
from tests.support.fakedifftools import (
    CRVAL,
    FakePsfCatalog,
    FakeReferencePsf,
    FakeToolRunner,
    fake_rb_classifier,
    fake_rb_model,
    fake_sip_to_pv,
    write_epoch,
)
from rapidpipe.stages import difference
from tests.unit.test_difference_stage import THIN, settings

_SPEC = importlib.util.spec_from_file_location(
    "import_psf_set", Path(__file__).resolve().parents[2] / "scripts" / "import_psf_set.py")
import_psf_set = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(import_psf_set)

SURVEY, DATASET, FID, SCA, EXPID, FIELD = "S", "D", 1, 7, 84613, 5321


def set_dir(root: Path, files: dict[str, float], size: int = 9) -> Path:
    """A set directory: each named file a ``size`` square Gaussian of the given sigma."""
    root.mkdir(parents=True)
    rows, cols = np.indices((size, size), dtype=float)
    c = (size - 1) / 2
    for name, sigma in files.items():
        stamp = np.exp(-((cols - c) ** 2 + (rows - c) ** 2) / (2 * sigma ** 2))
        fits.PrimaryHDU(stamp.astype(np.float32), fits.Header({"PSFKIND": "sciimage_psf"})).writeto(root / name)
    (root / "README.txt").write_text("not a set file")
    return root


def live_run(conn, body: dict | None = None) -> int:
    run = create_run(conn, "tester", "psf set", "abc123",
                     body or {"import": THIN["import"]}, SURVEY, DATASET)
    change(conn, [{"open": {"survey": SURVEY, "dataset": DATASET, "run": run}}], "tester", "live")
    return run


def import_set(capsys, run: int, directory: Path, psf_set: str, *extra: str) -> tuple[int, str, str]:
    code = import_psf_set.main(["--run", str(run), "--dir", str(directory), "--psf-set", psf_set, *extra])
    out, err = capsys.readouterr()
    return code, out, err


def official_set(conn, psf_set: str) -> list[tuple]:
    return query(conn, "SELECT o.job, o.opened_group, p.fid, p.sca, p.filename FROM official o"
                       " JOIN psfs p ON p.job = o.job AND p.psf_set = o.key->>'psf_set'"
                       " AND p.fid = (o.key->>'fid')::int AND p.sca = (o.key->>'sca')::int"
                       " WHERE o.product = 'psfs' AND o.closed_at IS NULL AND o.key->>'psf_set' = %s"
                       " ORDER BY p.fid, p.sca", (psf_set,))


@pytest.fixture()
def outputs(tmp_path, monkeypatch):
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", (tmp_path / "outputs").as_uri())


def test_a_set_imports_in_one_group_and_again_only_with_force_which_undo_reverts(
        connect, tmp_path, capsys, outputs):
    conn = connect()
    run = live_run(conn)
    directory = set_dir(tmp_path / "set", {"refimage_psf_fid1.fits": 1.5, "refimage_psf_fid2_sca03.fits": 1.6})

    code, out, _ = import_set(capsys, run, directory, "S1")
    first = json.loads(out)
    assert code == 0 and first["published"] and first["refusal"] is None
    rows = official_set(conn, "S1")
    assert sorted((fid, sca) for _, _, fid, sca, _ in rows) == sorted(
        [(1, sca) for sca in range(1, 19)] + [(2, 3)])
    assert {(job, group) for job, group, *_ in rows} == {(first["job"], first["group"])}
    assert len({name for *_, fid, _, name in rows if fid == 1}) == 1          # one copy for 18 rows

    jobs_before = query(conn, "SELECT count(*) FROM jobs")
    code, out, err = import_set(capsys, run, directory, "S1")
    assert (code, out) == (0, "") and "nothing submitted" in err
    assert query(conn, "SELECT count(*) FROM jobs") == jobs_before

    code, out, _ = import_set(capsys, run, directory, "S1", "--force")
    forced = json.loads(out)
    assert code == 0 and forced["refusal"] is None
    assert {(job, group) for job, group, *_ in official_set(conn, "S1")} == {(forced["job"], forced["group"])}
    assert query(conn, "SELECT count(*) FROM psfs WHERE psf_set = 'S1'") == [(38,)]   # the old rows kept

    undo(conn, forced["group"], "tester", "back to the first import")
    assert {job for job, *_ in official_set(conn, "S1")} == {first["job"]}


def test_two_sets_are_official_side_by_side_and_the_science_psf_is_untouched(
        connect, tmp_path, capsys, outputs):
    conn = connect()
    run = live_run(conn)
    science_key = {"dataset": DATASET, "fid": FID, "sca": SCA, "kind": "science"}
    published(conn, run, tmp_path, "science_psf", {"psfs": [{
        **science_key, "filename": "file:///science.fits", "checksum": "0" * 32, "status": 1}]},
        expects=[{"product": "psfs", "key": science_key, "binding": None}])
    for psf_set in ("S1", "S2"):
        code, _, _ = import_set(capsys, run, set_dir(tmp_path / psf_set, {"refimage_psf_fid1.fits": 1.5}),
                                psf_set)
        assert code == 0
    assert query(conn, "SELECT psf_set, count(*) FROM psfs_official WHERE kind = 'reference'"
                       " GROUP BY psf_set ORDER BY psf_set") == [("S1", 18), ("S2", 18)]
    assert query(conn, "SELECT fid, sca, psf_set FROM psfs_official WHERE kind = 'science'") == [
        (FID, SCA, None)]
    assert query(conn, "SELECT key FROM official WHERE product = 'psfs' AND key->>'kind' = 'science'"
                       " AND closed_at IS NULL") == [(science_key,)]

    # Two runs naming different sets each bind their own row; a set with no row plans nothing.
    l2_key = (EXPID, SCA, FID)
    for psf_set in ("S1", "S2", "S9"):
        body = settings()
        body["difference"].update(reference_psf="precomputed", reference_psf_set=psf_set)
        other = create_run(conn, "tester", f"reads {psf_set}", "abc123",
                           {**body, "reference": {"selection": {"min_frames": 2}}}, SURVEY, DATASET)
        snap = dataclasses.replace(loop.snapshot(conn, other), refs={(FIELD, FID): 1},
                                   l2={l2_key: {"field": FIELD, "overlapfields": [FIELD], "mjdobs": 1.0,
                                                "job": 1}})
        selection = loop.Selection.parse("F184", str(SCA), "all")
        if psf_set == "S9":
            with pytest.raises(loop.NotFound, match="no official reference psfs row of set 'S9'"):
                loop.plan(snap, [], selection)
            continue
        (diff,) = [s for s in loop.plan(snap, [], selection)[0] if s["stage"] == "difference"]
        (reference,) = [i for i in diff["inputs"] if i["key"].get("kind") == "reference"]
        assert reference["key"]["psf_set"] == psf_set
        assert query(conn, "SELECT psf_set FROM psfs WHERE job = %s AND fid = %s AND sca = %s",
                     (reference["source_job"], FID, SCA)) == [(psf_set,)]


def test_a_precomputed_difference_job_reads_its_bound_row_and_records_its_psfid(
        connect, tmp_path, capsys, monkeypatch, outputs):
    conn = connect()
    body = settings()
    body["difference"].update(reference_psf="precomputed", reference_psf_set="S1")
    run = live_run(conn, {**body, "import": THIN["import"], "reference": {"selection": {"min_frames": 2}}})
    files = write_epoch(tmp_path / "inputs")
    science = fits.getdata(files["science_psf"])
    code, out, _ = import_set(capsys, run, set_dir(tmp_path / "set", {"refimage_psf_fid1.fits": 1.4},
                                                   size=science.shape[0]), "S1")
    assert code == 0
    ra, dec = CRVAL
    corners = {f"{a}{i}": (ra if a == "ra" else dec) for i in range(5) for a in ("ra", "dec")}
    key = {"dataset": DATASET, "expid": EXPID, "sca": SCA, "fid": FID}
    l2_job = published(conn, run, tmp_path, "l2", {"l2files": [{
        "rid": 7001, **key, "field": FIELD, "hp6": 0, "hp9": 0, "dateobs": datetime(2027, 10, 7),
        "mjdobs": 61273.0087, "exptime": 140.0, "filename": files["science"].as_uri(), "checksum": "0" * 32,
        "crval1": ra, "crval2": dec, "crpix1": 32.0, "crpix2": 32.0, "cd11": -3e-5, "cd12": 0.0,
        "cd21": 0.0, "cd22": 3e-5, "ra": ra, "dec": dec, "overlapfields": [FIELD], **corners}]},
        expects=[{"product": "l2files", "key": key, "binding": None}])
    ref_key = {"dataset": DATASET, "field": FIELD, "fid": FID}
    published(conn, run, tmp_path, "ref", {
        "refimages": [{"rfid": 90011, **ref_key, "filename": files["reference"].as_uri(),
                       "checksum": "0" * 32, "status": 1, "infobits": 0}],
        "refimcatalogs": [{"rfid": 90011, "cattype": 1, "filename": files["catalog"].as_uri(),
                           "checksum": "0" * 32}]}, expects=[{"product": "refimages", "key": ref_key,
                                                             "binding": None}])
    psf_key = {"dataset": DATASET, "fid": FID, "sca": SCA, "kind": "science"}
    published(conn, run, tmp_path, "science_psf", {"psfs": [{
        **psf_key, "filename": files["science_psf"].as_uri(), "checksum": "0" * 32, "status": 1}]},
        expects=[{"product": "psfs", "key": psf_key, "binding": None}])

    (diff,) = [s for s in loop.plan(loop.snapshot(conn, run), [], loop.Selection.parse("F184", str(SCA), "all"))[0]
               if s["stage"] == "difference"]
    refpsf = FakeReferencePsf()
    monkeypatch.setattr(difference, "toolkit", lambda: difference.Toolkit(
        runner=FakeToolRunner(), sip_to_pv=fake_sip_to_pv, psf_catalog=FakePsfCatalog(),
        reference_psf=refpsf, rb_model=fake_rb_model, rb_classifier=fake_rb_classifier))
    code = cli.main(["job", "local", "--run", str(run), "--stage", "difference",
                     "--scope", json.dumps(diff["scope"]), "--inputs", json.dumps(diff["inputs"]),
                     "--expects", json.dumps(diff["expects"])])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["published"] and result["refusal"] is None
    assert refpsf.calls == []

    ((psfid, filename),) = query(conn, "SELECT psfid, filename FROM psfs_official WHERE psf_set = 'S1'"
                                       " AND fid = %s AND sca = %s", (FID, SCA))
    assert query(conn, "SELECT source_job FROM job_inputs WHERE job = %s AND product = 'psfs'"
                       " AND key->>'kind' = 'reference'", (result["job"],)) == [(
                           query(conn, "SELECT job FROM psfs WHERE psfid = %s", (psfid,))[0][0],)]
    attempt = Path((tmp_path / "outputs").as_posix(), str(run), "difference", str(result["job"]), "1")
    manifest = json.loads((attempt / "manifest.json").read_text())
    assert manifest["difference"]["refpsf"]["mode"] == "precomputed"
    assert manifest["difference"]["refpsf"]["psfs"]["psfid"] == psfid
    assert manifest["difference"]["refpsf"]["psfs"]["filename"] == filename
    stamp = fits.getdata(attempt / "diff" / "refimage_psf.fits.gz")
    assert fits.getheader(attempt / "diff" / "refimage_psf.fits.gz")["PSFID"] == psfid
    assert stamp.shape == science.shape and stamp.sum() == pytest.approx(1.0, abs=1e-5)
