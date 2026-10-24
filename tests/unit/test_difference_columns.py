"""`dev`'s pixel columns, :mod:`rapidpipe.science.difference.columns`, on synthetic images.

Contracts: nneg counts a stamp's finite negative pixels and nbad its
non-finite ones, on the pixel nearest a zero-based position, NaN where the
stamp leaves the image (`dev`'s ``test_nneg_nbad.py``); sumrat is near 1 on
a point source and near 0 on a dipole, and about 0 on noise with the true
median, about -0.32 with ZTF's lower median; the local background removes
a constant pedestal and keeps the non-finite pixels, so the columns of a
source on a pedestal are those of the source alone; the catalog gains the
three columns and keeps every other column and its row order.
"""

from __future__ import annotations

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from astropy.io import fits

from rapidpipe.science.difference import columns

SETTINGS = {"sumrat": {"stamp_size": 5, "filter_size": 3, "lower_median": False},
            "extra_catalog_cols": {"nneg_nbad_stamp_size": 5, "pixel_cols_bkg_box": 64,
                                   "pixel_cols_bkg_filter_size": 3}}


def stamp_image() -> np.ndarray:
    """dev's 21 x 21 image of ones with a known 5 x 5 stamp around zero-based pixel (10, 10)."""
    data = np.ones((21, 21))
    data[8, 8] = -1.0           # negative
    data[8, 12] = -0.5          # negative
    data[12, 10] = -3.0         # negative
    data[10, 11] = np.nan       # bad
    data[11, 9] = np.inf        # bad
    data[9, 9] = 0.0            # zero: neither negative nor bad
    data[10, 13] = -5.0         # negative, but outside the 5 x 5 stamp
    return data


def gaussian(shape, x0, y0, sigma, amplitude=1.0) -> np.ndarray:
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    return amplitude * np.exp(-((xx - x0) ** 2 + (yy - y0) ** 2) / (2 * sigma ** 2))


def counts(*args, **kwargs) -> tuple[list, list]:
    nneg, nbad = columns.nneg_nbad(*args, **kwargs)
    return nneg.tolist(), nbad.tolist()


def test_nneg_and_nbad_count_the_hand_built_stamp_on_the_nearest_pixel():
    data = stamp_image()
    assert counts(data, [10.0], [10.0]) == ([3.0], [2.0])
    assert counts(data, [10.4], [9.6]) == ([3.0], [2.0])     # rounds to (10, 10)
    assert counts(data, [11.0], [10.0]) == ([3.0], [2.0])    # gains (10, 13), loses (8, 8)
    assert counts(data, [9.0], [10.0]) == ([2.0], [2.0])
    assert counts(data, [10.0], [10.0], stamp_size=3) == ([0.0], [2.0])
    with pytest.raises(ValueError):
        columns.nneg_nbad(data, [10.0], [10.0], stamp_size=4)


def test_a_position_not_finite_or_whose_stamp_leaves_the_image_is_nan():
    x = [np.nan, 1.0, 19.0, 10.0, 10.0]     # not finite; off the left; off the right; inside; the stamp
    y = [10.0, 10.0, 10.0, 2.0, 10.0]
    nneg, nbad = columns.nneg_nbad(stamp_image(), x, y)
    np.testing.assert_array_equal(nneg, [np.nan, np.nan, np.nan, 0.0, 3.0])
    np.testing.assert_array_equal(nbad, [np.nan, np.nan, np.nan, 0.0, 2.0])
    assert np.isnan(columns.sumrat(stamp_image(), x, y)[:3]).all()


def test_pure_noise_is_about_half_negative():
    data = np.random.default_rng(1).normal(0, 1, (201, 201))
    xy = np.array([(x, y) for x in range(10, 191, 10) for y in range(10, 191, 10)], dtype=float)
    nneg, nbad = columns.nneg_nbad(data, xy[:, 0], xy[:, 1])
    assert np.mean(nneg) == pytest.approx(12.5, abs=0.5)
    assert set(nbad) == {0.0}


def test_sumrat_is_near_one_on_a_point_source_and_near_zero_on_a_dipole():
    point = gaussian((41, 41), 20, 20, 1.5)
    dipole = gaussian((41, 41), 19, 20, 1.5) - gaussian((41, 41), 21, 20, 1.5)
    assert columns.sumrat(point, [20.0], [20.0])[0] == pytest.approx(1.0)
    assert abs(columns.sumrat(dipole, [20.0], [20.0])[0]) < 0.05
    assert columns.sumrat(point, [20.0], [20.0])[0] > 0.4 > columns.sumrat(dipole, [20.0], [20.0])[0]


def test_noise_sumrat_is_about_zero_with_the_true_median_and_biased_with_ztfs():
    data = np.random.default_rng(2).normal(0, 1, (1006, 1006))
    grid = np.arange(5, 1001, 7, dtype=float)
    x, y = (a.ravel() for a in np.meshgrid(grid, grid))
    assert np.mean(columns.sumrat(data, x, y)) == pytest.approx(0.0, abs=0.03)
    assert np.mean(columns.sumrat(data, x, y, lower_median=True)) == pytest.approx(-0.32, abs=0.05)


def test_the_local_background_removes_a_pedestal_and_keeps_the_bad_pixels():
    pytest.importorskip("photutils")
    data = np.random.default_rng(5).normal(-3.0, 1.0, (256, 256))
    data[100:104, 50:54] = np.nan
    out = columns.subtract_local_background(data, box_size=64)
    assert abs(np.nanmedian(out)) < 0.1
    np.testing.assert_array_equal(np.isnan(out), np.isnan(data))


def test_a_source_on_a_pedestal_measures_as_the_source_alone():
    pytest.importorskip("photutils")
    data = -3.0 + gaussian((256, 256), 128, 128, 1.5, amplitude=8.0)
    plain = columns.sumrat(data, [128.0], [128.0])[0]
    measured = columns.pixel_columns(data, [128.0], [128.0], SETTINGS)
    assert plain < 0.9                                  # the pedestal pulls the stamp down
    assert measured["sumrat"][0] == pytest.approx(1.0, abs=1e-6)
    assert measured["nneg"][0] == 0


def test_the_catalog_gains_the_three_columns_and_keeps_the_rest(tmp_path):
    pytest.importorskip("photutils")
    data = -3.0 + gaussian((256, 256), 128, 128, 1.5, amplitude=8.0)
    fits.PrimaryHDU(data.astype(np.float32)).writeto(tmp_path / "diff.fits")
    before = pa.table({"id": [2, 1], "x_fit": [128.0, 1.0], "y_fit": [128.0, 128.0],
                       "flux_fit": [5.0, 6.0]})
    pq.write_table(before, tmp_path / "cat.parquet")
    assert columns.add_pixel_columns(tmp_path / "cat.parquet", tmp_path / "diff.fits", SETTINGS) == 2
    after = pq.read_table(tmp_path / "cat.parquet")
    assert after.column_names == before.column_names + list(columns.PIXEL_COLUMNS)
    assert after.select(before.column_names).equals(before)
    assert after.column("sumrat").to_pylist()[0] == pytest.approx(1.0, abs=1e-6)
    assert np.isnan(after.column("nneg").to_pylist()[1])          # x = 1: the stamp leaves the image
