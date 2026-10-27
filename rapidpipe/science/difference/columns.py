# ported-from: modules/utils/rapid_data_analysis.py, modules/utils/rapid_pipeline_subs.py @ c931b001
"""`dev`'s pixel columns of a difference-image catalog: ``sumrat``, ``nneg``, ``nbad``.

ZTF's candidate metrics (``imgdiffextract.pl``), as `dev` computes them
for every source of the SFFT PSF-fit catalogs (fa600637, babd5d16,
9af0f59f, e4a49c97, 0c1ef727, c32e28f6, 41473d60), measured on a copy of
the difference image with a local background subtracted (photutils
``Background2D``, a 64 px mesh), because sumrat and nneg count on the
sign of each pixel and an SFFT difference image can sit on a pedestal.
The copy is in memory; the image itself is unchanged.

- ``sumrat``: ``sum(p) / sum(|p|)`` over a 5 x 5 stamp on the pixel
  nearest the source after a 3 x 3 truncated median filter; about 0 for
  noise, near 1 for a point source, near 0 for a dipole. ZTF keeps a
  candidate at ``sumrat > 0.4``.
- ``nneg``: the stamp's finite pixels below 0, unfiltered.
- ``nbad``: the stamp's non-finite pixels.

A source whose position is not finite, whose stamp leaves the image or
whose filtered stamp has no usable pixel gets NaN, where `dev` writes
``-999`` and its loader loads NULL. When the background fails (photutils
drops every mesh box that keeps under 90 % of its pixels after the 3-sigma
clip, as on a heavy-tailed SFFT difference), the columns are measured on
the image itself, as `dev` does, with a warning.

`dev`'s functions read and write FITS files; these take arrays, and the
catalog is the parquet file the stage publishes, rewritten with the three
columns appended (:func:`add_pixel_columns`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import logging

import numpy as np

logger = logging.getLogger(__name__)

#: The columns, in `dev`'s order.
PIXEL_COLUMNS = ("sumrat", "nneg", "nbad")

#: `dev`'s chunk of stamps per median-filter stack, to bound its memory.
CHUNK = 20000

#: `dev`'s Background2D sigma clip (``SigmaClip(sigma=3.0)``).
BACKGROUND_CLIP_SIGMA = 3.0


def median_filter_truncate_stack(stamps: np.ndarray, filter_size: int = 3,
                                 lower_median: bool = False) -> np.ndarray:
    """`dev` ``median_filter_truncate_stack``: PDL ``med2d`` with ``Boundary => 'Truncate'``.

    Box positions outside a stamp and NaN pixels are skipped; an even count
    takes the mean of the two middle values, or the lower one when
    ``lower_median`` (ZTF exactly, which biases noise's sumrat to about
    -0.32). A box with no finite pixel is NaN.
    """
    if filter_size <= 0 or filter_size % 2 == 0:
        raise ValueError(f"filter_size = {filter_size} must be a positive odd integer")
    stamps = np.asarray(stamps, dtype=np.float64)
    h = filter_size // 2
    n, ny, nx = stamps.shape
    padded = np.full((n, ny + 2 * h, nx + 2 * h), np.nan)
    padded[:, h:h + ny, h:h + nx] = stamps
    neighbors = np.stack([padded[:, dy:dy + ny, dx:dx + nx]
                          for dy in range(filter_size) for dx in range(filter_size)], axis=1)
    neighbors = np.sort(neighbors, axis=1)          # NaN last: the finite values come first, in order
    count = np.sum(np.isfinite(neighbors), axis=1)
    index_lo = np.maximum(count - 1, 0) // 2
    filtered = np.take_along_axis(neighbors, index_lo[:, np.newaxis, :, :], axis=1)[:, 0]
    if not lower_median:
        index_hi = np.minimum(count // 2, filter_size * filter_size - 1)
        filtered_hi = np.take_along_axis(neighbors, index_hi[:, np.newaxis, :, :], axis=1)[:, 0]
        filtered = 0.5 * (filtered + filtered_hi)
    filtered[count == 0] = np.nan
    return filtered


def _stamp_centres(shape: tuple[int, int], x: np.ndarray, y: np.ndarray,
                   stamp_size: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The nearest pixel of each zero-based position, and the positions whose stamp fits.

    `dev`: pixel ``floor(x + 0.5)``; a stamp must lie wholly inside the image.
    """
    if stamp_size <= 0 or stamp_size % 2 == 0:
        raise ValueError(f"stamp_size = {stamp_size} must be a positive odd integer")
    ny, nx = shape
    h = stamp_size // 2
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    valid = np.isfinite(x) & np.isfinite(y)
    xind = np.zeros(len(x), dtype=int)
    yind = np.zeros(len(y), dtype=int)
    xind[valid] = np.floor(x[valid] + 0.5).astype(int)
    yind[valid] = np.floor(y[valid] + 0.5).astype(int)
    valid &= (xind >= h) & (xind <= nx - h - 1) & (yind >= h) & (yind <= ny - h - 1)
    return xind, yind, np.flatnonzero(valid)


def _stamps(data: np.ndarray, xind, yind, index, stamp_size: int) -> np.ndarray:
    offsets = np.arange(-(stamp_size // 2), stamp_size // 2 + 1)
    return data[yind[index, np.newaxis, np.newaxis] + offsets[np.newaxis, :, np.newaxis],
                xind[index, np.newaxis, np.newaxis] + offsets[np.newaxis, np.newaxis, :]]


def sumrat(data: np.ndarray, x, y, *, stamp_size: int = 5, filter_size: int = 3,
           lower_median: bool = False) -> np.ndarray:
    """`dev` ``compute_sumrat_for_diff_image`` on an array, zero-based positions; NaN where unmeasurable."""
    data = np.asarray(data, dtype=np.float64)
    xind, yind, index_valid = _stamp_centres(data.shape, x, y, stamp_size)
    out = np.full(len(xind), np.nan)
    for start in range(0, len(index_valid), CHUNK):
        index = index_valid[start:start + CHUNK]
        stamps = _stamps(data, xind, yind, index, stamp_size)
        if filter_size > 1:
            stamps = median_filter_truncate_stack(stamps, filter_size, lower_median)
        good = np.isfinite(stamps)
        total = np.sum(np.where(good, stamps, 0.0), axis=(1, 2))
        total_abs = np.sum(np.where(good, np.abs(stamps), 0.0), axis=(1, 2))
        usable = np.any(good, axis=(1, 2)) & (total_abs > 0.0)
        out[index[usable]] = total[usable] / total_abs[usable]
    return out


def nneg_nbad(data: np.ndarray, x, y, *, stamp_size: int = 5) -> tuple[np.ndarray, np.ndarray]:
    """`dev` ``compute_nneg_nbad_for_diff_image`` on an array, zero-based positions; NaN where unmeasurable."""
    data = np.asarray(data, dtype=np.float64)
    xind, yind, index = _stamp_centres(data.shape, x, y, stamp_size)
    nneg = np.full(len(xind), np.nan)
    nbad = np.full(len(xind), np.nan)
    if len(index):
        stamps = _stamps(data, xind, yind, index, stamp_size)
        finite = np.isfinite(stamps)
        nneg[index] = np.sum(finite & (np.where(finite, stamps, 0.0) < 0.0), axis=(1, 2))
        nbad[index] = np.sum(~finite, axis=(1, 2))
    return nneg, nbad


def subtract_local_background(data: np.ndarray, *, box_size: int = 64,
                              filter_size: int = 3) -> np.ndarray:
    """`dev` ``write_background_subtracted_diff_image``, in memory; non-finite pixels stay so.

    photutils ``Background2D`` on a ``box_size`` mesh, 3-sigma clipping, the
    SExtractor mode estimator, a ``filter_size`` median filter of the mesh,
    the non-finite pixels masked.
    """
    from astropy.stats import SigmaClip
    from photutils.background import Background2D, SExtractorBackground, StdBackgroundRMS

    data = np.asarray(data, dtype=np.float64)
    bad = ~np.isfinite(data)
    bkg = Background2D(np.where(bad, 0.0, data), (box_size, box_size),
                       filter_size=(filter_size, filter_size), mask=bad,
                       sigma_clip=SigmaClip(sigma=BACKGROUND_CLIP_SIGMA),
                       bkg_estimator=SExtractorBackground(),
                       bkg_rms_estimator=StdBackgroundRMS())
    # dev writes the copy as float32 and reads it back as float64; the same rounding here.
    return np.where(bad, data, data - bkg.background).astype(np.float32).astype(np.float64)


def pixel_columns(data: np.ndarray, x, y, settings: dict[str, Any]) -> dict[str, np.ndarray]:
    """The three columns for zero-based positions, measured after the local background.

    ``settings`` is the stage's ``[difference]`` table: ``sumrat`` (`dev`'s
    ``[SUMRAT]``) and ``extra_catalog_cols`` (``[EXTRA_CATALOG_COLS]``).
    """
    s, e = settings["sumrat"], settings["extra_catalog_cols"]
    try:
        image = subtract_local_background(data, box_size=int(e["pixel_cols_bkg_box"]),
                                          filter_size=int(e["pixel_cols_bkg_filter_size"]))
    except ValueError as exc:
        # dev's _compute_pixel_catalog_cols: the columns on the image itself.
        logger.warning("pixel columns: the local background failed (%s); measured on the image itself", exc)
        image = np.asarray(data, dtype=np.float64)
    nneg, nbad = nneg_nbad(image, x, y, stamp_size=int(e["nneg_nbad_stamp_size"]))
    return {"sumrat": sumrat(image, x, y, stamp_size=int(s["stamp_size"]),
                             filter_size=int(s["filter_size"]),
                             lower_median=bool(s["lower_median"])),
            "nneg": nneg, "nbad": nbad}


def add_pixel_columns(catalog: Path, image: Path, settings: dict[str, Any]) -> int:
    """Append the three columns to a joined PSF-fit catalog (parquet) from its image; the row count.

    `dev` ``compute_extra_cols_photutils``: positions ``x_fit``, ``y_fit``
    (zero-based), the catalog paired with the image its sources are
    positive in (the negative catalog with the negated image). Every other
    column and the row order are kept.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq
    from astropy.io import fits

    table = pq.read_table(catalog)
    clash = [c for c in PIXEL_COLUMNS if c in table.column_names]
    if clash:
        raise ValueError(f"{catalog} already has {clash}")
    x = table.column("x_fit").to_numpy(zero_copy_only=False).astype(float)
    y = table.column("y_fit").to_numpy(zero_copy_only=False).astype(float)
    values = pixel_columns(fits.getdata(image, 0), x, y, settings)
    for name in PIXEL_COLUMNS:
        table = table.append_column(name, pa.array(values[name], pa.float64()))
    pq.write_table(table, catalog)
    return table.num_rows


__all__: Sequence[str] = (
    "PIXEL_COLUMNS", "add_pixel_columns", "median_filter_truncate_stack", "nneg_nbad",
    "pixel_columns", "subtract_local_background", "sumrat",
)
