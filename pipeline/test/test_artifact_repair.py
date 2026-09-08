"""Tests for extreme-artifact pixel repair.

Run with:  python -m pytest pipeline/test/test_artifact_repair.py
"""

import os
import sys

import numpy as np
import pytest
from astropy.io import fits

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))

from pipeline.artifactRepairSubs import (find_extreme_artifact_pixels,
                                         repair_extreme_artifact_pixels)


def _frame(tmp_path, name='sci.fits', background=12.0, shape=(128, 128)):
    data = np.full(shape, background, dtype=np.float32)
    path = str(tmp_path / name)
    return path, data


def _write(path, data):
    fits.PrimaryHDU(data.astype(np.float32)).writeto(path, overwrite=True)


def test_positive_spike_is_repaired(tmp_path):
    p, d = _frame(tmp_path)
    d[64, 64] = 70000.0
    _write(p, d)
    assert repair_extreme_artifact_pixels(p, 10000.0) == 1
    out = fits.getdata(p)
    assert out[64, 64] == pytest.approx(12.0)


def test_negative_spike_is_repaired(tmp_path):
    """A one-sided test would miss these; the worst observed artifact was -2.6e6 DN/s."""
    p, d = _frame(tmp_path)
    d[30, 40] = -2.6e6
    _write(p, d)
    assert repair_extreme_artifact_pixels(p, 10000.0) == 1
    assert fits.getdata(p)[30, 40] == pytest.approx(12.0)


def test_adjacent_artifacts_do_not_repair_each_other(tmp_path):
    """A two-pixel cosmic ray, and a dead-pixel cluster like the one seen on jid130200."""
    p, d = _frame(tmp_path)
    d[50, 50] = 40000.0
    d[50, 51] = -30000.0
    for dy, dx in [(0, 0), (1, 0), (1, 4), (3, 1), (7, 0)]:
        d[80 + dy, 80 + dx] = -50000.0
    _write(p, d)
    n = repair_extreme_artifact_pixels(p, 10000.0)
    assert n == 7
    out = fits.getdata(p)
    assert np.all(np.abs(out) < 10000.0)
    assert out[50, 50] == pytest.approx(12.0)
    assert out[80, 80] == pytest.approx(12.0)


def test_a_bright_star_below_threshold_is_untouched(tmp_path):
    """The threshold sits above the stellar peak, so real sources must survive intact."""
    p, d = _frame(tmp_path)
    yy, xx = np.mgrid[-10:11, -10:11]
    star = 6000.0 * np.exp(-(yy ** 2 + xx ** 2) / (2 * 1.3 ** 2))
    d[54:75, 54:75] += star.astype(np.float32)
    before = d.copy()
    _write(p, d)
    assert repair_extreme_artifact_pixels(p, 10000.0) == 0
    assert np.allclose(fits.getdata(p), before)


def test_nothing_to_repair_leaves_the_file_alone(tmp_path):
    p, d = _frame(tmp_path)
    _write(p, d)
    before = fits.getdata(p).copy()
    assert repair_extreme_artifact_pixels(p, 10000.0) == 0
    assert np.array_equal(fits.getdata(p), before)


def test_repair_records_provenance_in_the_header(tmp_path):
    p, d = _frame(tmp_path)
    d[10, 10] = 99999.0
    _write(p, d)
    repair_extreme_artifact_pixels(p, 10000.0)
    hdr = fits.getheader(p)
    assert hdr['NARTRPR'] == 1
    assert hdr['ARTRPRTH'] == pytest.approx(10000.0)


def test_find_returns_row_col_index_tuple(tmp_path):
    """Must match the (rows, cols) convention used by restore_nans."""
    p, d = _frame(tmp_path)
    d[5, 7] = 50000.0
    d[9, 3] = -50000.0
    _write(p, d)
    rows, cols = find_extreme_artifact_pixels(p, 10000.0)
    assert sorted(zip(rows.tolist(), cols.tolist())) == [(5, 7), (9, 3)]


def test_threshold_is_respected(tmp_path):
    p, d = _frame(tmp_path)
    d[20, 20] = 9999.0
    d[21, 21] = 10001.0
    _write(p, d)
    assert repair_extreme_artifact_pixels(p, 10000.0) == 1
    out = fits.getdata(p)
    assert out[20, 20] == pytest.approx(9999.0)
    assert out[21, 21] == pytest.approx(12.0)


def test_non_finite_pixels_are_not_selected(tmp_path):
    p, d = _frame(tmp_path)
    d[40, 40] = np.nan
    d[41, 41] = np.inf
    _write(p, d)
    assert repair_extreme_artifact_pixels(p, 10000.0) == 0


def test_edge_pixel_is_repaired(tmp_path):
    p, d = _frame(tmp_path)
    d[0, 0] = 80000.0
    _write(p, d)
    assert repair_extreme_artifact_pixels(p, 10000.0) == 1
    assert fits.getdata(p)[0, 0] == pytest.approx(12.0)
