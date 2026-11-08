"""The closed-form Roman sky tessellation: its constants and the access class's contract.

Ported from the smdc branch's ``database/modules/utils/test/test_roman_tessellation.py``
(commit a2ba8ec9; the branch is retired) as pytest. Two of its classes are not
carried: the byte-parity check against rapid_systems' copy of the generator
(rapid_systems deleted that copy on 2026-10-13, 532d2f80, so this repository's
is the one source) and the equivalence battery against the legacy 1.4 GiB
SQLite file, which nothing bakes any more; the recorded certification of
2026-08-06 is its evidence.

Contracts: the NSIDE=512 shape (2,047 rings, 2,049 dec bins, 6,291,458 rows,
the ring counts summing to the table), the two pole-patch values of the 2024
conversion note and its worked example; the access class sets ``rtid`` as the
SQLite class did, its array form equals its scalar form, centres and corners
come out in the SQLite class's order, ``check_version`` accepts the pinned
release and refuses another tessellation, neighbours are complete at the poles
and symmetric on a sample, a dec bin is a whole ring ordered by ``ramin``, an
overlap box finds its own tile, and no pole tile is degenerate.
"""

from __future__ import annotations

import numpy as np
import pytest

from database.modules.utils import roman_tessellation as tess
from database.modules.utils import roman_tessellation_db as access

WORKED_RTID = 5321355  # the 2024 note works (11.1, -43.8) through by hand


@pytest.fixture
def t():
    return access.RomanTessellationClosedForm()


def test_the_production_shape_without_generating_a_row():
    assert (tess.NSIDE, tess.NRINGS, tess.NDECBINS, tess.NROWS) == (512, 2047, 2049, 6291458)


def test_the_ring_counts_sum_to_the_table_size():
    assert sum(tess.nrabins(i) for i in range(1, tess.NRINGS + 1)) + 2 == tess.NROWS


def test_the_pole_patch_values_match_the_2024_note():
    assert (tess.CAP_BOUNDARY, tess.POLE_CDEC) == (89.954315, 89.9771575)


def test_the_2024_worked_example():
    assert tess.rtid_of(11.1, -43.8) == WORKED_RTID


def test_get_rtid_returns_and_sets_the_attribute_as_the_sqlite_class_did(t):
    assert t.get_rtid(11.1, -43.8) == WORKED_RTID
    assert t.rtid == WORKED_RTID


def test_get_rtid_array_matches_the_scalar_form(t):
    rng = np.random.default_rng(20260806)
    ra = rng.uniform(0.0, 360.0, 5000)
    dec = np.degrees(np.arcsin(rng.uniform(-1.0, 1.0, 5000)))
    np.testing.assert_array_equal(t.get_rtid_array(ra, dec),
                                  np.array([t.get_rtid(a, d) for a, d in zip(ra, dec)]))


def test_centre_and_corner_attributes_in_the_sqlite_classs_order(t):
    t.get_center_sky_position(WORKED_RTID)
    assert t.ra0 == pytest.approx(11.067073, abs=5e-6)
    assert t.dec0 == pytest.approx(-43.80449, abs=5e-6)
    t.get_corner_sky_positions(WORKED_RTID)
    assert t.ramin < t.ramax and t.decmin < t.decmax
    assert (t.ra1, t.dec1) == (t.ramin, t.decmin)
    assert (t.ra3, t.dec3) == (t.ramax, t.decmax)


def test_close_is_a_no_op(t):
    t.close()
    assert t.exit_code == 0


def test_check_version_accepts_the_pinned_release_values(t):
    assert t.check_version(version="nside512-v2", digest="0" * 64, nside=512, nrows=6291458)
    assert t.exit_code == 0


def test_check_version_refuses_a_different_tessellation(t):
    assert not t.check_version(nside=256, nrows=6291458)
    assert t.exit_code == 70


def test_neighbour_counts_at_the_poles(t):
    assert len(t.get_all_neighboring_rtids(1)) == tess.nrabins(1)
    assert len(t.get_all_neighboring_rtids(tess.NROWS)) == tess.nrabins(tess.NRINGS)


def test_neighbours_are_symmetric_on_a_sample(t):
    rng = np.random.default_rng(11)
    for r in (int(r) for r in rng.integers(2, tess.NROWS, 300)):
        for other in t.get_all_neighboring_rtids(r):
            assert r in t.get_all_neighboring_rtids(other), f"{r} -> {other} not symmetric"


def test_a_dec_bin_is_a_whole_ring_ordered_by_ramin(t):
    t.get_corner_sky_positions(WORKED_RTID)
    rows = t.get_sky_tiles_in_dec_bin(t.decmin, t.decmax)
    assert len(rows) == 3936  # the 2024 note's nrabins at this ring
    assert [r[1] for r in rows] == sorted(r[1] for r in rows)
    assert WORKED_RTID in [r[0] for r in rows]


def test_overlapping_rtids_covers_a_tiles_own_box(t):
    t.get_center_sky_position(WORKED_RTID)
    t.get_corner_sky_positions(WORKED_RTID)
    rows = t.get_overlapping_rtids(t.ra0, t.dec0, t.ra1, t.dec1, t.ra2, t.dec2,
                                   t.ra3, t.dec3, t.ra4, t.dec4)
    assert WORKED_RTID in [r[0] for r in rows]
    assert t.exit_code == 0


@pytest.mark.parametrize("rtid", [1, tess.NROWS])
def test_pole_tiles_are_not_degenerate(rtid):
    # The SQLite class special-cased the south pole for a ramin == ramax box;
    # the canonical encoding has no such box.
    ramin, ramax, decmin, decmax = tess.corners_of(rtid)
    assert ramin != ramax
