"""The difference stage under ``run(job)``, on a synthetic epoch with the fake tools.

Contracts: a job naming one ``l2files``, one ``refimages`` and one
(science) ``psfs`` row writes ``diffimages`` (one row), ``sources``,
``objects`` and ``memberships`` with exactly their tables' columns from
``database/schema.sql``, sharing the ``pid`` drawn for the job; it
associates its sources with the catalog it read, recording the read and
the catalog key in the manifest; the published difference image and source set are SFFT's (`dev`'s
``diff_flavor = sfft``), and the packet stage's cutout and forced
photometry reads resolve beside it to SFFT's PSF and uncertainty;
``run_sfft = false`` fails the settings check and a nonzero SFFT exit
fails the job, never a fallback to ZOGY;
the work subdirectory publishes exactly the kept set (the SFFT
image, its uncertainty, mask and PSF, both differencers' PSF-fit catalogs,
and the neighbours the packet stage reads), every FITS gzipped, and nothing else; the tools run in `dev`'s
order with the reference background subtracted by bkgest; ``max_sources``
caps the source set (its choice is ``source_set``'s, tested with the load
science); the reference
zero point is the reference's ``MAGZP`` unless overridden; the reference
PSF is measured on the resampled, background-subtracted reference over the
covered pixels and is the one ZOGY and naive get, published and
recorded in the manifest's ``metrics``; too few stars stops the job with
exit 65 and the reason, before a manifest; ``[difference] reference_psf =
"science"`` measures nothing and gives ZOGY the science PSF for both
images, the manifest saying so; a reference ``psfs`` input
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
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from astropy.io import fits

import rapidpipe.stages.difference as difference
from rapidpipe.exitcodes import CommandExit
from rapidpipe.science.alerts import forced
from rapidpipe.science.alerts.cutouts import CUTOUT_FILES
from rapidpipe.science.difference.tools import ToolFailed
from rapidpipe.science.load.catalogs import source_set
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
    return {"difference": body, "associate": copy.deepcopy(THIN["associate"])}


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
            elif "FROM objects" in statement:
                self.rows = []                  # the field's catalog: empty, every source founds
            else:
                assert "nextval" in statement
                self.row = (PID,)

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

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
    assert [o["table"] for o in manifest(prefix)["outputs"]] == [
        "diffimages", "sources", "objects", "memberships"]
    for table in ("diffimages", "sources", "objects", "memberships"):
        assert set(pq.read_schema(prefix / f"{table}.parquet").names) == schema_columns(table)
    (row,) = read(prefix, "diffimages")
    assert {k: row[k] for k in ("pid", "dataset", "expid", "sca", "fid", "rid", "rfid")} == {
        "pid": PID, **KEY, "rid": RID, "rfid": RFID}
    assert row["jd"] == pytest.approx(MJDOBS + 2400000.5)
    assert row["filename"] == prefix.as_uri() + "/diff/sfftdiffimage_masked.fits.gz"
    sources = read(prefix, "sources")
    assert sources and {s["pid"] for s in sources} == {PID}
    assert {(s["dataset"], s["expid"], s["sca"], s["fid"]) for s in sources} == {
        tuple(KEY.values())}
    recorded = manifest(prefix)["difference"]
    assert (recorded["pid"], recorded["rfid"]) == (PID, RFID)
    assert recorded["zero_point"] == {"value": 17.0, "source": "header"}
    assert recorded["sources_kept"] == len(sources) <= recorded["sources_before_cap"]


def test_a_job_associates_its_sources_with_the_catalog_it_read_and_records_the_read(
        tmp_path, files, fakes):
    """An empty catalog: every accepted source founds an object in its own field, wherever on the
    image it falls, its own membership at offset zero; the read (no founding job) and the fields
    the publisher locks (each touched field and its neighbours) are in the manifest. The fake
    catalog's sources are at S/N 4, so the cut is lowered to take them."""
    body = settings()
    body["associate"]["min_snr"] = 1.0
    prefix = run_difference(tmp_path, files, body=body)
    recorded = manifest(prefix)
    (row,) = read(prefix, "diffimages")
    sources = {s["sid"]: s for s in read(prefix, "sources")}
    founded, members = read(prefix, "objects"), read(prefix, "memberships")
    assert recorded["reads"] == []
    own = [m for m in members if m["dra"] == 0.0 and m["ddec"] == 0.0]
    assert {m["aid"] for m in own} == {o["aid"] for o in founded}
    field_of = {o["aid"]: o["field"] for o in founded}
    assert all(field_of[m["aid"]] == sources[m["sid"]]["field"] for m in own)
    touched = set(field_of.values())
    assert row["field"] in touched
    near = touched.union(*(difference.field_neighbours(f) for f in touched))
    assert recorded["catalog"] == {"dataset": "D", "survey": "S", "fields": sorted(near),
                                   "radius": THIN["associate"]["match_radius"]}
    assert {(m["object_job"], m["source_job"]) for m in members} == {(9, 9)}
    assert recorded["metrics"]["n_objects_founded"] == len(founded)
    assert recorded["metrics"]["n_objects_read"] == 0
    assert recorded["metrics"]["n_sources_out_of_field"] == sum(
        sources[m["sid"]]["field"] != row["field"] for m in own)


def test_the_job_publishes_the_kept_set_gzipped_and_nothing_else(tmp_path, files, fakes):
    work = run_difference(tmp_path, files) / "diff"
    images = {"sfftdiffimage_masked.fits",                   # the difference
              "sfftdiffimage_uncert_masked.fits",            # its uncertainty
              "awaicgen_output_mosaic_cov_map_resampled.fits",  # its mask
              "sfftdiffpsf.fits",                            # forced photometry
              "bkg_subbed_science_image.fits",               # forced photometry, science cutout
              "awaicgen_output_mosaic_image_resampled_gainmatched.fits",  # template cutout
              "refimage_psf.fits"}                           # the measured reference PSF
    catalogs = {f"{d}_masked_psfcat{sign}.parquet"         # the published one and diagnostics
                for d in ("sfftdiffimage", "zogy_diffimage") for sign in ("", "_negative")}
    assert {p.name for p in work.iterdir()} == {name + ".gz" for name in images} | catalogs
    for name in images:
        assert fits.getdata(work / (name + ".gz")) is not None, name


def test_the_published_source_set_is_sffts_positive_catalog(tmp_path, files, fakes, monkeypatch):
    runner, catalog = fakes
    kit = difference.toolkit()

    def zogy_ids_apart(settings_, **kwargs):  # the fake's catalogs are alike; ZOGY's ids are moved
        outcome = catalog(settings_, **kwargs)
        if "zogy" in kwargs["label"] and kwargs["parquet"] and outcome.produced:
            table = pq.read_table(kwargs["parquet"])
            ids = table.column("id").to_numpy() + 1000
            pq.write_table(table.set_column(0, "id", pa.array(ids)), kwargs["parquet"])
        return outcome

    monkeypatch.setattr(difference, "toolkit", lambda: difference.Toolkit(
        runner=runner, sip_to_pv=kit.sip_to_pv, psf_catalog=zogy_ids_apart,
        reference_psf=kit.reference_psf))
    prefix = run_difference(tmp_path, files)
    work = prefix / "diff"
    sfft_catalog = pq.read_table(work / "sfftdiffimage_masked_psfcat.parquet")
    zogy_catalog = pq.read_table(work / "zogy_diffimage_masked_psfcat.parquet")
    (row,) = read(prefix, "diffimages")
    header = fits.getheader(work / "sfftdiffimage_masked.fits.gz")
    expected, _ = source_set(sfft_catalog, KEY, PID, MJDOBS, header["NAXIS1"], header["NAXIS2"],
                             max_sources=THIN["difference"]["max_sources"])
    sources = read(prefix, "sources")
    assert [(s["sid"], s["xfit"], s["yfit"]) for s in sources] == [
        (s["sid"], s["xfit"], s["yfit"]) for s in expected.to_pylist()]
    assert sources and not {s["sid"] for s in sources} & set(zogy_catalog.column("id").to_pylist())
    assert all(s["isdiffpos"] for s in sources)
    assert manifest(prefix)["difference"]["differencer"] == "sfft"


def test_the_packet_stage_reads_resolve_to_sffts_files_beside_the_published_image(
        tmp_path, files, fakes):
    """The packet stage takes the cutout and forced photometry files beside ``diffimages.filename``."""
    prefix = run_difference(tmp_path, files)
    (row,) = read(prefix, "diffimages")
    plain = row["filename"].rsplit("/", 1)[-1].removesuffix(".gz")
    assert plain == "sfftdiffimage_masked.fits"
    beside = {forced.DIFF_PSF_BASENAMES[plain], forced.DIFF_UNCERT_BASENAMES[plain],
              *CUTOUT_FILES.values()}
    assert beside >= {"sfftdiffpsf.fits", "sfftdiffimage_uncert_masked.fits"}
    for name in beside:
        assert (prefix / "diff" / (name + ".gz")).is_file(), name


def test_run_sfft_false_fails_the_settings_check(tmp_path, files, fakes):
    with pytest.raises(ValueError, match="run_sfft = false"):
        run_difference(tmp_path, files, body=settings(sfft={"run_sfft": False}))
    assert not (tmp_path / "prefix" / "manifest.json").exists()


def test_a_nonzero_sfft_exit_fails_the_job_without_falling_back_to_zogy(tmp_path, files, fakes):
    runner, _ = fakes
    runner.sfft_exit_code = 3
    with pytest.raises(ToolFailed) as failed:
        run_difference(tmp_path, files)
    assert (failed.value.tool, failed.value.returncode) == ("sfft", 3)
    assert not (tmp_path / "prefix" / "manifest.json").exists()


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
    assert {"refpsf_sigma_px", "refpsf_stars", "sci_psf_sigma_px", "psf_centroid_offset_x_px",
            "psf_centroid_offset_y_px", "ref_sigma_px", "ref_sci_sigma_ratio", "noise_correlation",
            "usable_fraction", "n_candidates", "n_sources", "n_sources_snr_gt20"} <= set(metrics)
    # Every metric is a number; one the job cannot make is absent (the fake finds no stars).
    assert all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in metrics.values())
    assert not {"core_fraction", "registration_dx_px"} & set(metrics)
    assert metrics["n_sources"] == recorded["difference"]["sources_kept"]
    assert metrics["n_candidates"] == recorded["difference"]["sources_before_cap"]
    assert 0 < metrics["usable_fraction"] <= 1
    assert (metrics["refpsf_sigma_px"], metrics["refpsf_stars"]) == (refpsf.sigma, refpsf.stars)
    assert metrics["ref_sigma_px"] == refpsf.sigma
    assert metrics["noise_correlation"] > 0
    # The science PSF is the fixture's 9 x 9 Gaussian of sigma 1.2, centred.
    assert refpsf.calls[0]["science_psf_shape"] == (9, 9)
    assert 1.0 < metrics["sci_psf_sigma_px"] < 1.2
    assert (metrics["psf_centroid_offset_x_px"], metrics["psf_centroid_offset_y_px"]) == \
        pytest.approx((0.0, 0.0), abs=1e-6)
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

    def look_then_trim(work_dir, **kept):   # the trim gzips the image; read it first, not mapped
        seen["masked"] = fits.getdata(work_dir / "sfftdiffimage_masked.fits", memmap=False)
        seen["unmasked"] = (work_dir / "sfftdiffimage_unmasked.fits").is_file()
        return trim(work_dir, **kept)

    monkeypatch.setattr(difference, "trim", look_then_trim)
    run_difference(tmp_path, files)
    assert np.isnan(seen["masked"][:, :3]).all() and np.isfinite(seen["masked"][:, 10:-10]).any()
    assert seen["unmasked"]



def test_the_pixel_columns_add_three_columns_and_change_nothing_else(tmp_path, files, fakes,
                                                                    monkeypatch):
    """Identical inputs with the columns step and without it (the code before dev-columns): the
    same source ids in the same order, every other column of every row and catalog, and every
    published image's pixels; the SFFT catalogs and the source set gain sumrat, nneg and nbad."""
    (tmp_path / "with").mkdir()
    (tmp_path / "without").mkdir()
    after = run_difference(tmp_path / "with", files)
    monkeypatch.setattr(difference.columns, "add_pixel_columns", lambda *args: 0)
    before = run_difference(tmp_path / "without", files)
    new = ["sumrat", "nneg", "nbad"]
    old_sources, new_sources = (pq.read_table(p / "sources.parquet") for p in (before, after))
    assert new_sources.num_rows and new_sources.column("sid").equals(old_sources.column("sid"))
    kept = [c for c in old_sources.column_names if c not in new]
    assert new_sources.select(kept).equals(old_sources.select(kept))
    assert all(old_sources.column(c).null_count == old_sources.num_rows for c in new)
    assert all(new_sources.column(c).null_count < new_sources.num_rows for c in new)
    for path in sorted((before / "diff").iterdir()):
        twin = after / "diff" / path.name
        if path.suffix == ".parquet":
            old, now = pq.read_table(path), pq.read_table(twin)
            gained = new if path.name.startswith("sfft") else []
            assert now.column_names == old.column_names + gained, path.name
            assert now.select(old.column_names).equals(old), path.name
        else:
            np.testing.assert_array_equal(fits.getdata(twin), fits.getdata(path), err_msg=path.name)
    ignore = {"filename", "checksum"}           # the prefix's own URI; the gzip of a DATE card
    assert [{k: v for k, v in r.items() if k not in ignore} for r in read(before, "diffimages")] == [
        {k: v for k, v in r.items() if k not in ignore} for r in read(after, "diffimages")]


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


def test_the_bright_footprint_flag_is_set_beside_the_fit_bits_and_drops_nothing():
    import pyarrow as pa

    from rapidpipe.products.diffimage import SOURCE_FLAG_BRIGHT_FOOTPRINT

    sources = pa.table({"xfit": [100.0, 103.0, 300.0], "yfit": [100.0, 100.0, 300.0],
                        "flags": pa.array([0, 4, 0], pa.int16())})
    flux_at_15 = 10 ** (0.4 * (25.0 - 15.0))     # radius 9.5 px on zero point 25
    rows = np.array([[0, 0, flux_at_15, 1000.0, 1.0],      # aperture S/N 1000: a footprint
                     [0, 0, flux_at_15, 50.0, 1.0]])       # S/N 50: none
    xy = np.array([[95.0, 100.0], [300.0, 300.0]])
    flagged, inside = difference.flag_bright_footprint(sources, xy, rows, 25.0)
    assert inside.tolist() == [True, True, False]
    assert flagged.column("flags").to_pylist() == [
        SOURCE_FLAG_BRIGHT_FOOTPRINT, 4 | SOURCE_FLAG_BRIGHT_FOOTPRINT, 0]
    assert flagged.schema == sources.schema


def test_reference_psf_science_measures_nothing_and_gives_zogy_the_science_psf_for_both(
        tmp_path, files, fakes, refpsf):
    body = settings()
    body["difference"]["reference_psf"] = "science"
    runner, _ = fakes
    prefix = run_difference(tmp_path, files, body=body)
    assert refpsf.calls == []
    zogy = next(a for a in runner.calls if a[1].endswith("py_zogy.py"))
    assert zogy[4:6] == ["sciimage_psf_f184_sca07_normalized.fits", "refimage_psf.fits"]
    stamp = fits.getdata(prefix / "diff" / "refimage_psf.fits.gz")
    science = fits.getdata(files["science_psf"]).astype(float)
    assert stamp == pytest.approx(science / science.sum(), abs=1e-6)
    recorded = manifest(prefix)
    assert recorded["difference"]["refpsf"]["mode"] == "science"
    assert not {"refpsf_stars", "refpsf_sigma_px", "ref_sci_sigma_ratio"} & set(recorded["metrics"])
