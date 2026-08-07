"""The reference-image mosaic's geometry — extent, centre and rotation.

`awaicgen` needs four values that the master `.ini` leaves as the literal
`to_be_filled_by_script`: `awaicgen_mosaic_size_x`, `awaicgen_mosaic_size_y`,
`awaicgen_RA_center` and `awaicgen_Dec_center`. The deleted launchers computed
them and substituted them into the `[AWAICGEN]` block before dispatch, so the
W4B migration to release content had nothing to migrate — the keys' values
never existed in a file. Nothing has computed them since, and
`build_awaicgen_command_line_args` raises `KeyError` on the first of them,
after the PSF and all coadd inputs have been downloaded and reformatted.

This module is that computation, ported verbatim rather than re-derived. The
authority is the deleted reference-image launcher at
`e03f22c^:pipeline/awsBatchSubmitJobs_launchSingleReferenceImagePipeline.py`;
the science launcher beside it
(`awsBatchSubmitJobs_launchSingleSciencePipeline.py`) carries the same lines,
and they agree exactly, which is why one function serves both callers here.

The monolith's own split is kept, because it is the placement criterion:

* **Extent** — launcher lines 226-228, under the comment "Update the awaicgen
  dictionary for quantities that do not vary with sky location". Computed once
  at module scope from `[REF_IMAGE]`, the same for every field in a
  submission. Pure release content, so it stays a config read.
* **Centre** — launcher lines 352-355 and 370-382, inside the per-field submit
  loop, from the tessellation. Varies per field, so it is a per-invocation
  manifest fact (`tile_position`), which the vocabulary already declares with
  exactly the `ra0`/`dec0` shape this needs.

Two details of the port are easy to get wrong by re-deriving instead of
reading, and both are deliberate:

1. `pixel_scale` is `fabs(cdelt1_refimage)` and multiplies BOTH axes (launcher
   226-228). `cdelt2_refimage` exists and is read at line 167, but it is not
   used for the extent. The reference image is square (7000x7000) with equal
   magnitudes, so the two agree numerically today — but the ported code is the
   ported code, and an anisotropic pixel would follow the monolith.
2. The centre is the tile centre with NO offset: `ra0_refimage = ra0_field`
   (launcher 370-371), "the reference image is centered on the sky tile with
   zero rotation" (launcher 368). The corner and CRPIX arithmetic around it
   feeds the reference image's own WCS, not awaicgen's `-R`/`-D`.
"""

import math
from typing import Any, Mapping

# The tessellation is closed-form and needs no database connection, so the
# centre can be recomputed anywhere the rtid is known — including in a test.
from database.modules.utils.roman_tessellation_db import (
    RomanTessellationClosedForm)


def mosaic_extent(ref_image: Mapping[str, Any]) -> tuple[float, float]:
    """The mosaic's extent in degrees, from the reference-image geometry.

    Launcher lines 226-228, verbatim::

        pixel_scale = math.fabs(cdelt1_refimage)
        awaicgen_mosaic_size_x = pixel_scale * float(naxis1_refimage)
        awaicgen_mosaic_size_y = pixel_scale * float(naxis2_refimage)

    Parameters
    ----------
    ref_image : mapping
        The release's `[ref_image]` section, carrying `cdelt1_refimage`,
        `naxis1_refimage` and `naxis2_refimage`.

    Returns
    -------
    (float, float)
        `(mosaic_size_x, mosaic_size_y)` in degrees.
    """
    pixel_scale = math.fabs(float(ref_image["cdelt1_refimage"]))
    return (pixel_scale * float(ref_image["naxis1_refimage"]),
            pixel_scale * float(ref_image["naxis2_refimage"]))


def mosaic_center(rtid: int) -> tuple[float, float]:
    """The mosaic's centre in degrees — the tile centre, unoffset.

    Launcher lines 352-355 and 370-371::

        rtid = field
        roman_tessellation_db.get_center_sky_position(rtid)
        ra0_field = roman_tessellation_db.ra0
        dec0_field = roman_tessellation_db.dec0
        ...
        ra0_refimage = ra0_field
        dec0_refimage = dec0_field

    Kept as a function of `rtid` alone so a test can compute the expected
    value from the same closed form the manifest fact was built from, without
    going through gathering.
    """
    tessellation = RomanTessellationClosedForm()
    tessellation.get_center_sky_position(int(rtid))
    return float(tessellation.ra0), float(tessellation.dec0)


def resolve_awaicgen_geometry(awaicgen: dict, ref_image: Mapping[str, Any],
                              tile_position: Mapping[str, float]) -> dict:
    """Fill the four launcher-computed keys into an `[awaicgen]` section.

    The one call the stages make. `awaicgen` is mutated and returned — it is
    already a copy, because `context.science_section` returns one precisely so
    a stage can do this without reaching the next stage.

    `awaicgen_mosaic_rotation` is also overwritten by the launcher (line 232,
    `str(crota2_refimage)`), and unlike the four it HAS a release-content value
    — 0.0, which is what `[ref_image] crota2_refimage` is set to as well. It is
    written here anyway rather than left to the section's own key, so that the
    single place the mosaic's geometry is decided decides all of it, and so
    that changing `crota2_refimage` cannot silently disagree with the coadd.

    Parameters
    ----------
    awaicgen : dict
        The release's `[awaicgen]` section, as handed to the coadd.
    ref_image : mapping
        The release's `[ref_image]` section.
    tile_position : mapping
        The unit's `tile_position` fact: the tessellation tile's centre and
        corners. Only `ra0`/`dec0` are read.

    Returns
    -------
    dict
        The same `awaicgen` mapping, with the five keys set as strings —
        the type `build_awaicgen_command_line_args` floats back out, and the
        type the launcher wrote (`str(...)` at lines 230-232, 381-382).
    """
    size_x, size_y = mosaic_extent(ref_image)
    awaicgen["awaicgen_mosaic_size_x"] = str(size_x)
    awaicgen["awaicgen_mosaic_size_y"] = str(size_y)
    awaicgen["awaicgen_mosaic_rotation"] = str(
        float(ref_image["crota2_refimage"]))
    awaicgen["awaicgen_RA_center"] = str(float(tile_position["ra0"]))
    awaicgen["awaicgen_Dec_center"] = str(float(tile_position["dec0"]))
    return awaicgen
