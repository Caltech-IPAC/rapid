"""The reference image's header stamp.

`dev`: ``addKeywordsToReferenceImageHeader`` (``pipeline/referenceImageSubs.py``),
called on the mosaic and again on its uncertainty image: ``BUNIT``,
``FIELD``, ``FID``, ``FILTER``, ``COV5PERC``, ``NFRAMES``, ``JDSTART``,
``JDEND``, ``MAGZP``, ``TOTEXPTM`` and one ``INFILnnn`` per input file,
then the HDU rewritten as a float32 ``PrimaryHDU`` with astropy's
``CHECKSUM``/``DATASUM``.

Departures (R4): ``FID`` is not stamped (a database id; `register`
derives it from the filter name); the run model's identifiers are added
with the names the finalize stamp uses -- ``RPRUN``, ``RPATTMPT``,
``RPINST`` (the reference-image instance) and ``RPSTAGE``
(``reference``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits

STAGE_NAME = "reference"


@dataclass(frozen=True)
class StampValues:
    """Everything the stamp records."""

    field: int
    exposure_filter: str
    cov5percent: float
    nframes: int
    refimage_input_filenames: tuple[str, ...]
    jdstart: float
    jdend: float
    zprefimg: float
    total_refimage_exptime: float
    run: str
    attempt: str
    instance: str


def infile_keyword(i: int) -> str:
    """`dev`'s ``INFILnnn`` name for the ``i``-th input (1-based)."""
    zero_padding = ""
    if i < 10:
        zero_padding = "00"
    elif i < 100:
        zero_padding = "0"
    return "INFIL" + zero_padding + str(i)


def stamp_cards(values: StampValues) -> list[tuple[str, Any, str | None]]:
    """The ``(keyword, value, comment)`` cards in `dev`'s order, then the run model's."""
    cards: list[tuple[str, Any, str | None]] = [
        ("BUNIT", "DN/s", None),
        ("FIELD", values.field, "Roman sky-tile number"),
        ("FILTER", values.exposure_filter, None),
        ("COV5PERC", values.cov5percent, None),
        ("NFRAMES", values.nframes, "Total number of images coadded"),
        ("JDSTART", values.jdstart, "Obs. JD of earliest image used [days]"),
        ("JDEND", values.jdend, "Obs. JD of latest image used [days]"),
        ("MAGZP", values.zprefimg, "Zero point of reference image [mag]"),
        ("TOTEXPTM", values.total_refimage_exptime, "Total input-image exposure time [s]"),
    ]
    for i, fn in enumerate(values.refimage_input_filenames, start=1):
        cards.append((infile_keyword(i), fn, None))
    cards += [
        ("RPRUN", values.run, "RAPID run id"),
        ("RPATTMPT", values.attempt, "RAPID reference attempt id"),
        ("RPINST", values.instance, "RAPID reference-image instance id"),
        ("RPSTAGE", STAGE_NAME, "RAPID stage that wrote this file"),
    ]
    return cards


def add_keywords_to_reference_image_header(reference_image_filename: str | Path,
                                           values: StampValues) -> None:
    """`dev` ``addKeywordsToReferenceImageHeader``, in place, with the stamp above."""
    hdu_index = 0
    hdul = fits.open(reference_image_filename)
    hdr = hdul[hdu_index].header
    data = hdul[hdu_index].data
    np_data = np.array(data)
    hdul.close()

    for keyword, value, comment in stamp_cards(values):
        hdr[keyword] = value if comment is None else (value, comment)

    new_hdu = fits.PrimaryHDU(header=hdr, data=np_data.astype(np.float32))
    new_hdu.writeto(reference_image_filename, overwrite=True, checksum=True)
