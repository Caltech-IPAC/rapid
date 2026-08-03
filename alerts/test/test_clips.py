"""Section A of the test plan: cutout clips are made correctly.

The two priority tests here guard the 0-based/1-based indexing bug found
2026-07-14: sources.xfit/yfit are 0-based (photutils convention) while
FITS pixel coordinates are 1-based, and the provider must add +1 before
cutting. The database itself pins the convention: sources.ra/dec equals
the diff image's TPV WCS evaluated at exactly (xfit+1, yfit+1) (verified
at 0.0 mas on real sources -- test_live_db.py keeps watching that).

TODO (test plan, not yet implemented):
  A3  CRPIX rounding convention at xfit = n.49 / n.50 / n.51
  A4  pixel fidelity: byte order and NaN preservation through the clip
  A5  just-fits boundary geometry (center exactly 64 px from each edge)
  A6  full header whitelist (SIP cards, CDELT/PC variants), and clips
      parse in both astropy and fitsio
  E19 temp-file hygiene: no .fits litter after many extract_stamp calls
"""

import io

import fitsio
import numpy as np
from astropy.io import fits

from alerts.providers import (STAMP_FILL_VALUE, Source, extract_stamp,
                                    load_fits_image)
from conftest import CHIP_PID, PRODUCT_OFFSETS, make_source_row
from wcs_eval import separation_mas, tpv_pixel_to_sky


def clip_to_numpy(clip_bytes):
    """Parse cutout bytes (a small FITS file) into (data, header)."""
    with fits.open(io.BytesIO(clip_bytes)) as hdul:
        return hdul[0].data, hdul[0].header


def make_detection(row):
    """DB row dict -> Source, the same way the provider does it."""
    row = dict(row)
    row["band"] = row.pop("filter_name")
    return Source.from_row(row, strict=True)


# ---------------------------------------------------------------------------
# A1: THE indexing regression test.
#
# A detection whose 0-based (xfit, yfit) is a whole number must produce a
# clip centered on exactly that pixel of the parent image. chip_image gives
# every pixel a unique value (row*1000 + col), so the assert is exact: if
# the provider drops the +1 conversion (the historical bug), the center
# lands one pixel off in both axes and the value cannot match.
# ---------------------------------------------------------------------------

def test_clip_centers_on_zero_based_source_position(make_provider,
                                                    chip_data, tpv_header):
    provider = make_provider()
    x0, y0 = 150.0, 200.0                      # 0-based, integral on purpose
    detection = make_detection(
        make_source_row(9001, x0, y0, 60500.5, tpv_header))

    cutouts = provider.get_cutouts(detection)

    diff, _ = clip_to_numpy(cutouts.difference)
    expected_center = (y0 * 1000 + x0                      # chip_image value
                       + PRODUCT_OFFSETS["sfftdiffimage_masked.fits"])
    assert diff[64, 64] == expected_center
    # same grid, so science and template center on the same pixel
    sci, _ = clip_to_numpy(cutouts.science)
    assert sci[64, 64] == (y0 * 1000 + x0
                           + PRODUCT_OFFSETS["bkg_subbed_science_image.fits"])


# ---------------------------------------------------------------------------
# A2: the WCS/position consistency invariant (the stronger, self-documenting
# form of A1).
#
# The detection's ra/dec is defined as WCS(xfit+1, yfit+1) -- the relation
# the real database satisfies. Each clip carries its own WCS; evaluating it
# at the stamp center must give back that ra/dec to within stamp-grid
# rounding (<= 0.5 px per axis). This holds for every clip and would catch
# a wrong CRPIX shift, a dropped +1, or distortion cards going missing.
# ---------------------------------------------------------------------------

def test_clip_wcs_reproduces_catalog_position(make_provider, chip_data,
                                              tpv_header):
    provider = make_provider()
    # fractional position: rounding to the stamp grid is exercised too
    row = make_source_row(9001, 150.3, 200.6, 60500.5, tpv_header)
    detection = make_detection(row)

    cutouts = provider.get_cutouts(detection)

    # 0.71 px diagonal worst-case rounding, in mas on this plate scale
    plate_scale_mas = 0.1061 * 1000
    for clip_bytes in (cutouts.difference, cutouts.science,
                       cutouts.template):
        _, header = clip_to_numpy(clip_bytes)
        ra_c, dec_c = tpv_pixel_to_sky(header, 65.0, 65.0)  # stamp center
        sep = separation_mas(ra_c, dec_c, row["ra"], row["dec"])
        assert sep < 0.75 * plate_scale_mas


# ---------------------------------------------------------------------------
# extract_stamp unit tests (migrated from the old test_rapid_alerts.py
# script, sections 8/8a/8b/8c)
# ---------------------------------------------------------------------------

def test_stamp_roundtrip_and_center(chip_image):
    stamp_bytes = extract_stamp(chip_image, 150.0, 150.0)   # 1-based coords
    stamp, _ = clip_to_numpy(stamp_bytes)
    assert stamp.shape == (129, 129)
    assert stamp[64, 64] == chip_image[149, 149]
    assert extract_stamp(None, 150.0, 150.0) is None
    assert extract_stamp(chip_image, None, 150.0) is None


def test_edge_stamp_padded_with_fill_value(chip_image):
    # a source at 1-based x=5 puts stamp columns 0..59 off-chip
    stamp, _ = clip_to_numpy(extract_stamp(chip_image, 5.0, 150.0))
    assert stamp.shape == (129, 129)
    assert np.all(stamp[:, :60] == STAMP_FILL_VALUE)   # off-chip -> fill
    assert stamp[64, 64] == chip_image[149, 4]         # center is the source
    assert stamp[64, 60] == chip_image[149, 0]         # first on-chip column


def test_stamp_with_no_chip_overlap_is_none(chip_image):
    assert extract_stamp(chip_image, -200.0, 150.0) is None
    assert extract_stamp(chip_image, 150.0, 1000.0) is None


def test_stamp_carries_shifted_wcs_and_whitelisted_cards_only(chip_image):
    parent_header = fitsio.FITSHDR({
        "CTYPE1": "RA---TPV", "CRVAL1": 268.1, "CRPIX1": 100.5,
        "CRPIX2": 200.5, "CD1_1": -2.6e-05, "PV1_5": 1.25e-4,
        "JOBPROCDATE": "2026-07-14",   # pipeline card, must not be copied
    })
    stamp_bytes = extract_stamp(chip_image, 150.0, 150.0,
                                header=parent_header)
    _, clip_header = clip_to_numpy(stamp_bytes)
    assert clip_header["CRPIX1"] == 100.5 - 85    # corner col = 149 - 64
    assert clip_header["CRPIX2"] == 200.5 - 85    # corner row = 149 - 64
    assert clip_header["CTYPE1"] == "RA---TPV"
    assert clip_header["CRVAL1"] == 268.1
    assert clip_header["PV1_5"] == 1.25e-4
    assert "JOBPROCDATE" not in clip_header


def test_load_fits_image_finds_pixels_in_any_hdu(tmp_path, chip_image):
    # Roman L2 cal files keep pixels in an extension behind a header-only
    # primary HDU; the loader must find them there
    path = str(tmp_path / "ext_image.fits")
    with fitsio.FITS(path, "rw") as out:
        out.write(None)                            # header-only primary
        out.write(chip_image, header={"CRPIX1": 100.5})
    pixels, header = load_fits_image(path)
    assert pixels is not None and pixels.shape == chip_image.shape
    assert header["CRPIX1"] == 100.5

    assert load_fits_image(str(tmp_path / "missing.fits")) == (None, None)
    assert load_fits_image(None) == (None, None)
