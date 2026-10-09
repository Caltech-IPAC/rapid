#!/usr/bin/env python3
"""Build Phase 1 HLTDS real/bogus NPZ splits."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from classification.npz_builders import (  # noqa: E402
    HLTDS_FILES,
    ShardedNPZWriter,
    arcsinh_stack_cutout,
    feature_row,
    filter_id,
    infer_filter_from_jid_dir,
    jid_num,
    normalize_detection_table,
    parse_size_bytes,
    split_jids,
    write_split_report,
)
from ingestion.fits_loader import load_fits  # noqa: E402

_RB_RATIO_CHOICES = {"3:7": (3, 7)}
_TRANSIENT_REAL_TYPES = {"transient", "injected"}
PU_MAG_LIMIT_AB = 26.0
PU_STATUS_UNLABELED = 0
PU_STATUS_POSITIVE = 1
PU_STATUS_TRUSTED_NEGATIVE = 2
log = logging.getLogger(__name__)


def _should_log_progress(index: int, total: int, verbose: int) -> bool:
    if total <= 0:
        return False
    if verbose >= 2:
        return True
    return index == 1 or index == total or index % 25 == 0


def _is_transient_real(truth_type: object) -> bool:
    return str(truth_type).strip().lower() in _TRANSIENT_REAL_TYPES


def _is_ou24_transient_real(truth_type: object) -> bool:
    return str(truth_type).strip().lower() == "transient"


def _is_injected_truth(truth_type: object) -> bool:
    return str(truth_type).strip().lower() == "injected"


def _is_old_real(truth_type: object) -> bool:
    text = str(truth_type).strip().lower()
    return text != "" and text != "nan"


def _parse_rb_ratio(value: str) -> tuple[int, int]:
    if value not in _RB_RATIO_CHOICES:
        raise ValueError(f"Unsupported RB ratio {value!r}; expected one of {sorted(_RB_RATIO_CHOICES)}")
    return _RB_RATIO_CHOICES[value]


def _finite_float(value: object) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return out if np.isfinite(out) else float("nan")


def _first_finite(row: pd.Series | dict, names: tuple[str, ...]) -> float:
    for name in names:
        if name not in row:
            continue
        value = _finite_float(row[name])
        if np.isfinite(value):
            return value
    return float("nan")


def corrected_hltds_ab_mag(row: pd.Series | dict, truth_type: object | None = None) -> float:
    """Return the canonical, ZPT-corrected HLTDS AB magnitude."""
    canonical = _first_finite(row, ("truth_mag_ab",))
    if np.isfinite(canonical):
        return canonical
    kind = str(truth_type if truth_type is not None else row.get("truth_obj_type", "")).strip().lower()
    if kind == "transient":
        return _first_finite(row, ("truth_mag",))
    if kind == "injected":
        return _first_finite(
            row,
            (
                "mag_ab",
                "catalog_magnitude",
                "injection_mag_ab",
                "injection_catalog_magnitude",
            ),
        )
    return _first_finite(row, ("mag_ab", "catalog_magnitude"))


def pu_status_for_hltds_row(
    row: pd.Series | dict,
    *,
    mag_limit_ab: float = PU_MAG_LIMIT_AB,
) -> tuple[int, int, float]:
    """Classify one HLTDS detection for positive-unlabeled RB training.

    Confirmed transients are positive regardless of magnitude. The magnitude
    limit is an evaluation policy and must not alter PU training labels.
    """
    truth_type = row.get("truth_obj_type", "")
    mag_ab = corrected_hltds_ab_mag(row, truth_type)
    del mag_limit_ab
    if _is_transient_real(truth_type):
        return 1, PU_STATUS_POSITIVE, float(mag_ab)
    return 0, PU_STATUS_UNLABELED, float(mag_ab)


def _valid_hltds_row_keys(
    *,
    data_dir: Path,
    labels_dir: Path,
    jid: str,
    image_size: int,
) -> set[tuple[str, int]]:
    jid_dir = data_dir / jid
    label_path = labels_dir / jid / "sfft_psfcat_labeled.parquet"
    det = normalize_detection_table(pd.read_parquet(label_path))
    sci = load_fits(jid_dir / HLTDS_FILES["sci"])
    ref = load_fits(jid_dir / HLTDS_FILES["ref"])
    diff = load_fits(jid_dir / HLTDS_FILES["diff"])
    valid: set[tuple[str, int]] = set()
    for row_index, row in det.iterrows():
        stack = arcsinh_stack_cutout(
            sci["data"],
            ref["data"],
            diff["data"],
            float(row["xcentroid"]),
            float(row["ycentroid"]),
            image_size=image_size,
        )
        if stack is not None:
            valid.add((jid, int(row_index)))
    return valid


def select_ou24_equal_injected_bogus(
    *,
    data_dir: Path,
    labels_dir: Path,
    jids: list[str],
    split_map: dict[str, str],
    splits: tuple[str, ...],
    ratio: str,
    seed: int,
    image_size: int,
    verbose: int = 0,
) -> tuple[set[tuple[str, int]], set[tuple[str, int]], dict]:
    """
    Keep every valid OpenUniverse2024 transient detection, add an equal number of
    injected transient detections (sampled without replacement), then sample
    bogus rows to reach the requested overall real:bogus ratio.
    """
    real_part, bogus_part = _parse_rb_ratio(ratio)
    rng = np.random.default_rng(seed)

    ou24_keys: list[tuple[str, int]] = []
    injected_keys: list[tuple[str, int]] = []
    bogus_keys: list[tuple[str, int]] = []

    total = len(jids)
    log.info("Pre-scanning %d labeled JIDs for ou24-equal-injected selection", total)
    for idx, jid in enumerate(jids, start=1):
        if _should_log_progress(idx, total, verbose):
            log.info("Pre-scan progress: %d/%d (%s)", idx, total, jid)
        det = pd.read_parquet(
            labels_dir / jid / "sfft_psfcat_labeled.parquet",
            columns=["truth_obj_type"],
        )
        valid_rows = _valid_hltds_row_keys(
            data_dir=data_dir,
            labels_dir=labels_dir,
            jid=jid,
            image_size=image_size,
        )
        for row_index, truth_type in enumerate(det["truth_obj_type"].tolist()):
            key = (jid, int(row_index))
            if key not in valid_rows:
                continue
            if _is_ou24_transient_real(truth_type):
                ou24_keys.append(key)
            elif _is_injected_truth(truth_type):
                injected_keys.append(key)
            else:
                bogus_keys.append(key)

    ou24_set = set(ou24_keys)
    n_ou24 = len(ou24_set)
    n_inj_pick = min(n_ou24, len(injected_keys))
    if n_ou24 == 0:
        raise RuntimeError("No valid OpenUniverse2024 transient detections found.")

    inj_picked: set[tuple[str, int]] = set()
    if n_inj_pick > 0:
        picks = rng.choice(len(injected_keys), size=n_inj_pick, replace=False)
        inj_picked = {injected_keys[int(i)] for i in picks}

    selected_pos = ou24_set | inj_picked
    n_real = len(selected_pos)
    target_bogus = int(np.floor(n_real * bogus_part / real_part))
    n_bogus_pick = min(target_bogus, len(bogus_keys))
    selected_bogus: set[tuple[str, int]] = set()
    if n_bogus_pick > 0:
        picks = rng.choice(len(bogus_keys), size=n_bogus_pick, replace=False)
        selected_bogus = {bogus_keys[int(i)] for i in picks}

    per_split: dict[str, dict] = {}
    for split in splits:
        split_pos = {k for k in selected_pos if split_map[k[0]] == split}
        split_bogus = {k for k in selected_bogus if split_map[k[0]] == split}
        split_ou24 = {k for k in ou24_set if split_map[k[0]] == split}
        split_inj = {k for k in inj_picked if split_map[k[0]] == split}
        per_split[split] = {
            "rb_real_kind": "ou24-equal-injected",
            "requested_real_bogus_ratio": ratio,
            "selected_ou24_reals": int(len(split_ou24)),
            "selected_injected_reals": int(len(split_inj)),
            "selected_reals": int(len(split_pos)),
            "selected_bogus": int(len(split_bogus)),
        }

    summary = {
        "global": {
            "rb_real_kind": "ou24-equal-injected",
            "requested_real_bogus_ratio": ratio,
            "valid_ou24_candidates": n_ou24,
            "valid_injected_candidates": int(len(injected_keys)),
            "valid_bogus_candidates": int(len(bogus_keys)),
            "selected_ou24_reals": n_ou24,
            "selected_injected_reals": int(n_inj_pick),
            "selected_reals_total": n_real,
            "target_bogus": target_bogus,
            "selected_bogus": int(n_bogus_pick),
            "bogus_limited_by_available": bool(n_bogus_pick < target_bogus),
            "injected_limited_by_available": bool(n_inj_pick < n_ou24),
        },
        **per_split,
    }
    log.info(
        "ou24-equal-injected: ou24=%d injected=%d bogus=%d (target bogus=%d)",
        n_ou24,
        n_inj_pick,
        n_bogus_pick,
        target_bogus,
    )
    return selected_pos, selected_bogus, summary


def select_transient_rb_bogus(
    *,
    data_dir: Path,
    labels_dir: Path,
    jids: list[str],
    split_map: dict[str, str],
    splits: tuple[str, ...],
    ratio: str,
    seed: int,
    image_size: int,
    verbose: int = 0,
    rb_real_kind: str = "transient",
) -> tuple[set[tuple[str, int]], dict[str, dict]]:
    """Select valid HLTDS bogus rows so RB targets the requested real:bogus ratio."""
    real_part, bogus_part = _parse_rb_ratio(ratio)
    rng = np.random.default_rng(seed)
    neg_by_split: dict[str, list[tuple[str, int]]] = {split: [] for split in splits}
    real_counts = {split: 0 for split in splits}
    summary_kind = "ou24-transient" if rb_real_kind == "ou24-transient" else "transient"

    total = len(jids)
    log.info("Pre-scanning %d labeled JIDs to count valid %s reals and bogus candidates", total, summary_kind)
    for idx, jid in enumerate(jids, start=1):
        if _should_log_progress(idx, total, verbose):
            log.info("Pre-scan progress: %d/%d (%s)", idx, total, jid)
        split = split_map[jid]
        det = pd.read_parquet(labels_dir / jid / "sfft_psfcat_labeled.parquet", columns=["truth_obj_type"])
        valid_rows = _valid_hltds_row_keys(
            data_dir=data_dir,
            labels_dir=labels_dir,
            jid=jid,
            image_size=image_size,
        )
        for row_index, truth_type in enumerate(det["truth_obj_type"].tolist()):
            key = (jid, int(row_index))
            if key not in valid_rows:
                continue
            if rb_real_kind == "ou24-transient":
                if _is_ou24_transient_real(truth_type):
                    real_counts[split] += 1
                elif not _is_injected_truth(truth_type):
                    neg_by_split[split].append(key)
            elif _is_transient_real(truth_type):
                real_counts[split] += 1
            else:
                neg_by_split[split].append(key)

    selected: set[tuple[str, int]] = set()
    summary: dict[str, dict] = {}
    for split in splits:
        n_real = int(real_counts[split])
        target_bogus = int(np.floor(n_real * bogus_part / real_part)) if n_real > 0 else 0
        candidates = neg_by_split[split]
        n_pick = min(target_bogus, len(candidates))
        if n_pick:
            picks = rng.choice(len(candidates), size=n_pick, replace=False)
            selected.update(candidates[int(i)] for i in picks)
        summary[split] = {
            "rb_real_kind": summary_kind,
            "requested_real_bogus_ratio": ratio,
            "transient_real_candidates": n_real,
            "bogus_candidates": int(len(candidates)),
            "selected_reals": n_real,
            "selected_bogus": int(n_pick),
            "bogus_limited_by_available": bool(n_pick < target_bogus),
        }
        log.info(
            "Pre-scan %s: valid_reals=%d bogus_candidates=%d selected_bogus=%d",
            split,
            n_real,
            len(candidates),
            n_pick,
        )
    return selected, summary


def select_ou24_no_inject_bogus(
    *,
    data_dir: Path,
    labels_dir: Path,
    jids: list[str],
    split_map: dict[str, str],
    splits: tuple[str, ...],
    ratio: str,
    seed: int,
    image_size: int,
    verbose: int = 0,
) -> tuple[set[tuple[str, int]], dict[str, dict]]:
    """
    Train/val: all valid OU24 transients plus sampled bogus at the requested ratio;
    injected rows are excluded from train/val entirely.

    Test: all valid OU24 and injected transients, plus sampled bogus at the same
    ratio using (OU24 + injected) as the positive count.
    """
    real_part, bogus_part = _parse_rb_ratio(ratio)
    rng = np.random.default_rng(seed)
    neg_by_split: dict[str, list[tuple[str, int]]] = {split: [] for split in splits}
    ou24_counts = {split: 0 for split in splits}
    injected_counts = {split: 0 for split in splits}

    total = len(jids)
    log.info("Pre-scanning %d labeled JIDs for ou24-no-inject selection", total)
    for idx, jid in enumerate(jids, start=1):
        if _should_log_progress(idx, total, verbose):
            log.info("Pre-scan progress: %d/%d (%s)", idx, total, jid)
        split = split_map[jid]
        det = pd.read_parquet(
            labels_dir / jid / "sfft_psfcat_labeled.parquet",
            columns=["truth_obj_type"],
        )
        valid_rows = _valid_hltds_row_keys(
            data_dir=data_dir,
            labels_dir=labels_dir,
            jid=jid,
            image_size=image_size,
        )
        for row_index, truth_type in enumerate(det["truth_obj_type"].tolist()):
            key = (jid, int(row_index))
            if key not in valid_rows:
                continue
            if _is_ou24_transient_real(truth_type):
                ou24_counts[split] += 1
            elif _is_injected_truth(truth_type):
                injected_counts[split] += 1
            else:
                neg_by_split[split].append(key)

    selected: set[tuple[str, int]] = set()
    summary: dict[str, dict] = {}
    for split in splits:
        n_ou24 = int(ou24_counts[split])
        n_injected = int(injected_counts[split])
        if split == "test":
            n_real = n_ou24 + n_injected
        else:
            n_real = n_ou24
        target_bogus = int(np.floor(n_real * bogus_part / real_part)) if n_real > 0 else 0
        candidates = neg_by_split[split]
        n_pick = min(target_bogus, len(candidates))
        if n_pick:
            picks = rng.choice(len(candidates), size=n_pick, replace=False)
            selected.update(candidates[int(i)] for i in picks)
        summary[split] = {
            "rb_real_kind": "ou24-no-inject",
            "requested_real_bogus_ratio": ratio,
            "selected_ou24_reals": n_ou24,
            "selected_injected_reals": n_injected if split == "test" else 0,
            "selected_reals": n_real,
            "bogus_candidates": int(len(candidates)),
            "selected_bogus": int(n_pick),
            "bogus_limited_by_available": bool(n_pick < target_bogus),
            "includes_injected": split == "test",
        }
        log.info(
            "Pre-scan %s: ou24=%d injected=%d bogus_candidates=%d selected_bogus=%d",
            split,
            n_ou24,
            n_injected if split == "test" else 0,
            len(candidates),
            n_pick,
        )
    return selected, summary


def write_jid(
    writer: ShardedNPZWriter,
    split: str,
    data_dir: Path,
    labels_dir: Path,
    jid: str,
    image_size: int,
    rb_real_kind: str,
    selected_bogus: set[tuple[str, int]] | None = None,
    selected_positives: set[tuple[str, int]] | None = None,
) -> dict:
    jid_dir = data_dir / jid
    label_path = labels_dir / jid / "sfft_psfcat_labeled.parquet"
    det = normalize_detection_table(pd.read_parquet(label_path))
    sci = load_fits(jid_dir / HLTDS_FILES["sci"])
    ref = load_fits(jid_dir / HLTDS_FILES["ref"])
    diff = load_fits(jid_dir / HLTDS_FILES["diff"])
    filt = infer_filter_from_jid_dir(jid_dir, sci.get("filter", "UNKNOWN"))
    stats = {"n": 0, "class_counts": {"0": 0, "1": 0}, "jids": {jid}}
    for row_index, row in det.iterrows():
        stack = arcsinh_stack_cutout(
            sci["data"],
            ref["data"],
            diff["data"],
            float(row["xcentroid"]),
            float(row["ycentroid"]),
            image_size=image_size,
        )
        if stack is None:
            continue
        truth_type = str(row.get("truth_obj_type", ""))
        key = (jid, int(row_index))
        if rb_real_kind == "ou24-equal-injected":
            if selected_positives is None or selected_bogus is None:
                raise ValueError("ou24-equal-injected requires selected positives and bogus sets")
            if key in selected_positives:
                y = 1
            elif key in selected_bogus:
                y = 0
            else:
                continue
        elif rb_real_kind == "ou24-no-inject":
            if split == "test":
                if _is_ou24_transient_real(truth_type) or _is_injected_truth(truth_type):
                    y = 1
                elif selected_bogus is None or key not in selected_bogus:
                    continue
                else:
                    y = 0
            else:
                if _is_injected_truth(truth_type):
                    continue
                y = 1 if _is_ou24_transient_real(truth_type) else 0
                if y == 0 and (selected_bogus is None or key not in selected_bogus):
                    continue
        elif rb_real_kind == "ou24-transient":
            if _is_injected_truth(truth_type):
                continue
            y = 1 if _is_ou24_transient_real(truth_type) else 0
            if y == 0 and (selected_bogus is None or key not in selected_bogus):
                continue
        elif rb_real_kind == "transient":
            y = 1 if _is_transient_real(truth_type) else 0
            if y == 0 and (selected_bogus is None or key not in selected_bogus):
                continue
        else:
            y = 1 if _is_old_real(truth_type) else 0
        pu_label, pu_status, mag_ab_corrected = pu_status_for_hltds_row(row)
        writer.add(
            split,
            {
                "X": stack,
                "feats": feature_row(row),
                "y": y,
                "pu_label": pu_label,
                "pu_status": pu_status,
                "survey_id": 0,
                "filter_id": filter_id(filt),
                "metadata": {
                    "jid": jid,
                    "xcentroid": float(row["xcentroid"]),
                    "ycentroid": float(row["ycentroid"]),
                    "det_id": int(row.get("id", row_index)),
                    "row_index": int(row_index),
                    "mjd": float(sci.get("mjd", np.nan)),
                    "filter": filt,
                    "parquet_path": str(label_path),
                    "truth_obj_type": truth_type,
                    "truth_id": row.get("truth_id", ""),
                    "truth_mag_ab": corrected_hltds_ab_mag(row, truth_type),
                    "truth_zpt": _first_finite(row, ("truth_zpt", "zpt")),
                    "pu_label": pu_label,
                    "pu_status": pu_status,
                    "pu_status_name": {
                        PU_STATUS_UNLABELED: "unlabeled",
                        PU_STATUS_POSITIVE: "positive",
                        PU_STATUS_TRUSTED_NEGATIVE: "trusted_negative",
                    }[pu_status],
                    "mag_ab_corrected": mag_ab_corrected,
                    "pu_mag_limit_ab": PU_MAG_LIMIT_AB,
                },
            },
        )
        stats["n"] += 1
        stats["class_counts"][str(y)] += 1
    return stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", required=True, type=Path)
    p.add_argument("--labels-dir", required=True, type=Path)
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--image-size", type=int, default=64)
    p.add_argument("--split", choices=["stratified"], default="stratified")
    p.add_argument(
        "--rb-real-kind",
        choices=["all-labeled", "transient", "ou24-transient", "ou24-equal-injected", "ou24-no-inject"],
        default="all-labeled",
        help=(
            "all-labeled = old behavior; transient = all transient/injected are real; "
            "ou24-transient = only OU24 transient is real; ou24-equal-injected = all "
            "OU24 transients plus an equal number of injected transients, with 3:7 bogus; "
            "ou24-no-inject = train/val OU24+bogus only (3:7), test adds injected too."
        ),
    )
    p.add_argument("--rb-real-bogus-ratio", choices=sorted(_RB_RATIO_CHOICES), default="3:7")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--max-shard-size", default="8gb", help="Maximum estimated uncompressed shard size; keep <=10gb.")
    p.add_argument("-v", "--verbose", action="count", default=0)
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)

    jids = sorted(
        [p.parent.name for p in args.labels_dir.glob("jid*/sfft_psfcat_labeled.parquet")],
        key=jid_num,
    )
    log.info("Discovered %d labeled JIDs", len(jids))
    jid_to_filter = {jid: infer_filter_from_jid_dir(args.data_dir / jid) for jid in jids}
    split_map = split_jids(jid_to_filter, seed=args.seed)
    splits = ("train", "val", "test")
    selected_bogus = None
    selected_positives = None
    rb_transient_summary = None
    if args.rb_real_kind == "ou24-equal-injected":
        selected_positives, selected_bogus, rb_transient_summary = select_ou24_equal_injected_bogus(
            data_dir=args.data_dir,
            labels_dir=args.labels_dir,
            jids=jids,
            split_map=split_map,
            splits=splits,
            ratio=args.rb_real_bogus_ratio,
            seed=args.seed,
            image_size=args.image_size,
            verbose=args.verbose,
        )
    elif args.rb_real_kind == "ou24-no-inject":
        selected_bogus, rb_transient_summary = select_ou24_no_inject_bogus(
            data_dir=args.data_dir,
            labels_dir=args.labels_dir,
            jids=jids,
            split_map=split_map,
            splits=splits,
            ratio=args.rb_real_bogus_ratio,
            seed=args.seed,
            image_size=args.image_size,
            verbose=args.verbose,
        )
    elif args.rb_real_kind in {"transient", "ou24-transient"}:
        selected_bogus, rb_transient_summary = select_transient_rb_bogus(
            data_dir=args.data_dir,
            labels_dir=args.labels_dir,
            jids=jids,
            split_map=split_map,
            splits=splits,
            ratio=args.rb_real_bogus_ratio,
            seed=args.seed,
            image_size=args.image_size,
            verbose=args.verbose,
            rb_real_kind=args.rb_real_kind,
        )
    writer = ShardedNPZWriter(
        args.output_dir,
        image_size=args.image_size,
        max_shard_bytes=parse_size_bytes(args.max_shard_size),
        extra_keys=("pu_label", "pu_status"),
    )
    report = {split: {"n": 0, "class_counts": {"0": 0, "1": 0}, "jids": set()} for split in splits}
    total = len(jids)
    for idx, jid in enumerate(jids, start=1):
        if _should_log_progress(idx, total, args.verbose):
            log.info("Write progress: %d/%d (%s)", idx, total, jid)
        split = split_map[jid]
        stats = write_jid(
            writer,
            split,
            args.data_dir,
            args.labels_dir,
            jid,
            args.image_size,
            args.rb_real_kind,
            selected_bogus,
            selected_positives,
        )
        report[split]["n"] += stats["n"]
        for key, value in stats["class_counts"].items():
            report[split]["class_counts"][key] += value
        report[split]["jids"].update(stats["jids"])
    shards = writer.close(splits)
    for split in splits:
        report[split]["jids"] = sorted(report[split]["jids"], key=jid_num)
        report[split]["shards"] = shards[split]
        report[split]["max_shard_size"] = args.max_shard_size
    write_split_report(
        args.output_dir,
        report,
        {
            "sharded": True,
            "rb_real_kind": args.rb_real_kind,
            "rb_transient_sampling": rb_transient_summary,
            "pu_learning": {
                "scope": "HLTDS only",
                "citation": "Bekker and Davis 2020, arXiv:1811.04820",
                "mag_limit_ab": PU_MAG_LIMIT_AB,
                "magnitude_definition": "truth_mag_ab = mag + zpt",
                "status_ids": {
                    str(PU_STATUS_UNLABELED): "unlabeled",
                    str(PU_STATUS_POSITIVE): "positive",
                    str(PU_STATUS_TRUSTED_NEGATIVE): "trusted_negative",
                },
            },
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
