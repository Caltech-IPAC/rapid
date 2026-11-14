"""The import stage on a synthetic source directory: two tiny exposures, a reference, a PSF, a Gaia mirror.

Contracts: each output Parquet file has exactly its table's columns from
``database/schema.sql`` (without ``job``, and without the defaulted
``created``); key values and the source system's ids are carried; the
source set is the ``max_sources`` brightest by flux over flux error inside
the fit-position bounds; overlapping fields and sky indices are computed
from the image's WCS; the manifest declares every table and the
provenance line; both signs' PSF-fit catalogs load into ``sources`` and
both SExtractor catalogs into ``xsources``, with the extra columns `dev`
wrote into them, as `dev`'s loaders read them; a PSF imports under its declared kind, which `dev`'s
``PSFKIND`` keyword must not contradict, on the SCA of its ``SCA`` card, else of its ``DETECTOR``
card; an import supplies no identity
column but the source system's ids the plan keeps.
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from astropy.io import fits

from rapidpipe.exitcodes import CommandExit
from rapidpipe.foundation.local import JobContext
from rapidpipe.science.spatial import healpix_indexes, tessellation_field
from rapidpipe.stages import import_products
from tests.support.catalogs import write_gaia_store

REPO = Path(__file__).resolve().parents[2]
RA, DEC = 267.96138, -28.72761
SETTINGS = {"import": {"max_sources": 3, "xy_fit_min": -0.5, "xy_fit_max_offset": 0.5,
                       "limmag_nsigma": 5.0, "limmag_clip_sigma": 3.0, "limmag_maxiters": 10},
            "difference": {"sources_database_prefilter": {"sumrat_prefilter_threshold": 0.4,
                                                 "rb_prefilter_threshold": 0.0,
                                                 "snr_prefilter_threshold": 5.0},
                           "xsources_database_prefilter": {"sumrat_prefilter_threshold": 0.4,
                                                  "rb_prefilter_threshold": 0.0,
                                                  "snr_prefilter_threshold": 5.0}}}
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


def catalog(path: Path, sumrat=None, rb=None, snr=None) -> None:
    """Eight sources: S/N 10..80 at x = 10..17, the brightest outside the bounds, one with zero error;
    sumrat 0.9 each and no rb_score column unless given."""
    n = 8
    snr = np.arange(10.0, 10.0 * n + 1, 10.0) if snr is None else np.asarray(snr, dtype=float)
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
        "n_pixels": np.full(n, 9), "sumrat": np.full(n, 0.9) if sumrat is None else sumrat,
        **({} if rb is None else {"rb_score": rb}), **floats}), path)


#: dev's SExtractor columns the XSources loader reads, then the extra columns its pipeline
#: appends (compute_extra_cols_sxtractor), each vector with one header line at its first element.
SEXTRACTOR = ("NUMBER", "FLAGS", "XWIN_IMAGE", "YWIN_IMAGE", "ALPHAWIN_J2000", "DELTAWIN_J2000",
              "AWIN_WORLD", "BWIN_WORLD", "AWIN_IMAGE", "BWIN_IMAGE", "FWHM_IMAGE")
EXTRA = ("RB_SCORE", "NNEG", "NBAD", "SUMRAT", "SHARPNESS", "ROUNDNESS1", "ROUNDNESS2", "FLUX_FIT",
         "SNR_FIT", "REDUCED_CHI2", "N_PIXELS_FIT", "FLAGS_FIT", "CFIT", "RB_LABEL")


def sextractor_catalog(path: Path, rows: list[dict], extra: bool = True) -> None:
    """A SExtractor ASCII_HEAD catalog; each row gives x, y, snr and any extra column by name (else 0.5)."""
    header, n = [], 1
    for name in SEXTRACTOR:
        header.append(f"#{n:4d} {name:<22s} x")
        n += 1
    for name in ("FLUX_APER", "FLUXERR_APER"):
        header.append(f"#{n:4d} {name:<22s} x")
        n += 6
    header.append(f"#{n:4d} {'CLASS_STAR':<22s} x")   # a vector's width is read from the next line
    n += 1
    if extra:
        for name in EXTRA:
            header.append(f"#{n:4d} {name:<22s} x")
            n += 1
    lines = []
    for i, row in enumerate(rows, start=1):
        values = [i, 0, row["x"], row["y"], RA + i * 1e-4, DEC, 1e-4, 1e-4, 2.0, 2.0, 2.5]
        values += [100.0] * 6 + [100.0 / row["snr"]] * 6 + [0.9]
        if extra:
            values += [row.get(name, 0.5) for name in EXTRA]
        lines.append(" ".join(f"{v:.6f}" if isinstance(v, float) else str(v) for v in values))
    path.write_text("\n".join(header + lines) + "\n")


#: dev's loader's cuts on a 65-pixel image: kept, at the edge, sumrat <= 0.4, rb missing
#: (-999, never cut), rb <= 0, aperture S/N <= 5.
XSOURCES_POSITIVE = [{"x": 10.0, "y": 20.0, "snr": 20.0, "SUMRAT": 0.9, "RB_SCORE": 0.8, "RB_LABEL": 1},
                     {"x": 1.5, "y": 20.0, "snr": 20.0, "SUMRAT": 0.9},
                     {"x": 12.0, "y": 20.0, "snr": 20.0, "SUMRAT": 0.2},
                     {"x": 14.0, "y": 20.0, "snr": 20.0, "SUMRAT": 0.9, "RB_SCORE": -999.0,
                      "RB_LABEL": -1, "FLUX_FIT": -999.0},
                     {"x": 16.0, "y": 20.0, "snr": 20.0, "SUMRAT": 0.9, "RB_SCORE": 0.0},
                     {"x": 18.0, "y": 20.0, "snr": 3.0, "SUMRAT": 0.9}]
XSOURCES_NEGATIVE = [{"x": 30.0, "y": 30.0, "snr": 9.0, "SUMRAT": 0.7, "RB_SCORE": 0.6, "FLAGS_FIT": 4}]


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
    (src / "gaia").mkdir()
    write_gaia_store(src / "gaia", [{"ra": RA, "dec": DEC, "source_id": 1},
                                    {"ra": RA + 1e-3, "dec": DEC, "source_id": 2}])
    return src


GAIA = {"name": "gaia", "version": "DR3", "prefix": "{source}/gaia"}


def gaia_scope(source: Path) -> dict:
    return {**GAIA, "prefix": GAIA["prefix"].format(source=source)}


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


PSF_INPUT = {"kind": "input", "product": "psfs", "key": {}, "source_job": 9, "binding": None}


def run_import(tmp_path: Path, source: Path, scope: dict, name: str, inputs=(PSF_INPUT,)) -> Path:
    prefix = tmp_path / "outputs" / name
    prefix.mkdir(parents=True)
    job = JobContext(job=1, run=1, stage="import", scope={"source_dir": str(source), **scope},
                     survey="S", dataset="D", settings=SETTINGS, inputs=list(inputs),
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
    # S/N 80 lies outside the image and S/N 30 has zero error: the three kept are 7, 6, 5.
    assert sorted(r["sid"] for r in rows) == [5, 6, 7]
    catalog_rows = {r["id"]: r for r in pq.read_table(
        source / "jid1" / import_products.DIFFERENCE_CATALOG).to_pylist()}
    for row in rows:
        assert (row["ra"], row["dec"]) == (catalog_rows[row["sid"]]["ra"], catalog_rows[row["sid"]]["dec"])


def test_reference_psf_and_gaia_write_their_tables(tmp_path, source):
    prefix = run_import(tmp_path, source, {
        "reference": {"rfid": 55, "dir": "ref"},
        "psf": {"kind": "science", "path": "psf.fits"},
        "gaia": gaia_scope(source)}, "shared")
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
    assert gaia["location"] == (source / "gaia").resolve().as_uri() + "/"     # named, not copied
    assert manifest["gaia"]["release"] == "gaia_test" and manifest["gaia"]["ref_epoch"] == 2016.0
    assert not (prefix / "refimimages.parquet").exists()


def test_a_gaia_prefix_alone_needs_no_source_dir_and_a_non_mirror_is_refused(tmp_path, source):
    prefix = tmp_path / "outputs" / "mirror-only"
    prefix.mkdir(parents=True)
    job = JobContext(job=1, run=1, stage="import", scope={"gaia": gaia_scope(source)},
                     survey="S", dataset="D", settings=SETTINGS, inputs=[], attempt=1,
                     prefix=prefix.as_uri() + "/", connect=fake_connect(""))
    import_products.run(job)
    assert sorted(p.name for p in prefix.iterdir()) == ["catalogs.parquet", "manifest.json"]
    (gaia,) = read(prefix, "catalogs")
    # a file:// URL names the same mirror
    by_url = run_import(tmp_path, source, {"gaia": {**GAIA, "prefix": (source / "gaia").as_uri()}}, "by-url")
    assert read(by_url, "catalogs")[0]["location"] == gaia["location"]
    # a directory that is not a mirror, or an incomplete one, is refused before a manifest
    with pytest.raises(ValueError, match="not a gaia copy"):
        run_import(tmp_path, source, {"gaia": {**GAIA, "prefix": str(source)}}, "not-a-mirror")
    assert not (tmp_path / "outputs" / "not-a-mirror" / "manifest.json").exists()
    (source / "gaia" / "gaia.json").write_text(json.dumps({"release": "gaia_test", "complete": False,
                                                           "leaves_done": 1, "leaves_total": 2016}))
    with pytest.raises(ValueError, match="not complete"):
        run_import(tmp_path, source, {"gaia": gaia_scope(source)}, "incomplete")
    # a developer's copy of some leaves is registered knowingly, and recorded as such
    partial = run_import(tmp_path, source, {"gaia": {**gaia_scope(source), "partial": True}}, "partial")
    recorded = json.loads((partial / "manifest.json").read_text())["gaia"]
    assert recorded["complete"] is False and (recorded["leaves_done"], recorded["leaves_total"]) == (1, 2016)
    # files to copy need a source_dir
    with pytest.raises(ValueError, match="no source_dir"):
        import_products.run(JobContext(job=1, run=1, stage="import",
                                       scope={"psf": {"kind": "science", "path": "psf.fits"}},
                                       survey="S", dataset="D", settings=SETTINGS, inputs=[], attempt=1,
                                       prefix=prefix.as_uri() + "/", connect=fake_connect("")))


def test_ned_and_lvs_copies_register_under_their_kinds(tmp_path, source):
    from tests.support.catalogs import write_hp6_store, write_lvs_store
    ned = tmp_path / "ned"
    ned.mkdir()
    write_hp6_store(ned, [{"ra": RA, "dec": DEC, "prefname": "HOST"}])
    write_lvs_store(ned, [{"ra": RA, "dec": DEC, "objname": "HOST"}])
    prefix = run_import(tmp_path, source, {
        "ned": {"version": "36.1_20260527_v2", "prefix": str(ned)},
        "lvs": {"version": "20260424", "prefix": str(ned)}}, "ned-lvs")
    rows = {r["name"]: r for r in read(prefix, "catalogs")}
    assert set(rows) == {"ned", "lvs"}
    assert rows["ned"]["version"] == "36.1_20260527_v2" and rows["lvs"]["version"] == "20260424"
    assert rows["ned"]["location"] == rows["lvs"]["location"] == ned.resolve().as_uri() + "/"
    recorded = json.loads((prefix / "manifest.json").read_text())
    assert recorded["ned"]["release"] == "NED_TEST" and recorded["ned"]["complete"] is True
    assert recorded["lvs"]["release"] == "LVS_TEST" and recorded["lvs"]["rows"] == 1
    # a name other than the kind is refused; a directory without the copy's record is refused
    with pytest.raises(ValueError, match="named 'ned'"):
        run_import(tmp_path, source, {"ned": {"name": "ned-x", "version": "1", "prefix": str(ned)}}, "named")
    with pytest.raises(ValueError, match="not a lvs copy"):
        run_import(tmp_path, source, {"lvs": {"version": "1", "prefix": str(source)}}, "no-lvs")
    # an incomplete NED copy needs "partial": true
    write_hp6_store(ned, [{"ra": RA, "dec": DEC, "prefname": "HOST"}], complete=False)
    with pytest.raises(ValueError, match="not complete"):
        run_import(tmp_path, source, {"ned": {"version": "1", "prefix": str(ned)}}, "incomplete-ned")
    partial = run_import(tmp_path, source, {"ned": {"version": "1", "prefix": str(ned), "partial": True}},
                         "partial-ned")
    assert json.loads((partial / "manifest.json").read_text())["ned"]["complete"] is False


def test_an_import_job_takes_at_most_one_epoch(tmp_path, source):
    with pytest.raises(ValueError, match="at most one epoch"):
        run_import(tmp_path, source, {"epochs": EPOCHS}, "both")


def test_an_epoch_without_its_psfs_input_is_rejected_65_with_its_reason(tmp_path, source):
    with pytest.raises(CommandExit, match="psfs input") as stopped:
        run_import(tmp_path, source, {"epochs": [EPOCHS[0]]}, "nopsf", inputs=())
    assert stopped.value.code == 65
    assert not (tmp_path / "outputs" / "nopsf" / "manifest.json").exists()


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


# Commissioning's phavg science PSFs carry no SCA card, only DETECTOR ('WFI08').
@pytest.mark.parametrize("cards, sca", [({"DETECTOR": "WFI05"}, 8), ({"SCA": None, "DETECTOR": "WFI05"}, 5)])
def test_a_psfs_sca_is_its_sca_card_else_its_detector_card(tmp_path, source, cards, sca):
    for keyword, value in cards.items():
        if value is None:
            fits.delval(source / "psf.fits", keyword)
        else:
            fits.setval(source / "psf.fits", keyword, value=value)
    prefix = run_import(tmp_path, source, {"psf": {"kind": "science", "path": "psf.fits"}}, "psf")
    assert [p["sca"] for p in read(prefix, "psfs")] == [sca]


#: The source system's ids an import keeps in identity columns (plan Part 1:
#: they are the tables' primary keys, so a re-import gives new ids by hand).
#: Every other identity column an import writes is drawn from its sequence,
#: never supplied (PR 327: an explicit psfid ahead of the sequence failed a
#: later drawn insert on the key).
KEPT_SOURCE_IDS = {("l2files", "rid"), ("diffimages", "pid"), ("refimages", "rfid")}


def identity_columns() -> set[tuple[str, str]]:
    schema = (REPO / "database/schema.sql").read_text()
    identities = set()
    for table, body in re.findall(r"CREATE TABLE (\w+) \((.*?)\n\);", schema, re.S):
        identities |= {(table, line.split()[0]) for line in body.splitlines()
                       if "AS IDENTITY" in line}
    return identities


def test_an_import_supplies_no_identity_column_but_the_kept_source_ids(tmp_path, source):
    prefix = run_import(tmp_path, source, {
        "epochs": [EPOCHS[0]], "reference": {"rfid": 55, "dir": "ref"},
        "psf": {"kind": "science", "path": "psf.fits"},
        "gaia": gaia_scope(source)}, "all")
    tables = {o["table"] for o in json.loads((prefix / "manifest.json").read_text())["outputs"]}
    supplied = {(table, column) for table, column in identity_columns() if table in tables
                and column in pq.read_schema(prefix / f"{table}.parquet").names}
    assert {table for table, _ in identity_columns()} & tables, "the import writes no identity table"
    assert supplied <= KEPT_SOURCE_IDS


def test_the_negative_psf_fit_catalog_loads_numbered_after_the_positive_and_capped_by_sign(
        tmp_path, source):
    catalog(source / "jid1" / import_products.DIFFERENCE_CATALOG_NEGATIVE)
    prefix = run_import(tmp_path, source, {"epochs": [EPOCHS[0]]}, "both")
    rows = read(prefix, "sources")
    # The same eight in each sign: 7, 6, 5 kept by the cap of 3, the negatives' sid after id 8.
    assert sorted((r["isdiffpos"], r["sid"]) for r in rows) == [
        (False, 13), (False, 14), (False, 15), (True, 5), (True, 6), (True, 7)]
    assert set(pq.read_schema(prefix / "sources.parquet").names) == schema_columns("sources")
    manifest = json.loads((prefix / "manifest.json").read_text())
    assert manifest["sources_kept"] == {"positive": 3, "negative": 3}
    assert manifest["xsources"] is None


def test_without_the_negative_catalog_the_set_holds_the_positive_sources_only(tmp_path, source):
    prefix = run_import(tmp_path, source, {"epochs": [EPOCHS[0]]}, "positive")
    assert {r["isdiffpos"] for r in read(prefix, "sources")} == {True}
    manifest = json.loads((prefix / "manifest.json").read_text())
    assert manifest["sources_kept"] == {"positive": 3, "negative": None}


def test_xsources_are_devs_loaders_rows_with_the_extra_columns_dev_wrote(tmp_path, source):
    names = import_products.XSOURCES_CATALOGS
    sextractor_catalog(source / "jid1" / names["positive"], XSOURCES_POSITIVE)
    sextractor_catalog(source / "jid1" / names["negative"], XSOURCES_NEGATIVE)
    prefix = run_import(tmp_path, source, {"epochs": [EPOCHS[0]]}, "x")
    manifest = json.loads((prefix / "manifest.json").read_text())
    assert "xsources" in {o["table"] for o in manifest["outputs"]}
    assert manifest["xsources"] == {"read": {"positive": 6, "negative": 1},
                                    "kept": {"positive": 2, "negative": 1}}
    assert set(pq.read_schema(prefix / "xsources.parquet").names) == schema_columns("xsources")
    rows = read(prefix, "xsources")
    assert [(r["xsid"], r["num"], r["isdiffpos"]) for r in rows] == [
        (1, 1, True), (2, 4, True), (3, 1, False)]
    assert (rows[0]["rb"], rows[0]["rblabel"], rows[0]["sumrat"]) == (0.8, 1, 0.9)
    assert rows[1]["rb"] is None and rows[1]["fluxfit"] is None and rows[1]["rblabel"] == -1
    assert (rows[2]["rb"], rows[2]["flagsfit"], rows[2]["fluxap1"]) == (0.6, 4, 100.0)
    assert {(r["pid"], r["expid"]) for r in rows} == {(EPOCHS[0]["pid"], EPOCHS[0]["expid"])}
    for row in rows:
        assert row["field"] == tessellation_field(row["ra"], row["dec"])


def test_a_catalog_without_devs_extra_columns_loads_them_null_and_is_not_cut_on_them(
        tmp_path, source):
    names = import_products.XSOURCES_CATALOGS
    sextractor_catalog(source / "jid1" / names["positive"], XSOURCES_POSITIVE, extra=False)
    sextractor_catalog(source / "jid1" / names["negative"], [], extra=False)
    prefix = run_import(tmp_path, source, {"epochs": [EPOCHS[0]]}, "bare")
    rows = read(prefix, "xsources")
    # Only the edge and the aperture S/N cut: rows 1, 3, 4 and 5 stay.
    assert [r["num"] for r in rows] == [1, 3, 4, 5]
    assert all(r["sumrat"] is None and r["rb"] is None for r in rows)


def test_one_sextractor_catalog_without_the_other_is_rejected_65(tmp_path, source):
    sextractor_catalog(source / "jid1" / import_products.XSOURCES_CATALOGS["positive"],
                       XSOURCES_POSITIVE)
    with pytest.raises(CommandExit) as caught:
        run_import(tmp_path, source, {"epochs": [EPOCHS[0]]}, "half")
    assert caught.value.code == 65
    assert "sfftdiffimage_masked_negative.txt" in str(caught.value)


def test_both_signs_sources_pass_the_runs_sources_prefilter(tmp_path, source):
    difference = source / "jid1"
    low = np.full(8, 0.9)
    low[6] = 0.4                    # S/N 7 at the threshold: cut, so 6, 5 and 4 are kept
    catalog(difference / import_products.DIFFERENCE_CATALOG, sumrat=low)
    catalog(difference / import_products.DIFFERENCE_CATALOG_NEGATIVE, sumrat=low)
    rows = read(run_import(tmp_path, source, {"epochs": [EPOCHS[0]]}, "cut"), "sources")
    assert sorted((r["isdiffpos"], r["sid"]) for r in rows) == [
        (False, 12), (False, 13), (False, 14), (True, 4), (True, 5), (True, 6)]


def test_the_sources_prefilter_is_the_sources_table_not_the_xsources(tmp_path, source, monkeypatch):
    names = import_products.XSOURCES_CATALOGS
    sextractor_catalog(source / "jid1" / names["positive"], XSOURCES_POSITIVE)
    sextractor_catalog(source / "jid1" / names["negative"], XSOURCES_NEGATIVE)
    catalog(source / "jid1" / import_products.DIFFERENCE_CATALOG, sumrat=np.full(8, np.nan))
    body = {**SETTINGS, "difference": {**SETTINGS["difference"],
                                       "sources_database_prefilter": {"sumrat_prefilter_threshold": ""}}}
    monkeypatch.setattr(sys.modules[__name__], "SETTINGS", body)
    prefix = run_import(tmp_path, source, {"epochs": [EPOCHS[0]]}, "apart")
    # No sources cut, so null sumrat passes; the xsources keep their cuts.
    assert sorted(r["sid"] for r in read(prefix, "sources")) == [5, 6, 7]
    assert len(read(prefix, "xsources")) == 3


def test_both_signs_sources_are_cut_on_rb_and_psf_fit_snr(tmp_path, source):
    difference = source / "jid1"
    rb = np.full(8, 0.9)
    rb[6] = 0.0                              # sid 7 scored at the threshold: cut
    rb[5] = np.nan                           # sid 6 unscored: kept
    snr = np.arange(10.0, 81.0, 10.0)
    snr[4] = 5.0                             # sid 5 at the S/N threshold: cut
    catalog(difference / import_products.DIFFERENCE_CATALOG, rb=rb, snr=snr)
    catalog(difference / import_products.DIFFERENCE_CATALOG_NEGATIVE, rb=rb, snr=snr)
    rows = read(run_import(tmp_path, source, {"epochs": [EPOCHS[0]]}, "rbsnr"), "sources")
    # Left by S/N: 6 (unscored), 4, then 2 (3 has zero error, 1 is the faintest).
    assert sorted((r["isdiffpos"], r["sid"]) for r in rows) == [
        (False, 10), (False, 12), (False, 14), (True, 2), (True, 4), (True, 6)]
