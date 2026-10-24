# ported-from: none
"""A RimTimSim ``_lite.fits.gz`` file as the intake's multi-extension FITS file.

RimTimSim (TRExS's Roman time-domain image simulator) delivered one field
of the Galactic bulge, SCA 2, in F087 and F213 (2026-06-22). `dev`'s
converter (``sims/src/rimtimsim/convert_rimtimsim.py``) made the ``lite``
files every `dev` run read, ``rimtimsim_WFI_<filter>_SCA<NN>_<exposure>
_lite.fits.gz``: the image transposed to agree with its WCS (the original
simulated data are incorrect), multiplied by the exposure time into DN at
gain 1, in HDU 1 under a copy of the primary header, CD matrix, zero SIP
coefficients, ``MJD-OBS`` from ``TSTART``, ``EXPTIME``, ``SCA_NUM``,
``BUNIT = "DN"`` and the delivery's per-filter ``ZPTMAG``, ``FILTER`` in the
RAPID spelling (``K213``). The exposure number is GBTDS's
(``gbtds_timestamps_field03.txt``'s ``GBTDS_EXPOSURENO``).

:func:`lite_to_fits` writes the SOC reader's layout from it with `dev`'s
own header builders (:mod:`rapidpipe.science.intake`): PRIMARY with the
pipeline's short keywords and the file's own keywords as ``HIERARCH meta
rimtimsim`` cards, then SCI, the image in DN with the file's WCS, every SIP
coefficient to fifth order present. What the file lacks, the reader says:

- No uncertainty or DQ array (the converter discarded the first; the
  simulation has no second): SCI is the only image HDU, and a run that asks
  for a DQ mask is refused. The file's NaN pixels stay NaN.
- No photometric calibration but the file's own ``ZPTMAG`` (the delivery's,
  25.8573 for F213 and 26.2982 for F087 on all 263 files), which the
  conversion writes; a file without it is refused.
- No Roman observation identifiers: the exposure number is ``OBSERVTN``,
  the others undefined; the image centre (the file's ``CRVAL``, ``CRPIX``
  at the centre) is ``TARGRA``, ``TARGDEC``.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits
from astropy.time import Time, TimeDelta

from rapidpipe.products.filters import RAPID_TO_ROMAN_FILTER_NAMES
from rapidpipe.science.intake import (
    ConversionError, build_image_hdu, build_primary_header)

log = logging.getLogger(__name__)

#: A RimTimSim lite stem: filter (Roman spelling), SCA, the GBTDS exposure number.
STEM = re.compile(r"rimtimsim_WFI_(?P<filter>F\d{3})_SCA(?P<sca>\d{2})_(?P<exposure>\d{9})_lite")

#: The file's suffix under the source root.
SUFFIX = ".fits.gz"

#: The WCS keywords carried from the file's image header into SCI.
WCS_KEYWORDS = re.compile(r"WCSAXES|CRPIX\d|CRVAL\d|CD\d_\d|CTYPE\d|CUNIT\d|LONPOLE|LATPOLE|WCSNAME|"
                          r"MJDREF|RADESYS|EQUINOX|[AB]_ORDER|[AB]_\d_\d")


def parse_stem(stem: str) -> dict[str, int | str]:
    """The identifiers a lite file name carries; ``ValueError`` for another name."""
    match = STEM.fullmatch(stem)
    if match is None:
        raise ValueError(f"{stem!r} is not a RimTimSim lite file stem")
    return {"filter": match["filter"], "sca": int(match["sca"]), "exposure": int(match["exposure"])}


def source_key(stem: str) -> str:
    """The file's path under the dataset's source root."""
    parse_stem(stem)
    return stem + SUFFIX


class _Node(dict):
    """A mapping read as attributes too, as the header builder reads a data model's ``meta``."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def model(header: fits.Header, stem: str) -> _Node:
    """The file's image header as the ``meta`` :func:`build_primary_header` reads."""
    ids = parse_stem(stem)
    roman_filter = RAPID_TO_ROMAN_FILTER_NAMES.get(str(header["FILTER"]).strip(), str(header["FILTER"]).strip())
    if roman_filter != ids["filter"]:
        raise ValueError(f"{stem}: the file's own filter {header['FILTER']} is not its name's")
    if int(header["SCA_NUM"]) != ids["sca"]:
        raise ValueError(f"{stem}: the file's own SCA {header['SCA_NUM']} is not its name's")
    if str(header.get("BUNIT", "")).strip() != "DN":
        raise ConversionError(f"{stem}: BUNIT {header.get('BUNIT')!r}, not DN")
    if "ZPTMAG" not in header:
        raise ConversionError(f"{stem}: no ZPTMAG")
    exptime = float(header["EXPTIME"])
    start = Time(float(header["MJD-OBS"]), format="mjd", scale="utc")
    observation = {name: None for name in ("program", "execution_plan", "pass", "segment",
                                           "visit", "exposure", "visit_id", "observation_id")}
    own = {key.lower().replace("-", "_"): header[key] for key in header
           if key and not WCS_KEYWORDS.fullmatch(key) and key not in ("CHECKSUM", "DATASUM")
           and not key.startswith(("NAXIS", "XTENSION", "BITPIX", "PCOUNT", "GCOUNT"))}
    return _Node(
        instrument=_Node(optical_element=roman_filter, detector=f"WFI{ids['sca']:02d}"),
        exposure=_Node(exposure_time=exptime, start_time=start,
                       end_time=start + TimeDelta(exptime, format="sec")),
        pointing=_Node(target_ra=float(header["CRVAL1"]), target_dec=float(header["CRVAL2"])),
        observation=_Node(observation, observation=ids["exposure"]),
        rimtimsim=_Node(own))


def lite_to_fits(source_path, fits_path, *, distortion=None, sip_degree: int, sip_max_pix_error: float,
                 nominal_zptmag, swname: str, swvers: str, created: str,
                 dq_mask_bits: int = 0) -> dict[str, Any]:
    """Convert one lite file, ``<stem>.fits.gz``, into the intake's FITS file; returns what
    :func:`rapidpipe.science.intake.build_fits_file` returns. ``nominal_zptmag`` is not read: the
    file's own ``ZPTMAG`` is the zero point."""
    if distortion is not None:
        raise ValueError("a RimTimSim file names no distortion reference: [intake] rederive_wcs is"
                         " the SOC reader's")
    if dq_mask_bits:
        raise ConversionError("a RimTimSim file has no DQ array: [intake] dq_mask_bits must be 0")
    stem = Path(source_path).name.removesuffix(SUFFIX)
    with fits.open(source_path, memmap=False) as hdul:
        header, data = hdul[1].header, hdul[1].data
        meta = model(header, stem)
        exptime = meta.exposure.exposure_time
        primary = build_primary_header(_Node(meta=meta), {}, source_path,
                                       {meta.instrument.optical_element: float(header["ZPTMAG"])},
                                       swname, swvers, created)
        wcs_header = fits.Header([card for card in header.cards if WCS_KEYWORDS.fullmatch(card.keyword)])
        rate = np.asarray(data, dtype=np.float32) / np.float32(exptime)  # the conversion returns it to DN
        sci_nan_input = int(np.count_nonzero(np.isnan(rate)))
        sci = build_image_hdu("data", rate, primary, wcs_header, rate.shape, exptime, sip_degree)
    sci.header["ASDFNODE"] = ("data", "the lite file's HDU 1")
    partial = Path(str(fits_path) + ".partial")
    try:
        fits.HDUList([fits.PrimaryHDU(header=primary), sci]).writeto(partial, overwrite=True, checksum=True)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    os.replace(partial, fits_path)
    log.info("wrote %s from %s: PRIMARY,SCI", fits_path, stem)
    return {"hdus": ["PRIMARY", "SCI"], "dq_masked": None, "sci_nan_input": sci_nan_input}
