"""The reference stage under ``run(job)``, on three synthetic frames with the fake tools.

Contracts: a job's named ``l2files`` rows are coadded in ``mjdobs`` order
and the stage writes ``refimages``, ``refimimages`` and ``refimcatalogs``
with exactly their tables' columns from ``database/schema.sql``, all three
carrying the one ``rfid`` drawn for the job, the files under the attempt's
prefix and the header stamped with the job; frames past ``max_frames`` are
not coadded and are named in the manifest; too few frames, a frame in
another filter, an input outside the scope's filter and SCA, a refused
setting and a tool that writes nothing each fail the job before a manifest
is written; the run settings body's ``[reference]`` table is valid as it
ships.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import re
import tomllib
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from astropy.io import fits

import rapidpipe.stages.reference as reference
from rapidpipe.foundation.local import JobContext
from rapidpipe.selftest.support import fakereftools
from rapidpipe.selftest.support.fakedifftools import cdf_dir
from rapidpipe.selftest.support.fakereftools import FRAMES, RTID, write_frames

REPO = Path(__file__).resolve().parents[2]
THIN = tomllib.loads((REPO / "rapidpipe/settings/foundation-thin.toml").read_text())
RFID = 4242
FID, SCA = 8, 1   # W146; the synthetic frames' SCA_NUM


def schema_columns(table: str) -> set[str]:
    body = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", (REPO / "database/schema.sql").read_text(),
                     re.S).group(1)
    names = {line.split()[0] for line in body.splitlines()
             if line.strip() and not line.strip().startswith(("UNIQUE", "CHECK", "--"))}
    return names - {"job", "created"}


def settings(**tables) -> dict:
    body = copy.deepcopy(THIN["reference"])
    body["paths"]["cfg_path"] = str(cdf_dir())
    body["mosaic"].update(naxis1=128, naxis2=128)
    body["statistics"]["clip_correction_seed"] = 1
    for table, values in tables.items():
        body[table].update(values)
    return {"reference": body}


def fake_connect(rows: dict[int, tuple[int, str]], queries: list):
    """A connection answering the filter, each ``l2files`` row by source job, and ``nextval``."""
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params=()):
            queries.append(statement)
            if "FROM filters" in statement:
                self.row = ("W146",) if params == (FID,) else None
            elif "FROM l2files" in statement:
                self.row = rows.get(params[0])
            else:
                assert "nextval" in statement
                self.row = (RFID,)

        def fetchone(self):
            return self.row

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

    return lambda: contextlib.nullcontext(Connection())


@pytest.fixture()
def frames(tmp_path) -> list[Path]:
    return write_frames(tmp_path / "l2")


@pytest.fixture(autouse=True)
def fakes(monkeypatch, tmp_path):
    monkeypatch.setattr(reference, "toolkit", fakereftools.fake_toolkit)
    monkeypatch.setenv("RAPIDPIPE_WORK", str(tmp_path / "work"))


def run_reference(tmp_path: Path, frames: list[Path], *, order=(2, 0, 1), body=None,
                  key_override=None) -> Path:
    """Run a job naming ``frames`` (as source jobs 10, 11, ... in ``order``); return its prefix."""
    prefix = tmp_path / "prefix"
    prefix.mkdir(exist_ok=True)
    inputs, rows = [], {}
    for n, index in enumerate(order):
        source_job = 10 + n
        key = {"dataset": "D", "expid": 100 + index, "sca": SCA, "fid": FID, **(key_override or {})}
        inputs.append({"kind": "input", "product": "l2files", "key": key,
                       "source_job": source_job, "binding": None})
        rows[source_job] = (7000 + index, frames[index].as_uri())
    job = JobContext(job=5, run=3, stage="reference", scope={"field": RTID, "fid": FID, "sca": SCA},
                     survey="S", dataset="D", settings=body or settings(), inputs=inputs,
                     attempt=1, prefix=prefix.as_uri() + "/", connect=fake_connect(rows, []))
    reference.run(job)
    return prefix


def read(prefix: Path, table: str) -> list[dict]:
    return pq.read_table(prefix / f"{table}.parquet").to_pylist()


def test_a_job_writes_its_three_tables_with_one_rfid_and_its_files(tmp_path, frames):
    prefix = run_reference(tmp_path, frames)
    manifest = json.loads((prefix / "manifest.json").read_text())
    assert [o["table"] for o in manifest["outputs"]] == ["refimages", "refimimages", "refimcatalogs"]
    for table in ("refimages", "refimimages", "refimcatalogs"):
        assert set(pq.read_schema(prefix / f"{table}.parquet").names) == (
            schema_columns(table) - {"rfcatid"})
    (ref,), (cat,) = read(prefix, "refimages"), read(prefix, "refimcatalogs")
    assert (ref["rfid"], ref["dataset"], ref["field"], ref["fid"], ref["nframes"]) == (
        RFID, "D", RTID, FID, 3)
    assert sorted(r["rid"] for r in read(prefix, "refimimages")) == [7000, 7001, 7002]
    assert {r["rfid"] for r in read(prefix, "refimimages")} == {cat["rfid"]} == {RFID}
    assert cat["cattype"] == reference.CATALOG_TYPE == 1
    image = Path(ref["filename"].removeprefix("file://"))
    assert ref["filename"].startswith(prefix.as_uri() + "/") and image.is_file()
    assert ref["checksum"] == hashlib.md5(image.read_bytes()).hexdigest()
    for name in ("awaicgen_output_mosaic_cov_map.fits", "awaicgen_output_mosaic_uncert_image.fits"):
        assert (image.parent / name).is_file()
    assert Path(cat["filename"].removeprefix("file://")).is_file()
    header = fits.getheader(image)
    assert (header["RPRUN"], header["RPJOB"], header["RPATTMPT"], header["RPSTAGE"]) == (
        3, 5, 1, "reference")
    assert [header[f"INFIL00{i}"] for i in (1, 2, 3)] == [f[1] for f in FRAMES]  # mjdobs order
    assert manifest["reference"]["rfid"] == RFID and manifest["reference"]["not_coadded"] == []
    assert manifest["reference"]["nsexcatsources"] == 4
    metrics = manifest["metrics"]
    assert {"gmin", "gmax", "coverage_lt1", "coverage_lt2", "coverage_lt3"} <= set(metrics)
    assert all(isinstance(v, (int, float)) for v in metrics.values())
    assert not any((tmp_path / "work").iterdir())


def test_frames_past_max_frames_are_not_coadded_and_are_named(tmp_path, frames):
    prefix = run_reference(tmp_path, frames, body=settings(selection={"max_frames": 2}))
    assert sorted(r["rid"] for r in read(prefix, "refimimages")) == [7000, 7001]
    assert json.loads((prefix / "manifest.json").read_text())["reference"]["not_coadded"] == [7002]


@pytest.mark.parametrize("case", ["too few", "another filter", "outside the scope"])
def test_bad_inputs_fail_before_a_manifest(tmp_path, frames, case):
    kwargs = {"too few": {"order": (0,)},
              "outside the scope": {"key_override": {"sca": 2}}}.get(case, {})
    if case == "another filter":
        with fits.open(frames[1]) as hdul:
            hdul = fits.HDUList([h.copy() for h in hdul])
        hdul[1].header["FILTER"] = "F184"
        hdul.writeto(frames[1], overwrite=True)
    with pytest.raises(ValueError):
        run_reference(tmp_path, frames, **kwargs)
    assert not (tmp_path / "prefix" / "manifest.json").exists()


@pytest.mark.parametrize("tables", [
    {"psfcat": {"enabled": True}},
    {"fake_sources": {"inject_fake_sources_flag": True}},
    {"selection": {"min_frames": 5, "max_frames": 4}},
    {"mosaic": {"ra_center": 10.0}},
    {"mosaic": {"cdelt1": -0.0001}},
    {"awaicgen": {"awaicgen_output_mosaic_image_file": "mosaic.fits"}},
])
def test_refused_settings_fail_before_a_manifest(tmp_path, frames, tables):
    with pytest.raises(ValueError):
        run_reference(tmp_path, frames, body=settings(**tables))
    assert not (tmp_path / "prefix" / "manifest.json").exists()


def test_awaicgen_writing_nothing_fails_the_job(tmp_path, frames, monkeypatch):
    monkeypatch.setattr(fakereftools.FakeReferenceToolRunner, "_awaicgen",
                        lambda self, args, cwd: None)
    with pytest.raises(RuntimeError, match="awaicgen"):
        run_reference(tmp_path, frames)
    assert not (tmp_path / "prefix" / "manifest.json").exists()


def test_the_thin_settings_body_reference_table_is_valid():
    checked = reference.check_settings(THIN["reference"])
    assert (checked.min_frames, checked.naxis1) == (2, 7000)
