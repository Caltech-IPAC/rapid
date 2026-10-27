"""Tests for rapidpipe.science.load.catalogs: `dev`'s join, HEALPix, rejection and CSV rows."""

from __future__ import annotations

import io
from pathlib import Path
import math

import healpy as hp
import numpy as np

from database.modules.utils.roman_tessellation_db import RomanTessellationClosedForm
from astropy.io import ascii
from astropy.table import Table

from rapidpipe.science.load import catalogs

#: The columns of the CSV the loader writes: `dev`'s
#: (loadPSFCatIntoDBSourcesTable.py L176-204), then the run columns.
CSV_COLUMNS = (
    "id", "ra", "dec", "xfit", "yfit", "fluxfit", "xerr", "yerr", "fluxerr",
    "npixfit", "qfit", "cfit", "redchi", "flags", "sharpness", "roundness1",
    "roundness2", "npix", "peak", "pid", "isdiffpos", "field", "hp6", "hp9",
    "expid", "fid", "sca", "mjdobs", "run", "attempt", "result_set",
)


#: `dev`'s Photutils column names (loadPSFCatIntoDBSourcesTable.py L570-571).
MAIN_COLUMNS = ("id", "group_id", "group_size", "local_bkg", "x_init", "y_init",
                "flux_init", "x_fit", "y_fit", "flux_fit", "x_err", "y_err",
                "flux_err", "n_pixels_fit", "qfit", "cfit", "reduced_chi2", "flags",
                "ra", "dec")
FINDER_COLUMNS = ("id", "x_centroid", "y_centroid", "sharpness", "roundness1",
                  "roundness2", "n_pixels", "peak", "flux", "mag", "daofind_mag")

_INT_COLUMNS = {"id", "group_id", "group_size", "n_pixels_fit", "flags", "n_pixels"}



def main_row(id_: int, x_fit: float, y_fit: float, ra: float, dec: float, **overrides) -> dict:
    """A PSF-fit catalog row; every column not given is a distinct, recognisable value."""
    row = {"id": id_, "group_id": id_, "group_size": 1, "local_bkg": 0.25,
           "x_init": x_fit, "y_init": y_fit, "flux_init": 100.0 + id_,
           "x_fit": x_fit, "y_fit": y_fit, "flux_fit": 101.5 + id_,
           "x_err": 0.0125, "y_err": 0.025, "flux_err": 1.5 + id_ / 10,
           "n_pixels_fit": 25, "qfit": 0.0625, "cfit": 0.03125, "reduced_chi2": 1.25,
           "flags": 0, "ra": ra, "dec": dec}
    row.update(overrides)
    return row


def finder_row(id_: int, **overrides) -> dict:
    row = {"id": id_, "x_centroid": 10.0, "y_centroid": 10.0, "sharpness": 0.5,
           "roundness1": 0.015625, "roundness2": -0.03125, "n_pixels": 9,
           "peak": 12.5 + id_, "flux": 50.0, "mag": -4.25, "daofind_mag": -1.75}
    row.update(overrides)
    return row


def _table(rows: list[dict], columns: tuple[str, ...]) -> Table:
    data = {}
    for name in columns:
        values = [r[name] for r in rows]
        data[name] = np.array(values, dtype=np.int64 if name in _INT_COLUMNS else np.float64)
    return Table(data, names=columns)


def write_photutils_catalogs(catalog: Path, finder: Path, main_rows: list[dict],
                             finder_rows: list[dict]) -> None:
    """Write the two catalogs as `dev`'s ``ascii.write`` does (basic format)."""
    ascii.write(_table(main_rows, MAIN_COLUMNS), catalog, overwrite=True)
    ascii.write(_table(finder_rows, FINDER_COLUMNS), finder, overwrite=True)




def _joined(tmp_path, main_rows, finder_rows):
    write_photutils_catalogs(tmp_path / "c.txt", tmp_path / "f.txt", main_rows, finder_rows)
    return catalogs.read_joined_catalog(tmp_path / "c.txt", tmp_path / "f.txt")


def _csv(joined, *, naxis=65, run_columns=("RUN", "ATT", "SET")):
    hp6, hp9 = catalogs.healpix_arrays(joined)
    buf = io.StringIO()
    n = catalogs.write_joined_table_inner_to_csv_file(
        "true", 1234, 7, 3, 61273.125, 4242, buf, joined, hp6, hp9,
        naxis1=naxis, naxis2=naxis, run_columns=run_columns)
    rows = [dict(zip(CSV_COLUMNS, line.split(","))) for line in buf.getvalue().splitlines()]
    return n, rows


def test_inner_join_on_id_drops_rows_missing_from_either_catalog(tmp_path):
    joined = _joined(tmp_path,
                     [main_row(1, 10, 10, 269.45, -28.77), main_row(2, 11, 11, 269.45, -28.77)],
                     [finder_row(1), finder_row(3)])
    assert list(joined["id"]) == [1]
    assert {"x_fit", "sharpness", "peak", "ra", "dec"} <= set(joined.colnames)


def test_healpix_is_nested_lonlat_at_levels_6_and_9(tmp_path):
    joined = _joined(tmp_path, [main_row(1, 10, 10, 269.45, -28.77)], [finder_row(1)])
    hp6, hp9 = catalogs.healpix_arrays(joined)
    assert hp6[0] == hp.ang2pix(64, 269.45, -28.77, nest=True, lonlat=True)
    assert hp9[0] == hp.ang2pix(512, 269.45, -28.77, nest=True, lonlat=True)


def test_fit_positions_outside_the_image_are_rejected_as_dev_rejects_them(tmp_path):
    rows = [main_row(1, 10.0, 12.0, 269.45, -28.77),
            main_row(2, -0.6, 12.0, 269.45, -28.77),       # x below -0.5
            main_row(3, 64.6, 12.0, 269.45, -28.77),       # x above 65 - 0.5
            main_row(4, float("nan"), 12.0, 269.45, -28.77),
            main_row(5, 64.5, -0.5, 269.46, -28.78)]       # both edges inclusive
    joined = _joined(tmp_path, rows, [finder_row(i) for i in range(1, 6)])
    n, csv_rows = _csv(joined)
    assert n == 2
    assert [r["id"] for r in csv_rows] == ["1", "5"]


def test_csv_rows_carry_devs_columns_then_the_run_columns(tmp_path):
    joined = _joined(tmp_path, [main_row(1, 10.0, 12.0, 269.45, -28.77)], [finder_row(1)])
    n, (row,) = _csv(joined)
    assert n == 1
    assert (row["pid"], row["isdiffpos"], row["expid"], row["fid"], row["sca"], row["mjdobs"]) == (
        "4242", "true", "1234", "3", "7", "61273.125")
    assert (row["xfit"], row["fluxfit"], row["npixfit"], row["redchi"], row["npix"], row["peak"]) == (
        "10.0", "102.5", "25", "1.25", "9", "13.5")
    assert (row["run"], row["attempt"], row["result_set"]) == ("RUN", "ATT", "SET")
    assert int(row["field"]) == RomanTessellationClosedForm().get_rtid(269.45, -28.77)
    assert int(row["hp9"]) == hp.ang2pix(512, 269.45, -28.77, nest=True, lonlat=True)
    assert math.isclose(float(row["ra"]), 269.45) and math.isclose(float(row["dec"]), -28.77)


def test_an_empty_or_fully_rejected_catalog_writes_nothing(tmp_path):
    joined = _joined(tmp_path, [main_row(1, 99.0, 12.0, 269.45, -28.77)], [finder_row(1)])
    assert _csv(joined) == (0, [])
    joined = _joined(tmp_path, [main_row(1, 10.0, 12.0, 269.45, -28.77)], [finder_row(2)])
    assert len(joined) == 0
    assert _csv(joined) == (0, [])


def test_tessellation_ids_match_the_per_row_lookup():
    ra = np.array([0.0, 90.0, 269.45, 359.99])
    dec = np.array([0.0, 45.0, -28.77, -89.9])
    closed = RomanTessellationClosedForm()
    assert list(catalogs.tessellation_ids(ra, dec)) == [closed.get_rtid(r, d) for r, d in zip(ra, dec)]


def test_source_set_keeps_the_brightest_inside_the_bounds_and_zero_keeps_all():
    import pyarrow as pa

    from rapidpipe.science.load.catalogs import source_set

    names = [name for name, _ in catalogs.CATALOG_COLUMNS]
    rows = {name: [1.0, 1.0, 1.0, 1.0] for name in names}
    rows.update(id=[1, 2, 3, 4], ra=[10.0] * 4, dec=[-5.0] * 4,
                x_fit=[5.0, 6.0, 7.0, 99.9],        # the fourth is off a 10-pixel image
                flux_fit=[10.0, 50.0, 30.0, 500.0], flux_err=[1.0, 1.0, 1.0, 1.0])
    key = {"dataset": "D", "expid": 1, "sca": 2, "fid": 3}
    table, candidates = source_set(pa.table(rows), key, 7, 60000.0, 10, 10, max_sources=2)
    assert candidates == 3
    assert table.column("sid").to_pylist() == [2, 3]
    assert set(table.column("pid").to_pylist()) == {7}
    everything, _ = source_set(pa.table(rows), key, 7, 60000.0, 10, 10, max_sources=0)
    assert sorted(everything.column("sid").to_pylist()) == [1, 2, 3]


def test_source_set_carries_the_pixel_columns_null_where_the_catalog_has_none():
    import pyarrow as pa

    from rapidpipe.science.load.catalogs import source_set

    names = [name for name, _ in catalogs.CATALOG_COLUMNS]
    rows = {name: [1.0, 1.0] for name in names}
    rows.update(id=[1, 2], ra=[10.0] * 2, dec=[-5.0] * 2, x_fit=[5.0, 6.0],
                flux_fit=[50.0, 10.0], flux_err=[1.0, 1.0])
    key = {"dataset": "D", "expid": 1, "sca": 2, "fid": 3}
    measured = pa.table({**rows, "sumrat": [0.8, np.nan], "nneg": [3.0, np.nan], "nbad": [0.0, np.nan]})
    table, _ = source_set(measured, key, 7, 60000.0, 10, 10, max_sources=0)
    assert [table.column(c).to_pylist() for c in ("sid", "sumrat", "nneg", "nbad")] == [
        [1, 2], [0.8, None], [3, None], [0, None]]
    older, _ = source_set(pa.table(rows), key, 7, 60000.0, 10, 10, max_sources=0)
    assert [older.column(c).null_count for c in ("sumrat", "nneg", "nbad")] == [2, 2, 2]


def test_source_set_loads_the_score_as_rb_and_the_label_as_rblabel_null_where_unscored():
    import pyarrow as pa

    from rapidpipe.science.load.catalogs import source_set

    rows = {name: [1.0, 1.0] for name, _ in catalogs.CATALOG_COLUMNS}
    rows.update(id=[1, 2], ra=[10.0] * 2, dec=[-5.0] * 2, x_fit=[5.0, 6.0],
                flux_fit=[50.0, 10.0], flux_err=[1.0, 1.0])
    key = {"dataset": "D", "expid": 1, "sca": 2, "fid": 3}
    scored = pa.table({**rows, "rb_score": [0.75, np.nan],
                       "rb_label": pa.array([1, -1], pa.int16())})
    table, _ = source_set(scored, key, 7, 60000.0, 10, 10, max_sources=0)
    assert [table.column(c).to_pylist() for c in ("sid", "rb", "rblabel")] == [
        [1, 2], [0.75, None], [1, -1]]
    zogy, _ = source_set(pa.table(rows), key, 7, 60000.0, 10, 10, max_sources=0)
    assert [zogy.column(c).null_count for c in ("rb", "rblabel")] == [2, 2]


def _prefilter_rows(x_fit, sumrat, flux_fit):
    import pyarrow as pa

    n = len(x_fit)
    rows = {name: [5.0] * n for name, _ in catalogs.CATALOG_COLUMNS}
    rows.update(id=list(range(1, n + 1)), ra=[10.0] * n, dec=[-5.0] * n, x_fit=x_fit,
                flux_fit=flux_fit, flux_err=[1.0] * n, sumrat=sumrat)
    return pa.table(rows)


KEY = {"dataset": "D", "expid": 1, "sca": 2, "fid": 3}


def test_the_prefilter_keeps_sumrat_above_the_threshold_and_drops_at_or_below_or_null():
    rows = _prefilter_rows([5.0] * 4, [0.41, 0.4, None, 0.1], [40.0, 30.0, 20.0, 10.0])
    table, candidates = catalogs.source_set(rows, KEY, 7, 60000.0, 10, 10, max_sources=0,
                                            sumrat_prefilter_threshold=0.4)
    assert table.column("sid").to_pylist() == [1] and candidates == 1


def test_the_prefilter_drops_a_source_within_2_px_of_the_edge():
    # zero-based, 10 pixels: nearest pixels 1, 2, 7 and 8; 2 and 7 are the last kept
    rows = _prefilter_rows([1.4, 1.6, 7.4, 7.6], [0.9] * 4, [40.0, 30.0, 20.0, 10.0])
    table, _ = catalogs.source_set(rows, KEY, 7, 60000.0, 10, 10, max_sources=0,
                                   sumrat_prefilter_threshold=0.4)
    assert table.column("sid").to_pylist() == [2, 3]


def test_the_prefilter_runs_before_the_cap():
    rows = _prefilter_rows([5.0] * 3, [0.1, 0.9, 0.9], [40.0, 30.0, 20.0])
    table, _ = catalogs.source_set(rows, KEY, 7, 60000.0, 10, 10, max_sources=1,
                                   sumrat_prefilter_threshold=0.4)
    assert table.column("sid").to_pylist() == [2]
