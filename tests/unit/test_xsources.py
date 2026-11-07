"""rapidpipe.science.difference.xsources: dev's xsources, its extra columns and its loader's cuts.

The fixture ``tests/fixtures/difference/xsources_sextractor.txt`` is a real
SExtractor ``ASCII_HEAD`` catalog, made with ``cdf/rapidSexParamsDiffImage.inp``
and dev's apertures from :func:`image` (four Gaussian sources and the noise
SExtractor found, its WCS straddling RA 0). Its five rows, one-based:
1 noise (S/N 1.4), 2 at (101.3, 101.3), 3 at (61.1, 91.3) beside the pixel
:func:`image` may blank, 4 at (31.3, 41.6), 5 at (81.8, 21.3).
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from rapidpipe.science.difference import xsources

REPO = Path(__file__).resolve().parents[2]
CATALOG = REPO / "tests" / "fixtures" / "difference" / "xsources_sextractor.txt"
SIGMA = 1.1
TRUTH = ((30.3, 40.6, 500.0), (80.8, 20.2, 150.0), (60.0, 90.4, 60.0), (100.5, 100.5, 30.0))
KEY = {"dataset": "D", "expid": 7, "sca": 8, "fid": 1}


def image() -> np.ndarray:
    """The synthetic difference image the fixture catalog was measured on (zero-based truth)."""
    rng = np.random.default_rng(7)
    yy, xx = np.mgrid[:120, :120]
    data = rng.normal(0.0, 1.0, (120, 120))
    for x, y, f in TRUTH:
        data += f * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * SIGMA ** 2)) / (2 * np.pi * SIGMA ** 2)
    return data


def psf() -> np.ndarray:
    yy, xx = np.mgrid[:15, :15]
    stamp = np.exp(-((xx - 7) ** 2 + (yy - 7) ** 2) / (2 * SIGMA ** 2))
    return stamp / stamp.sum()


def settings(**prefilter) -> dict:
    return {"sumrat": {"stamp_size": 5, "filter_size": 3, "lower_median": False},
            "extra_catalog_cols": {"nneg_nbad_stamp_size": 5, "pixel_cols_bkg_box": 32,
                                   "pixel_cols_bkg_filter_size": 3, "photutils_cols_snap_radius": 1,
                                   "photutils_cols_xy_bounds": "", "photutils_cols_fit_shape": [5, 5],
                                   "photutils_cols_num_processes": 1,
                                   "photutils_cols_prefilter_only": True},
            "psfcat_diffimage": {"fwhm": 2.0, "fit_shape": [17, 17], "aperture_radius": 8.0},
            "database_prefilter": {"sumrat_prefilter_threshold": 0.4, "rb_prefilter_threshold": 0.0,
                                   "snr_prefilter_threshold": 5.0, **prefilter}}


def thresholds(table: dict) -> dict:
    return {k: xsources.threshold(table, f"{k}_prefilter_threshold") for k in ("sumrat", "rb", "snr")}


def schema_columns(table: str) -> list[str]:
    body = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", (REPO / "database/schema.sql").read_text(),
                     re.S).group(1)
    return [m for m in re.findall(r"^\s+(\w+) ", body, re.M) if m not in ("UNIQUE", "job")]


def test_the_catalog_reads_with_the_vector_columns_named_as_devs_loader_reads_them():
    catalog = xsources.read_catalog(CATALOG)
    assert len(catalog) == 5
    for name, _ in xsources.SEXTRACTOR_COLUMNS:
        assert name in catalog.colnames
    snr = xsources.aperture_snr(catalog)
    assert snr[0] == pytest.approx(3.551375 / 2.564488)


@pytest.mark.parametrize("x, kept", [(0.4999, False), (2.49, False), (2.5, True), (98.4, True),
                                     (98.5, False), (np.nan, False)])
def test_the_position_cut_is_devs(x, kept):
    """On a 100-pixel image: inside [0.5, 100.5] and the nearest pixel from 3 to 98 (2 px margin);
    0.4999 is inside the 1e-4 tolerance but its nearest pixel is 0."""
    assert xsources.positions_within_image([x], [50.0], 100, 100)[0] == kept


def test_the_prefilter_cuts_only_finite_values_at_or_below_their_thresholds():
    sumrat = np.array([0.4, 0.41, np.nan, 0.9, 0.9])
    rb = np.array([0.5, 0.5, 0.5, 0.0, np.nan])
    snr = np.array([10.0, 10.0, 10.0, 10.0, np.inf])
    keep = xsources.prefilter(5, sumrat, rb, snr, sumrat_threshold=0.4, rb_threshold=0.0,
                              snr_threshold=5.0)
    assert keep.tolist() == [False, True, True, False, True]
    assert xsources.prefilter(5, sumrat, rb, snr).all()          # no thresholds, no cuts


def test_extra_columns_fit_only_the_sources_the_prefilter_and_the_image_keep():
    catalog = xsources.read_catalog(CATALOG)
    extra, fitted = xsources.extra_columns(catalog, image(), np.ones((120, 120)), psf(), settings())
    assert fitted == 4                                          # the noise row fails S/N 5
    assert np.isnan(extra["flux_fit"][0]) and np.isfinite(extra["sumrat"][0])
    xw = np.asarray(catalog["XWIN_IMAGE"]) - 1.0
    yw = np.asarray(catalog["YWIN_IMAGE"]) - 1.0
    for x, y, f in TRUTH:
        i = int(np.argmin(np.hypot(xw - x, yw - y)))             # the catalog's nearest source
        assert extra["flux_fit"][i] == pytest.approx(f, rel=0.15)
        assert extra["x_fit"][i] == pytest.approx(x, abs=0.3) and extra["y_fit"][i] == pytest.approx(y, abs=0.3)
        assert extra["sumrat"][i] > 0.4 and extra["snr_fit"][i] > 5
    assert set(xsources.PHOTUTILS_COLUMNS) | {"sumrat", "nneg", "nbad"} <= set(extra)


def test_without_prefilter_only_every_source_on_the_image_is_fitted():
    body = settings()
    body["extra_catalog_cols"]["photutils_cols_prefilter_only"] = False
    _, fitted = xsources.extra_columns(xsources.read_catalog(CATALOG), image(), np.ones((120, 120)),
                                       psf(), body)
    assert fitted == 5


def test_a_bad_pixel_in_the_core_sets_dao_flags_and_an_off_image_position_is_nan():
    data = image()
    data[90, 61] = np.nan                                       # beside the third source's peak
    out = xsources.photutils_columns(data, np.ones_like(data), psf(), [60.08, -5.0, np.nan],
                                     [90.29, 3.0, 3.0], fwhm=2.0, fit_shape=(5, 5), aperture_radius=8.0)
    assert out["dao_flags"][0] == 3 and out["n_pixels_fit"][0] == 24
    assert np.isnan([out[c][1] for c in xsources.PHOTUTILS_COLUMNS]).all()
    assert np.isnan([out[c][2] for c in xsources.PHOTUTILS_COLUMNS]).all()


def test_rb_input_carries_the_classifiers_columns_and_devs_flux_err():
    extra = {name: np.array([2.0, np.nan]) for name in xsources.PHOTUTILS_COLUMNS}
    extra["flux_fit"] = np.array([100.0, np.nan])
    extra["snr_fit"] = np.array([20.0, np.nan])
    table = xsources.rb_input(extra)
    from rapidpipe.science.difference.realbogus import CATALOG_COLUMNS
    assert set(CATALOG_COLUMNS.values()) <= set(table.column_names)
    assert table.column("flux_err").to_pylist()[0] == pytest.approx(5.0)
    assert table.column("id").to_pylist() == [0, 1]


def test_the_rows_keep_devs_cuts_and_map_onto_the_xsources_columns():
    catalog = xsources.read_catalog(CATALOG)
    extra, _ = xsources.extra_columns(catalog, image(), np.ones((120, 120)), psf(), settings())
    extra["rb_score"] = np.array([np.nan, 0.0, 0.7, 0.9, 0.6])   # row 2 scored exactly 0
    extra["rb_label"] = np.array([-1.0, 0.0, 1.0, 1.0, 1.0])
    negative = {k: v[3:4] for k, v in extra.items()}
    positive_rows, read = xsources.xsource_rows(
        catalog, extra, isdiffpos=True, key=KEY, pid=11, mjdobs=61348.5, naxis1=120, naxis2=120,
        thresholds=thresholds(settings()["database_prefilter"]))
    negative_rows, _ = xsources.xsource_rows(
        catalog[3:4], negative, isdiffpos=False, key=KEY, pid=11, mjdobs=61348.5, naxis1=120,
        naxis2=120, thresholds=thresholds(settings()["database_prefilter"]))
    assert read == 5
    assert sorted(positive_rows["num"].tolist()) == [3, 4, 5]    # 1 fails S/N, 2 fails rb <= 0
    table = xsources.xsources_table([positive_rows, negative_rows])
    assert table.column_names == schema_columns("xsources")
    assert table.column("xsid").to_pylist() == [1, 2, 3, 4]
    assert table.column("isdiffpos").to_pylist() == [True, True, True, False]
    assert set(table.column("pid").to_pylist()) == {11}
    assert set(table.column("mjdobs").to_pylist()) == {61348.5}
    rows = {r["num"]: r for r in table.slice(0, 3).to_pylist()}
    assert rows[4]["rb"] == pytest.approx(0.9) and rows[4]["rblabel"] == 1
    assert rows[4]["flags"] == 0 and rows[4]["fluxap1"] == pytest.approx(287.1921)


def test_a_missing_extra_value_is_null_and_rblabel_minus_one_stays():
    catalog = xsources.read_catalog(CATALOG)[3:4]
    extra = {"sumrat": np.array([np.nan]), "rb_score": np.array([np.nan]),
             "rb_label": np.array([-1.0])}
    rows, _ = xsources.xsource_rows(catalog, extra, isdiffpos=True, key=KEY, pid=1, mjdobs=1.0,
                                    naxis1=120, naxis2=120,
                                    thresholds=thresholds(settings()["database_prefilter"]))
    (row,) = xsources.xsources_table([rows]).to_pylist()
    assert row["sumrat"] is None and row["rb"] is None and row["fluxfit"] is None
    assert row["rblabel"] == -1


def test_sources_either_side_of_ra_zero_share_their_field_and_healpix():
    catalog = xsources.read_catalog(CATALOG)
    ra = np.asarray(catalog["ALPHAWIN_J2000"])
    assert (ra > 359.9).any() and (ra < 0.1).any()
    rows, _ = xsources.xsource_rows(catalog, {}, isdiffpos=True, key=KEY, pid=1, mjdobs=1.0,
                                    naxis1=120, naxis2=120,
                                    thresholds={"sumrat": None, "rb": None, "snr": None})
    assert len(set(rows["field"].tolist())) == 1
    assert len(set(rows["hp9"].tolist())) == 1


@pytest.mark.parametrize("damage", ["nan_patch", "zero_uncertainty", "all_nan", "nothing_passes"])
def test_a_value_that_cannot_be_computed_is_null_and_nothing_fails(damage):
    """dev's -999 cases: the source's values are null, the other sources' are computed."""
    data, error, body = image(), np.ones((120, 120)), settings()
    if damage == "nan_patch":
        data[85:97, 55:67] = np.nan                                  # over the third source
    elif damage == "zero_uncertainty":
        error[85:97, 55:67] = 0.0                                    # photutils cannot weight it
    elif damage == "all_nan":
        data[:] = np.nan
    else:
        body = settings(snr_prefilter_threshold=1e9)
    catalog = xsources.read_catalog(CATALOG)
    extra, _ = xsources.extra_columns(catalog, data, error, psf(), body)
    rows, _ = xsources.xsource_rows(catalog, extra, isdiffpos=True, key=KEY, pid=1, mjdobs=1.0,
                                    naxis1=120, naxis2=120,
                                    thresholds=thresholds(settings()["database_prefilter"]))
    table = {r["num"]: r for r in xsources.xsources_table([rows]).to_pylist()}
    third = table[3]                                                 # the source at (61.1, 91.3)
    if damage in ("nan_patch", "all_nan"):
        assert third["sumrat"] is None and third["fluxfit"] is None and third["sharpness"] is None
    elif damage == "zero_uncertainty":
        assert third["fluxfit"] is None and third["sumrat"] is not None
    else:
        assert all(r["fluxfit"] is None for r in table.values())
    if damage != "all_nan":
        assert table[4]["sumrat"] is not None                        # the bright source, untouched
    if damage in ("nan_patch", "zero_uncertainty"):
        assert table[4]["fluxfit"] == pytest.approx(500.0, rel=0.15)


def test_devs_extra_columns_read_back_from_its_catalog_with_its_fill_value_null(tmp_path):
    lines = ["#   1 NUMBER                 x", "#   2 FLAGS                  x",
             "#   3 SUMRAT                 x", "#   4 FLAGS_FIT              x",
             "#   5 RB_SCORE               x", "1 2 0.5 4 -999.0", "2 0 -999.0 -999.0 0.75"]
    path = tmp_path / "cat.txt"
    path.write_text("\n".join(lines) + "\n")
    extra = xsources.catalog_extra_columns(xsources.read_catalog(path))
    assert set(extra) == {name for name, _, _ in xsources.EXTRA_COLUMNS}
    np.testing.assert_array_equal(extra["sumrat"], [0.5, np.nan])
    np.testing.assert_array_equal(extra["flags"], [4.0, np.nan])   # FLAGS_FIT, not SExtractor's FLAGS
    np.testing.assert_array_equal(extra["rb_score"], [np.nan, 0.75])
    assert np.isnan(extra["cfit"]).all() and len(extra["cfit"]) == 2
