"""Shared Phase 3 training helpers."""

from __future__ import annotations

import csv
import json
import math
import os
import subprocess
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import tensorflow as tf
from tensorflow import keras

from classification import gpu_config  # noqa: F401 — restrict CUDA to GPU 0 before TF init
from classification.data_utils import FeatureZScore, load_npz_dataset, make_dataset, resolve_npz_paths
from classification.metrics import classification_report, compute_optimal_threshold, macro_f1


def set_reproducible(seed: int = 42) -> None:
    np.random.seed(seed)
    tf.keras.utils.set_random_seed(seed)


def prefer_gpu_memory_growth() -> list[str]:
    gpus = tf.config.list_physical_devices("GPU")
    require_gpu = os.environ.get("RUBRAT_REQUIRE_GPU", os.environ.get("UNIFIED_RAPID_REQUIRE_GPU"))
    if require_gpu == "1" and not gpus:
        raise RuntimeError("RUBRAT_REQUIRE_GPU=1 but TensorFlow cannot see a GPU")
    if len(gpus) > 1:
        tf.config.set_visible_devices(gpus[0], "GPU")
        gpus = tf.config.list_physical_devices("GPU")
    for gpu in gpus:
        try:
            tf.config.experimental.set_memory_growth(gpu, True)
        except RuntimeError:
            pass
    return [gpu.name for gpu in gpus]


def git_commit(root: str | Path | None = None) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def save_history(history: keras.callbacks.History | dict[str, list[float]], output_dir: str | Path) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    data = history.history if isinstance(history, keras.callbacks.History) else history
    write_json(output_dir / "history.json", {k: [float(x) for x in v] for k, v in data.items()})
    keys = sorted(data)
    rows = max((len(v) for v in data.values()), default=0)
    with (output_dir / "history.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["epoch", *keys])
        writer.writeheader()
        for i in range(rows):
            writer.writerow({"epoch": i + 1, **{k: float(data[k][i]) if i < len(data[k]) else "" for k in keys}})
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return
    for key, values in data.items():
        if not values or key.startswith("val_"):
            continue
        plt.figure()
        plt.plot(range(1, len(values) + 1), values, label=key)
        val_key = f"val_{key}"
        if val_key in data:
            plt.plot(range(1, len(data[val_key]) + 1), data[val_key], label=val_key)
        plt.xlabel("epoch")
        plt.ylabel(key)
        plt.legend()
        plt.tight_layout()
        plt.savefig(output_dir / f"{key}.png")
        plt.close()


def model_inputs(batch_inputs: dict[str, tf.Tensor]) -> dict[str, tf.Tensor]:
    return {
        "images": batch_inputs["image"],
        "tabular": batch_inputs["feats"],
        "survey_id": batch_inputs["survey_id"],
        "filter_id": batch_inputs["filter_id"],
    }


def map_for_output(ds: tf.data.Dataset, output_name: str) -> tf.data.Dataset:
    return ds.map(
        lambda x, y: (model_inputs(x), {output_name: y}),
        num_parallel_calls=tf.data.AUTOTUNE,
    )


def map_for_stage_b(ds: tf.data.Dataset, *, augment: bool = True) -> tf.data.Dataset:
    noise_std = tf.constant(0.01, dtype=tf.float32)
    translator = tf.keras.layers.RandomTranslation(
        height_factor=(-0.015, 0.015),
        width_factor=(-0.015, 0.015),
        fill_mode="constant",
        fill_value=0.0,
    )

    def _map(x, y):
        image = x["image"]
        is_oversampled = x.get("is_oversampled", tf.zeros(tf.shape(y), dtype=tf.bool))
        if augment:
            translated = translator(image, training=True)
            noisy = translated + tf.random.normal(tf.shape(translated), stddev=noise_std, dtype=tf.float32)
            mask = tf.reshape(tf.cast(is_oversampled, tf.bool), (-1, 1, 1, 1))
            image = tf.where(mask, noisy, image)
        clean = dict(x)
        clean["image"] = tf.cast(image, tf.float32)
        clean.pop("is_oversampled", None)
        return model_inputs(clean), {"sn": y}

    return ds.map(_map, num_parallel_calls=tf.data.AUTOTUNE)


def dataset_cardinality(paths: str | Path | Sequence[str | Path], label_key: str = "y") -> int:
    total = 0
    for shard in resolve_npz_paths(paths):
        with np.load(shard, allow_pickle=True) as z:
            total += int(z[label_key].shape[0])
    return total


def class_counts(paths: str | Path | Sequence[str | Path], label_key: str, num_classes: int) -> np.ndarray:
    counts = np.zeros(num_classes, dtype=np.int64)
    for shard in resolve_npz_paths(paths):
        with np.load(shard, allow_pickle=True) as z:
            labels = np.asarray(z[label_key], dtype=np.int64)
            labels = labels[(labels >= 0) & (labels < num_classes)]
            counts += np.bincount(labels, minlength=num_classes)[:num_classes]
    return counts


def balanced_class_weights(counts: Iterable[int]) -> dict[int, float]:
    counts_arr = np.asarray(list(counts), dtype=np.float64)
    nonzero = counts_arr > 0
    total = counts_arr[nonzero].sum()
    n_classes = nonzero.sum()
    weights = np.ones_like(counts_arr, dtype=np.float64)
    weights[nonzero] = total / (n_classes * counts_arr[nonzero])
    return {int(i): float(w) for i, w in enumerate(weights)}


def balanced_sample_weight_lookup(
    paths: str | Path | Sequence[str | Path],
    *,
    label_key: str = "y",
    group_keys: Sequence[str],
    max_weight: float | None = None,
) -> dict[tuple, float]:
    """Inverse-frequency sample weights keyed by train-shard group tuples."""
    counts: dict[tuple, int] = {}
    total = 0
    for shard in resolve_npz_paths(paths):
        with np.load(shard, allow_pickle=True) as z:
            labels = np.asarray(z[label_key], dtype=np.int64)
            survey = (
                np.asarray(z["survey_id"], dtype=np.int32)
                if "survey_id" in z.files
                else np.zeros(len(labels), np.int32)
            )
            filt = (
                np.asarray(z["filter_id"], dtype=np.int32)
                if "filter_id" in z.files
                else np.zeros(len(labels), np.int32)
            )
            meta = (
                np.asarray(z["metadata"], dtype=object)
                if "metadata" in z.files
                else np.empty(len(labels), dtype=object)
            )
            for i in range(len(labels)):
                meta_dict = dict(meta[i]) if isinstance(meta[i], dict) else {}
                values = {
                    "survey_id": int(survey[i]),
                    "filter_id": int(filt[i]),
                    "y": int(labels[i]),
                    "label": int(labels[i]),
                    "jid": str(meta_dict.get("jid", "")),
                }
                key = tuple(values[name] for name in group_keys)
                counts[key] = counts.get(key, 0) + 1
                total += 1
    if not counts:
        return {}
    n_groups = len(counts)
    lookup: dict[tuple, float] = {}
    for key, cnt in counts.items():
        weight = float(total) / (n_groups * cnt)
        if max_weight is not None:
            weight = min(weight, float(max_weight))
        lookup[key] = weight
    return lookup


def _meta_dict(value: object) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _group_value(
    *,
    group_key: str,
    label: int,
    survey_id: int,
    filter_id: int,
    metadata: object,
) -> int | str:
    meta = _meta_dict(metadata)
    values = {
        "survey_id": int(survey_id),
        "filter_id": int(filter_id),
        "y": int(label),
        "label": int(label),
        "jid": str(meta.get("jid", "")),
    }
    if group_key not in values:
        raise ValueError(f"Unsupported group key {group_key!r}")
    return values[group_key]


def _sample_weight_for(
    *,
    lookup: dict[tuple, float],
    group_keys: Sequence[str],
    label: int,
    survey_id: int,
    filter_id: int,
    metadata: object,
    default: float = 1.0,
) -> np.float32:
    key = tuple(
        _group_value(
            group_key=name,
            label=label,
            survey_id=survey_id,
            filter_id=filter_id,
            metadata=metadata,
        )
        for name in group_keys
    )
    return np.asarray(float(lookup.get(key, default)), dtype=np.float32)


def dataset_group_audit(
    paths: str | Path | Sequence[str | Path],
    *,
    label_key: str = "y",
    group_key: str = "jid",
    num_classes: int = 2,
) -> dict[str, Any]:
    """Summarize row/class/group composition for NPZ training splits."""
    class_count = np.zeros(num_classes, dtype=np.int64)
    survey_counts: dict[str, int] = {}
    filter_counts: dict[str, int] = {}
    group_counts: dict[str, dict[str, Any]] = {}
    total = 0
    shards = resolve_npz_paths(paths)
    for shard in shards:
        with np.load(shard, allow_pickle=True) as z:
            labels = np.asarray(z[label_key], dtype=np.int64)
            survey = (
                np.asarray(z["survey_id"], dtype=np.int32)
                if "survey_id" in z.files
                else np.zeros(len(labels), np.int32)
            )
            filt = (
                np.asarray(z["filter_id"], dtype=np.int32)
                if "filter_id" in z.files
                else np.zeros(len(labels), np.int32)
            )
            meta = (
                np.asarray(z["metadata"], dtype=object)
                if "metadata" in z.files
                else np.empty(len(labels), dtype=object)
            )
            total += int(labels.shape[0])
            valid = labels[(labels >= 0) & (labels < num_classes)]
            class_count += np.bincount(valid, minlength=num_classes)[:num_classes]
            for value, count in zip(*np.unique(survey, return_counts=True)):
                key = str(int(value))
                survey_counts[key] = survey_counts.get(key, 0) + int(count)
            for value, count in zip(*np.unique(filt, return_counts=True)):
                key = str(int(value))
                filter_counts[key] = filter_counts.get(key, 0) + int(count)
            for i, label in enumerate(labels):
                group = str(
                    _group_value(
                        group_key=group_key,
                        label=int(label),
                        survey_id=int(survey[i]),
                        filter_id=int(filt[i]),
                        metadata=meta[i] if i < len(meta) else {},
                    )
                )
                entry = group_counts.setdefault(
                    group,
                    {"n": 0, "class_counts": {str(cls): 0 for cls in range(num_classes)}},
                )
                entry["n"] += 1
                if 0 <= int(label) < num_classes:
                    entry["class_counts"][str(int(label))] += 1
    group_sizes = np.asarray([entry["n"] for entry in group_counts.values()], dtype=np.int64)
    return {
        "paths": [str(p) for p in shards],
        "label_key": label_key,
        "group_key": group_key,
        "n_rows": int(total),
        "class_counts": {str(i): int(v) for i, v in enumerate(class_count)},
        "positive_fraction": float(class_count[1] / total) if num_classes > 1 and total else None,
        "survey_counts": survey_counts,
        "filter_counts": filter_counts,
        "n_groups": int(len(group_counts)),
        "group_size_min": int(group_sizes.min()) if group_sizes.size else 0,
        "group_size_median": float(np.median(group_sizes)) if group_sizes.size else 0.0,
        "group_size_max": int(group_sizes.max()) if group_sizes.size else 0,
        "groups": group_counts,
    }


def dataset_audit_bundle(
    splits: dict[str, str | Path | Sequence[str | Path] | None],
    *,
    label_key: str = "y",
    group_key: str = "jid",
    num_classes: int = 2,
) -> dict[str, Any]:
    audits = {
        name: dataset_group_audit(paths, label_key=label_key, group_key=group_key, num_classes=num_classes)
        for name, paths in splits.items()
        if paths is not None
    }
    overlaps: dict[str, int] = {}
    names = sorted(audits)
    for i, left in enumerate(names):
        left_groups = set(audits[left]["groups"])
        for right in names[i + 1 :]:
            overlaps[f"{left}__{right}"] = len(left_groups & set(audits[right]["groups"]))
    return {
        "schema_version": "npz_group_audit_v1",
        "splits": audits,
        "group_overlaps": overlaps,
    }


@keras.utils.register_keras_serializable(package="unified_rapid")
class WarmupCosineDecay(keras.optimizers.schedules.LearningRateSchedule):
    def __init__(
        self,
        initial_learning_rate: float,
        total_steps: int,
        warmup_steps: int = 0,
        min_learning_rate: float = 0.0,
        name: str = "warmup_cosine_decay",
    ):
        self.initial_learning_rate = float(initial_learning_rate)
        self.total_steps = max(1, int(total_steps))
        self.warmup_steps = max(0, int(warmup_steps))
        self.min_learning_rate = float(min_learning_rate)
        self.name = str(name)

    def __call__(self, step: tf.Tensor) -> tf.Tensor:
        with tf.name_scope(self.name):
            step = tf.cast(step, tf.float32)
            initial_lr = tf.cast(self.initial_learning_rate, tf.float32)
            min_lr = tf.cast(self.min_learning_rate, tf.float32)
            total_steps = tf.cast(self.total_steps, tf.float32)
            warmup_steps = tf.cast(self.warmup_steps, tf.float32)

            if self.warmup_steps > 0:
                warmup_progress = tf.clip_by_value(step / tf.maximum(1.0, warmup_steps), 0.0, 1.0)
                warmup_lr = initial_lr * warmup_progress
            else:
                warmup_lr = initial_lr

            cosine_steps = tf.maximum(1.0, total_steps - warmup_steps)
            cosine_progress = tf.clip_by_value((step - warmup_steps) / cosine_steps, 0.0, 1.0)
            cosine_decay = 0.5 * (1.0 + tf.cos(tf.constant(math.pi, dtype=tf.float32) * cosine_progress))
            cosine_lr = min_lr + (initial_lr - min_lr) * cosine_decay

            if self.warmup_steps > 0:
                return tf.where(step < warmup_steps, warmup_lr, cosine_lr)
            return cosine_lr

    def get_config(self) -> dict[str, Any]:
        return {
            "initial_learning_rate": self.initial_learning_rate,
            "total_steps": self.total_steps,
            "warmup_steps": self.warmup_steps,
            "min_learning_rate": self.min_learning_rate,
            "name": self.name,
        }


class ValidationReportCallback(keras.callbacks.Callback):
    def __init__(
        self,
        val_paths: str | Path | Sequence[str | Path],
        scaler: FeatureZScore | str | Path,
        *,
        output_name: str,
        label_key: str,
        num_classes: int,
        output_dir: str | Path,
        threshold: float = 0.5,
        group_key: str | None = None,
        group_names: dict[int, str] | None = None,
        robust_alpha: float = 0.5,
        tune_threshold: bool = False,
    ):
        super().__init__()
        self.val_paths = val_paths
        self.scaler = scaler
        self.output_name = output_name
        self.label_key = label_key
        self.num_classes = int(num_classes)
        self.output_dir = Path(output_dir)
        self.threshold = float(threshold)
        self.group_key = group_key
        self.group_names = {int(k): str(v) for k, v in (group_names or {}).items()}
        self.robust_alpha = float(robust_alpha)
        self.tune_threshold = bool(tune_threshold)
        self.best = -np.inf

    def on_epoch_end(self, epoch, logs=None):
        logs = logs if logs is not None else {}
        data = load_npz_dataset(self.val_paths, self.scaler)
        inputs = {
            "images": data["X"].astype(np.float32),
            "tabular": data["feats"].astype(np.float32),
            "survey_id": data["survey_id"].astype(np.int32),
            "filter_id": data["filter_id"].astype(np.int32),
        }
        probs = self.model.predict(inputs, verbose=0)[self.output_name]
        y_true = np.asarray(data[self.label_key], dtype=np.int64)
        if self.num_classes == 2 and probs.ndim == 2 and probs.shape[1] == 1:
            scores = probs.reshape(-1)
            y_pred = (scores >= self.threshold).astype(np.int64)
        else:
            scores = None
            y_pred = np.argmax(probs, axis=-1).astype(np.int64)
        score = macro_f1(y_true, y_pred, self.num_classes)
        key = f"val_{self.output_name}_macro_f1"
        logs[key] = score
        report = classification_report(y_true, y_pred, [str(i) for i in range(self.num_classes)])
        report["threshold"] = self.threshold
        if self.tune_threshold and scores is not None and len(np.unique(y_true)) >= 2:
            opt = compute_optimal_threshold(y_true, scores)
            logs[f"val_{self.output_name}_opt_macro_f1"] = float(opt["macro_f1"])
            logs[f"val_{self.output_name}_opt_threshold"] = float(opt["threshold"])
            report["optimal_threshold"] = opt
        if self.group_key is not None:
            group_values = np.asarray(
                [
                    _group_value(
                        group_key=self.group_key,
                        label=int(y_true[i]),
                        survey_id=int(data["survey_id"][i]) if "survey_id" in data else 0,
                        filter_id=int(data["filter_id"][i]) if "filter_id" in data else 0,
                        metadata=data["metadata"][i] if "metadata" in data else {},
                    )
                    for i in range(len(y_true))
                ],
                dtype=object,
            )
            group_scores = []
            per_group = {}
            for raw_group in sorted(set(group_values), key=str):
                mask = group_values == raw_group
                group_true = y_true[mask]
                group_pred = y_pred[mask]
                group_score = macro_f1(group_true, group_pred, self.num_classes)
                group_scores.append(group_score)
                group_name = (
                    self.group_names.get(raw_group, str(raw_group)) if isinstance(raw_group, int) else str(raw_group)
                )
                entry = classification_report(group_true, group_pred, [str(i) for i in range(self.num_classes)])
                entry["n"] = int(mask.sum())
                entry["group_value"] = int(raw_group) if isinstance(raw_group, (int, np.integer)) else str(raw_group)
                if self.tune_threshold and scores is not None and len(np.unique(group_true)) >= 2:
                    entry["optimal_threshold"] = compute_optimal_threshold(group_true, scores[mask])
                per_group[group_name] = entry
            if group_scores:
                group_arr = np.asarray(group_scores, dtype=np.float64)
                group_mean = float(np.mean(group_arr))
                group_min = float(np.min(group_arr))
                group_robust = float(self.robust_alpha * group_mean + (1.0 - self.robust_alpha) * group_min)
                logs[f"val_{self.output_name}_group_macro_f1_mean"] = group_mean
                logs[f"val_{self.output_name}_group_macro_f1_min"] = group_min
                logs[f"val_{self.output_name}_group_robust_macro_f1"] = group_robust
                report["group_metric"] = {
                    "group_key": self.group_key,
                    "macro_f1_mean": group_mean,
                    "macro_f1_min": group_min,
                    "robust_alpha": self.robust_alpha,
                    "robust_macro_f1": group_robust,
                }
                report["per_group"] = per_group
        report["epoch"] = int(epoch + 1)
        report["output_name"] = self.output_name
        report["label_key"] = self.label_key
        write_json(self.output_dir / "latest_validation_report.json", report)
        if score > self.best:
            self.best = score
            write_json(self.output_dir / "best_validation_report.json", report)


def make_finite_training_dataset(
    paths: str | Path | Sequence[str | Path],
    scaler: FeatureZScore | str | Path,
    *,
    label_key: str,
    output_name: str,
    batch_size: int,
    shuffle: bool,
    seed: int,
    repeat: bool = True,
) -> tf.data.Dataset:
    """Build a streaming dataset for ``model.fit`` with fixed step counts.

    ``repeat`` defaults to True so train/val iterators stay valid across epochs.
    """
    return map_for_output(
        make_dataset(
            paths,
            scaler,
            label_key=label_key,
            batch_size=batch_size,
            shuffle=shuffle,
            seed=seed,
            repeat=repeat,
        ),
        output_name,
    )


def make_grouped_finite_training_dataset(
    paths: str | Path | Sequence[str | Path],
    scaler: FeatureZScore | str | Path,
    *,
    label_key: str,
    output_name: str,
    batch_size: int,
    seed: int,
    group_key: str = "jid",
    max_rows_per_group_per_epoch: int | None = None,
    repeat: bool = True,
    sample_weight_lookup: dict[tuple, float] | None = None,
    sample_weight_group_keys: Sequence[str] = ("jid", "y"),
    sample_weight_default: float = 1.0,
) -> tf.data.Dataset:
    """Build an in-memory grouped dataset that samples groups before rows.

    This is intended for small effective-sample datasets where many rows from
    the same visit/object would otherwise dominate an epoch.
    """
    data = load_npz_dataset(paths, scaler)
    labels = np.asarray(data[label_key])
    survey = np.asarray(data["survey_id"], dtype=np.int32)
    filt = np.asarray(data["filter_id"], dtype=np.int32)
    meta = np.asarray(data["metadata"], dtype=object) if "metadata" in data else np.empty(len(labels), dtype=object)
    groups: dict[int | str, list[int]] = {}
    for i, label in enumerate(labels):
        value = _group_value(
            group_key=group_key,
            label=int(label),
            survey_id=int(survey[i]),
            filter_id=int(filt[i]),
            metadata=meta[i] if i < len(meta) else {},
        )
        groups.setdefault(value, []).append(i)
    group_items = [(group, np.asarray(indices, dtype=np.int64)) for group, indices in groups.items()]
    if not group_items:
        raise ValueError(f"No groups found in {paths!r} for group_key={group_key!r}")
    include_sample_weight = sample_weight_lookup is not None
    lookup = sample_weight_lookup or {}
    weight_keys = tuple(sample_weight_group_keys)

    def gen():
        rng = np.random.default_rng(seed)
        while True:
            group_order = rng.permutation(len(group_items))
            for group_idx in group_order:
                _, indices = group_items[int(group_idx)]
                if max_rows_per_group_per_epoch is not None and len(indices) > max_rows_per_group_per_epoch:
                    picked = rng.choice(indices, size=int(max_rows_per_group_per_epoch), replace=False)
                else:
                    picked = np.array(indices, copy=True)
                rng.shuffle(picked)
                for i in picked:
                    inputs = {
                        "images": data["X"][i].astype(np.float32),
                        "tabular": data["feats"][i].astype(np.float32),
                        "survey_id": np.asarray(survey[i], dtype=np.int32),
                        "filter_id": np.asarray(filt[i], dtype=np.int32),
                    }
                    targets = {output_name: labels[i]}
                    if include_sample_weight:
                        yield (
                            inputs,
                            targets,
                            {
                                output_name: _sample_weight_for(
                                    lookup=lookup,
                                    group_keys=weight_keys,
                                    label=int(labels[i]),
                                    survey_id=int(survey[i]),
                                    filter_id=int(filt[i]),
                                    metadata=meta[i] if i < len(meta) else {},
                                    default=sample_weight_default,
                                )
                            },
                        )
                    else:
                        yield inputs, targets
            if not repeat:
                break

    image_shape = tuple(data["X"].shape[1:])
    feature_dim = int(data["feats"].shape[1])
    base_signature = (
        {
            "images": tf.TensorSpec(shape=image_shape, dtype=tf.float32),
            "tabular": tf.TensorSpec(shape=(feature_dim,), dtype=tf.float32),
            "survey_id": tf.TensorSpec(shape=(), dtype=tf.int32),
            "filter_id": tf.TensorSpec(shape=(), dtype=tf.int32),
        },
        {output_name: tf.TensorSpec(shape=(), dtype=tf.as_dtype(labels.dtype))},
    )
    if include_sample_weight:
        output_signature = (*base_signature, {output_name: tf.TensorSpec(shape=(), dtype=tf.float32)})
    else:
        output_signature = base_signature
    ds = tf.data.Dataset.from_generator(gen, output_signature=output_signature)
    return ds.batch(batch_size).prefetch(tf.data.AUTOTUNE)


def make_weighted_finite_training_dataset(
    paths: str | Path | Sequence[str | Path],
    scaler: FeatureZScore | str | Path,
    *,
    label_key: str,
    output_name: str,
    batch_size: int,
    shuffle: bool,
    seed: int,
    repeat: bool = True,
    sample_weight_lookup: dict[tuple, float],
    sample_weight_group_keys: Sequence[str],
    sample_weight_default: float = 1.0,
) -> tf.data.Dataset:
    """Build an in-memory globally shuffled dataset with Keras sample weights."""
    data = load_npz_dataset(paths, scaler)
    labels = np.asarray(data[label_key])
    survey = np.asarray(data["survey_id"], dtype=np.int32)
    filt = np.asarray(data["filter_id"], dtype=np.int32)
    meta = np.asarray(data["metadata"], dtype=object) if "metadata" in data else np.empty(len(labels), dtype=object)
    n = int(labels.shape[0])
    if n == 0:
        raise ValueError(f"No rows found in {paths!r}")
    weight_keys = tuple(sample_weight_group_keys)

    def gen():
        rng = np.random.default_rng(seed)
        while True:
            order = rng.permutation(n) if shuffle else np.arange(n)
            for i in order:
                inputs = {
                    "images": data["X"][i].astype(np.float32),
                    "tabular": data["feats"][i].astype(np.float32),
                    "survey_id": np.asarray(survey[i], dtype=np.int32),
                    "filter_id": np.asarray(filt[i], dtype=np.int32),
                }
                yield (
                    inputs,
                    {output_name: labels[i]},
                    {
                        output_name: _sample_weight_for(
                            lookup=sample_weight_lookup,
                            group_keys=weight_keys,
                            label=int(labels[i]),
                            survey_id=int(survey[i]),
                            filter_id=int(filt[i]),
                            metadata=meta[i] if i < len(meta) else {},
                            default=sample_weight_default,
                        )
                    },
                )
            if not repeat:
                break

    image_shape = tuple(data["X"].shape[1:])
    feature_dim = int(data["feats"].shape[1])
    output_signature = (
        {
            "images": tf.TensorSpec(shape=image_shape, dtype=tf.float32),
            "tabular": tf.TensorSpec(shape=(feature_dim,), dtype=tf.float32),
            "survey_id": tf.TensorSpec(shape=(), dtype=tf.int32),
            "filter_id": tf.TensorSpec(shape=(), dtype=tf.int32),
        },
        {output_name: tf.TensorSpec(shape=(), dtype=tf.as_dtype(labels.dtype))},
        {output_name: tf.TensorSpec(shape=(), dtype=tf.float32)},
    )
    ds = tf.data.Dataset.from_generator(gen, output_signature=output_signature)
    return ds.batch(batch_size).prefetch(tf.data.AUTOTUNE)


def steps_for(paths: str | Path | Sequence[str | Path], label_key: str, batch_size: int) -> int:
    return int(math.ceil(dataset_cardinality(paths, label_key=label_key) / batch_size))
