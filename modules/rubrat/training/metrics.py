"""Metric utilities for classification evaluation."""

from __future__ import annotations

import numpy as np


def confusion_matrix(y_true, y_pred, num_classes: int) -> np.ndarray:
    y_true = np.asarray(y_true, dtype=np.int64).reshape(-1)
    y_pred = np.asarray(y_pred, dtype=np.int64).reshape(-1)
    mat = np.zeros((num_classes, num_classes), dtype=np.int64)
    for t, p in zip(y_true, y_pred):
        if 0 <= t < num_classes and 0 <= p < num_classes:
            mat[t, p] += 1
    return mat


def per_class_recall(y_true, y_pred, num_classes: int) -> np.ndarray:
    mat = confusion_matrix(y_true, y_pred, num_classes)
    denom = mat.sum(axis=1)
    return np.divide(np.diag(mat), denom, out=np.zeros(num_classes, dtype=np.float64), where=denom > 0)


def macro_f1(y_true, y_pred, num_classes: int) -> float:
    mat = confusion_matrix(y_true, y_pred, num_classes)
    tp = np.diag(mat).astype(np.float64)
    fp = mat.sum(axis=0) - tp
    fn = mat.sum(axis=1) - tp
    precision = np.divide(tp, tp + fp, out=np.zeros(num_classes, dtype=np.float64), where=(tp + fp) > 0)
    recall = np.divide(tp, tp + fn, out=np.zeros(num_classes, dtype=np.float64), where=(tp + fn) > 0)
    f1 = np.divide(
        2 * precision * recall,
        precision + recall,
        out=np.zeros(num_classes, dtype=np.float64),
        where=(precision + recall) > 0,
    )
    return float(np.mean(f1))


def compute_optimal_threshold(
    y_true,
    y_score,
    *,
    low: float = 0.01,
    high: float = 0.99,
    n_grid: int = 990,
    num_classes: int = 2,
) -> dict[str, float]:
    """Grid-search a score cutoff on validation labels to maximize macro-F1."""
    y_true = np.asarray(y_true, dtype=np.int64).reshape(-1)
    y_score = np.asarray(y_score, dtype=np.float64).reshape(-1)
    if y_true.shape[0] != y_score.shape[0]:
        raise ValueError("y_true and y_score must have the same length")
    if len(np.unique(y_true)) < 2:
        raise ValueError("Need both classes in y_true to optimize a binary threshold")

    best_threshold = 0.5
    best_macro_f1 = -1.0
    best_precision = 0.0
    best_recall = 0.0
    for threshold in np.linspace(low, high, n_grid):
        y_pred = (y_score >= threshold).astype(np.int64)
        macro = macro_f1(y_true, y_pred, num_classes)
        if macro > best_macro_f1:
            mat = confusion_matrix(y_true, y_pred, num_classes)
            tp = np.diag(mat).astype(np.float64)
            fp = mat.sum(axis=0) - tp
            fn = mat.sum(axis=1) - tp
            precision = np.divide(tp, tp + fp, out=np.zeros(num_classes, dtype=np.float64), where=(tp + fp) > 0)
            recall = np.divide(tp, tp + fn, out=np.zeros(num_classes, dtype=np.float64), where=(tp + fn) > 0)
            best_threshold = float(threshold)
            best_macro_f1 = float(macro)
            best_precision = float(np.mean(precision))
            best_recall = float(np.mean(recall))
    return {
        "threshold": best_threshold,
        "macro_f1": best_macro_f1,
        "macro_precision": best_precision,
        "macro_recall": best_recall,
        "grid_low": float(low),
        "grid_high": float(high),
        "grid_size": int(n_grid),
    }


def calibration_table(y_true, y_score, *, n_bins: int = 10) -> list[dict]:
    """Return binary calibration bins for class-1 probabilities."""
    y_true = np.asarray(y_true, dtype=np.int64).reshape(-1)
    y_score = np.asarray(y_score, dtype=np.float64).reshape(-1)
    if y_true.shape[0] != y_score.shape[0]:
        raise ValueError("y_true and y_score must have the same length")
    edges = np.linspace(0.0, 1.0, int(n_bins) + 1)
    rows = []
    for i in range(int(n_bins)):
        lo = float(edges[i])
        hi = float(edges[i + 1])
        if i == int(n_bins) - 1:
            mask = (y_score >= lo) & (y_score <= hi)
        else:
            mask = (y_score >= lo) & (y_score < hi)
        n = int(mask.sum())
        rows.append(
            {
                "bin_low": lo,
                "bin_high": hi,
                "n": n,
                "mean_score": float(y_score[mask].mean()) if n else None,
                "empirical_positive_rate": float(y_true[mask].mean()) if n else None,
            }
        )
    return rows


def classification_report(y_true, y_pred, class_names: list[str] | None = None) -> dict:
    num_classes = len(class_names) if class_names is not None else int(max(np.max(y_true), np.max(y_pred)) + 1)
    recalls = per_class_recall(y_true, y_pred, num_classes)
    mat = confusion_matrix(y_true, y_pred, num_classes)
    return {
        "macro_f1": macro_f1(y_true, y_pred, num_classes),
        "per_class_recall": {
            (class_names[i] if class_names else str(i)): float(recalls[i]) for i in range(num_classes)
        },
        "confusion_matrix": mat.tolist(),
    }
