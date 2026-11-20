"""``rapidpipe.science.alerts.calibration``: a flux in nJy from a zero point and a pixel area map.

Contracts: 1 DN/s at zero point 31.4 is 1 nJy and each 2.5 mag brighter a
factor of 10; the map's value is interpolated bilinearly, ``x`` along the
second array axis; a position in the outer half pixel takes the edge's
value and one beyond it, or not finite, gives none; a null factor
calibrates by the zero point alone and a null zero point gives none; a
CRDS-shaped file's map is read as it is stored.
"""

from __future__ import annotations

import math

import asdf
import numpy as np
import pytest

from rapidpipe.science.alerts import calibration

#: A tilted plane, steeper along x than y: map[y, x] = 1 + 1e-3 x + 1e-5 y.
NY, NX = 6, 8
PLANE = 1.0 + 1e-3 * np.arange(NX)[None, :] + 1e-5 * np.arange(NY)[:, None]


def test_the_scale_is_one_at_the_njy_zero_point_and_ten_per_two_and_a_half_magnitudes():
    assert calibration.njy_per_dn_s(31.4) == pytest.approx(1.0)
    assert calibration.njy_per_dn_s(28.9) == pytest.approx(10.0)
    assert calibration.njy_per_dn_s(26.4) == pytest.approx(100.0)
    # ZPTMAG = -2.5 log10(Jy per DN/s / 3631): 25.801 is 1.736e-7 Jy, 173.6 nJy, per DN/s
    assert calibration.njy_per_dn_s(25.801) == pytest.approx(3631e9 * 10 ** (-0.4 * 25.801), rel=2e-4)


@pytest.mark.parametrize("zptmag", [None, math.nan, math.inf])
def test_no_zero_point_gives_no_scale(zptmag):
    assert calibration.njy_per_dn_s(zptmag) is None


@pytest.mark.parametrize("x, y", [(0.0, 0.0), (3.0, 2.0), (3.5, 2.0), (3.0, 2.25), (6.4, 4.7), (7.0, 5.0)])
def test_the_factor_is_bilinear_with_x_along_the_second_axis(x, y):
    assert calibration.interpolate_area_map(PLANE, x, y) == pytest.approx(1.0 + 1e-3 * x + 1e-5 * y, abs=1e-12)


def test_a_position_in_the_outer_half_pixel_takes_the_edges_value():
    assert calibration.interpolate_area_map(PLANE, -0.5, 2.0) == pytest.approx(PLANE[2, 0])
    assert calibration.interpolate_area_map(PLANE, NX - 0.5, NY - 0.5) == pytest.approx(PLANE[NY - 1, NX - 1])


@pytest.mark.parametrize("x, y", [(-0.51, 2.0), (NX - 0.49, 2.0), (3.0, -1.0), (3.0, NY), (math.nan, 2.0),
                                  (3.0, math.inf)])
def test_a_position_off_the_detector_or_not_finite_gives_no_factor(x, y):
    assert calibration.interpolate_area_map(PLANE, x, y) is None


def test_a_factor_that_is_not_finite_gives_none():
    holed = PLANE.copy()
    holed[2, 3] = np.nan
    assert calibration.interpolate_area_map(holed, 3.2, 2.1) is None
    assert calibration.interpolate_area_map(holed, 6.0, 4.0) is not None


def test_area_factors_loads_each_distinct_map_once_and_gives_each_position_its_own_maps_value():
    maps, loaded = {"a.asdf": PLANE, "b.asdf": PLANE + 0.5}, []

    def load(name):
        loaded.append(name)
        return maps[name]

    positions = {"one": ("a.asdf", 3.0, 2.0), "two": ("b.asdf", 3.0, 2.0), "three": ("a.asdf", 6.5, 1.0)}
    factors = calibration.area_factors(positions, load)
    assert loaded == ["a.asdf", "b.asdf"]
    assert factors == pytest.approx({"one": PLANE[2, 3], "two": PLANE[2, 3] + 0.5,
                                     "three": 1.0 + 1e-3 * 6.5 + 1e-5})


def test_area_factors_leaves_out_no_name_a_map_that_cannot_be_loaded_and_a_position_off_the_detector():
    positions = {"no name": (None, 3.0, 2.0), "empty name": ("", 3.0, 2.0), "missing": ("gone.asdf", 3.0, 2.0),
                 "off": ("a.asdf", -4.0, 2.0), "kept": ("a.asdf", 3.0, 2.0)}
    factors = calibration.area_factors(positions, lambda name: PLANE if name == "a.asdf" else None)
    assert factors == pytest.approx({"kept": PLANE[2, 3]})


def test_calibrate_multiplies_by_the_scale_and_the_factor_and_reads_no_factor_as_one():
    assert calibration.calibrate(2.0, 100.0, 1.01) == pytest.approx(202.0)
    assert calibration.calibrate(2.0, 100.0, None) == pytest.approx(200.0)
    assert calibration.calibrate(-2.0, 100.0, 0.99) == pytest.approx(-198.0)


@pytest.mark.parametrize("flux, scale", [(None, 100.0), (2.0, None)])
def test_calibrate_gives_none_without_a_flux_or_a_scale(flux, scale):
    assert calibration.calibrate(flux, scale, 1.0) is None


def test_a_crds_shaped_files_map_is_read_as_stored(tmp_path):
    path = tmp_path / "roman_wfi_area_0085.asdf"
    asdf.AsdfFile({"roman": {"meta": {"reftype": "AREA"}, "data": PLANE.astype(np.float32)}}).write_to(path)
    area_map = calibration.load_area_map(path)
    assert area_map.shape == (NY, NX) and area_map.dtype == np.float64
    assert area_map[1, 5] == pytest.approx(PLANE[1, 5], rel=1e-6)
