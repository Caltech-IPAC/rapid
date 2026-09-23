"""Tests for rapidpipe.science.load.catalogs: `dev`'s join, HEALPix, rejection and CSV rows."""

from __future__ import annotations

import io
import math

import healpy as hp
import numpy as np

from database.modules.utils.roman_tessellation_db import RomanTessellationClosedForm
from rapidpipe.db import sources
from rapidpipe.science.load import catalogs
from tests.unit.fakeloaddb import finder_row, main_row, write_photutils_catalogs


def _joined(tmp_path, main_rows, finder_rows):
    write_photutils_catalogs(tmp_path / "c.txt", tmp_path / "f.txt", main_rows, finder_rows)
    return catalogs.read_joined_catalog(tmp_path / "c.txt", tmp_path / "f.txt")


def _csv(joined, *, naxis=65, run_columns=("RUN", "ATT", "SET")):
    hp6, hp9 = catalogs.healpix_arrays(joined)
    buf = io.StringIO()
    n = catalogs.write_joined_table_inner_to_csv_file(
        "true", 1234, 7, 3, 61273.125, 4242, buf, joined, hp6, hp9,
        naxis1=naxis, naxis2=naxis, run_columns=run_columns)
    rows = [dict(zip(sources.COLUMNS, line.split(","))) for line in buf.getvalue().splitlines()]
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
