"""The ported awaicgen mosaic geometry, against the history's own formula.

`pipeline/mosaic_geometry.py` is an extraction, not a new derivation, so what
these tests assert is fidelity to the deleted launcher rather than plausibility
of the numbers. The launcher is
`e03f22c^:pipeline/awsBatchSubmitJobs_launchSingleReferenceImagePipeline.py`
(the science launcher beside it carries the identical lines at 213-217 and
508-509).

The expected values are recomputed here from the launcher's arithmetic written
out longhand — deliberately not by calling the module under test, and
deliberately not as hardcoded constants, so that a change to the ported code
that alters the derivation fails even where it happens to preserve today's
7000x7000 square numbers.

Field coverage is three real g0001 fields plus both poles. The pole tiles are
the case the closed form treats specially (`center_of` returns early for
`rtid == 1` and `rtid == NROWS` rather than going through the ring
arithmetic), so a port that quietly assumed the general branch would pass on
mid-latitude fields alone.
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from database.modules.utils import roman_tessellation as tess  # noqa: E402
from database.modules.utils.roman_tessellation_db import (  # noqa: E402
    RomanTessellationClosedForm)
from pipeline.mosaic_geometry import (  # noqa: E402
    mosaic_center, mosaic_extent, resolve_awaicgen_geometry)

# `[ref_image]` as the release declares it (cdf/science/pipeline.toml:132-139),
# which is also `[REF_IMAGE]` in the master .ini the launcher read.
REF_IMAGE = {
    "naxis1_refimage": 7000,
    "naxis2_refimage": 7000,
    "cdelt1_refimage": -0.000030555555556,
    "cdelt2_refimage": 0.000030555555556,
    "crota2_refimage": 0.0,
}

# Three g0001 fields from the readiness window's 109, plus the two poles.
# 511 is the tile the vocabulary's own fixture uses; 2 and 6291457 are the
# tiles adjacent to the north and south pole tiles respectively.
POLE_NORTH = 1
POLE_SOUTH = tess.NROWS
FIELDS = [511, 3145729, 4096, POLE_NORTH, POLE_SOUTH, 2, tess.NROWS - 1]


def launcher_extent(ref_image):
    """The launcher's lines 226-228, written out longhand.

        pixel_scale = math.fabs(cdelt1_refimage)
        awaicgen_mosaic_size_x = pixel_scale * float(naxis1_refimage)
        awaicgen_mosaic_size_y = pixel_scale * float(naxis2_refimage)

    Note `cdelt1` on both axes — that is what the launcher did, and the
    reason this helper does not reach for `cdelt2_refimage` on y.
    """
    pixel_scale = math.fabs(float(ref_image["cdelt1_refimage"]))
    return (pixel_scale * float(ref_image["naxis1_refimage"]),
            pixel_scale * float(ref_image["naxis2_refimage"]))


def launcher_center(rtid):
    """The launcher's lines 352-355 and 370-371, written out longhand.

        rtid = field
        roman_tessellation_db.get_center_sky_position(rtid)
        ra0_field  = roman_tessellation_db.ra0
        dec0_field = roman_tessellation_db.dec0
        ra0_refimage, dec0_refimage = ra0_field, dec0_field

    Driven through the tessellation's own `center_of` rather than through
    the access class, so this is an independent path to the same value.
    """
    return tess.center_of(int(rtid))


# --- extent ------------------------------------------------------------

def test_the_extent_is_the_pixel_scale_times_the_axis_length():
    assert mosaic_extent(REF_IMAGE) == launcher_extent(REF_IMAGE)


def test_the_extent_is_in_degrees_and_covers_the_reference_image():
    size_x, size_y = mosaic_extent(REF_IMAGE)
    # 7000 pixels at 0.11"/pix is ~0.214 deg; the assertion is the identity,
    # not the constant, but a sanity band catches a units slip.
    assert 0.2 < size_x < 0.25
    assert size_x == size_y


def test_both_axes_take_cdelt1_as_the_launcher_did():
    """cdelt2 is read by the launcher (line 167) and not used for extent.

    With an anisotropic pixel the two differ, and the ported code must
    follow the launcher rather than the more obvious cdelt2-on-y.
    """
    anisotropic = dict(REF_IMAGE, naxis2_refimage=3500,
                       cdelt2_refimage=0.000061111111112)
    size_x, size_y = mosaic_extent(anisotropic)
    pixel_scale = math.fabs(float(anisotropic["cdelt1_refimage"]))
    assert size_y == pixel_scale * 3500.0
    assert size_y != float(anisotropic["cdelt2_refimage"]) * 3500.0
    assert size_x == pixel_scale * 7000.0


# --- centre ------------------------------------------------------------

@pytest.mark.parametrize("rtid", FIELDS)
def test_the_centre_is_the_tile_centre_for_each_field(rtid):
    assert mosaic_center(rtid) == launcher_center(rtid)


@pytest.mark.parametrize("rtid", FIELDS)
def test_the_centre_matches_the_access_class_the_launcher_called(rtid):
    """The launcher went through `roman_tessellation_db`, not the kernel."""
    tessellation = RomanTessellationClosedForm()
    tessellation.get_center_sky_position(int(rtid))
    assert mosaic_center(rtid) == (tessellation.ra0, tessellation.dec0)


def test_the_pole_tiles_are_the_special_branch_and_still_agree():
    """Both poles resolve, and to the pole declination rather than a ring."""
    _, dec_north = mosaic_center(POLE_NORTH)
    _, dec_south = mosaic_center(POLE_SOUTH)
    assert dec_north == pytest.approx(tess.POLE_CDEC)
    assert dec_south == pytest.approx(-tess.POLE_CDEC)
    assert dec_north > 89.0 and dec_south < -89.0


def test_the_centre_is_unoffset_from_the_tile():
    """"the reference image is centered on the sky tile" (launcher line 368).

    A port that applied a half-mosaic offset — an easy thing to invent when
    re-deriving from the CRPIX lines just below — moves every reference image
    by ~0.1 deg. This pins the absence of that offset.
    """
    for rtid in FIELDS:
        ra, dec = mosaic_center(rtid)
        tile_ra, tile_dec = tess.center_of(int(rtid))
        assert (ra, dec) == (tile_ra, tile_dec)


# --- the section the coadd is handed ------------------------------------

def tile_position(rtid):
    ra0, dec0 = tess.center_of(int(rtid))
    return {"ra0": ra0, "dec0": dec0}


@pytest.mark.parametrize("rtid", FIELDS)
def test_the_four_keys_are_filled_for_each_field(rtid):
    awaicgen = {}
    resolved = resolve_awaicgen_geometry(awaicgen, REF_IMAGE,
                                         tile_position(rtid))
    expected_x, expected_y = launcher_extent(REF_IMAGE)
    expected_ra, expected_dec = launcher_center(rtid)
    assert float(resolved["awaicgen_mosaic_size_x"]) == expected_x
    assert float(resolved["awaicgen_mosaic_size_y"]) == expected_y
    assert float(resolved["awaicgen_RA_center"]) == expected_ra
    assert float(resolved["awaicgen_Dec_center"]) == expected_dec


def test_the_values_are_strings_as_the_launcher_wrote_them():
    """`str(...)` at launcher lines 230-232 and 381-382.

    `build_awaicgen_command_line_args` floats them back out, so a float here
    would work — but the section is also what the fidelity test walks, and
    the launcher's type is the ported type.
    """
    resolved = resolve_awaicgen_geometry({}, REF_IMAGE, tile_position(511))
    for key in ("awaicgen_mosaic_size_x", "awaicgen_mosaic_size_y",
                "awaicgen_RA_center", "awaicgen_Dec_center",
                "awaicgen_mosaic_rotation"):
        assert isinstance(resolved[key], str)


def test_the_rotation_comes_from_crota2():
    """Launcher line 232: `str(crota2_refimage)`, not the section's own key."""
    resolved = resolve_awaicgen_geometry(
        {"awaicgen_mosaic_rotation": "99.0"},
        dict(REF_IMAGE, crota2_refimage=12.5),
        tile_position(511))
    assert float(resolved["awaicgen_mosaic_rotation"]) == 12.5


def test_it_fills_in_place_and_leaves_the_rest_of_the_section_alone():
    awaicgen = {"zprefimg": "17.0", "awaicgen_num_threads": "2"}
    resolved = resolve_awaicgen_geometry(awaicgen, REF_IMAGE,
                                         tile_position(511))
    assert resolved is awaicgen
    assert resolved["zprefimg"] == "17.0"
    assert resolved["awaicgen_num_threads"] == "2"


# --- the whole key set the coadd requires -------------------------------

def test_every_key_build_awaicgen_command_line_args_reads_is_available():
    """The completeness check that would have caught all five drops at once.

    The four geometry keys come from this module; the rest must be in release
    content. `awaicgen_num_threads` was the fifth casualty of the same W4B
    migration and is asserted here rather than found by a fifth ramp attempt.
    """
    import re
    import tomllib

    root = Path(__file__).resolve().parents[2]
    source = (root / "modules" / "utils"
              / "rapid_pipeline_subs.py").read_text()
    body = source.split("def build_awaicgen_command_line_args", 1)[1]
    body = body.split("\ndef ", 1)[0]
    required = set(re.findall(r'awaicgen_dict\["([^"]+)"\]', body))
    # A guard on the extraction itself: if the regex or the function-body
    # split stopped matching, `required` would be empty and the real
    # assertion below would pass vacuously. 15 distinct keys today.
    assert len(required) == 15

    with open(root / "cdf" / "science" / "pipeline.toml", "rb") as handle:
        section = tomllib.load(handle)["awaicgen"]

    supplied = resolve_awaicgen_geometry(dict(section), REF_IMAGE,
                                         tile_position(511))
    missing = sorted(key for key in required if key not in supplied)
    assert missing == []
