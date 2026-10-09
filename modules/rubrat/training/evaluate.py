"""Validation-selected RB evaluation with optional HLTDS magnitude policy."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from classification.evaluation import build_model_report, predict_model
from classification.metrics import compute_optimal_threshold
from rubrat.magnitude import apply_hltds_magnitude_policy, corrected_ab_magnitude


def _metadata(values: np.ndarray) -> list[dict[str, Any]]:
    return [dict(value) if isinstance(value, dict) else {} for value in np.asarray(values, dtype=object)]


def evaluate(
    checkpoint: str | Path,
    val: Sequence[str | Path],
    test: Sequence[str | Path],
    scaler: str | Path,
    output_dir: str | Path,
    *,
    survey: str,
    mag_limit_ab: float | None = 26.0,
    batch_size: int = 128,
) -> dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    val_data, val_preds = predict_model(checkpoint, val, scaler, batch_size=batch_size)
    threshold = compute_optimal_threshold(val_data["y"], val_preds["rb"].reshape(-1))
    test_data, test_preds = predict_model(checkpoint, test, scaler, batch_size=batch_size)
    scores = np.asarray(test_preds["rb"], dtype=np.float64).reshape(-1)
    labels = np.asarray(test_data["y"], dtype=np.int64)
    metadata = _metadata(test_data["metadata"])
    full = build_model_report(
        task="rb",
        y_true=labels,
        probs=scores.reshape(-1, 1),
        metadata=test_data["metadata"],
        output_name="rb",
        label_key="y",
        class_names=["bogus", "real"],
        threshold=float(threshold["threshold"]),
    )
    policy = None
    policy_summary = None
    if survey == "hltds" and mag_limit_ab is not None:
        mask, policy_summary = apply_hltds_magnitude_policy(labels, metadata, limit_ab=mag_limit_ab)
        policy = build_model_report(
            task="rb_hltds_mag_policy",
            y_true=labels[mask],
            probs=scores[mask].reshape(-1, 1),
            metadata=np.asarray(metadata, dtype=object)[mask],
            output_name="rb",
            label_key="y",
            class_names=["bogus", "real"],
            threshold=float(threshold["threshold"]),
        )
    result = {
        "schema_version": "rubrat_evaluation_v1",
        "survey": survey,
        "checkpoint": str(checkpoint),
        "validation_threshold": threshold,
        "full": full,
        "headline_policy": policy,
        "headline_policy_summary": policy_summary,
        "magnitude_definition": "truth_mag_ab = mag + zpt",
        "mag_limit_ab": mag_limit_ab if survey == "hltds" else None,
    }
    (output / "metrics.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    predicted = (scores >= float(threshold["threshold"])).astype(np.int64)
    with (output / "predictions.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = ["row_id", "jid", "filter", "truth_obj_type", "y_true", "score", "y_pred", "truth_mag_ab"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for i, row in enumerate(metadata):
            writer.writerow(
                {
                    "row_id": row.get("row_id", f"{row.get('jid', '')}:{row.get('row_index', i)}"),
                    "jid": row.get("jid", ""),
                    "filter": row.get("filter", "UNKNOWN"),
                    "truth_obj_type": row.get("truth_obj_type", ""),
                    "y_true": int(labels[i]),
                    "score": float(scores[i]),
                    "y_pred": int(predicted[i]),
                    "truth_mag_ab": corrected_ab_magnitude(row),
                }
            )
    return result
