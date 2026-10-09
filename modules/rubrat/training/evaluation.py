"""Phase 4 evaluation helpers for CNN classifiers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd
import tensorflow as tf
from tensorflow import keras

from classification import gpu_config  # noqa: F401
from classification import model_factory as _model_factory  # noqa: F401  # registers custom Keras layers
from classification.data_utils import FeatureZScore, coerce_feats, load_npz_dataset, resolve_npz_paths
from classification.metrics import confusion_matrix
from classification.training import prefer_gpu_memory_growth
from ingestion.rubr_adapter import build_feats_dict_legacy, load_detection_catalog

REPORT_SCHEMA_VERSION = "cnn_eval_v1"
RB_THRESHOLD = 0.5
RUBR_THRESHOLD = 0.577


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def binary_labels(scores: np.ndarray, threshold: float = RB_THRESHOLD) -> np.ndarray:
    return (np.asarray(scores, dtype=np.float64).reshape(-1) >= float(threshold)).astype(np.int64)


def class_predictions(probs: np.ndarray) -> np.ndarray:
    arr = np.asarray(probs)
    if arr.ndim == 1 or (arr.ndim == 2 and arr.shape[1] == 1):
        return binary_labels(arr)
    return np.argmax(arr, axis=-1).astype(np.int64)


def precision_recall_f1_from_confusion(mat: np.ndarray) -> dict[str, Any]:
    mat = np.asarray(mat, dtype=np.int64)
    tp = np.diag(mat).astype(np.float64)
    fp = mat.sum(axis=0) - tp
    fn = mat.sum(axis=1) - tp
    support = mat.sum(axis=1)
    precision = np.divide(tp, tp + fp, out=np.zeros_like(tp), where=(tp + fp) > 0)
    recall = np.divide(tp, tp + fn, out=np.zeros_like(tp), where=(tp + fn) > 0)
    f1 = np.divide(2 * precision * recall, precision + recall, out=np.zeros_like(tp), where=(precision + recall) > 0)
    return {
        "precision": precision.tolist(),
        "recall": recall.tolist(),
        "f1": f1.tolist(),
        "support": support.astype(int).tolist(),
        "macro_precision": float(np.mean(precision)),
        "macro_recall": float(np.mean(recall)),
        "macro_f1": float(np.mean(f1)),
        "accuracy": float(tp.sum() / mat.sum()) if mat.sum() else 0.0,
    }


def classification_metrics(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    *,
    num_classes: int,
    class_names: Sequence[str] | None = None,
) -> dict[str, Any]:
    mat = confusion_matrix(y_true, y_pred, num_classes)
    metrics = precision_recall_f1_from_confusion(mat)
    names = list(class_names) if class_names is not None else [str(i) for i in range(num_classes)]
    return {
        "class_names": names,
        "confusion_matrix": mat.tolist(),
        **metrics,
        "per_class": {
            names[i]: {
                "precision": float(metrics["precision"][i]),
                "recall": float(metrics["recall"][i]),
                "f1": float(metrics["f1"][i]),
                "support": int(metrics["support"][i]),
            }
            for i in range(num_classes)
        },
    }


def roc_curve_points(y_true: np.ndarray, scores: np.ndarray) -> dict[str, Any]:
    y = np.asarray(y_true, dtype=np.int64).reshape(-1)
    s = np.asarray(scores, dtype=np.float64).reshape(-1)
    if len(np.unique(y)) < 2:
        return {"auc": None, "fpr": [], "tpr": [], "thresholds": [], "reason": "single_class"}
    order = np.argsort(-s)
    y_sorted = y[order]
    s_sorted = s[order]
    pos = float(np.sum(y == 1))
    neg = float(np.sum(y == 0))
    tp = np.cumsum(y_sorted == 1)
    fp = np.cumsum(y_sorted == 0)
    tpr = np.concatenate([[0.0], tp / pos, [1.0]])
    fpr = np.concatenate([[0.0], fp / neg, [1.0]])
    thresholds = np.concatenate([[np.inf], s_sorted, [-np.inf]])
    auc = float(np.trapezoid(tpr, fpr))
    return {"auc": auc, "fpr": fpr.tolist(), "tpr": tpr.tolist(), "thresholds": thresholds.tolist()}


def pr_curve_points(y_true: np.ndarray, scores: np.ndarray) -> dict[str, Any]:
    y = np.asarray(y_true, dtype=np.int64).reshape(-1)
    s = np.asarray(scores, dtype=np.float64).reshape(-1)
    if len(np.unique(y)) < 2:
        return {"auc": None, "precision": [], "recall": [], "thresholds": [], "reason": "single_class"}
    order = np.argsort(-s)
    y_sorted = y[order]
    s_sorted = s[order]
    tp = np.cumsum(y_sorted == 1)
    fp = np.cumsum(y_sorted == 0)
    precision = tp / np.maximum(tp + fp, 1)
    recall = tp / max(float(np.sum(y == 1)), 1.0)
    precision = np.concatenate([[1.0], precision])
    recall = np.concatenate([[0.0], recall])
    thresholds = np.concatenate([[np.inf], s_sorted])
    auc = float(np.trapezoid(precision, recall))
    return {"auc": auc, "precision": precision.tolist(), "recall": recall.tolist(), "thresholds": thresholds.tolist()}


def metadata_list(metadata: np.ndarray | Sequence[Any]) -> list[dict[str, Any]]:
    out = []
    for item in np.asarray(metadata, dtype=object).reshape(-1):
        if isinstance(item, dict):
            out.append(dict(item))
        else:
            out.append({})
    return out


def per_filter_reports(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    metadata: np.ndarray | Sequence[Any],
    *,
    num_classes: int,
    class_names: Sequence[str] | None = None,
) -> dict[str, Any]:
    metas = metadata_list(metadata)
    filters = [str(m.get("filter", m.get("band", "unknown"))) for m in metas]
    out = {}
    for filt in sorted(set(filters)):
        mask = np.asarray([f == filt for f in filters], dtype=bool)
        out[filt] = classification_metrics(y_true[mask], y_pred[mask], num_classes=num_classes, class_names=class_names)
    return out


def filter_subset_report(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    metadata: np.ndarray | Sequence[Any],
    *,
    filter_name: str,
    num_classes: int,
    class_names: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    metas = metadata_list(metadata)
    mask = np.asarray([str(m.get("filter", m.get("band", ""))) == filter_name for m in metas], dtype=bool)
    if not np.any(mask):
        return None
    return classification_metrics(y_true[mask], y_pred[mask], num_classes=num_classes, class_names=class_names)


def keras_inputs_from_npz(data: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    return {
        "images": data["X"].astype(np.float32),
        "tabular": data["feats"].astype(np.float32),
        "survey_id": data["survey_id"].astype(np.int32),
        "filter_id": data["filter_id"].astype(np.int32),
    }


def _slice_keras_inputs(data: dict[str, np.ndarray], start: int, end: int) -> dict[str, np.ndarray]:
    return {
        "images": data["X"][start:end].astype(np.float32),
        "tabular": data["feats"][start:end].astype(np.float32),
        "survey_id": data["survey_id"][start:end].astype(np.int32),
        "filter_id": data["filter_id"][start:end].astype(np.int32),
    }


def _configure_inference_devices(*, use_cpu: bool) -> list[str]:
    if use_cpu:
        tf.config.set_visible_devices([], "GPU")
        return []
    return prefer_gpu_memory_growth()


def _predict_array(model: keras.Model, data: dict[str, np.ndarray], *, batch_size: int, chunk_size: int) -> np.ndarray:
    n = int(len(data["y"]))
    chunks: list[np.ndarray] = []
    for start in range(0, n, chunk_size):
        end = min(start + chunk_size, n)
        pred = model.predict(_slice_keras_inputs(data, start, end), batch_size=batch_size, verbose=0)
        if isinstance(pred, dict):
            pred = next(iter(pred.values()))
        chunks.append(np.asarray(pred))
    return np.concatenate(chunks, axis=0)


def _merge_loaded_shards(parts: list[dict[str, np.ndarray]]) -> dict[str, np.ndarray]:
    keys = parts[0].keys()
    merged = {key: np.concatenate([part[key] for part in parts], axis=0) for key in keys}
    merged["feats"] = coerce_feats(merged["feats"])
    return merged


def predict_model(
    checkpoint: str | Path,
    npz_paths: str | Path | Sequence[str | Path],
    scaler: FeatureZScore | str | Path,
    *,
    batch_size: int = 128,
    chunk_size: int = 1024,
    force_cpu: bool = False,
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """Run inference shard-by-shard with chunked predict and CPU fallback on GPU OOM."""
    scaler_obj = scaler if isinstance(scaler, FeatureZScore) else FeatureZScore(scaler)
    shards = resolve_npz_paths(npz_paths)
    output_name = "rb"
    retry_errors = (
        tf.errors.ResourceExhaustedError,
        tf.errors.InternalError,
    )

    attempts = [True] if force_cpu else [False, True]
    last_error: Exception | None = None

    for use_cpu in attempts:
        if use_cpu and not force_cpu:
            print("GPU inference failed — retrying on CPU.")
        try:
            keras.backend.clear_session()
            _configure_inference_devices(use_cpu=use_cpu)
            model = keras.models.load_model(checkpoint, compile=False)
            output_name = model.output_names[0]
            loaded_parts: list[dict[str, np.ndarray]] = []
            pred_parts: list[np.ndarray] = []
            effective_batch = batch_size

            for shard in shards:
                data = load_npz_dataset(shard, scaler_obj)
                try:
                    preds = _predict_array(model, data, batch_size=effective_batch, chunk_size=chunk_size)
                except retry_errors as exc:
                    effective_batch = max(8, effective_batch // 2)
                    preds = _predict_array(model, data, batch_size=effective_batch, chunk_size=chunk_size)
                    last_error = exc
                loaded_parts.append(data)
                pred_parts.append(preds)

            merged = _merge_loaded_shards(loaded_parts)
            scores = np.concatenate(pred_parts, axis=0)
            return merged, {output_name: scores}
        except retry_errors as exc:
            last_error = exc
            if use_cpu:
                raise
            continue

    raise RuntimeError("Inference failed on both GPU and CPU.") from last_error


def build_model_report(
    *,
    task: str,
    y_true: np.ndarray,
    probs: np.ndarray,
    metadata: np.ndarray | Sequence[Any],
    output_name: str,
    label_key: str,
    class_names: Sequence[str],
    threshold: float | None = None,
) -> dict[str, Any]:
    num_classes = len(class_names)
    if num_classes == 2 and probs.ndim == 2 and probs.shape[1] == 1:
        y_pred = binary_labels(probs, RB_THRESHOLD if threshold is None else threshold)
        score_vector = probs.reshape(-1)
    else:
        y_pred = class_predictions(probs)
        score_vector = probs[:, 1] if num_classes == 2 and probs.ndim == 2 else None
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "task": task,
        "output_name": output_name,
        "label_key": label_key,
        "threshold": threshold,
        "n_samples": int(len(y_true)),
        "metrics": classification_metrics(y_true, y_pred, num_classes=num_classes, class_names=class_names),
        "per_filter": per_filter_reports(y_true, y_pred, metadata, num_classes=num_classes, class_names=class_names),
        "k213": filter_subset_report(
            y_true, y_pred, metadata, filter_name="K213", num_classes=num_classes, class_names=class_names
        ),
    }
    if score_vector is not None:
        report["roc_curve"] = roc_curve_points(y_true, score_vector)
        report["pr_curve"] = pr_curve_points(y_true, score_vector)
    return report


def legacy_feature_rows_from_metadata(metadata: np.ndarray | Sequence[Any]) -> np.ndarray:
    """Re-extract legacy six RuBR features from PSF rows referenced by metadata."""
    rows = []
    parquet_cache: dict[str, pd.DataFrame] = {}
    for meta in metadata_list(metadata):
        row_dict: dict[str, Any] | None = None
        parquet_path = meta.get("parquet_path") or meta.get("psf_path")
        row_index = meta.get("row_index")
        if parquet_path is not None and row_index is not None and Path(str(parquet_path)).is_file():
            path = str(parquet_path)
            if path not in parquet_cache:
                if Path(path).suffix == ".parquet":
                    parquet_cache[path] = pd.read_parquet(path)
                else:
                    parquet_cache[path] = load_detection_catalog(Path(path))
            df = parquet_cache[path]
            i = int(row_index)
            if 0 <= i < len(df):
                row_dict = df.iloc[i].to_dict()
        if row_dict is None:
            row_dict = meta
        feat = build_feats_dict_legacy(row_dict)
        rows.append([feat[k] for k in sorted(feat)])
    return np.asarray(rows, dtype=np.float32)


def images_channels_last_to_rubr_channels_first(X: np.ndarray) -> np.ndarray:
    arr = np.asarray(X, dtype=np.float32)
    if arr.ndim != 4 or arr.shape[-1] != 3:
        raise ValueError(f"Expected channels-last (N,H,W,3), got {arr.shape}")
    # New CNN order is science, reference, sfft_diff. Legacy RuBR order is
    # reference, science, sfft_diff.
    return np.transpose(arr[..., [1, 0, 2]], (0, 3, 1, 2)).astype(np.float32)


def load_rubr_scores(
    *,
    predictions_path: str | Path | None,
    model_path: str | Path | None,
    X_channels_first: np.ndarray,
    legacy_feats: np.ndarray,
    batch_size: int = 128,
) -> tuple[np.ndarray | None, dict[str, Any]]:
    if predictions_path:
        path = Path(predictions_path)
        if path.suffix == ".npz":
            with np.load(path, allow_pickle=True) as z:
                key = "scores" if "scores" in z.files else "pred" if "pred" in z.files else z.files[0]
                return np.asarray(z[key], dtype=np.float64).reshape(-1), {"source": str(path), "key": key}
        df = pd.read_csv(path)
        for key in ("score", "scores", "pred", "rb_score"):
            if key in df.columns:
                return df[key].to_numpy(dtype=np.float64), {"source": str(path), "key": key}
        raise ValueError(f"Could not find a score column in {path}")
    if model_path:
        from rubr_compat.load import load_rubr_model

        model = load_rubr_model(model_path)
        try:
            pred = model.predict({"images": X_channels_first, "feats": legacy_feats}, batch_size=batch_size, verbose=0)
        except Exception:
            pred = model.predict([X_channels_first, legacy_feats], batch_size=batch_size, verbose=0)
        return np.asarray(pred, dtype=np.float64).reshape(-1), {"source": str(model_path), "kind": "keras_model"}
    return None, {"status": "not_run", "reason": "provide --rubr-predictions or --rubr-model"}


def attention_scores_for_batch(model: keras.Model, inputs: dict[str, np.ndarray]) -> np.ndarray:
    encoder = model.get_layer("rotinv_encoder")
    query_builder = model.get_layer("tabular_query_builder")
    fusion = model.get_layer("cross_attention_fusion")
    images = tf.convert_to_tensor(inputs["images"], dtype=tf.float32)
    tabular = tf.convert_to_tensor(inputs["tabular"], dtype=tf.float32)
    survey_id = tf.convert_to_tensor(inputs["survey_id"], dtype=tf.int32)
    filter_id = tf.convert_to_tensor(inputs["filter_id"], dtype=tf.int32)
    cnn_tokens = encoder(images, training=False)
    query_vec, query_tok = query_builder([tabular, survey_id, filter_id], training=False)
    _, scores = fusion([query_vec, query_tok, cnn_tokens], training=False, return_attention_scores=True)
    return scores.numpy()


def attention_scores_to_maps(scores: np.ndarray) -> np.ndarray:
    arr = np.asarray(scores)
    if arr.ndim == 4:
        if arr.shape[2] != 1 or arr.shape[3] != 64:
            raise ValueError(f"Expected (B,heads,1,64), got {arr.shape}")
        return arr[:, :, 0, :].reshape(arr.shape[0], arr.shape[1], 8, 8)
    if arr.ndim == 3:
        if arr.shape[1] != 1 or arr.shape[2] != 64:
            raise ValueError(f"Expected (heads,1,64), got {arr.shape}")
        return arr[:, 0, :].reshape(arr.shape[0], 8, 8)
    raise ValueError(f"Expected attention scores with 3 or 4 dims, got {arr.shape}")


def representative_indices(y_true: np.ndarray, y_pred: np.ndarray, max_per_group: int = 2) -> dict[str, list[int]]:
    y = np.asarray(y_true).reshape(-1)
    p = np.asarray(y_pred).reshape(-1)
    groups = {
        "tp": np.where((y == 1) & (p == 1))[0],
        "fp": np.where((y == 0) & (p == 1))[0],
        "fn": np.where((y == 1) & (p == 0))[0],
        "tn": np.where((y == 0) & (p == 0))[0],
    }
    return {k: v[:max_per_group].astype(int).tolist() for k, v in groups.items()}


def write_attention_artifacts(
    *,
    model: keras.Model,
    data: dict[str, np.ndarray],
    y_pred: np.ndarray,
    output_dir: str | Path,
    max_per_group: int = 2,
) -> dict[str, Any]:
    y_true = np.asarray(data["y"], dtype=np.int64)
    reps = representative_indices(y_true, y_pred, max_per_group=max_per_group)
    flat_indices = [i for vals in reps.values() for i in vals]
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if not flat_indices:
        report = {"status": "not_written", "reason": "no representative binary TP/FP/FN/TN examples"}
        write_json(output_dir / "attention_report.json", report)
        return report
    subset = {
        "images": data["X"][flat_indices].astype(np.float32),
        "tabular": data["feats"][flat_indices].astype(np.float32),
        "survey_id": data["survey_id"][flat_indices].astype(np.int32),
        "filter_id": data["filter_id"][flat_indices].astype(np.int32),
    }
    scores = attention_scores_for_batch(model, subset)
    maps = attention_scores_to_maps(scores)
    np.savez_compressed(
        output_dir / "attention_maps.npz", indices=np.asarray(flat_indices, dtype=np.int64), scores=scores, maps=maps
    )
    report = {"status": "written", "indices": flat_indices, "groups": reps, "shape": list(maps.shape)}
    write_json(output_dir / "attention_report.json", report)
    return report
