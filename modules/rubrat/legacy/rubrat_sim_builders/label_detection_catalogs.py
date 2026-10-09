#!/usr/bin/env python3
"""
Cross-match pipeline detection catalogs to simulation truth and write labels.

For each jid folder, truth is built from:
  1. ``Roman_TDS_index_*.txt`` when present (filter ``obj_type`` via ``--truth-obj-types``)
  2. ``Roman_TDS_*_lite_inject.txt`` when present (pipeline injections; always included)

If both exist (e.g. 20260520 variable-star injection runs), rows are concatenated so
detections can match OpenUniverse galaxies/stars/transients and injected sources.

Detection catalogs labeled (when present):
  - ``*_psfcat_finder.txt``  (DAOStarFinder; used by RuBR)
  - ``*_psfcat.parquet``     (PSF-fit; x_fit/y_fit)

Outputs per jid under ``--output-dir``:
  - ``truth_used.csv``       — truth table used for matching
  - ``<catalog_stem>_labeled.csv`` or ``.parquet`` (e.g. ``sfft_psfcat_labeled.parquet``)
  - ``labeling_summary.json`` at the run root

Use ``--catalog-stems`` to label only selected catalogs (e.g. ZOGY only)::

    PYTHONPATH=src python scripts/label_detection_catalogs.py \\
        --data-dir data/20260520 \\
        --output-dir data/hltds_zogy_labels_20260520 \\
        --catalog-stems zogy_psfcat,zogy_finder \\
        --match-radius-px 4.0 \\
        --mag-lim 26.0 \\
        --truth-obj-types transient

Example::

    rubrat labels build \\
        --data-dir /path/to/hltds-products \\
        --output-dir artifacts/labels/hltds \\
        --truth-obj-types transient
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from astropy.io import fits
from scipy.spatial import cKDTree

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ingestion.rubr_adapter import (
    label_detections,
    load_detection_catalog,
    load_truth_index_file,
)

log = logging.getLogger(__name__)

_INDEX_GLOB = "Roman_TDS_index_*.txt"
_INJECT_GLOB = "Roman_TDS_*_lite_inject.txt"

# Catalogs to label if files exist (glob pattern -> output stem).
_CATALOG_PATTERNS: list[tuple[str, str, str]] = [
    ("sfftdiffimage_masked_psfcat_finder.txt", "sfft_finder", "finder"),
    ("zogy_diffimage_masked_psfcat_finder.txt", "zogy_finder", "finder"),
    ("sfftdiffimage_masked_psfcat.parquet", "sfft_psfcat", "parquet"),
    ("zogy_diffimage_masked_psfcat.parquet", "zogy_psfcat", "parquet"),
]

KNOWN_CATALOG_STEMS = tuple(stem for _, stem, _ in _CATALOG_PATTERNS)


def _science_zeropoint(jid_dir: Path) -> float:
    """Read the per-exposure photometric zeropoint from the RAPID science image."""
    path = jid_dir / "bkg_subbed_science_image.fits"
    if not path.is_file():
        raise FileNotFoundError(f"Science image required for ZPT-corrected truth magnitudes: {path}")
    with fits.open(path, memmap=False, mode="readonly") as hdul:
        for hdu in hdul:
            for key in ("ZPTMAG", "MAGZP", "ZPT"):
                value = hdu.header.get(key)
                try:
                    zpt = float(value)
                except (TypeError, ValueError):
                    continue
                if np.isfinite(zpt):
                    return zpt
    raise ValueError(f"No finite ZPTMAG/MAGZP/ZPT header in {path}")


def _select_catalog_patterns(catalog_stems: set[str] | None) -> list[tuple[str, str, str]]:
    if catalog_stems is None:
        return list(_CATALOG_PATTERNS)
    unknown = sorted(catalog_stems - set(KNOWN_CATALOG_STEMS))
    if unknown:
        raise ValueError(f"Unknown --catalog-stems: {unknown}. Choose from: {', '.join(KNOWN_CATALOG_STEMS)}")
    return [item for item in _CATALOG_PATTERNS if item[1] in catalog_stems]


def _flux_to_mag(flux: np.ndarray) -> np.ndarray:
    flux = np.asarray(flux, dtype=float)
    return np.where(flux > 0, -2.5 * np.log10(np.maximum(flux, 1e-30)), 99.0)


def build_truth_for_jid(
    jid_dir: Path,
    *,
    image_width: int,
    image_height: int,
    truth_obj_types: set[str] | None,
    index_glob: str,
    inject_glob: str,
) -> tuple[pd.DataFrame, str]:
    """
    Build a label_detections-compatible truth table for one jid.

    Returns (truth_df, source_description).
    """
    parts: list[pd.DataFrame] = []
    sources: list[str] = []
    science_zpt = _science_zeropoint(jid_dir)

    index_files = sorted(jid_dir.glob(index_glob))
    if index_files:
        df = load_truth_index_file(index_files[0])
        if truth_obj_types and "obj_type" in df.columns:
            df = df[df["obj_type"].isin(truth_obj_types)].copy()
        elif truth_obj_types:
            log.warning(
                "%s: no obj_type column; cannot filter to %s",
                jid_dir.name,
                truth_obj_types,
            )
        for col in ("x", "y"):
            if col not in df.columns:
                raise ValueError(f"Truth index missing column {col!r} in {index_files[0]}")
        x = df["x"].to_numpy(dtype=float)
        y = df["y"].to_numpy(dtype=float)
        in_bounds = (x >= 0) & (x < image_width) & (y >= 0) & (y < image_height)
        df = df[in_bounds].reset_index(drop=True)
        if "mag" not in df.columns:
            if "flux" in df.columns:
                df["mag"] = _flux_to_mag(df["flux"].to_numpy())
            else:
                df["mag"] = 0.0
        if "zpt" not in df.columns:
            df["zpt"] = science_zpt
        else:
            df["zpt"] = pd.to_numeric(df["zpt"], errors="coerce").fillna(science_zpt)
        parts.append(df)
        sources.append(f"index:{index_files[0].name}")

    inject_files = sorted(jid_dir.glob(inject_glob))
    if inject_files:
        raw = pd.read_csv(
            inject_files[0],
            sep=r"\s+",
            comment="#",
            engine="python",
        )
        raw = raw.rename(columns={"xpix": "x", "ypix": "y"})
        if "x" not in raw.columns or "y" not in raw.columns:
            raise ValueError(f"Inject file {inject_files[0]} missing xpix/ypix columns: {list(raw.columns)}")
        x = raw["x"].to_numpy(dtype=float)
        y = raw["y"].to_numpy(dtype=float)
        in_bounds = (x >= 0) & (x < image_width) & (y >= 0) & (y < image_height)
        df = raw.loc[in_bounds].copy().reset_index(drop=True)
        flux = df["flux"].to_numpy(dtype=float) if "flux" in df.columns else np.ones(len(df))
        df["mag"] = _flux_to_mag(flux)
        df["zpt"] = science_zpt
        df["obj_type"] = "injected"
        parts.append(df)
        sources.append(f"inject:{inject_files[0].name}")

    if not parts:
        raise FileNotFoundError(f"No truth in {jid_dir}: need {_INDEX_GLOB} or {_INJECT_GLOB}")

    return pd.concat(parts, ignore_index=True), "+".join(sources)


def load_psf_parquet_catalog(path: Path) -> pd.DataFrame:
    """Load PSF-fit parquet as a finder-compatible detection table."""
    df = pd.read_parquet(path)
    if "x_fit" in df.columns:
        df = df.rename(columns={"x_fit": "xcentroid", "y_fit": "ycentroid"})
    elif "x_centroid" in df.columns:
        df = df.rename(columns={"x_centroid": "xcentroid", "y_centroid": "ycentroid"})
    for col in ("xcentroid", "ycentroid"):
        if col not in df.columns:
            raise ValueError(
                f"PSF parquet {path} missing position column (need x_fit or x_centroid). Columns: {list(df.columns)}"
            )
    if "npix" not in df.columns and "n_pixels" in df.columns:
        df = df.rename(columns={"n_pixels": "npix"})
    if "npix" not in df.columns and "n_pixels_fit" in df.columns:
        df["npix"] = df["n_pixels_fit"]
    for col in ("sharpness", "roundness1", "peak", "flux"):
        if col not in df.columns:
            df[col] = np.nan
    if "mag" not in df.columns and "flux" in df.columns:
        df["mag"] = _flux_to_mag(df["flux"].to_numpy())
    return df.reset_index(drop=True)


def label_detections_with_match(
    detections_df: pd.DataFrame,
    truth_df: pd.DataFrame,
    *,
    match_radius_px: float,
    mag_lim: float | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Same matching policy as :func:`label_detections`, plus metadata.

    Returns
    -------
    labels : (N,) int — 1 = matched to truth, 0 = bogus
    truth_idx : (N,) int — index into *truth_df* for matches, else -1
    match_dist_px : (N,) float — distance to matched truth, else nan
    """
    n_det = len(detections_df)
    labels = np.zeros(n_det, dtype=np.int64)
    truth_idx = np.full(n_det, -1, dtype=np.int64)
    match_dist = np.full(n_det, np.nan, dtype=np.float64)

    if n_det == 0 or len(truth_df) == 0:
        return labels, truth_idx, match_dist

    truth_mag_ab = truth_df["mag"].to_numpy(float) + truth_df["zpt"].to_numpy(float)
    eligible = np.isfinite(truth_mag_ab)
    if mag_lim is not None:
        eligible &= truth_mag_ab <= float(mag_lim)
    bright = truth_df[eligible].reset_index(drop=True)
    bright_orig_idx = truth_df.index[eligible].to_numpy(dtype=np.int64)
    if len(bright) == 0:
        return labels, truth_idx, match_dist

    det_coords = np.column_stack(
        [
            detections_df["xcentroid"].to_numpy(float),
            detections_df["ycentroid"].to_numpy(float),
        ]
    )
    finite = np.isfinite(det_coords).all(axis=1)
    if not finite.any():
        return labels, truth_idx, match_dist

    finite_idx = np.where(finite)[0]
    tree = cKDTree(det_coords[finite])

    truth_coords = np.column_stack(
        [
            bright["x"].to_numpy(float),
            bright["y"].to_numpy(float),
        ]
    )
    dists, tree_idx = tree.query(truth_coords, distance_upper_bound=match_radius_px)
    valid = dists < np.inf
    order = np.argsort(dists)
    assigned: set[int] = set()

    for i in order:
        if not valid[i]:
            break
        det_i = int(finite_idx[tree_idx[i]])
        if det_i not in assigned:
            labels[det_i] = 1
            truth_idx[det_i] = int(bright_orig_idx[i])
            match_dist[det_i] = float(dists[i])
            assigned.add(det_i)

    return labels, truth_idx, match_dist


def _attach_truth_columns(
    dets: pd.DataFrame,
    truth_df: pd.DataFrame,
    truth_idx: np.ndarray,
    match_dist: np.ndarray,
    labels: np.ndarray,
) -> pd.DataFrame:
    out = dets.copy()
    out["y"] = labels.astype(np.int64)
    out["match_distance_px"] = match_dist
    out["truth_row"] = truth_idx

    obj_type = np.full(len(out), "", dtype=object)
    truth_id = np.full(len(out), "", dtype=object)
    truth_mag_ab = np.full(len(out), np.nan, dtype=np.float64)
    truth_mag_instrumental = np.full(len(out), np.nan, dtype=np.float64)
    truth_zpt = np.full(len(out), np.nan, dtype=np.float64)

    for i in range(len(out)):
        tix = int(truth_idx[i])
        if tix < 0:
            continue
        row = truth_df.loc[tix]
        if "obj_type" in truth_df.columns:
            obj_type[i] = str(row.get("obj_type", ""))
        for id_col in ("inj_id", "object_id", "sicbro_id"):
            if id_col in truth_df.columns:
                truth_id[i] = str(row.get(id_col, ""))
                break
        truth_mag_instrumental[i] = float(row.get("mag", np.nan))
        truth_zpt[i] = float(row.get("zpt", np.nan))
        truth_mag_ab[i] = truth_mag_instrumental[i] + truth_zpt[i]

    out["truth_obj_type"] = obj_type
    out["truth_id"] = truth_id
    out["truth_mag_ab"] = truth_mag_ab
    out["truth_mag_instrumental"] = truth_mag_instrumental
    out["truth_zpt"] = truth_zpt
    # Compatibility alias. This is explicitly AB-corrected, never instrumental.
    out["truth_mag"] = truth_mag_ab
    return out


def label_jid(
    jid_dir: Path,
    out_dir: Path,
    *,
    image_width: int,
    image_height: int,
    truth_obj_types: set[str] | None,
    match_radius_px: float,
    mag_lim: float | None,
    index_glob: str,
    inject_glob: str,
    catalog_patterns: list[tuple[str, str, str]] | None = None,
) -> dict:
    """Label all catalogs for one jid. Returns summary dict."""
    jid = jid_dir.name
    jid_out = out_dir / jid
    jid_out.mkdir(parents=True, exist_ok=True)

    truth_df, truth_source = build_truth_for_jid(
        jid_dir,
        image_width=image_width,
        image_height=image_height,
        truth_obj_types=truth_obj_types,
        index_glob=index_glob,
        inject_glob=inject_glob,
    )
    truth_df.to_csv(jid_out / "truth_used.csv", index=False)

    summary: dict = {
        "jid": jid,
        "truth_source": truth_source,
        "n_truth": int(len(truth_df)),
        "catalogs": {},
    }

    patterns = catalog_patterns if catalog_patterns is not None else list(_CATALOG_PATTERNS)

    for pattern, stem, kind in patterns:
        matches = sorted(jid_dir.glob(pattern))
        if not matches:
            continue
        cat_path = matches[0]
        try:
            if kind == "finder":
                dets = load_detection_catalog(cat_path)
            else:
                dets = load_psf_parquet_catalog(cat_path)
        except Exception as exc:
            summary["catalogs"][stem] = {"path": str(cat_path), "error": str(exc)}
            log.warning("%s %s: failed to load — %s", jid, stem, exc)
            continue

        labels, tidx, mdist = label_detections_with_match(
            dets,
            truth_df,
            match_radius_px=match_radius_px,
            mag_lim=mag_lim,
        )
        # Sanity: should match label_detections
        # The legacy adapter intentionally retains its historical magnitude-26
        # default. Only use it as a cross-check when a label-time limit was
        # explicitly requested; the canonical research policy has no cutoff.
        ref = (
            label_detections(dets, truth_df, match_radius_px=match_radius_px, mag_lim=mag_lim)
            if mag_lim is not None
            else labels
        )
        if not np.array_equal(labels, ref):
            log.warning("%s %s: label_detections mismatch (using extended matcher)", jid, stem)

        labeled = _attach_truth_columns(dets, truth_df, tidx, mdist, labels)
        n_pos = int((labels == 1).sum())
        n_det = len(labels)

        if kind == "parquet":
            out_path = jid_out / f"{stem}_labeled.parquet"
            labeled.to_parquet(out_path, index=False)
        else:
            out_path = jid_out / f"{stem}_labeled.csv"
            labeled.to_csv(out_path, index=False)

        summary["catalogs"][stem] = {
            "path": str(cat_path),
            "output": str(out_path),
            "n_detections": n_det,
            "n_positive": n_pos,
            "positive_rate": round(n_pos / n_det, 4) if n_det else 0.0,
        }
        log.info(
            "%s %s: %d/%d positive (%.2f%%) → %s",
            jid,
            stem,
            n_pos,
            n_det,
            100.0 * n_pos / n_det if n_det else 0.0,
            out_path.name,
        )

    if not summary["catalogs"]:
        patterns_list = ", ".join(pattern for pattern, _, _ in patterns)
        raise FileNotFoundError(f"No catalogs labeled for {jid}; expected one of: {patterns_list}")

    return summary


def discover_jid_dirs(data_dir: Path) -> list[Path]:
    return sorted(p for p in data_dir.glob("jid*") if p.is_dir())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="Root with jid* subdirs (e.g. .../20260513)",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Where to write labeled catalogs and truth_used.csv",
    )
    p.add_argument("--match-radius-px", type=float, default=4.0)
    p.add_argument(
        "--mag-lim",
        type=float,
        default=None,
        help="Optional legacy label-time magnitude limit. Omit for research datasets; apply the mag<=26 policy at evaluation time.",
    )
    p.add_argument("--image-width", type=int, default=4090)
    p.add_argument("--image-height", type=int, default=4090)
    p.add_argument(
        "--truth-obj-types",
        default="transient",
        help=(
            "Comma-separated obj_type values to keep from Roman_TDS_index "
            "(default: transient). Use 'all' to keep every obj_type."
        ),
    )
    p.add_argument("--index-glob", default=_INDEX_GLOB)
    p.add_argument("--inject-glob", default=_INJECT_GLOB)
    p.add_argument("--max-jids", type=int, default=0, help="0 = all jids")
    p.add_argument(
        "--catalog-stems",
        default="",
        help=(f"Comma-separated catalog stems to label (default: all). Choices: {', '.join(KNOWN_CATALOG_STEMS)}"),
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    data_dir = args.data_dir.expanduser().resolve()
    out_dir = args.output_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.truth_obj_types.strip().lower() == "all":
        truth_obj_types = None
    else:
        truth_obj_types = {s.strip() for s in args.truth_obj_types.split(",") if s.strip()}

    if args.catalog_stems.strip():
        catalog_stems = {s.strip() for s in args.catalog_stems.split(",") if s.strip()}
        catalog_patterns = _select_catalog_patterns(catalog_stems)
    else:
        catalog_stems = None
        catalog_patterns = list(_CATALOG_PATTERNS)

    jid_dirs = discover_jid_dirs(data_dir)
    if args.max_jids > 0:
        jid_dirs = jid_dirs[: args.max_jids]

    if not jid_dirs:
        log.error("No jid* directories under %s", data_dir)
        return 1

    log.info(
        "Labeling %d jids under %s (catalogs: %s)",
        len(jid_dirs),
        data_dir,
        ", ".join(stem for _, stem, _ in catalog_patterns),
    )

    summaries: list[dict] = []
    errors: list[dict] = []

    for jid_dir in jid_dirs:
        try:
            summaries.append(
                label_jid(
                    jid_dir,
                    out_dir,
                    image_width=args.image_width,
                    image_height=args.image_height,
                    truth_obj_types=truth_obj_types,
                    match_radius_px=args.match_radius_px,
                    mag_lim=args.mag_lim,
                    index_glob=args.index_glob,
                    inject_glob=args.inject_glob,
                    catalog_patterns=catalog_patterns,
                )
            )
        except Exception as exc:
            log.error("%s: %s", jid_dir.name, exc)
            errors.append({"jid": jid_dir.name, "error": str(exc)})

    report = {
        "data_dir": str(data_dir),
        "output_dir": str(out_dir),
        "n_jids": len(jid_dirs),
        "n_ok": len(summaries),
        "n_errors": len(errors),
        "match_radius_px": args.match_radius_px,
        "label_time_mag_limit_ab": args.mag_lim,
        "magnitude_definition": "truth_mag_ab = mag + zpt",
        "truth_obj_types": list(truth_obj_types) if truth_obj_types else "all",
        "catalog_stems": sorted(catalog_stems) if catalog_stems else list(KNOWN_CATALOG_STEMS),
        "summaries": summaries,
        "errors": errors,
    }
    report_path = out_dir / "labeling_summary.json"
    report_path.write_text(json.dumps(report, indent=2))
    log.info("Wrote %s", report_path)

    if errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
