"""Cutout stamps, ported from `dev`'s ``alerts/providers.py`` onto ``astropy.io.fits``.

`dev`'s ``extract_stamp`` (providers.py L497-565) and ``load_fits_image``
(L465-494), with ``fitsio`` replaced by ``astropy.io.fits`` (the rebuild reads
and writes FITS through astropy only). The geometry is `dev`'s, unchanged:

- the stamp is ``2 * half_width + 1`` pixels square (`dev`: 64, so 129x129),
  centred on the 1-based FITS position ``(xfit + 1, yfit + 1)`` -- the caller
  adds the one, as `dev`'s ``get_cutouts`` does (L1956-1961), because
  ``sources.xfit/yfit`` are 0-based photutils positions;
- pixels beyond the chip edge are :data:`STAMP_FILL_VALUE` (0.0);
- a stamp that would not overlap the chip at all is ``None``;
- the stamp carries the parent's WCS cards (:data:`WCS_CARD_PREFIXES`) with
  CRPIX shifted to the stamp frame;
- pixels are float32.

`dev` round-trips each stamp through a temporary file because cfitsio only
writes to paths; astropy writes to a buffer, so no temporary file is made.
The bytes are a complete FITS file either way.

No database; files only through the path the caller gives.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import numpy as np
from astropy.io import fits

logger = logging.getLogger(__name__)

STAMP_HALF_WIDTH = 64  # stamps are 2*64+1 = 129x129 pixels

# Value for stamp pixels that fall outside the chip (edge clips).
STAMP_FILL_VALUE = 0.0

# Header cards copied from the parent image into each cutout, so every clip
# is a self-describing FITS image with a valid WCS. CRPIX1/2 are shifted by
# the stamp's corner offset; everything else copies unchanged. (`dev`,
# providers.py L456-462.)
WCS_CARD_PREFIXES = (
    "CTYPE", "CUNIT", "CRVAL", "CRPIX", "CDELT", "CD1_", "CD2_",
    "PC1_", "PC2_", "PV1_", "PV2_",
    "A_", "B_", "AP_", "BP_",           # SIP polynomials and their ORDERs
    "RADESYS", "EQUINOX", "LONPOLE", "LATPOLE",
    "MJD-OBS", "BUNIT", "FILTER",
)


def load_fits_image(path: str | Path | None) -> tuple[np.ndarray | None, fits.Header | None]:
    """The pixels and header of the first HDU with image data, as `dev` reads them.

    ``(None, None)`` when the path is None, unreadable, or has no image HDU.
    """
    if path is None:
        return None, None
    try:
        with fits.open(path, memmap=False) as hdus:
            for hdu in hdus:
                if isinstance(hdu, (fits.PrimaryHDU, fits.ImageHDU)) and hdu.data is not None:
                    return np.asarray(hdu.data), hdu.header.copy()
        logger.warning("No image HDU in %s", path)
    except Exception:  # noqa: BLE001 - dev's rule: an unreadable image is a missing one
        logger.warning("Could not load image %s", path, exc_info=True)
    return None, None


def extract_stamp(image_data: np.ndarray | None, x: float | None, y: float | None,
                  header: fits.Header | None = None,
                  half_width: int = STAMP_HALF_WIDTH) -> bytes | None:
    """Cut a square stamp around a 1-based FITS pixel position, as FITS-file bytes."""
    if image_data is None or x is None or y is None:
        return None
    # FITS pixel coordinates are 1-based; numpy indexing is 0-based
    col = int(round(x)) - 1
    row = int(round(y)) - 1
    nrows, ncols = image_data.shape
    top, bottom = row - half_width, row + half_width + 1
    left, right = col - half_width, col + half_width + 1
    # the part of the stamp window that actually lies on the chip
    ontop, onbottom = max(top, 0), min(bottom, nrows)
    onleft, onright = max(left, 0), min(right, ncols)
    if ontop >= onbottom or onleft >= onright:
        return None
    side = 2 * half_width + 1
    stamp = np.full((side, side), STAMP_FILL_VALUE, dtype=np.float32)
    stamp[ontop - top:onbottom - top, onleft - left:onright - left] = \
        image_data[ontop:onbottom, onleft:onright]

    stamp_header = fits.Header()
    if header is not None:
        for card in header.cards:
            name = str(card.keyword)
            if not name.startswith(WCS_CARD_PREFIXES):
                continue
            value = card.value
            # 1-based parent pixel p lands at p - left (p - top) in the clip
            if name == "CRPIX1":
                value = value - left
            elif name == "CRPIX2":
                value = value - top
            stamp_header[name] = (value, card.comment)

    buffer = io.BytesIO()
    fits.PrimaryHDU(data=stamp, header=stamp_header).writeto(buffer)
    return buffer.getvalue()
