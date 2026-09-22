"""Tests for rapidpipe.science.spatial: no database, no I/O.

Each derivation is checked against the legacy formula computed inline
with healpy/math directly, not by importing the legacy script (which
pulls in boto3, database connection modules and S3 access this test
suite must not depend on).
"""

from __future__ import annotations

import math

import healpy as hp
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
