"""The import stage on a synthetic source directory: two tiny exposures, a reference, a PSF, Gaia.

Contracts: each output Parquet file has exactly its table's columns from
``database/schema.sql`` (without ``job``, and without the defaulted
``created``); key values and the source system's ids are carried; the
source set is the ``max_sources`` brightest by flux over flux error inside
the fit-position bounds; overlapping fields and sky indices are computed
from the image's WCS; the manifest declares every table and the
provenance line; a PSF imports under its declared kind, which `dev`'s
``PSFKIND`` keyword must not contradict.
"""

from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from astropy.io import fits

from rapidpipe.foundation.local import JobContext
from rapidpipe.science.spatial import healpix_indexes, tessellation_field
from rapidpipe.stages import import_products

REPO = Path(__file__).resolve().parents[2]
RA, DEC = 267.96138, -28.72761
SETTINGS = {"import": {"max_sources": 3, "xy_fit_min": -0.5, "xy_fit_max_offset": 0.5,
                       "limmag_nsigma": 5.0, "limmag_clip_sigma": 3.0, "limmag_maxiters": 10}}
EPOCHS = [{"expid": 101, "rid": 7001, "pid": 9001, "rfid": 55, "l2": "l2/one.fits",
           "difference": "jid1", "mjd": 61685.3},
          {"expid": 102, "rid": 7002, "pid": 9002, "rfid": 55, "l2": "l2/two.fits",
           "difference": "jid2", "mjd": 61685.4}]


def schema_columns(table: str) -> set[str]:
    body = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", (REPO / "database/schema.sql").read_text(),
                     re.S).group(1)
    names = {line.split()[0] for line in body.splitlines()
             if line.strip() and not line.strip().startswith(("UNIQUE", "CHECK", "--"))}
    return names - {"job", "created"}


def wcs_header(n: int) -> fits.Header:
    header = fits.Header()
    header.update({"CTYPE1": "RA---TAN", "CTYPE2": "DEC--TAN", "CRVAL1": RA, "CRVAL2": DEC,
                   "CRPIX1": n / 2 + 0.5, "CRPIX2": n / 2 + 0.5, "CD1_1": -3.0e-5, "CD1_2": 0.0,
                   "CD2_1": 0.0, "CD2_2": 3.0e-5})
    return header


def image(path: Path, n: int = 64, **cards) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = wcs_header(n)
    header.update(cards)
    data = np.random.default_rng(0).normal(100.0, 10.0, (n, n)).astype(np.float32)
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(data, header)]).writeto(path)


def catalog(path: Path) -> None:
    """Eight sources: S/N 1..8 at x = 10..17, the brightest outside the bounds, one with zero error."""
    n = 8
    snr = np.arange(1.0, n + 1)
    flux_err = np.full(n, 10.0)
    flux_err[2] = 0.0
    x = 10.0 + np.arange(n)
    x[-1] = 70.0  # outside a 65-pixel difference image
    floats = {name: np.full(n, 0.5) for name in (
        "x_err", "y_err", "qfit", "cfit", "reduced_chi2", "sharpness", "roundness1",
        "roundness2", "peak")}
    pq.write_table(pa.table({
        "id": np.arange(1, n + 1), "ra": RA + np.arange(n) * 1e-4, "dec": np.full(n, DEC),
        "x_fit": x, "y_fit": np.full(n, 20.0), "flux_fit": snr * 10.0, "flux_err": flux_err,
        "n_pixels_fit": np.full(n, 25), "flags": np.zeros(n, dtype=np.int64),
        "n_pixels": np.full(n, 9), **floats}), path)


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    src = tmp_path / "source"
    for epoch in EPOCHS:
        image(src / epoch["l2"], SCA_NUM=8, FILTER="W146", EXPTIME=10.0, ZPTMAG=26.0,
              BUNIT="DN", **{"MJD-OBS": epoch["mjd"], "DATE-OBS": "2027-10-07T07:18:18.000",
                             "TARGRA": RA, "TARGDEC": DEC})
        for name in import_products.DIFFERENCE_FILES:
            image(src / epoch["difference"] / name, n=65, INFOBITS=0)
        catalog(src / epoch["difference"] / import_products.DIFFERENCE_CATALOG)
    for name in import_products.REFERENCE_FILES:
        image(src / "ref" / name, FIELD=4658155, FID=8, NFRAMES=25, COV5PERC=32.5)
    (src / "ref" / import_products.REFERENCE_CATALOG).write_text("#   1 NUMBER\n1\n")
    psf = np.zeros((9, 9), dtype=np.float32)
    psf[4, 4] = 1.0
    fits.PrimaryHDU(psf, fits.Header({"SCA": 8, "FILTER": "F146"})).writeto(src / "psf.fits")
    pq.write_table(pa.table({"source_id": [1, 2], "ra": [RA, RA + 1e-3], "dec": [DEC, DEC]}),
                   src / "gaia.parquet")
    return src


def fake_connect(psf_filename: str):
    """A connection factory whose one query, the PSF lookup, returns ``psf_filename``."""
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params):
            assert "FROM psfs" in statement

        def fetchone(self):
            return (psf_filename,)

    class Connection:
        def cursor(self):
            return Cursor()

    return lambda: contextlib.nullcontext(Connection())


def run_import(tmp_path: Path, source: Path, scope: dict, name: str) -> Path:
    prefix = tmp_path / "outputs" / name
    prefix.mkdir(parents=True)
    job = JobContext(job=1, run=1, stage="import", scope={"source_dir": str(source), **scope},
                     survey="S", dataset="D", settings=SETTINGS,
                     inputs=[{"kind": "input", "product": "psfs", "key": {}, "source_job": 9,
                              "binding": None}],
                     attempt=1, prefix=prefix.as_uri() + "/",
                     connect=fake_connect((source / "psf.fits").as_uri()))
    import_products.run(job)
    return prefix


def read(prefix: Path, table: str) -> list[dict]:
    return pq.read_table(prefix / f"{table}.parquet").to_pylist()


def test_each_epoch_writes_its_tables_with_the_schema_columns_and_its_keys(tmp_path, source):
    for epoch in EPOCHS:
        prefix = run_import(tmp_path, source, {"epochs": [epoch]}, str(epoch["expid"]))
        manifest = json.loads((prefix / "manifest.json").read_text())
        assert manifest["provenance"] == import_products.PROVENANCE
        assert {o["table"] for o in manifest["outputs"]} == {
            "exposures", "l2files", "diffimages", "sources"}
        for table in ("exposures", "l2files", "diffimages", "sources"):
            assert set(pq.read_schema(prefix / f"{table}.parquet").names) == schema_columns(table)
        (l2,), (diff,) = read(prefix, "l2files"), read(prefix, "diffimages")
        assert (l2["expid"], l2["rid"], l2["sca"], l2["fid"], l2["dataset"]) == (
            epoch["expid"], epoch["rid"], 8, 8, "D")
        assert (diff["pid"], diff["rid"], diff["rfid"]) == (epoch["pid"], epoch["rid"], 55)
        assert l2["filename"].startswith(prefix.as_uri()) and Path(l2["filename"][7:]).is_file()
        assert {s["pid"] for s in read(prefix, "sources")} == {epoch["pid"]}


def test_overlap_and_sky_indices_are_computed_from_the_wcs(tmp_path, source):
    prefix = run_import(tmp_path, source, {"epochs": [EPOCHS[0]]}, "one")
    (l2,) = read(prefix, "l2files")
    assert abs(l2["ra0"] - RA) < 1e-6 and abs(l2["dec0"] - DEC) < 1e-6
    assert l2["field"] == tessellation_field(l2["ra0"], l2["dec0"])
    assert (l2["hp6"], l2["hp9"]) == healpix_indexes(l2["ra0"], l2["dec0"])
    assert l2["field"] in l2["overlapfields"] and l2["overlapfields"] == sorted(l2["overlapfields"])
    assert np.isfinite(l2["limmag"])
    for source_row in read(prefix, "sources"):
        assert source_row["field"] == tessellation_field(source_row["ra"], source_row["dec"])


def test_the_source_set_is_the_brightest_inside_the_bounds(tmp_path, source):
    prefix = run_import(tmp_path, source, {"epochs": [EPOCHS[0]]}, "one")
    rows = read(prefix, "sources")
    # S/N 8 lies outside the image and S/N 3 has zero error: the three kept are 7, 6, 5.
    assert sorted(r["sid"] for r in rows) == [5, 6, 7]
    catalog_rows = {r["id"]: r for r in pq.read_table(
        source / "jid1" / import_products.DIFFERENCE_CATALOG).to_pylist()}
    for row in rows:
        assert (row["ra"], row["dec"]) == (catalog_rows[row["sid"]]["ra"], catalog_rows[row["sid"]]["dec"])


def test_reference_psf_and_gaia_write_their_tables(tmp_path, source):
    prefix = run_import(tmp_path, source, {
        "reference": {"rfid": 55, "dir": "ref"},
        "psf": {"kind": "science", "path": "psf.fits"},
        "gaia": {"name": "gaia", "version": "DR3", "path": "gaia.parquet"}}, "shared")
    for table in ("refimages", "refimcatalogs", "psfs", "catalogs"):
        assert set(pq.read_schema(prefix / f"{table}.parquet").names) == (
            schema_columns(table) - {"rfcatid", "psfid"})
    (ref,), (cat,) = read(prefix, "refimages"), read(prefix, "refimcatalogs")
    assert (ref["rfid"], ref["field"], ref["fid"], ref["nframes"]) == (55, 4658155, 8, 25)
    assert (cat["rfid"], cat["cattype"]) == (55, import_products.REFERENCE_CATALOG_TYPE)
    # No psfid: the table's identity draws it.
    assert [(p["fid"], p["sca"], p["kind"]) for p in read(prefix, "psfs")] == [(8, 8, "science")]
    # The file has no PSFKIND: imported under the declared kind, the absence recorded.
    manifest = json.loads((prefix / "manifest.json").read_text())
    assert manifest["psf"] == {"kind": "science", "PSFKIND": None}
    (gaia,) = read(prefix, "catalogs")
    assert (gaia["dataset"], gaia["name"], gaia["version"]) == ("D", "gaia", "DR3")
    assert Path(gaia["location"][7:]).is_file()
    assert not (prefix / "refimimages.parquet").exists()


def test_an_import_job_takes_at_most_one_epoch(tmp_path, source):
    with pytest.raises(ValueError, match="at most one epoch"):
        run_import(tmp_path, source, {"epochs": EPOCHS}, "both")


# dev's two PSF files on the 20260821 night carry PSFKIND 'sciimage_psf'
# (sciimage_psf_f146_sca08.fits) and 'refimage_psf' (refimage_psf_f146_sca08.fits).
@pytest.mark.parametrize("keyword, kind", [("sciimage_psf", "science"), ("refimage_psf", "reference")])
def test_a_psf_whose_keyword_matches_its_declared_kind_imports_under_it(
        tmp_path, source, keyword, kind):
    fits.setval(source / "psf.fits", "PSFKIND", value=keyword)
    prefix = run_import(tmp_path, source, {"psf": {"kind": kind, "path": "psf.fits"}}, kind)
    assert [p["kind"] for p in read(prefix, "psfs")] == [kind]
    assert json.loads((prefix / "manifest.json").read_text())["psf"] == {
        "kind": kind, "PSFKIND": keyword}


@pytest.mark.parametrize("keyword, kind", [("refimage_psf", "science"), ("sciimage_psf", "reference")])
def test_a_psf_whose_keyword_names_the_other_kind_fails_before_a_manifest(
        tmp_path, source, keyword, kind):
    fits.setval(source / "psf.fits", "PSFKIND", value=keyword)
    with pytest.raises(ValueError, match="PSFKIND"):
        run_import(tmp_path, source, {"psf": {"kind": kind, "path": "psf.fits"}}, kind)
    assert not (tmp_path / "outputs" / kind / "manifest.json").exists()
