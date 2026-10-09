#!/usr/bin/env python
"""Evaluate an RB PU checkpoint by PU provenance slices."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
from tensorflow import keras

from classification import gpu_config  # noqa: F401
from classification import model_factory as _model_factory  # noqa: F401  # register custom Keras layers
from classification.data_utils import load_npz_dataset
from classification.pu import validate_pu_shards
from classification.training import prefer_gpu_memory_growth, write_json

SURVEY_IDS = {"hltds": 0, "gbtds": 1}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--survey", choices=sorted(SURVEY_IDS), required=True)
    p.add_argument("--checkpoint", required=True)
    p.add_argument("--test", nargs="+", required=True)
    p.add_argument("--feats-scaler", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--threshold", type=float, default=0.5)
    return p.parse_args()


def _metadata_list(values: np.ndarray) -> list[dict]:
    out = []
    for item in np.asarray(values, dtype=object).reshape(-1):
        out.append(dict(item) if isinstance(item, dict) else {})
    return out


def _truth_kind(meta: dict) -> str:
    if "pu_truth_kind" in meta:
        return str(meta["pu_truth_kind"])
    return str(meta.get("truth_obj_type", "unknown"))


def _finite_float(value: object) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return out if np.isfinite(out) else float("nan")


def _slice_masks(metadata: list[dict], pu_label: np.ndarray, *, survey: str) -> dict[str, np.ndarray]:
    n = len(metadata)
    kinds = np.asarray([_truth_kind(m) for m in metadata], dtype=object)
    mags = np.asarray([_finite_float(m.get("pu_truth_mag", np.nan)) for m in metadata], dtype=np.float64)
    transient = kinds == "transient"
    masks = {
        "all": np.ones(n, dtype=bool),
        "pu_positive": pu_label == 1,
        "unlabeled": pu_label == 0,
        "transient": transient,
        "variable": kinds == "variable",
        "other_catalog": kinds == "other_catalog",
        "unmatched": kinds == "unmatched",
    }
    if survey == "gbtds":
        masks["bright_transient"] = transient & np.isfinite(mags) & (mags <= 26.0)
        masks["faint_transient"] = transient & np.isfinite(mags) & (mags > 26.0)
        masks["unknown_mag_transient"] = transient & ~np.isfinite(mags)
    return masks


def _slice_row(name: str, mask: np.ndarray, scores: np.ndarray, pu_label: np.ndarray, threshold: float) -> dict:
    vals = scores[mask]
    labels = pu_label[mask]
    positives = labels == 1
    row = {
        "slice": name,
        "n": int(vals.size),
        "n_pu_positive": int(positives.sum()),
        "n_unlabeled": int((labels == 0).sum()),
        "mean_score": None,
        "median_score": None,
        "p90_score": None,
        "p95_score": None,
        "precision_at_threshold": None,
        "recall_at_threshold": None,
        "threshold": float(threshold),
    }
    if vals.size:
        row.update(
            {
                "mean_score": float(np.mean(vals)),
                "median_score": float(np.median(vals)),
                "p90_score": float(np.percentile(vals, 90)),
                "p95_score": float(np.percentile(vals, 95)),
            }
        )
    if positives.any():
        row["recall_at_threshold"] = float(np.mean(vals[positives] >= threshold))
    predicted_positive = vals >= threshold
    if name == "all" and predicted_positive.any():
        row["precision_at_threshold"] = float(np.mean(labels[predicted_positive] == 1))
    return row


def main() -> int:
    args = parse_args()
    prefer_gpu_memory_growth()
    expected_survey_id = SURVEY_IDS[args.survey]
    counts = validate_pu_shards(args.test, expected_survey_id=expected_survey_id)
    data = load_npz_dataset(args.test, args.feats_scaler)
    model = keras.models.load_model(args.checkpoint, compile=False)
    preds = model.predict(
        {
            "images": data["X"].astype(np.float32),
            "tabular": data["feats"].astype(np.float32),
            "survey_id": data["survey_id"].astype(np.int32),
            "filter_id": data["filter_id"].astype(np.int32),
        },
        verbose=0,
    )["rb"].reshape(-1)
    pu_label = np.asarray(data["pu_label"], dtype=np.int64)
    metadata = _metadata_list(data["metadata"])
    rows = [
        _slice_row(name, mask, preds, pu_label, args.threshold)
        for name, mask in _slice_masks(metadata, pu_label, survey=args.survey).items()
    ]
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    payload = {
        "task": "rb_pu_eval",
        "survey": args.survey,
        "checkpoint": str(args.checkpoint),
        "test": args.test,
        "threshold": float(args.threshold),
        "counts": counts,
        "slices": rows,
    }
    write_json(output / "metrics.json", payload)
    keys = list(rows[0]) if rows else []
    with (output / "slice_metrics.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)
    np.savez_compressed(output / "scores.npz", score=preds.astype(np.float32), pu_label=pu_label)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
