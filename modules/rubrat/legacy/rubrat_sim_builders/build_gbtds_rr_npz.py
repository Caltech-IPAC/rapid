#!/usr/bin/env python3
"""Build Phase 1 GBTDS RB or RR NPZ splits."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
from astropy.io import fits

log = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from classification.npz_builders import (  # noqa: E402
    GBTDS_FILES,
    ShardedNPZWriter,
    SpatialCornerSplit,
    arcsinh_stack_cutout,
    feature_row,
    filter_id,
    infer_filter_from_jid_dir,
    jid_num,
    load_rts_truth,
    load_split_json,
    parse_size_bytes,
    spatial_corner_mask_fn,
    spatial_corner_jid_split,
    truth_match_labels,
    write_split_report,
)
from ingestion.fits_loader import load_fits  # noqa: E402
from ingestion.rubr_adapter import load_detection_catalog  # noqa: E402

TRANSIENT_ID_THRESHOLD = 5_000_000
_RB_RATIO_CHOICES = {"3:7": (3, 7)}
PU_MAG_BOUNDARY = 26.0
PU_STATUS_UNLABELED = 0
PU_STATUS_POSITIVE = 1
PU_STATUS_TRUSTED_NEGATIVE = 2
GBTDS_TRUTH_BOGUS = 0
GBTDS_TRUTH_VARIABLE = 1
GBTDS_TRUTH_TRANSIENT = 2


def is_complete_science_jid(jid_dir: Path) -> bool:
    """Return whether a job contains every product needed for dataset construction."""
    required = (GBTDS_FILES["sci"], GBTDS_FILES["ref"], GBTDS_FILES["diff"])
    has_catalog = any(
        (jid_dir / name).exists()
        for name in ("sfftdiffimage_masked_psfcat.parquet", "sfftdiffimage_masked_psfcat.txt")
    )
    return has_catalog and all((jid_dir / name).exists() for name in required)


def truth_for_jid(jid_dir: Path, truth_tables: dict[str, pd.DataFrame]) -> tuple[str, pd.DataFrame]:
    """Select the truth magnitudes corresponding to a job's observing filter."""
    filt = infer_filter_from_jid_dir(jid_dir)
    truth = truth_tables.get(jid_dir.name)
    if truth is None:
        truth = truth_tables.get(filt)
    if truth is None:
        raise ValueError(f"No {filt} truth table was loaded for {jid_dir}")
    return filt, truth


def parse_lightcurve_specs(specs: list[str]) -> dict[str, Path]:
    """Parse repeatable ``FILTER=PATH`` light-curve arguments."""
    result: dict[str, Path] = {}
    for spec in specs:
        if "=" not in spec:
            raise ValueError(f"Invalid --lightcurve {spec!r}; expected FILTER=PATH")
        filt, raw_path = spec.split("=", 1)
        filt = filt.strip().upper()
        path = Path(raw_path).expanduser()
        if not filt or not raw_path.strip():
            raise ValueError(f"Invalid --lightcurve {spec!r}; expected FILTER=PATH")
        if filt in result:
            raise ValueError(f"Duplicate --lightcurve entry for {filt}")
        if not path.is_file():
            raise FileNotFoundError(f"Light-curve file not found for {filt}: {path}")
        result[filt] = path
    return result


def load_epoch_truth_tables(
    jid_dirs: list[Path],
    truth_by_filter: dict[str, pd.DataFrame],
    lightcurve_paths: dict[str, Path],
    *,
    time_tolerance_days: float = 1.0e-7,
) -> tuple[dict[str, pd.DataFrame], dict[str, dict]]:
    """Build per-exposure transient truth with light-curve AB magnitudes."""
    tables: dict[str, pd.DataFrame] = {}
    report: dict[str, dict] = {}
    for filt, base_truth in truth_by_filter.items():
        path = lightcurve_paths[filt]
        source_ids = base_truth["sicbro_id"].to_numpy(np.int64)
        source_columns = [str(sid) for sid in source_ids]
        curves = pd.read_parquet(path, columns=["OBS_TIME_BJD", *source_columns])
        times = curves["OBS_TIME_BJD"].to_numpy(float)
        fluxes = curves[source_columns].to_numpy(float)
        if not np.isfinite(times).all() or len(np.unique(times)) != len(times):
            raise ValueError(f"{path} has non-finite or duplicate OBS_TIME_BJD values")
        if not np.isfinite(fluxes).all() or not (fluxes > 0).all():
            raise ValueError(f"{path} has non-finite or non-positive transient fluxes")

        filter_jids = [jid_dir for jid_dir in jid_dirs if infer_filter_from_jid_dir(jid_dir) == filt]
        matched_epoch_rows: list[int] = []
        max_time_delta = 0.0
        for jid_dir in filter_jids:
            header = fits.getheader(jid_dir / GBTDS_FILES["sci"])
            if "MJD-OBS" not in header or "ZPTMAG" not in header:
                raise ValueError(f"{jid_dir} science header lacks MJD-OBS or ZPTMAG")
            bjd = float(header["MJD-OBS"]) + 2_400_000.5
            epoch_index = int(np.argmin(np.abs(times - bjd)))
            delta = float(abs(times[epoch_index] - bjd))
            if delta > time_tolerance_days:
                raise ValueError(
                    f"No light-curve epoch matches {jid_dir.name}: nearest {delta:.6g} days away"
                )
            zptmag = float(header["ZPTMAG"])
            epoch_flux = fluxes[epoch_index]
            epoch_truth = base_truth.copy()
            epoch_truth["mag"] = zptmag - 2.5 * np.log10(epoch_flux)
            epoch_truth["epoch_flux"] = epoch_flux
            epoch_truth["epoch_bjd"] = bjd
            epoch_truth["zptmag"] = zptmag
            tables[jid_dir.name] = epoch_truth
            matched_epoch_rows.append(epoch_index)
            max_time_delta = max(max_time_delta, delta)

        if len(filter_jids) != len(times) or set(matched_epoch_rows) != set(range(len(times))):
            raise ValueError(
                f"{filt} exposure/light-curve coverage mismatch: "
                f"{len(filter_jids)} jobs, {len(times)} epochs, "
                f"{len(set(matched_epoch_rows))} matched rows"
            )
        all_mags = np.concatenate([tables[jid.name]["mag"].to_numpy(float) for jid in filter_jids])
        report[filt] = {
            "file": path.name,
            "n_exposures": len(filter_jids),
            "n_transient_sources": len(source_ids),
            "max_time_delta_days": max_time_delta,
            "epoch_mag_min": float(np.min(all_mags)),
            "epoch_mag_max": float(np.max(all_mags)),
        }
    return tables, report


def _should_log_progress(index: int, total: int, verbose: int) -> bool:
    if total <= 0:
        return False
    if verbose >= 2:
        return True
    return index == 1 or index == total or index % 25 == 0


def load_variable_source_ids(catalog: Path) -> set[int]:
    """Catalog sources flagged as variables (``variable==1``, ``sicbro_id < 5M``)."""
    df = pd.read_csv(catalog, sep="\t", usecols=lambda c: c in {"sicbro_id", "variable"}, low_memory=False)
    if "variable" not in df.columns:
        log.warning("%s has no variable column; treating variable id set as empty.", catalog)
        return set()
    var_ids = df.loc[
        (df["sicbro_id"] < TRANSIENT_ID_THRESHOLD) & (df["variable"] == 1),
        "sicbro_id",
    ].astype(int)
    log.info("Loaded %d variable catalog source ids from %s", len(var_ids), catalog)
    return set(var_ids.to_list())


def classify_gbtds_detection(sid: int, *, variable_ids: set[int]) -> str:
    if sid < 0:
        return "unmatched"
    if sid >= TRANSIENT_ID_THRESHOLD:
        return "transient"
    if int(sid) in variable_ids:
        return "variable"
    return "other"


def cascade_labels_for_gbtds_detection(
    sid: int,
    *,
    variable_ids: set[int],
) -> tuple[int, int, int, str]:
    """Return C1, C2, 3-class truth, and kind for the GBTDS cascade task."""
    kind = classify_gbtds_detection(sid, variable_ids=variable_ids)
    if kind == "transient":
        return 1, -1, GBTDS_TRUTH_TRANSIENT, kind
    if kind == "variable":
        return 0, 1, GBTDS_TRUTH_VARIABLE, kind
    return 0, 0, GBTDS_TRUTH_BOGUS, kind


def _finite_float(value: object) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return out if np.isfinite(out) else float("nan")


def pu_status_for_gbtds_detection(
    *,
    sid: int,
    truth_mag: float,
    variable_ids: set[int],
    mag_boundary: float = PU_MAG_BOUNDARY,
) -> tuple[int, int, str, float, int]:
    """Return PU provenance for one GBTDS RB detection.

    Only confirmed injected transients are PU positives. Variables, unmatched
    rows, and other catalog matches remain unlabeled.
    """
    mag = _finite_float(truth_mag)
    mag_known = int(np.isfinite(mag))
    del mag_boundary  # Evaluation boundary only; never changes PU labels.
    if sid < 0:
        return 0, PU_STATUS_UNLABELED, "unmatched", mag, mag_known
    if sid >= TRANSIENT_ID_THRESHOLD:
        return 1, PU_STATUS_POSITIVE, "transient", mag, mag_known
    if int(sid) in variable_ids:
        return 0, PU_STATUS_UNLABELED, "variable", mag, mag_known
    return 0, PU_STATUS_UNLABELED, "other_catalog", mag, mag_known


def _read_psf_table(path: Path) -> pd.DataFrame:
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path, sep=r"\s+", comment="#", engine="python")


def load_gbtds_detections(jid_dir: Path) -> pd.DataFrame:
    psf_candidates = [
        jid_dir / "sfftdiffimage_masked_psfcat.parquet",
        jid_dir / "sfftdiffimage_masked_psfcat.txt",
    ]
    finder_candidates = [
        jid_dir / "sfftdiffimage_masked_psfcat_finder.parquet",
        jid_dir / "sfftdiffimage_masked_psfcat_finder.txt",
    ]
    psf_path = next((p for p in psf_candidates if p.exists()), None)
    finder_path = next((p for p in finder_candidates if p.exists()), None)
    if psf_path is None:
        raise FileNotFoundError(f"No sfft PSF catalog in {jid_dir}")
    psf = load_detection_catalog(psf_path)
    if finder_path is None:
        return psf
    finder = load_detection_catalog(finder_path)
    merged = psf.copy()
    for col in ("sharpness", "roundness1", "roundness2", "npix", "peak", "flux", "mag"):
        if col not in merged.columns and col in finder.columns and len(finder) == len(merged):
            merged[col] = finder[col].to_numpy()
    return merged


def write_jid(
    writer: ShardedNPZWriter,
    data_dir: Path,
    jid: str,
    image_size: int,
    truth_by_filter: dict[str, pd.DataFrame],
    task: str,
    radius_px: float,
    rb_real_kind: str,
    *,
    jid_split: str | None,
    spatial_corner: SpatialCornerSplit | None,
    active_splits: set[str],
    selected_bogus: set[tuple[str, int]] | None = None,
    included_rows: set[tuple[str, int]] | None = None,
    variable_ids: set[int] | None = None,
    verbose: int = 0,
) -> dict[str, dict]:
    jid_dir = data_dir / jid
    det = load_gbtds_detections(jid_dir)
    filt, truth = truth_for_jid(jid_dir, truth_by_filter)
    labels, match_ids = truth_match_labels(det, truth, radius_px=radius_px)
    truth_mag_by_id = {
        int(row.sicbro_id): _finite_float(row.mag)
        for row in truth.itertuples(index=False)
        if hasattr(row, "sicbro_id") and hasattr(row, "mag")
    }
    truth_flux_by_id = {
        int(row.sicbro_id): _finite_float(row.epoch_flux)
        for row in truth.itertuples(index=False)
        if hasattr(row, "sicbro_id") and hasattr(row, "epoch_flux")
    }
    sci = load_fits(jid_dir / GBTDS_FILES["sci"])
    ref = load_fits(jid_dir / GBTDS_FILES["ref"])
    diff = load_fits(jid_dir / GBTDS_FILES["diff"])
    stats_by_split: dict[str, dict] = {}

    def _stats(split_name: str) -> dict:
        if split_name not in stats_by_split:
            stats_by_split[split_name] = {
                "n": 0,
                "class_counts": {"0": 0, "1": 0},
                "rr_counts": {"0": 0, "1": 0},
                "cascade_c1_counts": {"0": 0, "1": 0},
                "cascade_c2_counts": {"0": 0, "1": 0, "-1": 0},
                "gbtds_truth_counts": {"0": 0, "1": 0, "2": 0},
                "truth_kind_counts": {"transient": 0, "variable": 0, "unmatched": 0, "other": 0},
                "jids": {jid},
            }
        return stats_by_split[split_name]

    for row_index, row in det.iterrows():
        x = float(row["xcentroid"])
        y = float(row["ycentroid"])
        if spatial_corner is not None:
            out_split = spatial_corner.output_split(jid, x, y, jid_split)
        else:
            out_split = jid_split
        if out_split is None or out_split not in active_splits:
            continue

        sid = int(match_ids[row_index])
        variable_ids_set = variable_ids if variable_ids is not None else set()
        is_transient = sid >= TRANSIENT_ID_THRESHOLD
        is_variable = 0 <= sid < TRANSIENT_ID_THRESHOLD and int(sid) in variable_ids_set
        det_kind = classify_gbtds_detection(
            sid,
            variable_ids=variable_ids_set,
        )
        row_key = (jid, int(row_index))

        if task == "rr" and not (is_transient or is_variable):
            continue
        if task == "rb" and rb_real_kind == "transient":
            is_real = is_transient
            if not is_real and (selected_bogus is None or row_key not in selected_bogus):
                continue
        elif task == "rb" and rb_real_kind == "corrected":
            if included_rows is not None and row_key not in included_rows:
                continue
            is_real = det_kind in {"transient", "variable"}
        elif task == "gbtds-cascade":
            if included_rows is not None and row_key not in included_rows:
                continue
            y_gbtds_c1, y_gbtds_c2, gbtds_truth_class, _ = cascade_labels_for_gbtds_detection(
                sid,
                variable_ids=variable_ids_set,
            )
            is_real = bool(y_gbtds_c1)
        else:
            is_real = bool(labels[row_index])
        if verbose >= 2:
            log.debug(
                "%s row=%d split=%s kind=%s sid=%d y=%d",
                jid,
                int(row_index),
                out_split,
                det_kind,
                sid,
                int(is_real),
            )
        stack = arcsinh_stack_cutout(sci["data"], ref["data"], diff["data"], x, y, image_size=image_size)
        if stack is None:
            continue
        pu_label, pu_status, pu_truth_kind, pu_truth_mag, pu_mag_known = pu_status_for_gbtds_detection(
            sid=sid,
            truth_mag=truth_mag_by_id.get(sid, np.nan),
            variable_ids=variable_ids_set,
        )
        metadata = {
            "row_id": f"{jid}:{int(row_index)}",
            "jid": jid,
            "xcentroid": x,
            "ycentroid": y,
            "det_id": int(row.get("id", row_index)),
            "row_index": int(row_index),
            "source_row_index": int(row_index),
            "mjd": float(sci.get("mjd", np.nan)),
            "filter": filt,
            "parquet_path": str(jid_dir / "sfftdiffimage_masked_psfcat.txt"),
            "truth_obj_type": (
                "transient" if is_transient else ("variable" if is_variable else ("unmatched" if sid < 0 else "other"))
            ),
            "truth_id": sid if sid >= 0 else "",
            "pu_label": pu_label,
            "pu_status": pu_status,
            "pu_status_name": {
                PU_STATUS_UNLABELED: "unlabeled",
                PU_STATUS_POSITIVE: "positive",
                PU_STATUS_TRUSTED_NEGATIVE: "trusted_negative",
            }[pu_status],
            "pu_truth_kind": pu_truth_kind,
            "pu_truth_mag": pu_truth_mag,
            "truth_epoch_flux": truth_flux_by_id.get(sid, np.nan),
            "truth_magnitude_source": "lightcurve_epoch" if truth_flux_by_id else "catalog_filter_mean",
            "pu_mag_boundary": PU_MAG_BOUNDARY,
            "pu_match_radius_px": float(radius_px),
            "pu_mag_known": pu_mag_known,
        }
        for col in det.columns:
            if col not in metadata:
                metadata[col] = row[col]
        sample = {
            "X": stack,
            "feats": feature_row(row),
            "y": int(is_real),
            "survey_id": 1,
            "filter_id": filter_id(filt),
            "metadata": metadata,
        }
        if task == "rb":
            sample["pu_label"] = pu_label
            sample["pu_status"] = pu_status
        if task == "rr":
            sample["y_rr"] = 1 if is_transient else 0
        if task == "gbtds-cascade":
            sample["y_gbtds_c1"] = int(y_gbtds_c1)
            sample["y_gbtds_c2"] = int(y_gbtds_c2)
            sample["gbtds_truth_class"] = int(gbtds_truth_class)
        writer.add(out_split, sample)
        stats = _stats(out_split)
        y = int(is_real)
        stats["n"] += 1
        stats["class_counts"][str(y)] += 1
        if task == "rr":
            stats["rr_counts"][str(sample["y_rr"])] += 1
        if task == "gbtds-cascade":
            stats["cascade_c1_counts"][str(sample["y_gbtds_c1"])] += 1
            stats["cascade_c2_counts"][str(sample["y_gbtds_c2"])] += 1
            stats["gbtds_truth_counts"][str(sample["gbtds_truth_class"])] += 1
            stats["truth_kind_counts"][det_kind] += 1
    return stats_by_split


def _parse_rb_ratio(value: str) -> tuple[int, int]:
    if value not in _RB_RATIO_CHOICES:
        raise ValueError(f"Unsupported RB ratio {value!r}; expected one of {sorted(_RB_RATIO_CHOICES)}")
    return _RB_RATIO_CHOICES[value]


def select_transient_rb_bogus(
    *,
    data_dir: Path,
    jid_dirs: Iterable[Path],
    split_map: dict[str, str],
    output_splits: list[str],
    truth_by_filter: dict[str, pd.DataFrame],
    radius_px: float,
    ratio: str,
    seed: int,
    spatial_corner: SpatialCornerSplit | None = None,
    verbose: int = 0,
) -> tuple[set[tuple[str, int]], dict[str, dict]]:
    """Select bogus rows so transient-only GBTDS RB has real:bogus = ratio."""
    real_part, bogus_part = _parse_rb_ratio(ratio)
    rng = np.random.default_rng(seed)
    neg_by_split: dict[str, list[tuple[str, np.ndarray]]] = {split: [] for split in output_splits}
    real_counts = {split: 0 for split in output_splits}

    jid_dirs = list(jid_dirs)
    for jid_index, jid_dir in enumerate(jid_dirs, start=1):
        jid = jid_dir.name
        jid_split = split_map.get(jid)
        det = load_gbtds_detections(data_dir / jid)
        _, truth = truth_for_jid(jid_dir, truth_by_filter)
        _, match_ids = truth_match_labels(det, truth, radius_px=radius_px)
        row_indices = np.arange(len(det), dtype=np.int64)
        if spatial_corner is not None:
            corner_mask = spatial_corner_mask_fn(
                spatial_corner.corner,
                width=spatial_corner.width,
                height=spatial_corner.height,
            )(
                det["xcentroid"].to_numpy(float),
                det["ycentroid"].to_numpy(float),
            )
        else:
            corner_mask = np.zeros(len(det), dtype=bool)
        for split in output_splits:
            if split == "test":
                split_mask = corner_mask
            elif split == jid_split:
                split_mask = ~corner_mask
            else:
                continue
            real_mask = match_ids >= TRANSIENT_ID_THRESHOLD
            real_counts[split] += int(np.count_nonzero(split_mask & real_mask))
            bogus_rows = row_indices[split_mask & ~real_mask]
            if len(bogus_rows):
                neg_by_split[split].append((jid, bogus_rows))
        if _should_log_progress(jid_index, len(jid_dirs), verbose):
            log.info(
                "RB selection scan %d/%d jobs (latest %s, n_det=%d)",
                jid_index,
                len(jid_dirs),
                jid,
                len(det),
            )

    selected: set[tuple[str, int]] = set()
    summary: dict[str, dict] = {}
    for split in output_splits:
        n_real = int(real_counts[split])
        target_bogus = int(np.floor(n_real * bogus_part / real_part)) if n_real > 0 else 0
        candidate_blocks = neg_by_split[split]
        n_candidates = int(sum(len(rows) for _, rows in candidate_blocks))
        n_pick = min(target_bogus, n_candidates)
        if n_pick:
            picks = np.sort(rng.choice(n_candidates, size=n_pick, replace=False))
            block_start = 0
            pick_start = 0
            for jid, rows in candidate_blocks:
                block_end = block_start + len(rows)
                pick_end = int(np.searchsorted(picks, block_end, side="left"))
                local = picks[pick_start:pick_end] - block_start
                selected.update((jid, int(rows[int(i)])) for i in local)
                block_start = block_end
                pick_start = pick_end
        summary[split] = {
            "rb_real_kind": "transient",
            "requested_real_bogus_ratio": ratio,
            "transient_real_candidates": n_real,
            "bogus_candidates": n_candidates,
            "selected_reals": n_real,
            "selected_bogus": int(n_pick),
            "bogus_limited_by_available": bool(n_pick < target_bogus),
        }
    return selected, summary


def select_corrected_rb_rows(
    *,
    data_dir: Path,
    jid_dirs: Iterable[Path],
    split_map: dict[str, str],
    output_splits: list[str],
    truth_by_filter: dict[str, pd.DataFrame],
    variable_ids: set[int],
    radius_px: float,
    seed: int,
    spatial_corner: SpatialCornerSplit | None = None,
    verbose: int = 0,
) -> tuple[set[tuple[str, int]], dict[str, dict]]:
    """Select all transients + matched variables (equal count) + all unmatched rows."""
    rng = np.random.default_rng(seed)
    by_split: dict[str, dict[str, list[tuple[str, int]]]] = {
        split: {"transient": [], "variable": [], "unmatched": []} for split in output_splits
    }

    jid_dirs = list(jid_dirs)
    total = len(jid_dirs)
    for idx, jid_dir in enumerate(jid_dirs, start=1):
        jid = jid_dir.name
        jid_split = split_map.get(jid)
        det = load_gbtds_detections(data_dir / jid)
        _, truth = truth_for_jid(jid_dir, truth_by_filter)
        _, match_ids = truth_match_labels(det, truth, radius_px=radius_px)
        for row_index, row in det.iterrows():
            if spatial_corner is not None:
                split = spatial_corner.output_split(
                    jid,
                    float(row["xcentroid"]),
                    float(row["ycentroid"]),
                    jid_split,
                )
            else:
                split = jid_split
            if split not in by_split:
                continue
            sid = int(match_ids[row_index])
            kind = classify_gbtds_detection(sid, variable_ids=variable_ids)
            if kind == "other":
                continue
            by_split[split][kind].append((jid, int(row_index)))
        if _should_log_progress(idx, total, verbose):
            log.info("Corrected RB scan %d/%d jids (latest %s, n_det=%d)", idx, total, jid, len(det))

    included: set[tuple[str, int]] = set()
    summary: dict[str, dict] = {}
    for split in output_splits:
        trans = by_split[split]["transient"]
        var = by_split[split]["variable"]
        unmatched = by_split[split]["unmatched"]
        n_trans = len(trans)
        n_var_pick = min(n_trans, len(var))
        selected_var: list[tuple[str, int]] = []
        if n_var_pick:
            picks = rng.choice(len(var), size=n_var_pick, replace=False)
            selected_var = [var[int(i)] for i in picks]
        included.update(trans)
        included.update(selected_var)
        included.update(unmatched)
        summary[split] = {
            "rb_real_kind": "corrected",
            "transient_rows": n_trans,
            "variable_candidates": len(var),
            "variable_rows_selected": n_var_pick,
            "variable_rows_target": n_trans,
            "variable_match_transient_count": bool(n_var_pick == n_trans),
            "unmatched_rows": len(unmatched),
            "other_catalog_rows_excluded": "not retained",
            "selected_total": len(trans) + n_var_pick + len(unmatched),
            "selected_real": len(trans) + n_var_pick,
            "selected_bogus": len(unmatched),
        }
        log.info(
            "%s corrected RB: transient=%d variable=%d/%d unmatched=%d total=%d",
            split,
            n_trans,
            n_var_pick,
            len(var),
            len(unmatched),
            summary[split]["selected_total"],
        )
        if n_var_pick < n_trans:
            log.warning(
                "%s corrected RB: only %d variable rows available for %d transients",
                split,
                len(var),
                n_trans,
            )
    return included, summary


def select_gbtds_cascade_rows(
    *,
    data_dir: Path,
    jid_dirs: Iterable[Path],
    split_map: dict[str, str],
    output_splits: list[str],
    truth_by_filter: dict[str, pd.DataFrame],
    variable_ids: set[int],
    radius_px: float,
    image_size: int,
    seed: int,
    target_transients: int,
    target_variables: int,
    target_bogus: int,
    bogus_kind: str = "all",
    spatial_corner: SpatialCornerSplit | None = None,
    verbose: int = 0,
) -> tuple[set[tuple[str, int]], dict[str, dict]]:
    """Select a fixed-size GBTDS cascade subset from cutout-valid rows."""
    targets = {
        "transient": int(target_transients),
        "variable": int(target_variables),
        "bogus": int(target_bogus),
    }
    if any(value < 0 for value in targets.values()):
        raise ValueError(f"Cascade target counts must be non-negative, got {targets}")
    if bogus_kind not in {"all", "unmatched", "other"}:
        raise ValueError(f"Unsupported cascade bogus kind {bogus_kind!r}")
    rng = np.random.default_rng(seed)
    candidates: dict[str, list[tuple[str, int, str]]] = {
        "transient": [],
        "variable": [],
        "bogus": [],
    }
    jid_dirs = list(jid_dirs)
    active = set(output_splits)

    for idx, jid_dir in enumerate(jid_dirs, start=1):
        jid = jid_dir.name
        jid_split = split_map.get(jid)
        det = load_gbtds_detections(data_dir / jid)
        _, truth = truth_for_jid(jid_dir, truth_by_filter)
        _, match_ids = truth_match_labels(det, truth, radius_px=radius_px)
        sci = load_fits(jid_dir / GBTDS_FILES["sci"])["data"]
        ref = load_fits(jid_dir / GBTDS_FILES["ref"])["data"]
        diff = load_fits(jid_dir / GBTDS_FILES["diff"])["data"]
        for row_index, row in det.iterrows():
            if spatial_corner is not None:
                split = spatial_corner.output_split(
                    jid,
                    float(row["xcentroid"]),
                    float(row["ycentroid"]),
                    jid_split,
                )
            else:
                split = jid_split
            if split not in active:
                continue
            sid = int(match_ids[row_index])
            _, _, truth_class, det_kind = cascade_labels_for_gbtds_detection(
                sid,
                variable_ids=variable_ids,
            )
            bucket = (
                "transient"
                if truth_class == GBTDS_TRUTH_TRANSIENT
                else "variable"
                if truth_class == GBTDS_TRUTH_VARIABLE
                else "bogus"
            )
            if bucket == "bogus" and bogus_kind != "all" and det_kind != bogus_kind:
                continue
            stack = arcsinh_stack_cutout(
                sci,
                ref,
                diff,
                float(row["xcentroid"]),
                float(row["ycentroid"]),
                image_size=image_size,
            )
            if stack is None:
                continue
            candidates[bucket].append((jid, int(row_index), str(split)))
        if _should_log_progress(idx, len(jid_dirs), verbose):
            log.info(
                "Cascade subset scan %d/%d jids: transient=%d variable=%d bogus=%d",
                idx,
                len(jid_dirs),
                len(candidates["transient"]),
                len(candidates["variable"]),
                len(candidates["bogus"]),
            )

    selected: set[tuple[str, int]] = set()
    summary: dict[str, dict] = {}
    for bucket, target in targets.items():
        available = candidates[bucket]
        if len(available) < target:
            raise ValueError(
                f"Requested {target} GBTDS cascade {bucket} rows, but only "
                f"{len(available)} cutout-valid candidates are available."
            )
        if target:
            picks = rng.choice(len(available), size=target, replace=False)
            chosen = [available[int(i)] for i in picks]
        else:
            chosen = []
        selected.update((jid, row_index) for jid, row_index, _ in chosen)
        by_split = {split: 0 for split in output_splits}
        for _, _, split in chosen:
            by_split[split] = by_split.get(split, 0) + 1
        summary[bucket] = {
            "target": target,
            "available_cutout_valid": len(available),
            "selected": len(chosen),
            "selected_by_split": by_split,
        }
        if bucket == "bogus":
            summary[bucket]["bogus_kind"] = bogus_kind
    summary["total_selected"] = {
        "target": int(sum(targets.values())),
        "selected": int(len(selected)),
        "seed": int(seed),
    }
    return selected, summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", required=True, type=Path)
    p.add_argument("--catalog", required=True, type=Path)
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--image-size", type=int, default=64)
    p.add_argument("--task", choices=["rb", "rr", "gbtds-cascade"], required=True)
    p.add_argument(
        "--rb-real-kind",
        choices=["catalog", "transient", "corrected"],
        default="catalog",
        help=(
            "For --task rb: catalog = any matched catalog source is real; "
            "transient = only sicbro_id >= 5000000 is real; "
            "corrected = all injected transients + equal-count variables (real) "
            "+ all unmatched detections (bogus); other catalog matches excluded."
        ),
    )
    p.add_argument("--rb-real-bogus-ratio", choices=sorted(_RB_RATIO_CHOICES), default="3:7")
    p.add_argument("--cascade-target-transients", type=int, default=None)
    p.add_argument("--cascade-target-variables", type=int, default=None)
    p.add_argument("--cascade-target-bogus", type=int, default=None)
    p.add_argument(
        "--cascade-bogus-kind",
        choices=["all", "unmatched", "other"],
        default="all",
        help=(
            "For --task gbtds-cascade quota sampling, choose which bogus rows "
            "are eligible: all, unmatched only, or other catalog matches only."
        ),
    )
    p.add_argument("--split", choices=["spatial-corner"], default="spatial-corner")
    p.add_argument("--split-json", type=Path)
    p.add_argument("--split-key", choices=["train", "val", "test"])
    p.add_argument("--truth-match-radius-px", type=float, default=2.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--test-corner", default="bottom_right")
    p.add_argument(
        "--image-width",
        type=int,
        default=None,
        help="Full detector/image width for spatial-corner split if FITS shape inference fails.",
    )
    p.add_argument(
        "--image-height",
        type=int,
        default=None,
        help="Full detector/image height for spatial-corner split if FITS shape inference fails.",
    )
    p.add_argument("--rts-filter", default="F213")
    p.add_argument(
        "--lightcurve",
        action="append",
        default=[],
        metavar="FILTER=PATH",
        help="Run-specific wide light curve; repeat once per detected filter.",
    )
    p.add_argument("--max-shard-size", default="8gb", help="Maximum estimated uncompressed shard size; keep <=10gb.")
    p.add_argument(
        "-v", "--verbose", action="count", default=0, help="Increase logging (-v INFO progress, -vv DEBUG per-row)."
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose >= 2 else logging.INFO if args.verbose >= 1 else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    all_jid_dirs = sorted([p for p in args.data_dir.glob("jid*") if p.is_dir()], key=jid_num)
    if not all_jid_dirs:
        p.error(
            f"No jid* directories found under --data-dir {args.data_dir}. "
            "Pass the directory that directly contains jid folders, for example "
            "/path/to/.../jid91915, not an individual jid directory or a parent "
            "above the jid collection."
        )
    jid_dirs = [jid_dir for jid_dir in all_jid_dirs if is_complete_science_jid(jid_dir)]
    skipped_jids = [jid_dir.name for jid_dir in all_jid_dirs if jid_dir not in jid_dirs]
    if not jid_dirs:
        p.error(f"No complete science jobs found under --data-dir {args.data_dir}")
    if skipped_jids:
        log.warning("Excluding %d incomplete or reference-only jobs: %s", len(skipped_jids), ", ".join(skipped_jids))
    jid_filters = {jid_dir.name: infer_filter_from_jid_dir(jid_dir) for jid_dir in jid_dirs}
    filter_counts = dict(sorted(Counter(jid_filters.values()).items()))
    unknown_filters = sorted(filt for filt in set(jid_filters.values()) if filter_id(filt) < 0)
    if unknown_filters:
        p.error(f"Unknown filters in science jobs: {', '.join(unknown_filters)}")
    log.info("Complete science jobs by filter: %s", filter_counts)
    if (args.image_width is None) != (args.image_height is None):
        p.error("--image-width and --image-height must be provided together")
    spatial_corner = None
    if args.split_json:
        if not args.split_key:
            p.error("--split-key is required with --split-json")
        split_map = load_split_json(args.split_json, args.split_key)
        output_splits = [args.split_key]
    else:
        split_map = spatial_corner_jid_split(args.data_dir, jid_dirs, seed=args.seed, test_corner=args.test_corner)
        if args.image_width is not None and args.image_height is not None:
            spatial_corner = SpatialCornerSplit(
                width=int(args.image_width),
                height=int(args.image_height),
                corner=args.test_corner,
            )
        else:
            try:
                spatial_corner = SpatialCornerSplit.from_jid_dirs(jid_dirs, corner=args.test_corner)
            except FileNotFoundError as exc:
                example_jids = ", ".join(j.name for j in jid_dirs[:3])
                p.error(
                    f"{exc} Looked under --data-dir {args.data_dir} in jid dirs "
                    f"like: {example_jids}. Expected each jid directory to contain "
                    f"{GBTDS_FILES['sci']!r}. If your files are valid but image "
                    "shape cannot be inferred, rerun with --image-width and "
                    "--image-height, e.g. --image-width 4088 --image-height 4088."
                )
        output_splits = ["train", "val", "test"]
        manifest = {
            "split": "spatial_corner",
            "test_assignment": "detection_centroid",
            "test_corner": args.test_corner,
            "truth_match_radius_px": args.truth_match_radius_px,
            "image_width": spatial_corner.width,
            "image_height": spatial_corner.height,
            "jids": {name: sorted([j for j, s in split_map.items() if s == name]) for name in ("train", "val")},
            "science_job_filter_counts": filter_counts,
            "excluded_jobs": skipped_jids,
            "truth_catalog": args.catalog.name,
            "lightcurve_files": sorted(Path(spec.split("=", 1)[1]).name for spec in args.lightcurve),
        }
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "split_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    detected_filters = sorted(set(jid_filters.values()))
    truth_by_filter = {filt: load_rts_truth(args.catalog, filt) for filt in detected_filters}
    truth_match_scope = "all_catalog_sources"
    if args.task == "rb" and args.rb_real_kind == "transient":
        # Only injected transients can be positive under this label policy.
        # Removing the millions of non-transient catalog rows leaves every RB
        # label unchanged and makes per-exposure matching tractable.
        truth_by_filter = {
            filt: truth.loc[truth["sicbro_id"] >= TRANSIENT_ID_THRESHOLD].reset_index(drop=True)
            for filt, truth in truth_by_filter.items()
        }
        truth_match_scope = "injected_transients"
        log.info(
            "Transient-only truth rows by filter: %s",
            {filt: len(truth) for filt, truth in truth_by_filter.items()},
        )
    try:
        lightcurve_paths = parse_lightcurve_specs(args.lightcurve)
    except (ValueError, FileNotFoundError) as exc:
        p.error(str(exc))
    lightcurve_report = None
    truth_tables = truth_by_filter
    if lightcurve_paths:
        missing_lightcurves = sorted(set(detected_filters) - set(lightcurve_paths))
        extra_lightcurves = sorted(set(lightcurve_paths) - set(detected_filters))
        if missing_lightcurves or extra_lightcurves:
            p.error(
                f"Light-curve filters do not match science filters; "
                f"missing={missing_lightcurves}, extra={extra_lightcurves}"
            )
        truth_tables, lightcurve_report = load_epoch_truth_tables(
            jid_dirs,
            truth_by_filter,
            lightcurve_paths,
        )
        log.info("Matched run-specific light curves: %s", lightcurve_report)
    variable_ids: set[int] | None = None
    if args.task in {"rb", "gbtds-cascade"}:
        variable_ids = load_variable_source_ids(args.catalog)

    selected_bogus = None
    rb_transient_summary = None
    included_rows: set[tuple[str, int]] | None = None
    rb_corrected_summary = None
    cascade_subset_summary = None
    if args.task == "rb" and args.rb_real_kind == "transient":
        selected_bogus, rb_transient_summary = select_transient_rb_bogus(
            data_dir=args.data_dir,
            jid_dirs=jid_dirs,
            split_map=split_map,
            output_splits=output_splits,
            truth_by_filter=truth_tables,
            radius_px=args.truth_match_radius_px,
            ratio=args.rb_real_bogus_ratio,
            seed=args.seed,
            spatial_corner=spatial_corner,
            verbose=args.verbose,
        )
    elif args.task == "rb" and args.rb_real_kind == "corrected":
        included_rows, rb_corrected_summary = select_corrected_rb_rows(
            data_dir=args.data_dir,
            jid_dirs=jid_dirs,
            split_map=split_map,
            output_splits=output_splits,
            truth_by_filter=truth_tables,
            variable_ids=variable_ids or set(),
            radius_px=args.truth_match_radius_px,
            seed=args.seed,
            spatial_corner=spatial_corner,
            verbose=args.verbose,
        )
    elif args.task == "gbtds-cascade" and any(
        value is not None
        for value in (
            args.cascade_target_transients,
            args.cascade_target_variables,
            args.cascade_target_bogus,
        )
    ):
        if None in (
            args.cascade_target_transients,
            args.cascade_target_variables,
            args.cascade_target_bogus,
        ):
            p.error(
                "--cascade-target-transients, --cascade-target-variables, and "
                "--cascade-target-bogus must be provided together"
            )
        included_rows, cascade_subset_summary = select_gbtds_cascade_rows(
            data_dir=args.data_dir,
            jid_dirs=jid_dirs,
            split_map=split_map,
            output_splits=output_splits,
            truth_by_filter=truth_tables,
            variable_ids=variable_ids or set(),
            radius_px=args.truth_match_radius_px,
            image_size=args.image_size,
            seed=args.seed,
            target_transients=args.cascade_target_transients,
            target_variables=args.cascade_target_variables,
            target_bogus=args.cascade_target_bogus,
            bogus_kind=args.cascade_bogus_kind,
            spatial_corner=spatial_corner,
            verbose=args.verbose,
        )
    writer = ShardedNPZWriter(
        args.output_dir,
        image_size=args.image_size,
        max_shard_bytes=parse_size_bytes(args.max_shard_size),
        extra_keys=(
            ("y_rr",)
            if args.task == "rr"
            else ("y_gbtds_c1", "y_gbtds_c2", "gbtds_truth_class")
            if args.task == "gbtds-cascade"
            else ("pu_label", "pu_status")
        ),
    )
    report = {split: {"n": 0, "class_counts": {"0": 0, "1": 0}, "jids": set()} for split in output_splits}
    if args.task == "rr":
        for split in output_splits:
            report[split]["rr_counts"] = {"0": 0, "1": 0}
    if args.task == "gbtds-cascade":
        for split in output_splits:
            report[split]["cascade_c1_counts"] = {"0": 0, "1": 0}
            report[split]["cascade_c2_counts"] = {"0": 0, "1": 0, "-1": 0}
            report[split]["gbtds_truth_counts"] = {"0": 0, "1": 0, "2": 0}
            report[split]["truth_kind_counts"] = {
                "transient": 0,
                "variable": 0,
                "unmatched": 0,
                "other": 0,
            }
    for idx, jid_dir in enumerate(jid_dirs, start=1):
        jid_split = split_map.get(jid_dir.name)
        if spatial_corner is None and jid_split not in report:
            continue
        if spatial_corner is None and jid_split is None:
            continue
        stats_map = write_jid(
            writer,
            args.data_dir,
            jid_dir.name,
            args.image_size,
            truth_tables,
            args.task,
            args.truth_match_radius_px,
            args.rb_real_kind,
            jid_split=jid_split,
            spatial_corner=spatial_corner,
            active_splits=set(output_splits),
            selected_bogus=selected_bogus,
            included_rows=included_rows,
            variable_ids=variable_ids,
            verbose=args.verbose,
        )
        if _should_log_progress(idx, len(jid_dirs), args.verbose):
            written = sum(stats["n"] for stats in stats_map.values())
            log.info("Wrote jid %d/%d (%s): %d samples", idx, len(jid_dirs), jid_dir.name, written)
        for split_name, stats in stats_map.items():
            report[split_name]["n"] += stats["n"]
            for key, value in stats["class_counts"].items():
                report[split_name]["class_counts"][key] += value
            report[split_name]["jids"].update(stats["jids"])
            if args.task == "rr":
                for key, value in stats["rr_counts"].items():
                    report[split_name]["rr_counts"][key] += value
            if args.task == "gbtds-cascade":
                for key, value in stats["cascade_c1_counts"].items():
                    report[split_name]["cascade_c1_counts"][key] += value
                for key, value in stats["cascade_c2_counts"].items():
                    report[split_name]["cascade_c2_counts"][key] += value
                for key, value in stats["gbtds_truth_counts"].items():
                    report[split_name]["gbtds_truth_counts"][key] += value
                for key, value in stats["truth_kind_counts"].items():
                    report[split_name]["truth_kind_counts"][key] += value
    shards = writer.close(output_splits)
    for split in output_splits:
        report[split]["jids"] = sorted(report[split]["jids"], key=jid_num)
        report[split]["shards"] = shards[split]
        report[split]["max_shard_size"] = args.max_shard_size
    write_split_report(
        args.output_dir,
        report,
        {
            "truth_match_radius_px": args.truth_match_radius_px,
            "truth_catalog": args.catalog.name,
            "science_job_filter_counts": filter_counts,
            "excluded_jobs": skipped_jids,
            "truth_magnitude_columns": detected_filters,
            "truth_match_scope": truth_match_scope,
            "truth_magnitude_source": "lightcurve_epoch" if lightcurve_report else "catalog_filter_mean",
            "lightcurve_truth": lightcurve_report,
            "sharded": True,
            "rb_real_kind": args.rb_real_kind if args.task == "rb" else None,
            **(
                {
                    "cascade_task": "gbtds",
                    "cascade_labels": {
                        "y_gbtds_c1": {"0": "not_transient", "1": "transient"},
                        "y_gbtds_c2": {"-1": "excluded_transient", "0": "bogus", "1": "variable"},
                        "gbtds_truth_class": {"0": "bogus", "1": "variable", "2": "transient"},
                    },
                }
                if args.task == "gbtds-cascade"
                else {}
            ),
            "rb_transient_sampling": rb_transient_summary,
            "rb_corrected_sampling": rb_corrected_summary,
            "cascade_subset_sampling": cascade_subset_summary,
            "test_assignment": "detection_centroid" if spatial_corner is not None else "jid",
            "test_corner": spatial_corner.corner if spatial_corner is not None else None,
            "pu_learning": {
                "scope": "GBTDS only",
                "citation": "Bekker and Davis 2020, arXiv:1811.04820",
                "positive_rule": "sicbro_id >= 5000000",
                "unlabeled_rule": "all non-transient detections, including variables with sicbro_id < 5000000",
                "mag_boundary": PU_MAG_BOUNDARY,
                "truth_match_radius_px": args.truth_match_radius_px,
                "status_ids": {
                    str(PU_STATUS_UNLABELED): "unlabeled",
                    str(PU_STATUS_POSITIVE): "positive",
                    str(PU_STATUS_TRUSTED_NEGATIVE): "trusted_negative",
                },
            }
            if args.task == "rb"
            else None,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
