"""
rubr_adapter.py — FITS-to-RuBR Integration Adapter
=====================================================
This module is the middleman between raw Roman WFI FITS imagery and the
RuBR machine-learning pipeline (train/eval scripts that consume ``.npz``
archives).  It bridges three concerns that do **not** exist anywhere in the
RAPID ingestion pipeline:

1. **Truth catalog generation** — the per-JID ``truth.csv`` is not produced
   by RAPID; it must be built from ``Roman_TDS_index_*.txt`` simulation
   output files.

2. **Cutout construction** — given whole-frame science / reference / diff
   images (already loaded by the ingestion layer), extract per-detection
   64×64 (or custom) pixel stacks that match RuBR's ``X`` tensor contract.

3. **NPZ packaging** — assemble and write ``{X, y, feats, metadata}``
   archives in the exact layout expected by ``model/data.py:load_dataset``.

Quick-start
-----------
Typical single-JID workflow::

    from ingestion.fits_loader import load_fits
    from ingestion.rubr_adapter import (
        build_truth_catalog,
        load_detection_catalog,
        build_rubr_batch,
        save_rubr_npz,
    )

    # 1. Load the three FITS planes via the ingestion layer
    sci  = load_fits("bkg_subbed_science_image.fits")
    ref  = load_fits("awaicgen_output_mosaic_image_resampled_gainmatched.fits")
    diff = load_fits("diffimage_masked.fits")

    # 2. Load pre-computed detections
    dets = load_detection_catalog("diffimage_masked_psfcat_finder.txt")

    # 3. Build truth from the raw simulation index (no pre-existing truth.csv needed)
    truth = build_truth_catalog("jid12345/")

    # 4. Assemble and save the NPZ
    batch = build_rubr_batch(sci, ref, diff, dets, truth)
    save_rubr_npz(batch, "output/batch_0.npz")

For multi-JID accumulation, use :class:`RuBRBatchBuilder`::

    builder = RuBRBatchBuilder()
    for jid_folder, sci_path, ref_path, diff_path, detcat_path in my_iter():
        sci   = load_fits(sci_path)
        ref   = load_fits(ref_path)
        diff  = load_fits(diff_path)
        dets  = load_detection_catalog(detcat_path)
        truth = build_truth_catalog(jid_folder)
        builder.add(sci, ref, diff, dets, truth)
    saved_files = builder.flush("output/", max_per_file=200_000)

Public API
----------
Filter normalisation:
    FILTER_ALIAS_MAP
    normalize_filter_name

Detection catalogs:
    load_detection_catalog

Truth catalog — generation layer:
    load_truth_index_file
    build_truth_catalog
    generate_jid_truth_csv

Truth catalog — loading:
    load_truth_catalog
    load_rts_catalog

Labelling:
    label_detections

Cutout + feature helpers:
    extract_cutout
    build_feats_dict

Batch assembly:
    build_rubr_batch
    save_rubr_npz
    RuBRBatchBuilder

Notes
-----
* **No pixel normalisation is performed** before saving.  RuBR's training
  scripts apply their own filter-specific normalisation (RotInv) or
  per-dataset z-score (DANN/Control); the adapter replicates the raw-save
  convention of ``make_dataset_inj_sources.py``.
* The **three image channels** are stacked in REF / SCI / DIFF order,
  matching the original training data produced by ``make_dataset_inj_sources.py``.
* Truth-crossmatch tolerances differ intentionally: ``generate_jid_truth_csv``
  uses 3 px (matching ``process_detections.py``); ``label_detections`` uses
  4 px by default (matching ``make_dataset_inj_sources.py``).  Both are
  configurable.
"""

from __future__ import annotations

import logging
import re
from io import StringIO
from pathlib import Path
from typing import Optional, Union

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from .utils import apply_dq_mask

__all__ = [
    # Filter normalisation
    "FILTER_ALIAS_MAP",
    "normalize_filter_name",
    # Detection catalog
    "load_detection_catalog",
    # Truth generation
    "load_truth_index_file",
    "build_truth_catalog",
    "generate_jid_truth_csv",
    # Truth loading
    "load_truth_catalog",
    # RTS aggregate catalog (RAPID_RTS_products)
    "load_rts_catalog",
    # Labelling
    "label_detections",
    # Cutout + feats
    "extract_cutout",
    "build_feats_dict",
    "build_feats_dict_legacy",
    # Batch assembly
    "build_rubr_batch",
    "save_rubr_npz",
    "RuBRBatchBuilder",
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

# Columns required in a truth DataFrame for labelling
_TRUTH_LABEL_COLS = ("x", "y", "mag", "zpt")


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
# 3  Truth catalog — generation layer
# ---------------------------------------------------------------------------


def load_truth_index_file(path: Union[str, Path]) -> pd.DataFrame:
    """
    Load a ``Roman_TDS_index_*.txt`` simulation truth index into a DataFrame.

    These files are Astropy ASCII tables produced by the Roman TDS simulation
    and contain all simulated sources (transients, stars, galaxies) for a
    single exposure.  They are **not** filtered here; use
    :func:`build_truth_catalog` to restrict to in-bounds transients.

    Parameters
    ----------
    path : str or Path
        Path to ``Roman_TDS_index_*.txt``.

    Returns
    -------
    pandas.DataFrame
        All rows and columns from the file.  Expected columns include (but
        are not limited to) ``obj_type``, ``x``, ``y``, ``mag``, ``zpt``.
        Additional simulation columns are preserved as-is.

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.
    ValueError
        If Astropy cannot parse the file.

    Notes
    -----
    The file is read with ``astropy.table.Table.read(..., format="ascii")``,
    which matches the writer used by ``process_detections.py``.

    Examples
    --------
    >>> df = load_truth_index_file("Roman_TDS_index_H158_1_1.txt")
    >>> "obj_type" in df.columns
    True
    """
    try:
        from astropy.table import Table
    except ImportError as exc:  # pragma: no cover
        raise ImportError("astropy is required for load_truth_index_file") from exc

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Truth index file not found: {path}")

    try:
        tbl = Table.read(str(path), format="ascii")
    except Exception as exc:
        raise ValueError(f"Could not parse truth index file {path}: {exc}") from exc

    df = tbl.to_pandas()
    log.debug("load_truth_index_file: loaded %d rows from %s", len(df), path)
    return df


def build_truth_catalog(
    jid_folder: Union[str, Path],
    *,
    image_width: int = 4090,
    image_height: int = 4090,
    truth_file_glob: str = "Roman_TDS_index_*.txt",
) -> pd.DataFrame:
    """
    Build a filtered truth DataFrame from a JID folder's simulation index.

    This function replicates steps 1–5 of ``process_detections.py``:

    1. Glob for ``Roman_TDS_index_*.txt`` in *jid_folder*.
    2. Read the first match via :func:`load_truth_index_file`.
    3. Keep only rows where ``obj_type == "transient"``.
    4. Keep only rows with ``0 <= x < image_width`` and
       ``0 <= y < image_height``.

    The returned DataFrame is ready to pass directly to
    :func:`label_detections` (it has ``x``, ``y``, ``mag``, ``zpt`` columns).

    Parameters
    ----------
    jid_folder : str or Path
        Path to the JID directory (e.g. ``"/data/.../jid12345"``).
    image_width, image_height : int
        Pixel dimensions of the detector.  Defaults match the Roman WFI
        full-frame size used in the pipeline (4090 × 4090).
    truth_file_glob : str
        Glob pattern to locate the truth index file.  The first match is used.

    Returns
    -------
    pandas.DataFrame
        Filtered truth table with columns including ``x``, ``y``, ``mag``,
        ``zpt``, ``obj_type``, and any other columns in the index file.

    Raises
    ------
    FileNotFoundError
        If no file matching *truth_file_glob* exists in *jid_folder*.
    ValueError
        If the truth file cannot be parsed or lacks expected columns.

    Examples
    --------
    >>> truth = build_truth_catalog("/data/example/.../jid12345")
    >>> truth["obj_type"].unique()
    array(['transient'], dtype=object)
    """
    jid_folder = Path(jid_folder)
    matches = sorted(jid_folder.glob(truth_file_glob))
    if not matches:
        raise FileNotFoundError(f"No truth index file matching {truth_file_glob!r} found in {jid_folder}")

    df = load_truth_index_file(matches[0])

    # Filter to transients only
    if "obj_type" in df.columns:
        df = df[df["obj_type"] == "transient"].copy()
    else:
        log.warning(
            "build_truth_catalog: 'obj_type' column absent in %s; no obj_type filtering applied",
            matches[0],
        )

    # Filter to in-bounds positions
    for col in ("x", "y"):
        if col not in df.columns:
            raise ValueError(f"Truth index file {matches[0]} is missing column {col!r}")

    x = df["x"].to_numpy(dtype=float)
    y = df["y"].to_numpy(dtype=float)
    in_bounds = (x >= 0) & (x < image_width) & (y >= 0) & (y < image_height)
    df = df[in_bounds].reset_index(drop=True)

    log.debug(
        "build_truth_catalog: %d in-bounds transients from %s",
        len(df),
        matches[0],
    )
    return df


def generate_jid_truth_csv(
    jid_folder: Union[str, Path],
    sex_detections_df: pd.DataFrame,
    psf_detections_df: pd.DataFrame,
    *,
    image_width: int = 4090,
    image_height: int = 4090,
    tol_px: float = 3.0,
    output_path: Optional[Union[str, Path]] = None,
    sex_x_col: str = "x",
    sex_y_col: str = "y",
    psf_x_col: str = "xcentroid",
    psf_y_col: str = "ycentroid",
    truth_file_glob: str = "Roman_TDS_index_*.txt",
) -> pd.DataFrame:
    """
    Replicate ``process_detections.py`` to produce a per-JID ``truth.csv``.

    This function is **required** because ``truth.csv`` is not generated by
    the RAPID pipeline.  It must be produced once per JID folder so that
    downstream scripts (``make_training_data.py``, etc.) that expect it can
    function.

    The function:

    1. Calls :func:`build_truth_catalog` to get filtered in-bounds transients.
    2. Cross-matches each truth source against *sex_detections_df* and
       *psf_detections_df* within *tol_px* pixels, assigning columns
       ``match_s`` and ``match_p`` (1-based detection index, or -1).
    3. Prepends a ``jid`` column whose value is the integer extracted from
       the folder name (e.g. ``jid12345`` → 12345).
    4. Writes the result to ``jid_folder/truth.csv`` (or *output_path*) in
       Astropy CSV format with ``overwrite=True``.
    5. Returns the resulting DataFrame.

    Parameters
    ----------
    jid_folder : str or Path
        Path to the JID directory.
    sex_detections_df : pandas.DataFrame
        SExtractor detections.  Must contain *sex_x_col* and *sex_y_col*.
    psf_detections_df : pandas.DataFrame
        PSF-catalog detections (e.g. from ``load_detection_catalog``).
        Must contain *psf_x_col* and *psf_y_col*.
    image_width, image_height : int
        Forwarded to :func:`build_truth_catalog`.
    tol_px : float
        Spatial matching tolerance in pixels (default 3.0 — matches
        ``process_detections.py``).
    output_path : str, Path, or None
        Where to write ``truth.csv``.  Defaults to
        ``jid_folder / "truth.csv"``.
    sex_x_col, sex_y_col : str
        Column names for x/y positions in *sex_detections_df*.
    psf_x_col, psf_y_col : str
        Column names for x/y positions in *psf_detections_df*.
    truth_file_glob : str
        Forwarded to :func:`build_truth_catalog`.

    Returns
    -------
    pandas.DataFrame
        The truth table written to disk, including ``jid``, ``match_s``,
        ``match_p`` columns.

    Raises
    ------
    FileNotFoundError
        If the truth index file is not found (propagated from
        :func:`build_truth_catalog`).
    ValueError
        If required coordinate columns are absent in the detection DataFrames.

    Notes
    -----
    * ``match_s`` / ``match_p`` are **1-based** indices into the respective
      detection DataFrames (matching the ``process_detections.py`` convention
      where 1 = first detection).  A value of **-1** means no match within
      *tol_px*.
    * Multiple truth sources may claim the same detection; this is not
      de-duplicated here (``process_detections.py`` also does not de-duplicate
      the match columns, only ``label_detections`` enforces one-to-one when
      building training labels).

    Examples
    --------
    >>> truth_df = generate_jid_truth_csv(
    ...     "/data/.../jid12345",
    ...     sex_detections_df=sex_df,
    ...     psf_detections_df=psf_df,
    ... )
    >>> "match_s" in truth_df.columns and "match_p" in truth_df.columns
    True
    >>> truth_df.columns[0]
    'jid'
    """
    try:
        from astropy.table import Table
    except ImportError as exc:  # pragma: no cover
        raise ImportError("astropy is required for generate_jid_truth_csv") from exc

    jid_folder = Path(jid_folder)

    # Extract numeric JID from folder name
    m = re.search(r"(\d+)$", jid_folder.name)
    jid_int: int = int(m.group(1)) if m else -1

    truth_df = build_truth_catalog(
        jid_folder,
        image_width=image_width,
        image_height=image_height,
        truth_file_glob=truth_file_glob,
    )

    truth_x = truth_df["x"].to_numpy(dtype=float)
    truth_y = truth_df["y"].to_numpy(dtype=float)
    truth_coords = np.column_stack([truth_x, truth_y])

    def _assign_matches(det_df: pd.DataFrame, x_col: str, y_col: str) -> np.ndarray:
        """Return 1-based match indices (-1 if no match within tol_px)."""
        n_truth = len(truth_df)
        result = np.full(n_truth, -1, dtype=np.int64)
        if det_df.empty or n_truth == 0:
            return result
        for col in (x_col, y_col):
            if col not in det_df.columns:
                raise ValueError(f"Detection DataFrame is missing column {col!r}. Columns: {list(det_df.columns)}")
        det_coords = np.column_stack(
            [
                det_df[x_col].to_numpy(dtype=float),
                det_df[y_col].to_numpy(dtype=float),
            ]
        )
        # Drop NaN positions from the detection tree
        valid_mask = np.isfinite(det_coords).all(axis=1)
        valid_det = det_coords[valid_mask]
        valid_idx = np.where(valid_mask)[0]
        if len(valid_det) == 0:
            return result
        tree = cKDTree(valid_det)
        dists, tree_idx = tree.query(truth_coords, distance_upper_bound=tol_px)
        matched = dists < np.inf
        # tree_idx indexes into valid_det; map back to original DataFrame index
        result[matched] = valid_idx[tree_idx[matched]] + 1  # 1-based
        return result

    match_s = _assign_matches(sex_detections_df, sex_x_col, sex_y_col)
    match_p = _assign_matches(psf_detections_df, psf_x_col, psf_y_col)

    truth_df = truth_df.copy()
    truth_df["match_s"] = match_s
    truth_df["match_p"] = match_p

    # Prepend the jid column (first column, matching process_detections.py)
    jid_col = pd.Series(np.full(len(truth_df), jid_int, dtype=np.int64), name="jid")
    truth_df = pd.concat([jid_col, truth_df], axis=1)

    # Write truth.csv via Astropy to match process_detections.py output format
    out = Path(output_path) if output_path is not None else jid_folder / "truth.csv"
    tbl = Table.from_pandas(truth_df)
    tbl.write(str(out), format="csv", overwrite=True)
    log.info("generate_jid_truth_csv: wrote %d rows to %s", len(truth_df), out)

    return truth_df


# ---------------------------------------------------------------------------
# 4  Truth catalog — loading
# ---------------------------------------------------------------------------


def load_truth_catalog(path: Union[str, Path]) -> pd.DataFrame:
    """
    Load a pre-existing ``truth.csv`` into a :class:`pandas.DataFrame`.

    This function loads a CSV that was previously written by
    :func:`generate_jid_truth_csv` (or equivalently by
    ``process_detections.py``).  It is suitable for workflows where
    ``truth.csv`` files have already been generated and only need to be
    consumed.

    For workflows starting from raw ``Roman_TDS_index_*.txt`` files, use
    :func:`build_truth_catalog` or :func:`generate_jid_truth_csv` instead.

    Parameters
    ----------
    path : str or Path
        Path to ``truth.csv``.

    Returns
    -------
    pandas.DataFrame
        Full truth table.  Expected columns include ``x``, ``y``, ``mag``,
        ``zpt``.  Additional columns (``jid``, ``match_s``, ``match_p``,
        ``obj_type``, etc.) are preserved as-is.

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.
    ValueError
        If required columns (``x``, ``y``, ``mag``, ``zpt``) are absent.

    Examples
    --------
    >>> truth = load_truth_catalog("/data/.../jid12345/truth.csv")
    >>> {"x", "y", "mag", "zpt"}.issubset(truth.columns)
    True
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Truth catalog not found: {path}")

    df = pd.read_csv(path)

    for col in _TRUTH_LABEL_COLS:
        if col not in df.columns:
            raise ValueError(f"Truth catalog {path} is missing required column {col!r}. Columns: {list(df.columns)}")

    log.debug("load_truth_catalog: loaded %d rows from %s", len(df), path)
    return df


# ---------------------------------------------------------------------------
# 4b  RTS aggregate catalog — truth for rimtimsim_v2 / RAPID_RTS_products
# ---------------------------------------------------------------------------


def load_rts_catalog(
    catalog_path: Union[str, Path],
    filter_name: str = "F213",
    transient_id_threshold: int = 5_000_000,
) -> pd.DataFrame:
    """
    Load the RAPID RTS aggregate source catalog and return the injected
    transients as a :func:`label_detections`-compatible truth DataFrame.

    The RAPID RTS workflow (``RAPID_RTS_products/``) does not produce per-JID
    truth sidecar files.  Instead, all source information — including the
    1 000 injected transients — lives in a single **tab-separated** file
    ``catalog_F213.txt`` at the root of the products directory.  Sources with
    ``sicbro_id >= transient_id_threshold`` are the injected transients;
    everything below that threshold consists of Robby's background list
    (static and ~1 % variable sources that are **not** labelled as real
    transients).

    The returned DataFrame has exactly the columns expected by
    :func:`label_detections`:

    * ``x``   — mean pixel column position (from ``MEAN_XCOL``)
    * ``y``   — mean pixel row position (from ``MEAN_YCOL``)
    * ``mag`` — AB magnitude in the chosen filter (e.g. ``F213``)
    * ``zpt`` — always ``0.0`` (magnitudes in the catalog are already absolute
      AB magnitudes, so ``mag + zpt == mag``)

    Additional columns are preserved (``sicbro_id``, ``RA_DEG``, ``DEC_DEG``,
    and the boolean phenotype flags).

    Parameters
    ----------
    catalog_path : str or Path
        Path to ``catalog_F213.txt`` (or the equivalent file for another
        filter).  The file is tab-separated ASCII with a header row.
    filter_name : str
        Column name in the catalog whose value is used as the source
        magnitude.  Defaults to ``"F213"`` matching ``catalog_F213.txt``.
        Pass ``"F158"`` for an H-band catalog, etc.
    transient_id_threshold : int
        ``sicbro_id`` values **at or above** this threshold are treated as
        injected transients (label=1).  Defaults to ``5_000_000``, which is
        the convention used in the May 2026 RTS rimtimsim run.

    Returns
    -------
    pandas.DataFrame
        One row per injected transient with columns:
        ``x``, ``y``, ``mag``, ``zpt``, ``sicbro_id``, ``RA_DEG``,
        ``DEC_DEG``, plus any remaining catalog columns.
        Index is reset to 0…N-1.

    Raises
    ------
    FileNotFoundError
        If *catalog_path* does not exist.
    ValueError
        If the file cannot be parsed, the ``sicbro_id`` column is absent,
        or *filter_name* is not a column in the file.

    Notes
    -----
    The catalog is ~760 MB (≈ 4.97 M rows).  Reading it takes ~5–10 s on a
    typical NFS-mounted system.  **Read once and reuse** the returned
    DataFrame across all JIDs rather than re-reading per JID — the
    :func:`build_rubr_batches <scripts.build_rubr_batches>` CLI caches it
    automatically when ``--truth-mode rts`` is active.

    Examples
    --------
    >>> truth = load_rts_catalog(
    ...     "/path/to/RAPID_RTS_products/catalog_F213.txt"
    ... )
    >>> len(truth)   # 1000 injected transients
    1000
    >>> {"x", "y", "mag", "zpt"}.issubset(truth.columns)
    True
    >>> truth["zpt"].unique()
    array([0.])

    Using with :func:`label_detections`::

        from ingestion.rubr_adapter import load_rts_catalog, label_detections, load_detection_catalog

        truth = load_rts_catalog(
            "/path/to/RAPID_RTS_products/catalog_F213.txt"
        )
        dets = load_detection_catalog(
            "/path/to/RAPID_RTS_products/jid91950/"
            "sfftdiffimage_masked_psfcat_finder.txt"
        )
        labels = label_detections(dets, truth, match_radius_px=4.0, mag_lim=26.0)
    """
    catalog_path = Path(catalog_path)
    if not catalog_path.exists():
        raise FileNotFoundError(f"RTS catalog not found: {catalog_path}")

    log.debug("load_rts_catalog: reading %s …", catalog_path)
    try:
        df = pd.read_csv(catalog_path, sep="\t", low_memory=False)
    except Exception as exc:
        raise ValueError(f"Could not parse RTS catalog {catalog_path}: {exc}") from exc

    if "sicbro_id" not in df.columns:
        raise ValueError(f"RTS catalog {catalog_path} is missing 'sicbro_id' column. Columns: {list(df.columns)}")
    if filter_name not in df.columns:
        raise ValueError(
            f"RTS catalog {catalog_path} has no column '{filter_name}'. "
            f"Available magnitude columns: "
            f"{[c for c in df.columns if c.startswith('F')]}"
        )
    for col in ("MEAN_XCOL", "MEAN_YCOL"):
        if col not in df.columns:
            raise ValueError(
                f"RTS catalog {catalog_path} is missing position column '{col}'. Columns: {list(df.columns)}"
            )

    # Filter to injected transients
    transients = df[df["sicbro_id"] >= transient_id_threshold].copy()
    transients = transients.reset_index(drop=True)

    # Rename position and magnitude columns to the label_detections contract
    transients = transients.rename(
        columns={
            "MEAN_XCOL": "x",
            "MEAN_YCOL": "y",
            filter_name: "mag",
        }
    )

    # Add zero-point column: catalog magnitudes are already absolute AB
    transients["zpt"] = 0.0

    log.info(
        "load_rts_catalog: %d injected transients (sicbro_id >= %d) from %s, filter=%s",
        len(transients),
        transient_id_threshold,
        catalog_path,
        filter_name,
    )
    return transients


# ---------------------------------------------------------------------------
# 5  Labelling
# ---------------------------------------------------------------------------


def label_detections(
    detections_df: pd.DataFrame,
    truth_df: pd.DataFrame,
    *,
    match_radius_px: float = 4.0,
    mag_lim: float = 26.0,
) -> np.ndarray:
    """
    Assign binary Real/Bogus labels to a list of detections.

    A detection is labelled **1** (real transient) when a truth source
    satisfying ``mag + zpt <= mag_lim`` lies within *match_radius_px* pixels.
    All other detections are labelled **0** (bogus).

    The crossmatch is **one-to-one** from the truth side: each bright truth
    source is matched to its nearest detection; if two truth sources are
    nearest to the same detection, only the closer truth source claims it
    (the further one is left unmatched rather than double-labelling the
    detection).

    This logic mirrors ``make_dataset_inj_sources.py:crossmatch_detections``
    (default 4 px tolerance, ``MAG_LIM=26``).

    Parameters
    ----------
    detections_df : pandas.DataFrame
        Detection table with columns ``xcentroid`` and ``ycentroid``.
    truth_df : pandas.DataFrame
        Truth table with columns ``x``, ``y``, ``mag``, ``zpt``.  Typically
        the output of :func:`build_truth_catalog` or
        :func:`load_truth_catalog`.
    match_radius_px : float
        Maximum pixel separation for a truth-to-detection match (default 4.0).
    mag_lim : float
        Maximum total magnitude ``mag + zpt`` for a truth source to qualify
        (default 26.0, i.e. sources fainter than 26th magnitude are excluded).

    Returns
    -------
    numpy.ndarray, shape (N,), dtype int64
        Binary label array aligned with *detections_df* row order.
        ``1`` = real transient, ``0`` = bogus.

    Examples
    --------
    >>> labels = label_detections(detections_df, truth_df)
    >>> (labels == 1).sum()   # number of matched real transients
    5
    """
    n_det = len(detections_df)
    labels = np.zeros(n_det, dtype=np.int64)

    if n_det == 0 or len(truth_df) == 0:
        return labels

    # Filter truth to bright enough sources
    total_mag = truth_df["mag"].to_numpy(dtype=float) + truth_df["zpt"].to_numpy(dtype=float)
    bright_mask = total_mag <= mag_lim
    bright_truth = truth_df[bright_mask].reset_index(drop=True)
    if len(bright_truth) == 0:
        return labels

    # Build KD-tree on detection positions
    det_coords = np.column_stack(
        [
            detections_df["xcentroid"].to_numpy(dtype=float),
            detections_df["ycentroid"].to_numpy(dtype=float),
        ]
    )
    # Remove detections with non-finite positions from the tree
    finite_det = np.isfinite(det_coords).all(axis=1)
    if not finite_det.any():
        return labels
    finite_idx = np.where(finite_det)[0]
    tree = cKDTree(det_coords[finite_det])

    truth_coords = np.column_stack(
        [
            bright_truth["x"].to_numpy(dtype=float),
            bright_truth["y"].to_numpy(dtype=float),
        ]
    )

    # Query: for each truth source find nearest detection within radius
    dists, tree_idx = tree.query(truth_coords, distance_upper_bound=match_radius_px)

    # One-to-one: process truth sources from closest to farthest so that when
    # two truth sources compete for the same detection, the nearer one wins.
    valid = dists < np.inf
    order = np.argsort(dists)
    assigned: set[int] = set()
    for i in order:
        if not valid[i]:
            break  # remaining are all Inf (sorted)
        det_i = int(finite_idx[tree_idx[i]])
        if det_i not in assigned:
            labels[det_i] = 1
            assigned.add(det_i)

    return labels


# ---------------------------------------------------------------------------
# 6  Cutout extraction
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
# 7  Feature dict
# ---------------------------------------------------------------------------


def build_feats_dict_legacy(row: Union[dict, "pd.Series"]) -> dict:
    """
    Build the legacy 6-key feature dictionary expected by RuBR's model.

    The dictionary contains exactly the keys:
    ``flux``, ``mag``, ``npix``, ``peak``, ``roundness``, ``sharpness``
    (alphabetically sorted, which is also the order produced by
    ``sorted(feat_dict.keys())`` — matching ``train_rotinv_with_features.py``'s
    key coercion that calls ``sorted`` on dict keys before converting to array).

    ``mag`` falls back to 99.0 when ``flux <= 0`` (matching
    ``make_dataset_inj_sources.py``).

    Parameters
    ----------
    row : dict or pandas.Series
        A row from a detection catalog with keys / index labels including at
        least: ``flux``, ``npix``, ``peak``, ``roundness1``, ``sharpness``.
        ``mag`` is also accepted directly and takes precedence over the
        flux-based fallback.

    Returns
    -------
    dict
        ``{"flux": ..., "mag": ..., "npix": ..., "peak": ...,
        "roundness": ..., "sharpness": ...}``
        All values are Python scalars (``float`` or ``int``).

    Examples
    --------
    >>> row = {"flux": 5.0, "sharpness": 0.7, "roundness1": 0.3, "npix": 25, "peak": 0.9}
    >>> d = build_feats_dict(row)
    >>> sorted(d.keys())
    ['flux', 'mag', 'npix', 'peak', 'roundness', 'sharpness']
    >>> d["roundness"]
    0.3
    """

    def _get(key: str, default=0.0):
        if isinstance(row, dict):
            return row.get(key, default)
        return getattr(row, key, default) if hasattr(row, key) else default

    flux = float(_get("flux", 0.0))
    if "mag" in (row.keys() if isinstance(row, dict) else row.index.tolist() if hasattr(row, "index") else []):
        raw_mag = _get("mag", 99.0)
        mag = float(raw_mag) if np.isfinite(float(raw_mag)) else 99.0
    else:
        mag = float(-2.5 * np.log10(flux)) if flux > 0 else 99.0

    return {
        "flux": flux,
        "mag": mag,
        "npix": int(_get("npix", 0)),
        "peak": float(_get("peak", 0.0)),
        "roundness": float(_get("roundness1", 0.0)),
        "sharpness": float(_get("sharpness", 0.0)),
    }


def build_feats_dict(row: Union[dict, "pd.Series"]) -> dict:
    """
    Build the Phase 1 canonical 9-feature dictionary from a PSF-fit row.

    Feature order is defined by ``classification.data_utils.EXPECTED_FEATURE_NAMES``.
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


# ---------------------------------------------------------------------------
# 8  Batch assembly
# ---------------------------------------------------------------------------


def build_rubr_batch(
    sci: dict,
    ref: dict,
    diff: dict,
    detections_df: pd.DataFrame,
    truth_df: pd.DataFrame,
    *,
    cutout_size: int = 64,
    match_radius_px: float = 4.0,
    mag_lim: float = 26.0,
    use_dq_mask: bool = True,
    skip_nan_cutouts: bool = True,
) -> dict:
    """
    Assemble a RuBR-compatible NPZ data batch from loaded FITS planes.

    This is the **core integration function**.  It takes three image
    dictionaries (already loaded by the ingestion layer via
    :func:`~ingestion.fits_loader.load_fits` or
    :func:`~ingestion.data_ingestion.load_roman_image`), a detection catalog,
    and a truth DataFrame, and returns a ``dict`` that can be fed directly
    into :func:`save_rubr_npz` and then to any RuBR training or evaluation
    script.

    Image dict contract
    -------------------
    Each of *sci*, *ref*, *diff* must be a dict as returned by
    :func:`~ingestion.fits_loader.load_fits`, containing at minimum:

    * ``"data"`` — ``(H, W)`` float32 ndarray (the science image plane).
    * ``"dq"``   — ``(H, W)`` uint32 ndarray (data quality bitmask).
    * ``"filter"`` — filter name string (e.g. ``"F158"`` or ``"H158"``).
    * ``"mjd"``   — float, mid-exposure MJD.
    * ``"obs_id"`` — str, observation identifier.

    Output contract
    ---------------
    The returned dict has exactly four keys matching
    ``model/data.py:load_dataset``'s expected archive layout:

    ============  ===================================================
    ``X``         ``(N, 3, H, H)`` float64.  Channel order:
                  **[0] REF, [1] SCI, [2] DIFF**.
                  ``load_dataset`` auto-transposes to NHWC for TF.
    ``y``         ``(N,)`` int64.  1 = real transient, 0 = bogus.
    ``feats``     ``(N,)`` object.  Each element is a dict with sorted
                  keys ``flux``, ``mag``, ``npix``, ``peak``,
                  ``roundness``, ``sharpness``.
    ``metadata``  ``(N,)`` object.  Each element is a dict with keys:
                  ``id``, ``x``, ``y``, ``xcentroid``, ``ycentroid``,
                  ``filter``, ``mjd``, ``obs_id``, ``jid_folder``,
                  plus all other columns from *detections_df*.
    ============  ===================================================

    Parameters
    ----------
    sci : dict
        Science image dict from ``load_fits`` / ``load_roman_image``.
    ref : dict
        Reference image dict (e.g. ``awaicgen_output_mosaic_image_resampled_gainmatched.fits``).
    diff : dict
        Difference image dict (e.g. ``diffimage_masked.fits``).
    detections_df : pandas.DataFrame
        Detection catalog from :func:`load_detection_catalog`.
    truth_df : pandas.DataFrame
        Truth catalog from :func:`build_truth_catalog` or
        :func:`load_truth_catalog`.
    cutout_size : int
        Side length of each cutout in pixels (default 64).
    match_radius_px : float
        Truth-to-detection match radius passed to :func:`label_detections`
        (default 4.0 px).
    mag_lim : float
        Magnitude limit passed to :func:`label_detections` (default 26.0).
    use_dq_mask : bool
        If True (default), call ``apply_dq_mask`` on each image plane with
        bad pixels set to **0.0** before cutout extraction.  This prevents
        DQ-flagged pixels from corrupting the model input while keeping
        ``skip_nan_cutouts`` from discarding edge-padded cutouts.
    skip_nan_cutouts : bool
        If True (default), drop any detection whose cutout (in any channel)
        contains NaN values after extraction (can arise from NaN-filled
        science images).

    Returns
    -------
    dict
        ``{"X": ndarray, "y": ndarray, "feats": ndarray, "metadata": ndarray}``
        All arrays have the same first axis length N (number of valid detections
        after filtering).  Returns empty arrays (N=0) if there are no detections.

    Raises
    ------
    ValueError
        If the three image planes do not share the same spatial shape, or if
        required columns are missing from *detections_df*.

    Examples
    --------
    >>> from ingestion.fits_loader import load_fits
    >>> from ingestion.rubr_adapter import (
    ...     build_truth_catalog, load_detection_catalog, build_rubr_batch, save_rubr_npz
    ... )
    >>> sci  = load_fits("bkg_subbed_science_image.fits")
    >>> ref  = load_fits("awaicgen_output_mosaic_image_resampled_gainmatched.fits")
    >>> diff = load_fits("diffimage_masked.fits")
    >>> dets = load_detection_catalog("diffimage_masked_psfcat_finder.txt")
    >>> truth = build_truth_catalog("jid12345/")
    >>> batch = build_rubr_batch(sci, ref, diff, dets, truth)
    >>> batch["X"].shape   # (N, 3, 64, 64)
    (87, 3, 64, 64)
    """
    # --- shape consistency check ---
    sci_shape = sci["data"].shape
    ref_shape = ref["data"].shape
    diff_shape = diff["data"].shape
    if not (sci_shape == ref_shape == diff_shape):
        raise ValueError(f"Image plane shapes must match: sci={sci_shape}, ref={ref_shape}, diff={diff_shape}")

    # --- empty-detection fast path ---
    empty = {
        "X": np.empty((0, 3, cutout_size, cutout_size), dtype=np.float64),
        "y": np.empty(0, dtype=np.int64),
        "feats": np.empty(0, dtype=object),
        "metadata": np.empty(0, dtype=object),
    }
    if len(detections_df) == 0:
        return empty

    # --- validate required detection columns ---
    for col in _DET_REQUIRED_COLS:
        if col not in detections_df.columns:
            raise ValueError(
                f"detections_df is missing required column {col!r}. Columns: {list(detections_df.columns)}"
            )

    # --- optionally apply DQ masking (fill=0.0 to avoid NaN interference) ---
    def _prepare_plane(img_dict: dict) -> np.ndarray:
        data = img_dict["data"]
        dq = img_dict.get("dq")
        if use_dq_mask and dq is not None:
            data = apply_dq_mask(data, dq, bad_bits=1, fill=0.0)
        return data

    ref_plane = _prepare_plane(ref)
    sci_plane = _prepare_plane(sci)
    diff_plane = _prepare_plane(diff)

    # --- metadata from sci dict ---
    filter_alias = normalize_filter_name(sci.get("filter", "F184"))
    mjd = sci.get("mjd", float("nan"))
    obs_id = sci.get("obs_id", "")

    # --- label all detections ---
    labels = label_detections(
        detections_df,
        truth_df,
        match_radius_px=match_radius_px,
        mag_lim=mag_lim,
    )

    # --- extract cutouts ---
    X_list: list[np.ndarray] = []
    y_list: list[int] = []
    feats_list: list[dict] = []
    meta_list: list[dict] = []

    for idx, row in detections_df.iterrows():
        x_det = float(row["xcentroid"])
        y_det = float(row["ycentroid"])

        # Extract one cutout per channel
        cut_ref = extract_cutout(ref_plane, x_det, y_det, cutout_size)
        cut_sci = extract_cutout(sci_plane, x_det, y_det, cutout_size)
        cut_diff = extract_cutout(diff_plane, x_det, y_det, cutout_size)

        # Skip if any plane is completely outside the image
        if cut_ref is None or cut_sci is None or cut_diff is None:
            continue

        stack = np.stack([cut_ref, cut_sci, cut_diff], axis=0)  # (3, H, W)

        if skip_nan_cutouts and np.isnan(stack).any():
            continue

        # Detection integer position index in the original DataFrame
        i_local = detections_df.index.get_loc(idx)

        X_list.append(stack)
        y_list.append(int(labels[i_local]))
        feats_list.append(build_feats_dict_legacy(row))

        meta: dict = {
            "id": int(row.get("id", idx)) if "id" in row.index else int(idx),
            "x": float(row.get("x", x_det)) if "x" in row.index else x_det,
            "y": float(row.get("y", y_det)) if "y" in row.index else y_det,
            "xcentroid": x_det,
            "ycentroid": y_det,
            "filter": filter_alias,
            "mjd": float(mjd),
            "obs_id": str(obs_id),
            "jid_folder": str(sci.get("filename", "")),
        }
        # Carry through all detection columns for downstream use
        for col in detections_df.columns:
            if col not in meta:
                meta[col] = row[col]
        meta_list.append(meta)

    if not X_list:
        return empty

    X_arr = np.stack(X_list, axis=0).astype(np.float64)  # (N, 3, H, W)
    y_arr = np.array(y_list, dtype=np.int64)
    feats_arr = np.empty(len(feats_list), dtype=object)
    for i, d in enumerate(feats_list):
        feats_arr[i] = d
    meta_arr = np.empty(len(meta_list), dtype=object)
    for i, d in enumerate(meta_list):
        meta_arr[i] = d

    log.info(
        "build_rubr_batch: %d detections → %d valid cutouts (%d real, %d bogus), filter=%s",
        len(detections_df),
        len(X_list),
        int(y_arr.sum()),
        int((y_arr == 0).sum()),
        filter_alias,
    )
    return {"X": X_arr, "y": y_arr, "feats": feats_arr, "metadata": meta_arr}


# ---------------------------------------------------------------------------
# 9  NPZ saving
# ---------------------------------------------------------------------------


def save_rubr_npz(
    batch_dict: dict,
    output_path: Union[str, Path],
) -> None:
    """
    Save a batch dict to a ``.npz`` archive compatible with RuBR.

    Writes exactly the four arrays ``X``, ``y``, ``feats``, ``metadata``
    using :func:`numpy.savez` with ``allow_pickle=True`` semantics (object
    arrays require pickle).

    Parameters
    ----------
    batch_dict : dict
        As returned by :func:`build_rubr_batch` or
        :meth:`RuBRBatchBuilder.flush`.
    output_path : str or Path
        Destination path.  The ``.npz`` extension is appended by NumPy if
        absent.

    Examples
    --------
    >>> save_rubr_npz(batch, "/outputs/batch_0.npz")
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(str(output_path), **batch_dict)
    log.debug("save_rubr_npz: wrote %s", output_path)


# ---------------------------------------------------------------------------
# 10  Batch builder (multi-JID accumulator)
# ---------------------------------------------------------------------------


class RuBRBatchBuilder:
    """
    Stateful accumulator for building large NPZ batches from many JID folders.

    Use :meth:`add` to append the output of each per-JID
    :func:`build_rubr_batch` call.  When enough samples have accumulated,
    call :meth:`flush` to concatenate and write one or more ``batch_N.npz``
    files.

    Parameters
    ----------
    None — no constructor arguments.

    Attributes
    ----------
    _X, _y, _feats, _metadata : list
        Internal accumulators, one element per :meth:`add` call.

    Examples
    --------
    ::

        builder = RuBRBatchBuilder()
        for sci_p, ref_p, diff_p, det_p, jid in my_jids:
            sci   = load_fits(sci_p)
            ref   = load_fits(ref_p)
            diff  = load_fits(diff_p)
            dets  = load_detection_catalog(det_p)
            truth = build_truth_catalog(jid)
            builder.add(sci, ref, diff, dets, truth)

        saved = builder.flush("output/", max_per_file=200_000)
        print(f"Written {len(saved)} batch files")
    """

    def __init__(self) -> None:
        self._X: list[np.ndarray] = []
        self._y: list[np.ndarray] = []
        self._feats: list[np.ndarray] = []
        self._metadata: list[np.ndarray] = []

    def __len__(self) -> int:
        """Return the total number of samples accumulated so far."""
        return sum(a.shape[0] for a in self._X)

    def add(
        self,
        sci: dict,
        ref: dict,
        diff: dict,
        detections_df: pd.DataFrame,
        truth_df: pd.DataFrame,
        **build_kwargs,
    ) -> int:
        """
        Process one FITS triplet and append its cutouts to the accumulator.

        Parameters
        ----------
        sci, ref, diff : dict
            Image dicts from :func:`~ingestion.fits_loader.load_fits`.
        detections_df : pandas.DataFrame
            From :func:`load_detection_catalog`.
        truth_df : pandas.DataFrame
            From :func:`build_truth_catalog` or :func:`load_truth_catalog`.
        **build_kwargs
            Extra keyword arguments forwarded to :func:`build_rubr_batch`
            (e.g. ``cutout_size``, ``match_radius_px``, ``mag_lim``).

        Returns
        -------
        int
            Number of valid cutouts added from this call (may be 0 if all
            detections were discarded by the cutout filters).
        """
        batch = build_rubr_batch(sci, ref, diff, detections_df, truth_df, **build_kwargs)
        n = batch["X"].shape[0]
        if n > 0:
            self._X.append(batch["X"])
            self._y.append(batch["y"])
            self._feats.append(batch["feats"])
            self._metadata.append(batch["metadata"])
        return n

    def flush(
        self,
        output_dir: Union[str, Path],
        *,
        max_per_file: int = 200_000,
        prefix: str = "batch",
    ) -> list[Path]:
        """
        Concatenate accumulated arrays and write one or more NPZ files.

        Parameters
        ----------
        output_dir : str or Path
            Directory to write batch files into (created if absent).
        max_per_file : int
            Maximum number of samples per output file (default 200 000).
            If the total sample count exceeds this, multiple files are written
            named ``{prefix}_0.npz``, ``{prefix}_1.npz``, etc.
        prefix : str
            Filename stem for the output files (default ``"batch"``).

        Returns
        -------
        list[Path]
            Sorted list of paths to all files written.  Empty list if there
            are no accumulated samples.

        Notes
        -----
        The internal accumulator is **reset to empty** after a successful
        flush, so subsequent :meth:`add` calls start fresh.
        """
        if not self._X:
            log.warning("RuBRBatchBuilder.flush called with no accumulated data")
            return []

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        X_all = np.concatenate(self._X, axis=0)
        y_all = np.concatenate(self._y, axis=0)
        feats_all = np.concatenate(self._feats, axis=0)
        meta_all = np.concatenate(self._metadata, axis=0)

        total = X_all.shape[0]
        written: list[Path] = []
        file_idx = 0
        start = 0
        while start < total:
            end = min(start + max_per_file, total)
            out_path = output_dir / f"{prefix}_{file_idx}.npz"
            save_rubr_npz(
                {
                    "X": X_all[start:end],
                    "y": y_all[start:end],
                    "feats": feats_all[start:end],
                    "metadata": meta_all[start:end],
                },
                out_path,
            )
            written.append(out_path)
            log.info(
                "RuBRBatchBuilder.flush: wrote %d samples to %s",
                end - start,
                out_path,
            )
            start = end
            file_idx += 1

        # Reset accumulator
        self._X.clear()
        self._y.clear()
        self._feats.clear()
        self._metadata.clear()

        return written
