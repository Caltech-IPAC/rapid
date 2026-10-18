"""
Tests of source_footprint_fields in modules/utils/rapid_pipeline_subs.py, which gives the fields
(sky tiles) that the sources of a science image can be assigned to, for
lookup_source_tables_to_crossmatch_and_distinct_fields and lookup_child_tables_for_field.

Run from the repository root:  python -m pytest modules/utils/tests
"""

import math

import numpy as np

import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.roman_tessellation as tess
from database.modules.utils.overlapping_fields import overlapping_fields


NAXIS = 4088
SCALE = 0.11 / 3600.0


def _pointing(rng):
    ra0 = rng.uniform(0.0, 360.0)
    dec0 = math.degrees(math.asin(rng.uniform(-0.99, 0.99)))
    th = rng.uniform(0.0, 2.0 * math.pi)
    cd = (-SCALE * math.cos(th), SCALE * math.sin(th), SCALE * math.sin(th), SCALE * math.cos(th))
    return ra0, dec0, NAXIS / 2 + 0.5, NAXIS / 2 + 0.5, cd


def _edge_fields(ra0, dec0, crpix1, crpix2, cd, step=8.0):

    # Fields of positions along the outermost loadable positions (0-based -0.5 and
    # NAXIS - 0.5) and 3 pixels inside them, where the sliver fields are.

    e = np.arange(-0.5, NAXIS - 0.5 + 1e-9, step)
    fields = set()
    for d in (-0.5, 2.5):
        for x, y in ((e, np.full_like(e, d)), (e, np.full_like(e, NAXIS - 1 - d)),
                     (np.full_like(e, d), e), (np.full_like(e, NAXIS - 1 - d), e)):
            for xi, yi in zip(x, y):
                ra, dec = util.tan_proj2(xi, yi, crpix1 - 1.0, crpix2 - 1.0, ra0, dec0, *cd)
                fields.add(tess.rtid_of(ra % 360.0, dec))
    return fields


def test_sliver_field_included():

    # A pointing where a source 3 pixels inside the edge falls in field 5261402, which
    # L2Files.overlapfields (image shrunk by 25 pixels) does not list.

    ra0, dec0 = 15.04115293018059, -42.24886985070529
    cd = (-2.749951205908536e-05, -1.331986530790987e-05, -1.331986530790987e-05, 2.749951205908536e-05)
    field = tess.rtid_of(ra0, dec0)
    stored = overlapping_fields(ra0, dec0, 2044.5, 2044.5, *cd, NAXIS, NAXIS, field=field)
    footprint = util.source_footprint_fields(ra0, dec0, 2044.5, 2044.5, *cd, NAXIS, NAXIS, field=field)
    assert 5261402 not in stored
    assert 5261402 in footprint
    assert set(stored) <= set(footprint)


def test_edge_sources_covered_and_neighbor_candidates():

    # For random pointings: every field of an edge position is in the footprint, and every
    # such field or one of its neighbors is in overlapfields (what lookup_child_tables_for_field
    # relies on to find candidate images through the GIN index).

    rng = np.random.default_rng(3)
    for _ in range(25):
        ra0, dec0, crpix1, crpix2, cd = _pointing(rng)
        field = tess.rtid_of(ra0, dec0)
        stored = set(overlapping_fields(ra0, dec0, crpix1, crpix2, *cd, NAXIS, NAXIS, field=field))
        footprint = set(util.source_footprint_fields(ra0, dec0, crpix1, crpix2, *cd, NAXIS, NAXIS, field=field))
        edge = _edge_fields(ra0, dec0, crpix1, crpix2, cd)
        assert edge <= footprint
        for f in edge:
            assert stored & ({f} | set(tess.neighbors_of(f)))


def test_wcs_pointing_key():
    wcs = (15.0, -42.0, 2044.5, 2044.5, -2.7e-05, -1.3e-05, -1.3e-05, 2.7e-05)
    assert util._wcs_pointing_key(wcs) == util._wcs_pointing_key(tuple(v + 1e-9 for v in wcs[:2]) + wcs[2:])
    assert util._wcs_pointing_key(wcs) != util._wcs_pointing_key((15.001,) + wcs[1:])
