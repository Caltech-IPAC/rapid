"""Per-frame preparation of the reference inputs, as `dev` does it.

`dev`: the loop over input frames in ``generateReferenceImage``
(``pipeline/referenceImageSubs.py``). Each delivered frame is gunzipped,
its HDU 1 read, the data divided by ``EXPTIME`` (``BUNIT`` DN/s) and
scaled by ``10 ** (0.4 * (zprefimg - ZPTMAG))`` so the coadd sits on the
filter's reference zero point, and written as a PRIMARY-HDU FITS file with
HDU 1's header. The uncertainty image is `dev`'s simple model,
``sqrt(|data_norm| * exptime / gain + rn ** 2) / exptime * scale``,
written with the reformatted file's header. The arithmetic below is
`dev`'s, operation for operation, so the float32 results are the same.

`dev` also accumulates the JD range and the total exposure time; the
caller does that from :class:`PreparedFrame`.

The filter-name map and :func:`get_reference_image_zeropoint` are `dev`'s
(``modules/utils/rapid_pipeline_subs.py``), ported because that module
imports boto3 at module scope.
"""

from __future__ import annotations

import gzip
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from astropy.io import fits

logger = logging.getLogger(__name__)

#: `dev`'s ``roman_to_rapid_filter_names``: Roman designations to the RAPID
#: names FITS ``FILTER`` headers and the ``filters`` table carry.
ROMAN_TO_RAPID_FILTER_NAMES = {
    "F062": "R062",
    "F087": "Z087",
    "F106": "Y106",
    "F129": "J129",
    "F158": "H158",
    "F184": "F184",
    "F213": "K213",
    "F146": "W146",
}
RAPID_TO_ROMAN_FILTER_NAMES = {v: k for k, v in ROMAN_TO_RAPID_FILTER_NAMES.items()}


def filter_spellings(name: str) -> set[str]:
    """Every spelling of ``name`` `dev` treats as the same filter (upper case)."""
    upper = str(name).strip().upper()
    spellings = {upper}
    alternate = ROMAN_TO_RAPID_FILTER_NAMES.get(upper, RAPID_TO_ROMAN_FILTER_NAMES.get(upper))
    if alternate is not None:
        spellings.add(alternate)
    return spellings


def same_filter(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` name one filter in either spelling."""
    return bool(filter_spellings(a) & filter_spellings(b))


def get_reference_image_zeropoint(awaicgen_dict: Mapping[str, Any], filter_name: str | None) -> float:
    """`dev` ``get_reference_image_zeropoint``.

    ``zprefimg_<filter>`` for the frame's filter in either spelling, else the
    scalar ``zprefimg``; :class:`KeyError` when neither is configured.
    """
    if filter_name is not None:
        name = str(filter_name).strip().upper()
        candidates = [name]
        alternate = ROMAN_TO_RAPID_FILTER_NAMES.get(name, RAPID_TO_ROMAN_FILTER_NAMES.get(name))
        if alternate is not None:
            candidates.append(alternate)
        for candidate in candidates:
            key = "zprefimg_" + candidate.lower()
            if key in awaicgen_dict:
                zprefimg = float(awaicgen_dict[key])
                logger.info("get_reference_image_zeropoint: %s = %s", key, zprefimg)
                return zprefimg

    if "zprefimg" not in awaicgen_dict:
        raise KeyError("get_reference_image_zeropoint: neither a per-filter "
                       f"zprefimg_<filter> entry for FILTER = {filter_name} nor the scalar "
                       "zprefimg is configured in the [awaicgen] table")
    zprefimg = float(awaicgen_dict["zprefimg"])
    logger.warning("no zprefimg_<filter> entry for FILTER = %s; falling back to the scalar "
                   "zprefimg = %s", filter_name, zprefimg)
    return zprefimg


def convert_mjd_to_jd(mjd: float) -> float:
    """`dev` ``convert_mjd_to_jd``."""
    jd = mjd + 2400000.5
    return jd


def gunzip(path: Path) -> Path:
    """`dev`'s ``gunzip -f``: ``x.fits.gz`` -> ``x.fits`` beside it; the ``.gz`` removed.

    A name not ending ``.gz`` is returned unchanged.
    """
    if path.suffix != ".gz":
        return path
    target = path.with_suffix("")
    with gzip.open(path, "rb") as src, target.open("wb") as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()
    return target


@dataclass(frozen=True)
class FrameHeader:
    """The HDU 1 keywords `dev` reads from each input frame."""

    exptime: float
    zptmag: float
    mjdobs: float
    filter: str | None


def read_frame_header(path: Path) -> FrameHeader:
    """HDU 1's ``EXPTIME``, ``ZPTMAG``, ``MJD-OBS`` and ``FILTER`` (gzipped or not).

    Raises :class:`KeyError` for a missing keyword and ``OSError``/
    ``IndexError`` for a file that is not a two-HDU FITS file; the stage
    maps these to input rejection.
    """
    with fits.open(path) as hdul:
        hdr = hdul[1].header
        return FrameHeader(
            exptime=float(hdr["EXPTIME"]),
            zptmag=float(hdr["ZPTMAG"]),
            mjdobs=float(hdr["MJD-OBS"]),
            filter=hdr.get("FILTER"),
        )


@dataclass(frozen=True)
class PreparedFrame:
    """One reformatted input: its two files and what `dev` accumulates from it."""

    reformatted: str          # file name in the work directory
    reformatted_unc: str
    exptime: float
    jd: float
    mjdobs: float
    zprefimg: float
    zptmag: float
    flux_scale_factor: float


def reformat_frame(work_dir: Path, input_filename: str, awaicgen_dict: Mapping[str, Any],
                   sca_gain: float, sca_readout_noise: float) -> PreparedFrame:
    """`dev`'s per-frame block: reformat, normalise, scale, and the uncertainty image.

    ``input_filename`` is the delivered ``.fits.gz`` (or ``.fits``) in
    ``work_dir``; it is gunzipped and the unzipped file removed afterwards,
    as `dev` removes it "to save disk space". Output names are `dev`'s:
    ``<name>_reformatted.fits`` and ``<name>_reformatted_unc.fits``.
    """
    refimage_input_filename = input_filename
    fname_input = str(gunzip(work_dir / refimage_input_filename))

    stem = refimage_input_filename
    for suffix in (".fits.gz", ".fits"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    fname_output = stem + "_reformatted.fits"
    fname_output_unc = stem + "_reformatted_unc.fits"

    hdul = fits.open(fname_input)
    hdr = hdul[1].header
    data = hdul[1].data

    exptime = hdr["EXPTIME"]

    hdr["BUNIT"] = "DN/s"

    data_norm = np.array(data) / exptime

    zprefimg = get_reference_image_zeropoint(awaicgen_dict, hdr.get("FILTER"))

    zptmag = hdr["ZPTMAG"]
    flux_scale_factor = 10 ** (0.4 * (zprefimg - zptmag))
    logger.info("For zptmag = %s: flux_scale_factor = %s; ZPREFIMG = %s",
                zptmag, flux_scale_factor, zprefimg)

    data_scaled = data_norm * flux_scale_factor

    mjdobs = float(hdr["MJD-OBS"])
    jd = convert_mjd_to_jd(mjdobs)

    hdu = fits.PrimaryHDU(header=hdr, data=data_scaled)
    hdu_list = [hdu]
    hdu = fits.HDUList(hdu_list)
    hdu.writeto(work_dir / fname_output, overwrite=True, checksum=True)

    hdul.close()

    hdul = fits.open(work_dir / fname_output)
    hdr = hdul[0].header
    data = hdul[0].data

    pos_data = np.abs(data_norm)  # Ensure data are positive
    data_unc = (np.sqrt(pos_data * exptime / sca_gain + sca_readout_noise ** 2) / exptime) \
        * flux_scale_factor

    hdu_unc = fits.PrimaryHDU(header=hdr, data=data_unc)
    hdu_list_unc = [hdu_unc]
    hdu_unc = fits.HDUList(hdu_list_unc)
    hdu_unc.writeto(work_dir / fname_output_unc, overwrite=True, checksum=True)

    hdul.close()

    Path(fname_input).unlink(missing_ok=True)

    return PreparedFrame(
        reformatted=fname_output, reformatted_unc=fname_output_unc,
        exptime=float(exptime), jd=float(jd), mjdobs=mjdobs, zprefimg=float(zprefimg),
        zptmag=float(zptmag), flux_scale_factor=float(flux_scale_factor))


def write_list_files(work_dir: Path, awaicgen_dict: Mapping[str, Any],
                     frames: list[PreparedFrame], max_n_images_to_coadd: int) -> None:
    """`dev`'s two awaicgen list files, one reformatted file name per line."""
    for key, attr in (("awaicgen_input_images_list_file", "reformatted"),
                      ("awaicgen_input_uncert_list_file", "reformatted_unc")):
        with (work_dir / awaicgen_dict[key]).open("w") as f:
            n = 0
            for frame in frames:
                f.write(getattr(frame, attr) + "\n")
                n += 1
                if n >= max_n_images_to_coadd:
                    break
