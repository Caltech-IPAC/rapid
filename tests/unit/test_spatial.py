"""Tests for rapidpipe.science.spatial: no database, no I/O.

Each derivation is checked against the legacy formula computed inline
with healpy/math directly, not by importing the legacy script (which
pulls in boto3, database connection modules and S3 access this test
suite must not depend on).
"""

from __future__ import annotations

import math

import healpy as hp
import numpy as np
import pytest

from database.modules.utils.roman_tessellation_db import RomanTessellationClosedForm
from rapidpipe.science import spatial

_POSITIONS = [
    (269.45, -28.77),
    (10.0, 45.0),
    (350.0, -60.0),
]


@pytest.mark.parametrize("ra,dec", _POSITIONS)
def test_healpix_indexes_match_legacy_formula(ra, dec):
    expected_hp6 = int(hp.ang2pix(64, ra, dec, nest=True, lonlat=True))
    expected_hp9 = int(hp.ang2pix(512, ra, dec, nest=True, lonlat=True))
    hp6, hp9 = spatial.healpix_indexes(ra, dec)
    assert hp6 == expected_hp6
    assert hp9 == expected_hp9


@pytest.mark.parametrize("ra,dec", _POSITIONS)
def test_unit_vector_matches_legacy_formula(ra, dec):
    alpha = math.radians(ra)
    delta = math.radians(dec)
    expected = (
        math.cos(delta) * math.cos(alpha),
        math.cos(delta) * math.sin(alpha),
        math.sin(delta),
    )
    actual = spatial.unit_vector(ra, dec)
    for e, a in zip(expected, actual):
        assert a == pytest.approx(e, abs=1e-12)


@pytest.mark.parametrize("ra,dec", _POSITIONS)
def test_unit_vector_is_unit_length(ra, dec):
    x, y, z = spatial.unit_vector(ra, dec)
    assert x * x + y * y + z * z == pytest.approx(1.0, abs=1e-9)


def test_tessellation_field_matches_closed_form():
    ra, dec = _POSITIONS[0]
    expected = RomanTessellationClosedForm().get_rtid(ra, dec)
    assert spatial.tessellation_field(ra, dec) == expected


# ======================================================================
# radec_index / index_to_radec: legacy AstroObjects id packing
# (modules/utils/rapid_pipeline_subs.py:2799-2808).
# ======================================================================

#: 1/3300 arcsecond, i.e. one packing unit on either axis -- the coarsest
#: precision radec_index/index_to_radec can preserve through a round trip.
_RADEC_INDEX_UNIT_DEG = 1.0 / (3300 * 3600)


def test_radec_index_known_value():
    # Computed by hand from the packed-integer formula itself (not via
    # spatial.radec_index): ra_units = round(10.0 * 11_880_000) =
    # 118_800_000; dec_units = round((45.0 + 90.0) * 11_880_000) =
    # 1_603_800_000; index = 118_800_000 * 2_138_400_001 + 1_603_800_000.
    assert spatial.radec_index(10.0, 45.0) == 254041921722600000


@pytest.mark.parametrize("ra,dec", [
    (269.45, -28.77),
    (10.0, 45.0),
    (350.0, -60.0),
    (0.0, 0.0),
    (0.0001, 0.0),
    (359.999, 0.0),
    (0.0, 90.0),
    (0.0, -90.0),
    (180.0, 89.999),
    (180.0, -89.999),
])
def test_radec_index_round_trips_through_index_to_radec(ra, dec):
    idx = spatial.radec_index(ra, dec)
    ra_back, dec_back = spatial.index_to_radec(idx)
    assert float(ra_back) == pytest.approx(ra, abs=_RADEC_INDEX_UNIT_DEG)
    assert float(dec_back) == pytest.approx(dec, abs=_RADEC_INDEX_UNIT_DEG)


def test_radec_index_accepts_array_input():
    ra = np.array([269.45, 10.0, 350.0])
    dec = np.array([-28.77, 45.0, -60.0])
    idx = spatial.radec_index(ra, dec)
    assert idx.dtype == np.int64
    for i, (r, d) in enumerate(zip(ra, dec)):
        assert idx[i] == spatial.radec_index(float(r), float(d))

    ra_back, dec_back = spatial.index_to_radec(idx)
    for i, (r, d) in enumerate(zip(ra, dec)):
        assert float(ra_back[i]) == pytest.approx(r, abs=_RADEC_INDEX_UNIT_DEG)
        assert float(dec_back[i]) == pytest.approx(d, abs=_RADEC_INDEX_UNIT_DEG)


# ======================================================================
# compute_angular_separation (modules/utils/rapid_pipeline_subs.py:766-801)
# ======================================================================

def test_compute_angular_separation_zero_for_identical_position():
    ra, dec = _POSITIONS[0]
    assert spatial.compute_angular_separation(ra, dec, ra, dec) == pytest.approx(0.0, abs=1e-9)


def test_compute_angular_separation_matches_legacy_formula():
    rtd = 180.0 / math.pi

    def legacy_xyz(ra, dec):
        alpha, delta = math.radians(ra), math.radians(dec)
        return (math.cos(delta) * math.cos(alpha),
                math.cos(delta) * math.sin(alpha),
                math.sin(delta))

    ara, adec = 10.0, 45.0
    bra, bdec = 12.0, 44.0
    ax, ay, az = legacy_xyz(ara, adec)
    bx, by, bz = legacy_xyz(bra, bdec)
    dx, dy, dz = bx - ax, by - ay, bz - az
    expected = rtd * 2.0 * math.asin(0.5 * math.sqrt(dx * dx + dy * dy + dz * dz))

    actual = spatial.compute_angular_separation(ara, adec, bra, bdec)
    assert actual == pytest.approx(expected, abs=1e-12)


def test_compute_angular_separation_ninety_degrees_apart():
    # Equator/equator, 90 degrees of RA apart: an exact quarter of a great
    # circle, independent of the chord-angle formula's own correctness.
    assert spatial.compute_angular_separation(0.0, 0.0, 90.0, 0.0) == pytest.approx(90.0, abs=1e-9)


# ======================================================================
# field_neighbours / field_center / field_corners: thin wrappers over
# RomanTessellationClosedForm, crossmatch stage 2's tile geometry.
# ======================================================================

_SAMPLE_RTID = RomanTessellationClosedForm().get_rtid(*_POSITIONS[0])


def test_field_neighbours_excludes_self():
    neighbours = spatial.field_neighbours(_SAMPLE_RTID)
    assert all(isinstance(n, int) for n in neighbours)
    assert _SAMPLE_RTID not in neighbours


def test_field_neighbours_matches_closed_form():
    expected = [int(n) for n in
                RomanTessellationClosedForm().get_all_neighboring_rtids(_SAMPLE_RTID)]
    assert spatial.field_neighbours(_SAMPLE_RTID) == expected


def test_field_neighbours_is_symmetric():
    neighbours = spatial.field_neighbours(_SAMPLE_RTID)
    for neighbour in neighbours:
        assert _SAMPLE_RTID in spatial.field_neighbours(neighbour)


def test_field_center_in_range_and_matches_closed_form():
    ra, dec = spatial.field_center(_SAMPLE_RTID)
    assert 0.0 <= ra < 360.0
    assert -90.0 <= dec <= 90.0

    expected = RomanTessellationClosedForm()
    expected.get_center_sky_position(_SAMPLE_RTID)
    assert ra == pytest.approx(expected.ra0)
    assert dec == pytest.approx(expected.dec0)


def test_field_center_round_trips_through_tessellation_field():
    ra, dec = spatial.field_center(_SAMPLE_RTID)
    assert spatial.tessellation_field(ra, dec) == _SAMPLE_RTID


def test_field_corners_in_range_and_matches_closed_form():
    corners = spatial.field_corners(_SAMPLE_RTID)
    assert len(corners) == 4
    for ra, dec in corners:
        assert 0.0 <= ra <= 360.0
        assert -90.0 <= dec <= 90.0

    expected = RomanTessellationClosedForm()
    expected.get_corner_sky_positions(_SAMPLE_RTID)
    assert corners == [
        (expected.ra1, expected.dec1),
        (expected.ra2, expected.dec2),
        (expected.ra3, expected.dec3),
        (expected.ra4, expected.dec4),
    ]


# ======================================================================
# overlapping_fields: ported vs. legacy, on one fixed WCS.
# ======================================================================
#
# database.modules.utils.overlapping_fields imports
# modules.utils.rapid_pipeline_subs, which imports boto3 and scipy at
# module scope -- neither is a dependency of this package (requirements.txt)
# and CI's plain unit-test job does not install them, so this test skips
# cleanly there rather than failing; it runs wherever both happen to be
# installed (e.g. a developer's fuller environment) to guard the two
# copies of the geometry never drifting apart. The importorskip calls are
# inside the test function, not at module scope, so a missing boto3/scipy
# skips only this one test rather than the whole module (an
# importorskip at module scope aborts collection of every test below it).


def test_overlapping_fields_port_agrees_with_legacy():
    pytest.importorskip("boto3")
    pytest.importorskip("scipy")
    from database.modules.utils.overlapping_fields import (
        overlapping_fields as legacy_overlapping_fields,
    )

    crval1, crval2 = 269.45, -28.77
    crpix1, crpix2 = 2044.0, 2044.0
    cd11, cd12 = -3.0556e-5, 0.0
    cd21, cd22 = 0.0, 3.0556e-5
    naxis1, naxis2 = 4088, 4088

    expected = legacy_overlapping_fields(
        crval1, crval2, crpix1, crpix2, cd11, cd12, cd21, cd22, naxis1, naxis2)
    actual = spatial.overlapping_fields(
        crval1, crval2, crpix1, crpix2, cd11, cd12, cd21, cd22, naxis1, naxis2)
    assert actual == expected
