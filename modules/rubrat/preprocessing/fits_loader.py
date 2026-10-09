"""
fits_loader.py — Roman WFI FITS Ingestion Module
=================================================
Load Roman WFI FITS image files (RAPID pipeline products and
OpenUniverse2024/IRSA) into the same standardized dict as load_asdf().

Supported file types
--------------------
  RAPID pipeline products — single SCI extension, (4089, 4089) float32
  RAPID RTS products      — single PRIMARY with 2D image (no named SCI ext)
  Raw TDS input images    — PRIMARY (empty) + SCI, (4088, 4088) float32
  OpenUniverse2024/IRSA   — PRIMARY + SCI (float64) + ERR + DQ, (4088, 4088)
  Difference PSF          — DET_DIST extension, (61, 61) float32
  WFI instrumental PSF    — DET_DIST extension, (61, 61) float64

gzipped FITS (.fits.gz) is handled transparently by astropy.io.fits.

Shape contract
--------------
  RAPID pipeline products: (4089, 4089)
  Raw input / IRSA:        (4088, 4088)
  PSF files:               (61, 61)
  Shape normalisation across formats is handled by the unified
  interface (Phase 6).  Callers must not assume a fixed shape.

Usage
-----
    from ingestion.fits_loader import load_fits, load_fits_meta, validate_fits_output

    result = load_fits('/path/to/bkg_subbed_science_image.fits')
    meta   = load_fits_meta('/path/to/file.fits')
    errors = validate_fits_output(result)
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

import numpy as np
from astropy.io import fits
from astropy.time import Time

__all__ = [
    "load_fits",
    "load_fits_meta",
    "validate_fits_output",
]

# Science extension names searched in priority order
_KNOWN_SCI_EXTNAMES = ("SCI", "DET_DIST")

# PRIMARY is only used as a science plane when both dimensions are at least
# this many pixels — rejects sfftsoln-style arrays (e.g. 1×362) that are 2D
# but not images.
_PRIMARY_FALLBACK_MIN_EDGE = 64

# Valid filter identifiers: standard Roman WFI names + RAPID pipeline aliases
_VALID_FITS_FILTERS = frozenset(
    {
        # Standard Roman WFI filter names (ASDF / GBTDS convention)
        "F062",
        "F087",
        "F106",
        "F129",
        "F146",
        "F158",
        "F184",
        "F213",
        # RAPID pipeline alias names (short aliases used in RAPID headers)
        "H158",
        "J129",
        "K213",
        "R062",
        "Y106",
        "Z087",
    }
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_fits(
    filepath: Union[str, Path],
    *,
    include_err: bool = True,
    include_dq: bool = True,
) -> dict:
    """
    Load a Roman WFI FITS image file into a standardized dict.

    Parameters
    ----------
    filepath:
        Local filesystem path. Gzipped files (.fits.gz) are handled
        transparently by astropy.
    include_err:
        If True (default), load the ERR plane into ``'err'``.
        If False or ERR extension absent, ``'err'`` is a zero float32 array.
    include_dq:
        If True (default), load the DQ plane into ``'dq'``.
        If False or DQ extension absent, ``'dq'`` is a zero uint32 array.

    Returns
    -------
    dict with keys matching load_asdf() output schema:
        data     : (H, W) float32   — science image (always cast from source dtype)
        dq       : (H, W) uint32    — data quality bitmask
        err      : (H, W) float32   — per-pixel uncertainty
        mjd      : float            — mid-exposure MJD (nan for PSF files)
        filter   : str              — filter name, e.g. 'F184', 'Y106'
        exptime  : float            — exposure time in seconds (0.0 if absent)
        ra_ref   : float            — CRVAL1 WCS reference RA in deg (nan if absent)
        dec_ref  : float            — CRVAL2 WCS reference Dec in deg (nan if absent)
        detector : str              — detector/SCA identifier ('UNKNOWN' if absent)
        filename : str              — filename component of filepath
        obs_id   : str              — observation ID ('' if absent in header)

    Raises
    ------
    ValueError
        If no usable science image is found (no SCI / DET_DIST / 2D PRIMARY) —
        rejects unsupported file types (e.g. sfftsoln_cconv.fits).
    FileNotFoundError
        If a local filepath does not exist.
    """
    fpath_str = str(filepath)
    fobj = _resolve_source(fpath_str)
    # _resolve_source returns the local path string unchanged.
    owns_fobj = not isinstance(fobj, str)

    try:
        # memmap=False ensures the array data is fully read into memory before
        # the context exits.
        with fits.open(fobj, memmap=False, mode="readonly") as hdul:
            sci_hdu = _select_sci_hdu(hdul, fpath_str)
            data = np.array(sci_hdu.data, dtype=np.float32)

            if include_err and "ERR" in hdul and hdul["ERR"].data is not None:
                err = np.array(hdul["ERR"].data, dtype=np.float32)
            else:
                err = np.zeros(data.shape, dtype=np.float32)

            if include_dq and "DQ" in hdul and hdul["DQ"].data is not None:
                dq = np.array(hdul["DQ"].data, dtype=np.uint32)
            else:
                dq = np.zeros(data.shape, dtype=np.uint32)

            return {
                "data": data,
                "dq": dq,
                "err": err,
                "mjd": _extract_mjd_from_hdul(hdul, sci_hdu),
                "filter": str(_get_header_value(hdul, "FILTER", _get_header_value(hdul, "BAND", "UNKNOWN"))),
                "exptime": float(_get_header_value(hdul, "EXPTIME", _get_header_value(hdul, "EXP_TIME", 0.0))),
                "ra_ref": float(_get_header_value(hdul, "CRVAL1", float("nan"))),
                "dec_ref": float(_get_header_value(hdul, "CRVAL2", float("nan"))),
                "detector": str(_get_header_value(hdul, "DETECTOR", _get_header_value(hdul, "SCA_NUM", "UNKNOWN"))),
                "filename": Path(fpath_str).name,
                "obs_id": str(_get_header_value(hdul, "OBS_ID", "")),
            }
    finally:
        if owns_fobj:
            try:
                fobj.close()
            except Exception:
                pass


def load_fits_meta(filepath: Union[str, Path]) -> dict:
    """
    Load only metadata from a FITS file (no array data materialised).

    Useful for quickly indexing large datasets without reading image planes.

    Returns
    -------
    dict with keys: mjd, filter, exptime, ra_ref, dec_ref, detector,
                    filename, obs_id
    """
    fpath_str = str(filepath)
    fobj = _resolve_source(fpath_str)
    owns_fobj = not isinstance(fobj, str)

    try:
        with fits.open(fobj, memmap=False, mode="readonly") as hdul:
            # Identify science HDU for MJD fallback chain; never access .data
            try:
                sci_hdu = _select_sci_hdu(hdul, fpath_str)
            except ValueError:
                sci_hdu = hdul[0]

            return {
                "mjd": _extract_mjd_from_hdul(hdul, sci_hdu),
                "filter": str(_get_header_value(hdul, "FILTER", _get_header_value(hdul, "BAND", "UNKNOWN"))),
                "exptime": float(_get_header_value(hdul, "EXPTIME", _get_header_value(hdul, "EXP_TIME", 0.0))),
                "ra_ref": float(_get_header_value(hdul, "CRVAL1", float("nan"))),
                "dec_ref": float(_get_header_value(hdul, "CRVAL2", float("nan"))),
                "detector": str(_get_header_value(hdul, "DETECTOR", _get_header_value(hdul, "SCA_NUM", "UNKNOWN"))),
                "filename": Path(fpath_str).name,
                "obs_id": str(_get_header_value(hdul, "OBS_ID", "")),
            }
    finally:
        if owns_fobj:
            try:
                fobj.close()
            except Exception:
                pass


def validate_fits_output(result: dict) -> list[str]:
    """
    Validate the output dict from :func:`load_fits`.

    Returns a list of human-readable error strings; empty list means valid.
    Does not raise — callers decide how to handle errors.

    Notes
    -----
    ``mjd = nan`` is *accepted without error*: PSF files (diffpsf.fits,
    WFI_SCA*_PSF_DET_DIST_normalized.fits) legitimately contain no
    temporal metadata.
    """
    errors: list[str] = []

    # --- required array keys ---
    for key in ("data", "dq", "err"):
        if key not in result:
            errors.append(f"Missing required key: '{key}'")

    # --- dtypes ---
    if "data" in result and result["data"].dtype != np.float32:
        errors.append(f"'data' dtype must be float32, got {result['data'].dtype}")
    if "err" in result and result["err"].dtype != np.float32:
        errors.append(f"'err' dtype must be float32, got {result['err'].dtype}")
    if "dq" in result and not np.issubdtype(result["dq"].dtype, np.integer):
        errors.append(f"'dq' dtype must be integer, got {result['dq'].dtype}")

    # --- shape consistency ---
    shapes = {k: result[k].shape for k in ("data", "dq", "err") if k in result}
    if len(set(shapes.values())) > 1:
        errors.append(f"Shape mismatch across arrays: {shapes}")

    # --- all-non-finite check (skip all-zero arrays: synthesised planes) ---
    for key in ("data", "err"):
        if key in result:
            arr = result[key]
            if arr.size > 0 and np.any(arr != 0) and not np.any(np.isfinite(arr)):
                errors.append(f"'{key}' array contains no finite values")

    # --- MJD: nan is allowed (PSF files); only validate when finite ---
    mjd = result.get("mjd")
    if mjd is None:
        errors.append("Missing required key: 'mjd'")
    elif isinstance(mjd, float) and np.isfinite(mjd) and mjd < 50000:
        errors.append(f"'mjd' value {mjd} is suspiciously small (< 50000)")

    # --- filter ---
    filt = result.get("filter")
    if filt and filt.upper() not in _VALID_FITS_FILTERS and filt != "UNKNOWN":
        errors.append(f"'filter' value '{filt}' not in known Roman WFI / RAPID filters: {sorted(_VALID_FITS_FILTERS)}")

    return errors


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_source(path: str):
    """
    Return the original local path string for astropy.io.fits.open().

    The demo and production flows expect files to be present locally. Download
    public examples first, then pass the downloaded path here.
    """
    if path.startswith("s3://"):
        raise ValueError("Download FITS files locally before calling load_fits().")
    return path


def _select_sci_hdu(hdul: fits.HDUList, filepath: str = "") -> fits.ImageHDU:
    """
    Return the science ImageHDU from an open HDUList.

    Searches for SCI then DET_DIST extensions (in that order), then falls
    back to ``hdul[0]`` (PRIMARY) when it holds a 2D image array whose
    shorter side is at least :data:`_PRIMARY_FALLBACK_MIN_EDGE` pixels (so
    1×362 solution tables are still rejected).  The PRIMARY fallback supports
    RAPID RTS / rimtimsim products that ship single-HDU FITS files without an
    ``EXTNAME`` of SCI.

    Raises ValueError for files without a recognised science image,
    which rejects unsupported types (e.g. sfftsoln_cconv.fits whose
    only HDU has shape (1, 362)).
    """
    for extname in _KNOWN_SCI_EXTNAMES:
        if extname in hdul:
            hdu = hdul[extname]
            if hdu.data is not None and hdu.data.ndim == 2:
                return hdu
    # Single-extension science: image lives in PRIMARY (RAPID_RTS_products, etc.)
    prim = hdul[0]
    if prim.data is not None and prim.data.ndim == 2:
        h, w = int(prim.data.shape[0]), int(prim.data.shape[1])
        if min(h, w) >= _PRIMARY_FALLBACK_MIN_EDGE:
            return prim
    raise ValueError(
        f"No supported science image (SCI, DET_DIST, or 2D PRIMARY) found "
        f"in {Path(filepath).name!r}. "
        f"Extensions present: {[h.name for h in hdul]}"
    )


def _get_header_value(hdul: fits.HDUList, key: str, default=None):
    """
    Retrieve a header keyword, checking hdul[0] first then each known
    science extension header.

    For single-extension RAPID files hdul[0] IS the SCI HDU.
    For multi-extension files (IRSA) hdul[0] is PRIMARY with all metadata.
    The fallback to SCI/DET_DIST covers the edge case where metadata lives
    only on the science extension header.
    """
    val = hdul[0].header.get(key)
    if val is not None:
        return val
    for extname in _KNOWN_SCI_EXTNAMES:
        if extname in hdul:
            val = hdul[extname].header.get(key)
            if val is not None:
                return val
    return default


def _extract_mjd(hdr: fits.Header) -> float:
    """
    Extract MJD from a single FITS header.

    Tries MJD-OBS, then MJD-BEG, then converts DATE-OBS (handles both
    ISO T-separator 'YYYY-MM-DDTHH:MM:SS' and space-separator
    'YYYY-MM-DD HH:MM:SS' formats).  Returns nan if no keyword found.
    """
    if "MJD-OBS" in hdr:
        return float(hdr["MJD-OBS"])
    if "MJD-BEG" in hdr:
        return float(hdr["MJD-BEG"])
    if "DATE-OBS" in hdr:
        dateobs = str(hdr["DATE-OBS"]).strip()
        for fmt in ("isot", "iso", "fits"):
            try:
                return float(Time(dateobs, format=fmt).mjd)
            except (ValueError, TypeError):
                continue
    return float("nan")


def _extract_mjd_from_hdul(
    hdul: fits.HDUList,
    sci_hdu: fits.ImageHDU,
) -> float:
    """
    Extract MJD, trying the science HDU header first then hdul[0].

    For single-extension files both checks target the same header.
    For multi-extension files PRIMARY (hdul[0]) usually holds MJD-OBS.
    """
    mjd = _extract_mjd(sci_hdu.header)
    if np.isnan(mjd) and sci_hdu is not hdul[0]:
        mjd = _extract_mjd(hdul[0].header)
    return mjd
