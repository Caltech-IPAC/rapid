"""
rubr_adapter.py — detection catalogs, cutouts, and features for the real/bogus classifier
==========================================================================================
The preprocessing that the classifier's training and inference must share, so that a
checkpoint is always fed exactly what it was trained on:

1. **Filter normalisation** — Roman filter names (``F158``) to the RAPID aliases the
   classifier's filter registry uses (``H158``).

2. **Detection catalog loading** — PhotUtils PSF-fit / DAOStarFinder catalogs (text or
   parquet) to a DataFrame with canonical column names.

3. **Cutout extraction** — a fixed-size, zero-padded pixel window centred on a detection,
   applied to the science, reference, and difference images alike.

4. **Feature construction** — the nine tabular features (see
   ``preprocessing.data_utils.EXPECTED_FEATURE_NAMES``) from the PSF-fit and finder columns.

The truth-catalog generation, labelling, and NPZ batch assembly that this module carried in
RuBR-AT (for the OpenUniverse and RImTimSim simulations) are kept, unmaintained, in
``legacy/rubrat_sim_builders/rubr_adapter_full.py``.

Public API
----------
Filter normalisation:
    FILTER_ALIAS_MAP
    normalize_filter_name

Detection catalogs:
    load_detection_catalog

Cutout + feature helpers:
    extract_cutout
    build_feats_dict
"""

from __future__ import annotations

import logging
import re
from io import StringIO
from pathlib import Path
from typing import Optional, Union

import numpy as np
import pandas as pd

__all__ = [
    # Filter normalisation
    "FILTER_ALIAS_MAP",
    "normalize_filter_name",
    # Detection catalog
    "load_detection_catalog",
    # Cutout + feats
    "extract_cutout",
    "build_feats_dict",
]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Mapping from standard Roman WFI filter names (used in FITS/ASDF headers)
#: to the short RAPID pipeline aliases that RuBR's normalisation tables use.
#:
#: RuBR's ``train_rotinv_with_features.py`` hardcodes per-filter mean/variance
#: statistics under alias keys; feeding a standard name (e.g. ``"F158"``) would
#: silently skip normalisation and break training.  Always pass image dicts
#: through :func:`normalize_filter_name` before building batches.
FILTER_ALIAS_MAP: dict[str, str] = {
    "F158": "H158",  # H-band  ≈ 1.58 μm
    "F129": "J129",  # J-band  ≈ 1.29 μm
    "F213": "K213",  # K-band  ≈ 2.13 μm
    "F062": "R062",  # R-band  ≈ 0.62 μm
    "F106": "Y106",  # Y-band  ≈ 1.06 μm
    "F087": "Z087",  # Z-band  ≈ 0.87 μm
    # Already-aliased names pass through unchanged
    "F184": "F184",
    "H158": "H158",
    "J129": "J129",
    "K213": "K213",
    "R062": "R062",
    "Y106": "Y106",
    "Z087": "Z087",
    # Wide-band filter — no common alias
    "F146": "F146",
}

# Required columns in a finder/detection catalog
_DET_REQUIRED_COLS = ("xcentroid", "ycentroid")
_DET_EXPECTED_COLS = (
    "xcentroid",
    "ycentroid",
    "sharpness",
    "roundness1",
    "npix",
    "peak",
    "flux",
)

def _normalize_detection_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add canonical aliases without creating duplicate DataFrame columns.

    RAPID's joined Parquet catalog contains both ``x_fit`` and
    ``x_centroid``.  The PSF-fit position is the training/inference position,
    so it takes precedence when the canonical column is absent.
    """
    result = df.copy()
    priorities = {
        "xcentroid": ("x_fit", "x_centroid"),
        "ycentroid": ("y_fit", "y_centroid"),
        "npixfit": ("n_pixels_fit",),
        "npix": ("n_pixels",),
    }
    for canonical, candidates in priorities.items():
        if canonical not in result.columns:
            source = next((name for name in candidates if name in result.columns), None)
            if source is not None:
                result[canonical] = result[source]
        aliases = [name for name in candidates if name in result.columns and name != canonical]
        if aliases:
            result = result.drop(columns=aliases)
    return result


# ---------------------------------------------------------------------------
# 1  Filter normalisation
# ---------------------------------------------------------------------------


def normalize_filter_name(name: str) -> str:
    """
    Convert a Roman WFI filter name to the RAPID alias expected by RuBR.

    Standard names from FITS/ASDF headers (e.g. ``"F158"``) are mapped to
    RAPID short aliases (e.g. ``"H158"``).  Names that are already aliases
    (e.g. ``"H158"``) pass through unchanged.

    Parameters
    ----------
    name : str
        Filter identifier string, e.g. ``"F158"`` or ``"H158"``.

    Returns
    -------
    str
        RAPID alias string ready for use as a ``metadata["filter"]`` value and
        as a key into RuBR's ``means`` / ``vars`` normalisation tables.

    Raises
    ------
    ValueError
        If *name* is not in :data:`FILTER_ALIAS_MAP`.

    Examples
    --------
    >>> normalize_filter_name("F158")
    'H158'
    >>> normalize_filter_name("H158")   # already an alias
    'H158'
    >>> normalize_filter_name("F184")   # same in both conventions
    'F184'
    """
    try:
        return FILTER_ALIAS_MAP[name]
    except KeyError:
        raise ValueError(f"Unknown filter name {name!r}. Known names: {sorted(FILTER_ALIAS_MAP)}")


# ---------------------------------------------------------------------------
# 2  Detection catalog loading
# ---------------------------------------------------------------------------


def load_detection_catalog(path: Union[str, Path]) -> pd.DataFrame:
    """
    Load a PSF-finder detection catalog into a :class:`pandas.DataFrame`.

    The function handles the two most common formats produced by the Roman
    transient pipeline:

    * **Whitespace-delimited text** (``diffimage_masked_psfcat_finder.txt``) —
      either with or without a leading ``#`` comment character on the header
      row, and with optional additional ``#`` comment lines anywhere in the
      file.
    * **CSV** (column-separated with comma, auto-detected).

    Column-name normalisation is applied automatically so that both the
    original DAOStarFinder format (``xcentroid``, ``ycentroid``, ``npix``)
    and the newer rimtimsim_v2 format (``x_centroid``, ``y_centroid``,
    ``n_pixels``) are accepted.  After loading, the DataFrame always uses
    the canonical names below.

    Required output columns (all must be present or derivable):

    ============  ============================================================
    ``xcentroid`` Detection centroid x-coordinate (pixel, 0-based).
    ``ycentroid`` Detection centroid y-coordinate (pixel, 0-based).
    ``sharpness`` DAO sharpness parameter.
    ``roundness1``DAO roundness parameter (one of two).
    ``npix``      Number of pixels in the source aperture.
    ``peak``      Peak pixel value.
    ``flux``      Integrated aperture flux.
    ============  ============================================================

    If ``flux`` is present but ``mag`` is not, an instrumental magnitude is
    computed as ``mag = -2.5 * log10(flux)`` (99.0 for non-positive flux).

    Parameters
    ----------
    path : str or Path
        Local filesystem path to the catalog file.

    Returns
    -------
    pandas.DataFrame
        DataFrame with at least the required columns listed above, plus any
        additional columns present in the file (e.g. ``roundness2``,
        ``daofind_mag``, ``mag``).

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.
    ValueError
        If the required positional columns (``xcentroid``, ``ycentroid``) are
        absent after loading.

    Examples
    --------
    >>> df = load_detection_catalog("diffimage_masked_psfcat_finder.txt")
    >>> df.columns.tolist()[:4]
    ['xcentroid', 'ycentroid', 'sharpness', 'roundness1']
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Detection catalog not found: {path}")

    if path.suffix.lower() in {".parquet", ".pqt"}:
        try:
            df = pd.read_parquet(path)
        except Exception as exc:
            raise ValueError(f"Could not parse detection catalog {path} as Parquet: {exc}") from exc
        df = _normalize_detection_columns(df)
        for col in _DET_REQUIRED_COLS:
            if col not in df.columns:
                raise ValueError(
                    f"Detection catalog {path} is missing required column {col!r}. Columns present: {list(df.columns)}"
                )
        if "mag" not in df.columns and "flux" in df.columns:
            flux = df["flux"].to_numpy(dtype=float)
            df = df.copy()
            df["mag"] = np.where(flux > 0, -2.5 * np.log10(np.maximum(flux, 1e-30)), 99.0)
        return df.reset_index(drop=True)

    # Sniff the delimiter from the first non-empty, non-comment line
    raw_lines = path.read_text().splitlines()
    first_data_line = ""
    first_comment_header: Optional[str] = None
    for line in raw_lines:
        stripped = line.lstrip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            candidate = stripped.lstrip("#").strip()
            if any(col in candidate for col in ("xcentroid", "x_fit", "ycentroid")):
                first_comment_header = candidate
        else:
            first_data_line = stripped
            break

    is_csv = "," in first_data_line

    df: Optional[pd.DataFrame] = None

    if is_csv:
        # --- plain comma-separated CSV ---
        try:
            df = pd.read_csv(path, comment="#")
        except Exception as exc:
            raise ValueError(f"Could not parse detection catalog {path} as CSV: {exc}") from exc
    else:
        # --- whitespace-delimited (with optional # comment header) ---
        try:
            if first_comment_header:
                # Re-parse: strip comment lines but use last comment line as header
                data_lines = [line for line in raw_lines if line.lstrip() and not line.lstrip().startswith("#")]
                header_cols = first_comment_header.split()
                df = pd.read_csv(
                    StringIO("\n".join([" ".join(header_cols)] + data_lines)),
                    sep=r"\s+",
                    engine="python",
                )
            else:
                df = pd.read_csv(path, sep=r"\s+", comment="#", engine="python")
        except Exception:
            df = None

    # --- last-resort: try plain CSV anyway ---
    if df is None:
        try:
            df = pd.read_csv(path)
        except Exception as exc:
            raise ValueError(f"Could not parse detection catalog {path}: {exc}") from exc

    # --- normalise column names (rimtimsim_v2 → canonical) ---
    df = _normalize_detection_columns(df)

    # --- validate required columns ---
    for col in _DET_REQUIRED_COLS:
        if col not in df.columns:
            raise ValueError(
                f"Detection catalog {path} is missing required column {col!r}. Columns present: {list(df.columns)}"
            )

    # --- derive mag from flux if absent ---
    if "mag" not in df.columns and "flux" in df.columns:
        flux = df["flux"].to_numpy(dtype=float)
        mag = np.where(flux > 0, -2.5 * np.log10(np.maximum(flux, 1e-30)), 99.0)
        df = df.copy()
        df["mag"] = mag

    log.debug("load_detection_catalog: loaded %d detections from %s", len(df), path)
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 3  Cutout extraction
# ---------------------------------------------------------------------------


def extract_cutout(
    image_2d: np.ndarray,
    x: float,
    y: float,
    size: int = 64,
    fill_value: float = 0.0,
) -> Optional[np.ndarray]:
    """
    Extract a square cutout centred on a detection position.

    The cutout is padded with *fill_value* when the source lies near the
    image boundary (partial cutout).  The centroid is rounded to the nearest
    integer pixel before slicing.

    Coordinate convention: *x* is the column direction (width) and *y* is
    the row direction (height), consistent with FITS ``xcentroid`` /
    ``ycentroid`` convention used by DAOStarFinder and the Roman pipeline.

    Parameters
    ----------
    image_2d : numpy.ndarray, shape (H, W)
        2-D image plane.  Will be cast to ``float64`` in the output.
    x, y : float
        Sub-pixel centroid coordinates (0-based pixel index).
    size : int
        Side length of the square cutout in pixels (default 64).
    fill_value : float
        Fill value used for out-of-bounds regions (default 0.0).

    Returns
    -------
    numpy.ndarray, shape (size, size), dtype float64
        Cutout array, padded if necessary.
    None
        If the centroid is completely outside the image bounds (no overlap
        between the cutout footprint and the image).

    Examples
    --------
    >>> import numpy as np
    >>> img = np.ones((128, 128), dtype=np.float32)
    >>> cut = extract_cutout(img, x=64, y=64, size=32)
    >>> cut.shape
    (32, 32)
    >>> cut = extract_cutout(img, x=0, y=0, size=64)   # near corner
    >>> cut.shape     # padded
    (64, 64)
    >>> extract_cutout(img, x=-50, y=-50, size=64)      # fully outside
    is None
    True
    """
    H, W = image_2d.shape
    cx = int(round(x))
    cy = int(round(y))

    # Fully outside check
    half = size // 2
    if cx + half <= 0 or cx - half >= W or cy + half <= 0 or cy - half >= H:
        return None

    # Source slice bounds in image coordinates
    y0_img = cy - half
    x0_img = cx - half
    y1_img = y0_img + size
    x1_img = x0_img + size

    # Corresponding slice bounds in the cutout (destination)
    dy0 = max(0, -y0_img)
    dx0 = max(0, -x0_img)
    dy1 = size - max(0, y1_img - H)
    dx1 = size - max(0, x1_img - W)

    # Clamped source bounds
    sy0 = max(0, y0_img)
    sx0 = max(0, x0_img)
    sy1 = min(H, y1_img)
    sx1 = min(W, x1_img)

    cutout = np.full((size, size), fill_value, dtype=np.float64)
    cutout[dy0:dy1, dx0:dx1] = image_2d[sy0:sy1, sx0:sx1].astype(np.float64)
    return cutout


# ---------------------------------------------------------------------------
# 4  Feature dict
# ---------------------------------------------------------------------------


def build_feats_dict(row: Union[dict, "pd.Series"]) -> dict:
    """
    Build the Phase 1 canonical 9-feature dictionary from a PSF-fit row.

    Feature order is defined by ``preprocessing.data_utils.EXPECTED_FEATURE_NAMES``.
    If ``flags != 0``, fit-dependent features are zero-filled while morphology
    features are preserved.
    """

    def _has(key: str) -> bool:
        if isinstance(row, dict):
            return key in row
        return hasattr(row, "index") and key in row.index

    def _get(key: str, default=0.0):
        if isinstance(row, dict):
            return row.get(key, default)
        return row[key] if _has(key) else default

    def _float(key: str, default=0.0) -> float:
        try:
            value = float(_get(key, default))
        except (TypeError, ValueError):
            value = default
        return float(np.nan_to_num(value, nan=default, posinf=default, neginf=default))

    flags = int(_float("flags", 1.0))
    flux_fit = _float("flux_fit")
    flux_err = max(_float("flux_err"), 1e-9)
    cfit = _float("cfit")
    reduced_chi2 = max(float(np.nan_to_num(_float("reduced_chi2"), nan=0.0)), 0.0)
    x_err = _float("x_err")
    y_err = _float("y_err")
    npixfit = _float("npixfit", _float("n_pixels_fit", _float("n_pixels", 1.0)))

    feats = {
        "arcsinh_snr": float(np.arcsinh(flux_fit / (3.0 * flux_err))),
        "cfit": cfit,
        "is_fit_clean": float(flags == 0),
        "log1p_chi2": float(np.log1p(reduced_chi2)),
        "log1p_pos_err": float(np.log1p(np.sqrt(x_err * x_err + y_err * y_err))),
        "log_npixfit": float(np.log(max(npixfit, 1.0))),
        "roundness1": _float("roundness1"),
        "roundness2": _float("roundness2"),
        "sharpness": _float("sharpness"),
    }
    if flags != 0:
        for key in ("arcsinh_snr", "cfit", "log1p_chi2", "log1p_pos_err"):
            feats[key] = 0.0
    return feats
