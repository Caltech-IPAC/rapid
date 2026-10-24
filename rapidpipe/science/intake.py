# ported-from: pipeline/ingestL2Files.py @ 2350a884
"""A Roman WFI L2 ASDF file as `dev`'s multi-extension FITS file.

Ported from `dev`'s ``pipeline/ingestL2Files.py`` (Russ Laher, with
Azalee Bostroem's observation identifiers): ``build_fits_file`` and the
helpers it calls, and ``gzip_file``. The conversion is `dev`'s, unchanged:

- HDU 0 (PRIMARY) holds keywords only: the short keywords the pipeline
  reads (``FILTER``, ``EXPTIME``, ``DATE-OBS``, ``MJD-OBS``, ``SCA_NUM``,
  ``ZPTMAG``, the observation identifiers, ...) and every scalar leaf of
  the ASDF ``meta`` tree as a ``HIERARCH`` card keyed by its path.
- HDU 1 (SCI) is the science image in DN (the L2 DN/s times ``EXPTIME``)
  with the FITS-SIP fit to the gWCS, every SIP coefficient to fifth order
  present, and the image keywords copied from the primary.
- The remaining arrays follow, one image HDU each (``err`` scaled as the
  data, the variances by ``EXPTIME`` squared, the rest unscaled, float16
  widened to float32), then the ASDF tables as binary tables.

Commissioning #238's DQ mask (``pipeline/dqMaskSubs.py`` @ 5300a6fc) is
the one addition: where ``DQ & dq_mask_bits`` is nonzero the pixel is NaN
in ``SCI``, ``ERR`` and every ``VAR_*`` array, each of those HDUs carries
``DQMASK`` (the bits) and ``NDQMASK`` (the pixels), and ``DQ`` is written
unchanged; ``dq_mask_bits = 0`` is `dev`'s conversion, nothing masked.

The second is the GBTDS WCS fix (:func:`rederive_wcs`, STScI's
``roman_notebooks`` ``ccs_simulations/fix_gbtds_wcs.py``): given a
distortion reference, the gWCS is re-derived from the file's own
``meta.wcsinfo`` by romancal's ``AssignWcsStep`` body before the SIP fit,
replacing the one tweakreg left. Without one the file's gWCS is used, as
`dev` does.

`dev`'s hard-coded tuning is the caller's: the SIP degree and its maximum
pixel error, the gzip level and the nominal zeropoints (used only when the
file carries no photometric calibration). `dev`'s ``print`` lines are log
records; its conversion timestamp and software name are arguments.
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from astropy.io import fits
from astropy.table import Table
from astropy.time import Time

from rapidpipe.products.filters import ROMAN_TO_RAPID_FILTER_NAMES

log = logging.getLogger(__name__)

#: The SIP order dev's L2Files table stores; headers are always filled to at least it.
DATABASE_SIP_DEGREE = 5

#: The ASDF node holding the science image.
SCIENCE_NODE_NAME = "data"

#: The ASDF node holding the data-quality flags.
DQ_NODE_NAME = "dq"

#: The nodes set NaN where the DQ mask is set (#238's ``dq_masked_node_names``).
DQ_MASKED_NODE_NAMES = ("data", "err", "var_poisson", "var_rnoise", "var_flat")

#: The exponent of EXPTIME converting each node from DN/s to DN; unlisted nodes are unscaled.
EXPTIME_SCALING_EXPONENT = {"data": 1, "err": 1, "var_poisson": 2, "var_rnoise": 2, "var_flat": 2}

#: The Roman observation identifiers: (FITS keyword, ASDF meta.observation attribute).
OBSERVATION_ID_KEYWORDS = [("PROGRAM", "program"), ("EXECPLAN", "execution_plan"),
                           ("PASS", "pass"), ("SEGMENT", "segment"),
                           ("OBSERVTN", "observation"), ("VISIT", "visit"),
                           ("EXPOSURE", "exposure"), ("VISIT_ID", "visit_id"),
                           ("OBS_ID", "observation_id")]

#: The primary keywords copied into the science-image header (BUNIT deliberately not).
SCIENCE_IMAGE_KEYWORDS = ["FILTER", "EXPTIME", "DATE-OBS", "DATE-END", "MJD-OBS", "TSTART", "TEND",
                          "DETECTOR", "SCA_NUM", "TARGAPER", "TARGRA", "TARGDEC",
                          *[keyword for keyword, _ in OBSERVATION_ID_KEYWORDS],
                          "ZPTMAG", "ORIGFILE", "SWNAME", "SWVERS", "CREATED"]


class ConversionError(ValueError):
    """The ASDF file lacks something the FITS file cannot do without."""


# ----------------------------------------------------------------------
# The ASDF metadata tree as FITS keywords
# ----------------------------------------------------------------------


def coerce_header_value(value):
    """``value`` in a form a FITS card holds, or None when it has no scalar form."""
    if value is None:
        return None
    if isinstance(value, (float, np.floating)) and not np.isfinite(value):
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.str_):
        return str(value)
    if isinstance(value, Time):
        return str(value.isot)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "unit") and hasattr(value, "value"):  # a Quantity keeps its value
        try:
            return float(value.value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, (list, tuple)) and len(value) <= 8:
        parts = []
        for element in value:
            coerced = coerce_header_value(element)
            if coerced is None:
                return None
            parts.append(str(coerced))
        return ",".join(parts)
    return None


def is_array_like(value) -> bool:
    """An array of one or more dimensions (ASDF's lazy NDArrayType included)."""
    if isinstance(value, (str, bytes)):
        return False
    if not (hasattr(value, "shape") and hasattr(value, "dtype")):
        return False
    return getattr(value, "ndim", 0) >= 1


def is_table_like(value) -> bool:
    """An astropy Table, or an array whose dtype has named fields."""
    if isinstance(value, Table):
        return True
    if not is_array_like(value):
        return False
    return getattr(value.dtype, "names", None) is not None


def is_metadata_container(value) -> bool:
    """A branch of the ASDF metadata tree to descend into."""
    if isinstance(value, dict):
        return True
    if isinstance(value, (str, bytes)) or is_array_like(value):
        return False
    return hasattr(value, "keys") and hasattr(value, "__getitem__")


def flatten_asdf_metadata(node, prefix="meta"):
    """Yield ``(dotted path, value, unit)`` for every scalar leaf; arrays and the gWCS are skipped."""
    if not is_metadata_container(node):
        return
    for key in sorted(node.keys()):
        try:
            value = node[key]
        except Exception as exc:  # noqa: BLE001 - one unreadable leaf costs only itself
            log.warning("could not read ASDF metadata %s.%s (%s); skipping", prefix, key, exc)
            continue
        path = f"{prefix}.{key}"
        if is_array_like(value):
            continue
        if is_metadata_container(value):
            yield from flatten_asdf_metadata(value, path)
            continue
        if key == "wcs":
            continue
        unit = str(value.unit) if hasattr(value, "unit") and hasattr(value, "value") else None
        yield path, value, unit


def asdf_metadata_cards(meta_node) -> list[fits.Card]:
    """Every scalar leaf of the metadata tree as a card keyed by its path, spelled with spaces."""
    cards, n_skipped = [], 0
    warnings.filterwarnings("ignore", "Keyword name .* a HIERARCH card will be created",
                            fits.verify.VerifyWarning)  # HIERARCH is the intent
    for path, value, unit in flatten_asdf_metadata(meta_node):
        coerced = coerce_header_value(value)
        if coerced is None:
            n_skipped += 1
            continue
        try:
            card = fits.Card(path.replace(".", " "), coerced, f"[{unit}]" if unit is not None else "")
            str(card)  # formats the card, failing here if it cannot be
        except Exception as exc:  # noqa: BLE001
            log.warning("could not make a FITS card for ASDF metadata %s (%s); skipping", path, exc)
            n_skipped += 1
            continue
        cards.append(card)
    log.info("asdf_metadata_cards: n_cards,n_skipped = %d,%d", len(cards), n_skipped)
    return cards


# ----------------------------------------------------------------------
# The conversion
# ----------------------------------------------------------------------


def gwcs_to_fits_sip_header(wcs_obj, shape, degree: int, max_pix_error: float):
    """The FITS-SIP header of a gWCS, fitted to ``degree`` over the image's full extent."""
    return wcs_obj.to_fits_sip(bounding_box=((0, shape[-1] - 1), (0, shape[-2] - 1)),
                               max_pix_error=max_pix_error, degree=degree)


def fill_missing_sip_keywords(hdr, degree: int) -> None:
    """Every SIP coefficient to ``degree``, the ones the fit left out as zero (awaicgen needs them all)."""
    for prefix in ("A", "B", "AP", "BP"):
        for i in range(degree + 1):
            for j in range(degree + 1):
                if f"{prefix}_{i}_{j}" not in hdr:
                    hdr[f"{prefix}_{i}_{j}"] = 0.0


def coerce_to_astropy_time(value):
    """``value`` as an astropy Time, or None."""
    if value is None:
        return None
    if isinstance(value, Time):
        return value
    if isinstance(value, datetime):
        return Time(value)
    try:
        return Time(str(value), format="isot")
    except Exception:  # noqa: BLE001
        pass
    try:
        return Time(datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%S.%f"))
    except Exception:  # noqa: BLE001
        return None


def compute_zptmag(dm, optical_element: str, nominal_zptmag: Mapping[str, float]):
    """The AB zeropoint [mag] for flux in DN/s, from the file's own photometric calibration.

    ``ZPTMAG = -2.5 log10(conversion_megajanskys * 1e6 * pixel_area / 3631)``; the
    nominal table only when ``meta.photometry`` has no usable values.
    """
    phot = getattr(dm.meta, "photometry", None)
    conv = getattr(phot, "conversion_megajanskys", None)
    pixarea = getattr(phot, "pixel_area", None)
    if conv is not None and pixarea is not None and conv > 0 and pixarea > 0:
        return -2.5 * np.log10(float(conv) * 1.0e6 * float(pixarea) / 3631.0)
    log.warning("meta.photometry unavailable; using the nominal ZPTMAG table")
    return nominal_zptmag.get(optical_element)


def build_primary_header(dm, asdf_tree, input_asdf_file, nominal_zptmag: Mapping[str, float],
                         swname: str, swvers: str, created: str) -> fits.Header:
    """The primary header: the pipeline's short keywords, then every scalar ASDF metadata leaf."""
    hdr = fits.Header()
    hdr["SWNAME"] = (swname, "software that made this file")
    hdr["SWVERS"] = (swvers, "version of that software")
    hdr["CREATED"] = (created, "UTC datetime this file was made")
    hdr["ORIGFILE"] = (os.path.basename(input_asdf_file), "ASDF file this file was made from")
    asdf_library = asdf_tree.get("asdf_library") if hasattr(asdf_tree, "get") else None
    if asdf_library is not None:
        version = asdf_library.get("version") if hasattr(asdf_library, "get") else None
        if version is not None:
            hdr["ASDFVERS"] = (str(version), "asdf library version of the input file")

    optical_element = str(dm.meta.instrument.optical_element).strip()
    if optical_element not in ROMAN_TO_RAPID_FILTER_NAMES:
        raise ConversionError(f"unexpected optical element {optical_element}")
    hdr["FILTER"] = (ROMAN_TO_RAPID_FILTER_NAMES[optical_element], "filter used")

    detector = str(dm.meta.instrument.detector).strip()
    hdr["DETECTOR"] = (detector, "detector assembly")
    try:
        hdr["SCA_NUM"] = (int(detector.replace("WFI", "")), "sensor chip assembly number")
    except ValueError:
        raise ConversionError(f"no SCA number in detector {detector}") from None

    # An unset float in the Roman data model is -999999.0: scaling by it makes a
    # well-formed file of garbage, so it is refused.
    exptime = float(dm.meta.exposure.exposure_time)
    if not np.isfinite(exptime) or exptime <= 0.0:
        raise ConversionError(f"exposure time {exptime} is not a positive, finite number")
    time_start = coerce_to_astropy_time(dm.meta.exposure.start_time)
    time_end = coerce_to_astropy_time(dm.meta.exposure.end_time)
    if time_start is None:
        raise ConversionError(f"cannot parse the start time {dm.meta.exposure.start_time}")
    dateobs = str(time_start.isot)
    hdr["EXPTIME"] = (exptime, "[s] time on source")
    hdr["DATE-OBS"] = (dateobs, "observation start in UTC calendar date")
    hdr["DATE-END"] = (str(time_end.isot) if time_end is not None else dateobs,
                       "observation end in UTC calendar date")
    hdr["MJD-OBS"] = (float(time_start.mjd), "[d] observation start as MJD")  # the same instant as DATE-OBS

    pointing = getattr(dm.meta, "pointing", None)
    targ_aper = getattr(pointing, "target_aperture", None)
    targ_ra = getattr(pointing, "target_ra", None)
    targ_dec = getattr(pointing, "target_dec", None)
    if targ_aper is not None:
        hdr["TARGAPER"] = (str(targ_aper), "target aperture")
    if targ_ra is not None:
        hdr["TARGRA"] = (float(targ_ra), "[deg] right ascension of target aperture")
    if targ_dec is not None:
        hdr["TARGDEC"] = (float(targ_dec), "[deg] declination of target aperture")
    for keyword, attribute in OBSERVATION_ID_KEYWORDS:
        hdr[keyword] = getattr(dm.meta.observation, attribute)

    zptmag = compute_zptmag(dm, optical_element, nominal_zptmag)
    if zptmag is None:
        raise ConversionError(f"no zeropoint for optical element {optical_element}")
    hdr["ZPTMAG"] = (zptmag, "[mag] AB zeropoint for flux in DN/s")
    hdr["BUNIT"] = ("DN", "units of the science image")
    hdr["EQUINOX"] = (2000.0, "[yr] equinox of equatorial coordinates")
    hdr["RADESYS"] = ("ICRS", "equatorial coordinate system")

    hdr.append(fits.Card("COMMENT", "-" * 60))
    hdr.append(fits.Card("COMMENT", "ASDF metadata below, keyed by its path in the ASDF tree"))
    hdr.append(fits.Card("COMMENT", "e.g. HIERARCH meta exposure exposure_time = 54.72"))
    hdr.append(fits.Card("COMMENT", "-" * 60))
    for card in asdf_metadata_cards(dm.meta):
        hdr.append(card)
    return hdr


def build_science_header(primary_hdr, wcs_header, shape, extname: str, sip_degree: int) -> fits.Header:
    """An image HDU's header: the FITS-SIP WCS and the primary's image keywords."""
    hdr = fits.Header()
    hdr.update(wcs_header)
    hdr["NAXIS"] = 2
    hdr["NAXIS1"] = shape[-1]
    hdr["NAXIS2"] = shape[-2]
    fill_missing_sip_keywords(hdr, max(sip_degree, DATABASE_SIP_DEGREE))
    for keyword in SCIENCE_IMAGE_KEYWORDS:
        if keyword in primary_hdr:
            hdr[keyword] = (primary_hdr[keyword], primary_hdr.comments[keyword])
    hdr["EXTNAME"] = extname
    return hdr


def asdf_image_nodes(dm) -> list[tuple[str, Any]]:
    """Every image array of two or more dimensions, the science image first."""
    nodes = []
    for key in dm.keys():
        if key == "meta":
            continue
        try:
            value = dm[key]
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read ASDF node %s (%s); skipping", key, exc)
            continue
        if not is_array_like(value) or is_table_like(value) or value.ndim < 2:
            continue
        if value.size == 0:
            log.warning("ASDF node %s has shape %s and no pixels; skipping", key, value.shape)
            continue
        nodes.append((key, value))
    nodes.sort(key=lambda node: (node[0] != SCIENCE_NODE_NAME, node[0]))
    return nodes


def asdf_table_nodes(dm) -> list[tuple[str, Any]]:
    """Every tabular node: structured arrays, tables, and romancal's lists of log strings."""
    nodes = []
    for key in dm.keys():
        if key == "meta":
            continue
        try:
            value = dm[key]
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read ASDF node %s (%s); skipping", key, exc)
            continue
        if is_table_like(value):
            nodes.append((key, value))
        elif isinstance(value, (list, tuple)) and value and all(isinstance(e, str) for e in value):
            nodes.append((key, Table({key: [str(e) for e in value]})))
    return nodes


def find_dq_masked_pixels(dq, mask_bits: int) -> np.ndarray:
    """True where any of ``mask_bits`` is set in the DQ array (#238's helper)."""
    if not 0 <= mask_bits < 2**32:
        raise ConversionError(f"dq_mask_bits {mask_bits} is not a 32-bit unsigned value")
    dq = np.asarray(dq)
    if not np.issubdtype(dq.dtype, np.integer):
        raise ConversionError(f"the DQ array has non-integer dtype {dq.dtype}")
    return (dq.astype(np.uint32, copy=False) & np.uint32(mask_bits)) != 0


def build_image_hdu(node_name, array, primary_hdr, wcs_header, science_shape, exptime,
                    sip_degree: int, dq_masked: np.ndarray | None = None,
                    dq_mask_bits: int = 0) -> fits.ImageHDU:
    """One ASDF array as an image HDU in DN, with the science WCS when laid out like the science image.

    With ``dq_masked``, a node of :data:`DQ_MASKED_NODE_NAMES` is NaN there
    and its header records ``DQMASK`` and ``NDQMASK``.
    """
    extname = "SCI" if node_name == SCIENCE_NODE_NAME else node_name.upper()
    exponent = EXPTIME_SCALING_EXPONENT.get(node_name, 0)
    if exponent == 0:
        data = np.array(array, dtype=np.float32) if array.dtype == np.float16 else np.array(array)
    else:
        data = np.array(array, dtype=np.float32)
        data *= np.float32(float(exptime) ** exponent)
    masked = dq_masked is not None and node_name in DQ_MASKED_NODE_NAMES
    if masked:
        data[dq_masked] = np.nan
    if array.shape == science_shape:
        hdr = build_science_header(primary_hdr, wcs_header, array.shape, extname, sip_degree)
    else:
        hdr = fits.Header()
        hdr["EXTNAME"] = extname
    hdr["ASDFNODE"] = (node_name, "node this array came from in the ASDF file")
    if exponent == 0:
        unit = str(getattr(array, "unit", "")).strip()
        if unit:
            hdr["BUNIT"] = (unit, "units of this array")
    elif exponent == 1:
        hdr["BUNIT"] = ("DN", "units of this array")
    else:
        hdr["BUNIT"] = ("DN**2", "units of this array")
    hdr["EXPTSCAL"] = (float(exptime) ** exponent, "factor applied to convert from the ASDF units")
    if masked:
        hdr["DQMASK"] = (dq_mask_bits, f"DQ bits ({hex(dq_mask_bits)}) set to NaN here")
        hdr["NDQMASK"] = (int(np.count_nonzero(dq_masked)), "number of pixels set to NaN by DQMASK")
    return fits.ImageHDU(data=data, header=hdr, name=extname)


def build_table_hdu(node_name, table):
    """One tabular node as a binary table, or None when it cannot be converted."""
    try:
        hdu = fits.BinTableHDU(data=Table(table), name=node_name.upper())
    except Exception as exc:  # noqa: BLE001
        log.warning("could not convert ASDF table %s to a FITS table (%s); skipping", node_name, exc)
        return None
    hdu.header["ASDFNODE"] = (node_name, "node this table came from in the ASDF file")
    return hdu


def build_fits_file(dm, asdf_path, fits_path, *, sip_degree: int, sip_max_pix_error: float,
                    nominal_zptmag: Mapping[str, float], swname: str, swvers: str,
                    created: str, dq_mask_bits: int = 0) -> dict[str, Any]:
    """Write the FITS file of an open data model one HDU at a time.

    Returns ``hdus`` (the EXTNAMEs), ``dq_masked`` (pixels the DQ mask set
    NaN, None when nothing was masked) and ``sci_nan_input`` (the science
    image's own NaN pixels before the mask). The HDUs go to
    ``<fits_path>.partial``, moved into place only after the last.
    """
    import asdf

    try:
        with asdf.open(asdf_path) as af:
            asdf_tree = dict(af.tree)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read the raw ASDF tree of %s (%s); continuing", asdf_path, exc)
        asdf_tree = {}
    primary_hdr = build_primary_header(dm, asdf_tree, asdf_path, nominal_zptmag, swname, swvers, created)
    exptime = primary_hdr["EXPTIME"]
    image_nodes = asdf_image_nodes(dm)
    if not image_nodes or image_nodes[0][0] != SCIENCE_NODE_NAME:
        raise ConversionError(f"the ASDF file has no {SCIENCE_NODE_NAME} array")
    science_shape = image_nodes[0][1].shape
    wcs_header = gwcs_to_fits_sip_header(dm.meta.wcs, science_shape, sip_degree, sip_max_pix_error)
    sci_nan_input = int(np.count_nonzero(np.isnan(image_nodes[0][1])))
    dq_masked = None
    if dq_mask_bits:
        # #238 converts a file without a usable DQ array unmasked, with a
        # warning; here a run that asked for the mask gets it or a failed job.
        dq_masked = find_dq_masked_pixels(dict(image_nodes)[DQ_NODE_NAME], dq_mask_bits)
        log.info("DQ mask %s: %d pixels (%.3f%%) set to NaN", hex(dq_mask_bits),
                 np.count_nonzero(dq_masked), 100.0 * np.mean(dq_masked))

    partial = Path(str(fits_path) + ".partial")
    partial.unlink(missing_ok=True)
    extnames = []
    try:
        fits.HDUList([fits.PrimaryHDU(header=primary_hdr)]).writeto(partial, overwrite=True, checksum=True)
        extnames.append("PRIMARY")
        for node_name, array in image_nodes:
            hdu = build_image_hdu(node_name, array, primary_hdr, wcs_header, science_shape, exptime,
                                  sip_degree, dq_masked, dq_mask_bits)
            fits.append(partial, hdu.data, hdu.header, checksum=True)
            extnames.append(hdu.name)
            del hdu  # one array in memory at a time
        for node_name, table in asdf_table_nodes(dm):
            hdu = build_table_hdu(node_name, table)
            if hdu is None:
                continue
            fits.append(partial, hdu.data, hdu.header, checksum=True)
            extnames.append(hdu.name)
            del hdu
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    os.replace(partial, fits_path)
    log.info("wrote %s with %d HDUs: %s", fits_path, len(extnames), ",".join(extnames))
    return {"hdus": extnames,
            "dq_masked": None if dq_masked is None else int(np.count_nonzero(dq_masked)),
            "sci_nan_input": sci_nan_input}


def rederive_wcs(dm, distortion) -> tuple[float, float]:
    """Replace the model's gWCS with one re-derived from its ``meta.wcsinfo`` and ``distortion``.

    romancal's ``load_wcs``, the body of ``AssignWcsStep`` without the
    step's CRDS lookup (the caller names the reference, the file's own).
    The notebook's fix first copies ``ra_ref``, ``dec_ref`` and
    ``roll_ref`` from the L1 file; r00340's L2 files carry the L1 values
    unchanged (12 of 12 sampled equal), so the L1 file is not read.
    Returns the pixel position, under the old gWCS, of the sky point the new
    one puts at the image centre, minus the centre: the shift the fix made.
    """
    from romancal.assign_wcs.assign_wcs import load_wcs

    ny, nx = dm.data.shape
    centre = ((nx - 1) / 2, (ny - 1) / 2)
    old = dm.meta.wcs
    load_wcs(dm, {"distortion": str(distortion)})
    x, y = old.invert(*dm.meta.wcs(*centre), with_bounding_box=False)
    return float(x - centre[0]), float(y - centre[1])


def asdf_to_fits(asdf_path, fits_path, *, distortion=None, **conversion) -> dict[str, Any]:
    """Convert one ASDF file, memory-mapped, into a multi-extension FITS file (:func:`build_fits_file`).

    With ``distortion`` (a reference file's path) the gWCS is re-derived
    first (:func:`rederive_wcs`) and the result carries ``wcs_shift_px``.
    """
    import roman_datamodels as rdm

    dm = rdm.open(asdf_path, memmap=True)
    try:
        shift = None if distortion is None else rederive_wcs(dm, distortion)
        result = build_fits_file(dm, asdf_path, fits_path, **conversion)
        return result if shift is None else {**result, "wcs_shift_px": shift}
    finally:
        dm.close()


def gzip_file(input_filename, output_filename, compresslevel: int) -> None:
    """A gzipped copy of a file at ``compresslevel``, the original kept."""
    with open(input_filename, "rb") as fh_in, gzip.open(output_filename, "wb",
                                                         compresslevel=compresslevel) as fh_out:
        shutil.copyfileobj(fh_in, fh_out)
