"""The difference stage under ``run(job)``, on a synthetic epoch with the fake tools.

Contracts: a job naming one ``l2files``, one ``refimages`` and one
(science) ``psfs`` row writes ``diffimages`` (one row) and ``sources`` with exactly their
tables' columns from ``database/schema.sql``, sharing the ``pid`` drawn for
the job; the work subdirectory publishes exactly the kept set (the ZOGY
image, its uncertainty, mask and PSF-fit catalog, and the neighbours the
packet stage reads), every FITS gzipped, and nothing else; the tools run in `dev`'s
order with the reference background subtracted by bkgest; ``max_sources``
caps the source set (its choice is ``source_set``'s, tested with the load
science); the reference
zero point is the reference's ``MAGZP`` unless overridden; the reference
PSF is measured on the resampled, background-subtracted reference over the
covered pixels and is the one ZOGY and naive get, published and
recorded in the manifest's ``metrics``; too few stars stops the job with
exit 65 and the reason, before a manifest; a reference ``psfs`` input
fails; SFFT and naive catalogs take their detection
overrides and the SFFT image is coverage-masked; every bad input fails the
job before a manifest is written; the shipped ``[difference]`` table is
valid.
"""

from __future__ import annotations

import contextlib
import copy
import json
import re
import tomllib
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest
from astropy.io import fits

import rapidpipe.stages.difference as difference
from rapidpipe.exitcodes import CommandExit
from rapidpipe.foundation.local import JobContext
from rapidpipe.selftest.support.fakedifftools import (
    FakePsfCatalog,
    FakeReferencePsf,
    FakeToolRunner,
    cdf_dir,
    fake_sip_to_pv,
    write_epoch,
)

REPO = Path(__file__).resolve().parents[2]
THIN = tomllib.loads((REPO / "rapidpipe/settings/foundation-thin.toml").read_text())
PID, RID, RFID = 31, 7001, 4242
KEY = {"dataset": "D", "expid": 84613, "sca": 7, "fid": 1}
MJDOBS = 61273.0087


def schema_columns(table: str) -> set[str]:
    body = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", (REPO / "database/schema.sql").read_text(),
                     re.S).group(1)
    names = {line.split()[0] for line in body.splitlines()
             if line.strip() and not line.strip().startswith(("UNIQUE", "CHECK", "--"))}
    return names - {"job", "created"}


def settings(**tables) -> dict:
    body = copy.deepcopy(THIN["difference"])
    body["paths"]["cfg_path"] = str(cdf_dir())
    body["statistics"]["clip_correction_seed"] = 1
    for table, values in tables.items():
        if isinstance(values, dict):
            body[table].update(values)
        else:
            body[table] = values
    return {"difference": body}


def fake_connect(files: dict[str, Path], queries: list):
    """A connection answering the named rows by table, and ``nextval``."""
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params=()):
            queries.append((statement, params))
            if "FROM l2files" in statement:
                self.row = (RID, files["science"].as_uri(), 140.0, MJDOBS, 0)
            elif "FROM refimages" in statement:
                self.row = (RFID, files["reference"].as_uri(), 0)
            elif "FROM refimcatalogs" in statement:
                self.row = (files["catalog"].as_uri(),)
            elif "FROM psfs" in statement:
                self.row = (files["science_psf"].as_uri(),)
            else:
                assert "nextval" in statement
                self.row = (PID,)

        def fetchone(self):
            return self.row

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

    return lambda: contextlib.nullcontext(Connection())


def named_inputs(**changes) -> list[dict]:
    inputs = {
        "l2files": {"kind": "input", "product": "l2files", "key": dict(KEY), "source_job": 2,
                    "binding": None},
        "refimages": {"kind": "input", "product": "refimages",
                      "key": {"dataset": "D", "field": 5321, "fid": 1}, "source_job": 8,
                      "binding": None},
        "psf": {"kind": "input", "product": "psfs", "source_job": 20, "binding": None,
                "key": {"dataset": "D", "fid": 1, "sca": 7, "kind": "science"}},
    }
    inputs.update(changes)
    return [i for i in inputs.values() if i is not None]


@pytest.fixture()
def refpsf():
    return FakeReferencePsf()


@pytest.fixture()
def fakes(monkeypatch, refpsf):
    runner, catalog = FakeToolRunner(), FakePsfCatalog()
    monkeypatch.setattr(difference, "toolkit", lambda: difference.Toolkit(
        runner=runner, sip_to_pv=fake_sip_to_pv, psf_catalog=catalog, reference_psf=refpsf))
    return runner, catalog


@pytest.fixture()
def files(tmp_path) -> dict[str, Path]:
    return write_epoch(tmp_path / "inputs")


def run_difference(tmp_path: Path, files, *, body=None, inputs=None, queries=None) -> Path:
    prefix = tmp_path / "prefix"
    prefix.mkdir(exist_ok=True)
    job = JobContext(job=9, run=3, stage="difference",
                     scope={"expid": KEY["expid"], "sca": KEY["sca"], "fid": KEY["fid"]},
                     survey="S", dataset="D", settings=body or settings(),
                     inputs=inputs or named_inputs(), attempt=1, prefix=prefix.as_uri() + "/",
                     connect=fake_connect(files, [] if queries is None else queries))
    difference.run(job)
    return prefix


def read(prefix: Path, table: str) -> list[dict]:
    return pq.read_table(prefix / f"{table}.parquet").to_pylist()


def manifest(prefix: Path) -> dict:
    return json.loads((prefix / "manifest.json").read_text())


# ----------------------------------------------------------------------
# The whole stage
# ----------------------------------------------------------------------


def test_a_job_writes_its_difference_image_row_and_source_set(tmp_path, files, fakes):
    prefix = run_difference(tmp_path, files)
    assert [o["table"] for o in manifest(prefix)["outputs"]] == ["diffimages", "sources"]
    for table in ("diffimages", "sources"):
        assert set(pq.read_schema(prefix / f"{table}.parquet").names) == schema_columns(table)
    (row,) = read(prefix, "diffimages")
    assert {k: row[k] for k in ("pid", "dataset", "expid", "sca", "fid", "rid", "rfid")} == {
        "pid": PID, **KEY, "rid": RID, "rfid": RFID}
    assert row["jd"] == pytest.approx(MJDOBS + 2400000.5)
    assert row["filename"] == prefix.as_uri() + "/diff/zogy_diffimage_masked.fits.gz"
    sources = read(prefix, "sources")
    assert sources and {s["pid"] for s in sources} == {PID}
    assert {(s["dataset"], s["expid"], s["sca"], s["fid"]) for s in sources} == {
        tuple(KEY.values())}
    recorded = manifest(prefix)["difference"]
    assert (recorded["pid"], recorded["rfid"]) == (PID, RFID)
    assert recorded["zero_point"] == {"value": 17.0, "source": "header"}
    assert recorded["sources_kept"] == len(sources) <= recorded["sources_before_cap"]


def test_the_job_publishes_the_kept_set_gzipped_and_nothing_else(tmp_path, files, fakes):
    work = run_difference(tmp_path, files) / "diff"
    images = {"zogy_diffimage_masked.fits",                  # the difference
              "zogy_diffimage_uncert_masked.fits",           # its uncertainty
              "awaicgen_output_mosaic_cov_map_resampled.fits",  # its mask
              "diffpsf.fits",                                # forced photometry
              "bkg_subbed_science_image.fits",               # forced photometry, science cutout
              "awaicgen_output_mosaic_image_resampled_gainmatched.fits",  # template cutout
              "refimage_psf.fits"}                           # ZOGY's reference PSF
    assert {p.name for p in work.iterdir()} == (
        {name + ".gz" for name in images} | {"zogy_diffimage_masked_psfcat.parquet"})
    for name in images:
        assert fits.getdata(work / (name + ".gz")) is not None, name


def test_tools_run_in_dev_order_with_the_reference_background_from_bkgest(tmp_path, files, fakes):
    runner, _ = fakes
    run_difference(tmp_path, files)
    assert runner.tools_called() == [
        "sextractor",                   # science-image catalog
        "swarp", "swarp", "swarp",      # reference image, coverage map, uncertainty
        "bkgest", "bkgest",             # science image, then the resampled reference
        "sextractor", "sextractor",     # gain-match catalogs, science then reference
        "zogy",
        "sextractor", "sextractor",     # ZOGY positive, negative
        "sextractor", "sextractor",     # SFFT positive, negative
        "sextractor", "sextractor",     # naive positive, negative
        "sfft",
    ]
    swarp = [c for c in runner.calls if c[0].endswith("swarp")]
    assert [c[c.index("-SUBTRACT_BACK") + 1] for c in swarp] == ["N", "N", "N"]
    reference_bkgest = [c for c in runner.calls if c[0].endswith("bkgest")][1]
    assert reference_bkgest[reference_bkgest.index("-i") + 1] == (
        "awaicgen_output_mosaic_image_resampled_bkgest_input.fits")


def test_every_differencer_gets_the_reference_psf_measured_on_the_resampled_reference(
        tmp_path, files, fakes, refpsf):
    runner, catalog = fakes
    prefix = run_difference(tmp_path, files)
    zogy = next(a for a in runner.calls if a[1].endswith("py_zogy.py"))
    assert zogy[4:6] == ["sciimage_psf_f184_sca07_normalized.fits", "refimage_psf.fits"]
    naive = next(c for c in catalog.calls if c["label"] == "positive naive difference image")
    assert naive["psf"].endswith("/refimage_psf.fits")
    (call,) = refpsf.calls
    # The 65 x 65 resampled reference less its uncovered 3-column strip (coverage 0).
    assert call["shape"] == (65, 65) and call["usable"] == 65 * 62
    stamp = fits.getdata(prefix / "diff" / "refimage_psf.fits.gz")
    assert stamp.shape == (61, 61) and stamp.sum() == pytest.approx(1.0, abs=1e-5)


def test_the_manifest_records_the_reference_psf_metrics(tmp_path, files, fakes, refpsf):
    recorded = manifest(run_difference(tmp_path, files))
    metrics = recorded["metrics"]
    assert set(metrics) == {"refpsf_sigma_px", "refpsf_stars", "sci_psf_sigma_px",
                            "psf_centroid_offset_px"}
    assert (metrics["refpsf_sigma_px"], metrics["refpsf_stars"]) == (refpsf.sigma, refpsf.stars)
    # The science PSF is the fixture's 9 x 9 Gaussian of sigma 1.2, centred.
    assert metrics["sci_psf_sigma_px"] == pytest.approx(refpsf.calls[0]["science_sigma_px"])
    assert 1.0 < metrics["sci_psf_sigma_px"] < 1.2
    assert metrics["psf_centroid_offset_px"] == pytest.approx([0.0, 0.0], abs=1e-6)
    assert recorded["difference"]["refpsf"]["stars"] == refpsf.stars


def test_sfft_and_naive_catalogs_take_their_detection_overrides(tmp_path, files, fakes):
    runner, _ = fakes
    run_difference(tmp_path, files)
    catalogs = [c for c in runner.calls if c[0].endswith("sex")
                and "rapidSexParamsDiffImage.inp" in c[c.index("-PARAMETERS_NAME") + 1]]
    zogy, sfft_runs, naive_runs = catalogs[:2], catalogs[2:4], catalogs[4:]

    def option(call, name):
        return call[call.index(name) + 1]

    assert {option(c, "-DETECT_MINAREA") for c in zogy} == {"5"}
    assert {option(c, "-FILTER") for c in zogy} == {"N"}
    for call in sfft_runs + naive_runs:
        assert (option(call, "-FILTER"), option(call, "-DETECT_MINAREA")) == ("Y", "2")
        assert option(call, "-FILTER_NAME") == f"{cdf_dir()}/rapidSexDiffImageSfftFilter.conv"
    # dev 8f8ea43f: the negative SFFT catalog is named after its detection image.
    assert option(sfft_runs[1], "-CATALOG_NAME") == "sfftdiffimage_masked_negative.txt"


def test_the_sfft_image_is_masked_by_the_reference_coverage(tmp_path, files, fakes, monkeypatch):
    seen = {}
    trim = difference.trim

    def look_then_trim(work_dir, **kept):   # SFFT's files are not published; read them first
        seen["masked"] = fits.getdata(work_dir / "sfftdiffimage_masked.fits")
        seen["unmasked"] = (work_dir / "sfftdiffimage_unmasked.fits").is_file()
        return trim(work_dir, **kept)

    monkeypatch.setattr(difference, "trim", look_then_trim)
    run_difference(tmp_path, files)
    assert np.isnan(seen["masked"][:, :3]).all() and np.isfinite(seen["masked"][:, 10:-10]).any()
    assert seen["unmasked"]


def test_max_sources_caps_the_source_set_and_records_the_count_before_it(tmp_path, files, fakes):
    prefix = run_difference(tmp_path, files, body=settings(max_sources=1))
    recorded = manifest(prefix)["difference"]
    assert len(read(prefix, "sources")) == recorded["sources_kept"] == 1
    assert recorded["sources_before_cap"] >= 1


def test_zero_point_override_wins_over_the_reference_header(tmp_path, files, fakes):
    prefix = run_difference(tmp_path, files, body=settings(awaicgen={"zprefimg": 26.5}))
    assert manifest(prefix)["difference"]["zero_point"] == {"value": 26.5, "source": "override"}


# ----------------------------------------------------------------------
# Refusals, each before a manifest
# ----------------------------------------------------------------------


def test_a_reference_without_magzp_and_no_override_fails(tmp_path, fakes):
    files = write_epoch(tmp_path / "inputs", magzp=None)
    with pytest.raises(ValueError, match="no MAGZP"):
        run_difference(tmp_path, files)
    assert not (tmp_path / "prefix" / "manifest.json").exists()


def test_too_few_reference_stars_stop_the_job_with_exit_65_and_the_reason(tmp_path, files, fakes,
                                                                         refpsf):
    refpsf.stars = 7
    with pytest.raises(CommandExit, match=r"^refpsf: 7 stars, 10 needed$") as stopped:
        run_difference(tmp_path, files)
    assert stopped.value.code == 65
    assert not (tmp_path / "prefix" / "manifest.json").exists()


@pytest.mark.parametrize("inputs, message", [
    (named_inputs(psf=None), "one psfs"),
    (named_inputs(refpsf={"kind": "input", "product": "psfs", "source_job": 21, "binding": None,
                          "key": {"dataset": "D", "fid": 1, "sca": 7, "kind": "reference"}}),
     "one psfs"),
    (named_inputs(gaia={"kind": "input", "product": "catalogs", "key": {}, "source_job": 1,
                        "binding": None}), "not \\['catalogs'\\]"),
    (named_inputs(l2files={"kind": "input", "product": "l2files", "source_job": 2,
                           "binding": None, "key": {**KEY, "expid": 1}}), "is not the epoch"),
])
def test_inputs_the_stage_does_not_take_fail(tmp_path, files, fakes, inputs, message):
    with pytest.raises(ValueError, match=message):
        run_difference(tmp_path, files, inputs=inputs)
    assert not (tmp_path / "prefix" / "manifest.json").exists()


def test_two_science_psfs_fail(tmp_path, files, fakes):
    science_twice = named_inputs(refpsf={
        "kind": "input", "product": "psfs", "source_job": 21, "binding": None,
        "key": {"dataset": "D", "fid": 1, "sca": 7, "kind": "science"}})
    with pytest.raises(ValueError, match="one psfs input"):
        run_difference(tmp_path, files, inputs=science_twice)
    assert not (tmp_path / "prefix" / "manifest.json").exists()


def test_the_shipped_difference_table_is_valid_and_refuses_fake_sources():
    difference.check_settings(THIN["difference"])
    assert THIN["difference"]["max_sources"] == 500
    with pytest.raises(ValueError, match="fake-source"):
        difference.check_settings(settings(fake_sources={"inject_fake_sources_flag": True})["difference"])
    with pytest.raises(ValueError, match="max_sources"):
        difference.check_settings(settings(max_sources=-1)["difference"])
