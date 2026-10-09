"""Strict data contracts for RuBR-AT NPZ shards."""

from __future__ import annotations

import glob
import json
import shlex
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

EXPECTED_FEATURE_NAMES = [
    "arcsinh_snr",
    "cfit",
    "is_fit_clean",
    "log1p_chi2",
    "log1p_pos_err",
    "log_npixfit",
    "roundness1",
    "roundness2",
    "sharpness",
]
FIT_DEPENDENT_INDICES = [0, 1, 3, 4]
FIT_CLEAN_INDEX = 2


def coerce_feats(raw: object) -> np.ndarray:
    if isinstance(raw, np.ndarray) and raw.dtype != object:
        arr = np.asarray(raw, dtype=np.float32)
    else:
        rows = []
        for item in np.asarray(raw, dtype=object).reshape(-1):
            rows.append([item[name] for name in EXPECTED_FEATURE_NAMES] if isinstance(item, dict) else list(item))
        arr = np.asarray(rows, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    if arr.ndim != 2 or arr.shape[1] != len(EXPECTED_FEATURE_NAMES):
        raise ValueError(f"Expected features with shape (N, 9); got {arr.shape}")
    return arr.astype(np.float32, copy=False)


class FeatureZScore:
    """Train-fitted feature scaling with clean-fit masking."""

    def __init__(self, scaler_path: str | Path):
        self.path = Path(scaler_path)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("feature_names") != EXPECTED_FEATURE_NAMES:
            raise ValueError(f"Feature schema mismatch in {self.path}")
        self.mean = np.asarray(payload["mean"], dtype=np.float32)
        self.std = np.asarray(payload["std"], dtype=np.float32)
        expected = (len(EXPECTED_FEATURE_NAMES),)
        if self.mean.shape != expected or self.std.shape != expected:
            raise ValueError(f"Scaler mean/std must have shape {expected}")
        self.std = np.where(self.std <= 0, 1.0, self.std).astype(np.float32)

    def transform(self, raw: object) -> np.ndarray:
        feats = coerce_feats(raw)
        fit_mask = feats[:, FIT_CLEAN_INDEX : FIT_CLEAN_INDEX + 1]
        result = (feats - self.mean) / self.std
        result[:, FIT_CLEAN_INDEX] = feats[:, FIT_CLEAN_INDEX]
        result[:, FIT_DEPENDENT_INDICES] *= fit_mask
        return result.astype(np.float32)

    __call__ = transform


def resolve_npz_paths(paths: str | Path | Sequence[str | Path], *, split: str | None = None) -> list[Path]:
    items = [paths] if isinstance(paths, (str, Path)) else list(paths)
    expanded: list[str | Path] = []
    for item in items:
        expanded.extend(shlex.split(item) if isinstance(item, str) else [item])
    resolved: list[Path] = []
    for item in expanded:
        text = str(item)
        if any(char in text for char in "*?[]"):
            resolved.extend(Path(value) for value in glob.glob(text))
        else:
            path = Path(item)
            if path.is_dir():
                patterns = [f"{split}_*.npz", f"{split}.npz"] if split else ["*.npz"]
                for pattern in patterns:
                    resolved.extend(path.glob(pattern))
            else:
                resolved.append(path)
    result = sorted({path for path in resolved if path.is_file()}, key=lambda path: (str(path.parent), path.name))
    if not result:
        raise FileNotFoundError(f"No NPZ files resolved from {paths!r}")
    return result


def load_npz_dataset(
    path: str | Path | Sequence[str | Path],
    scaler: FeatureZScore | str | Path | None = None,
    *,
    split: str | None = None,
) -> dict[str, np.ndarray]:
    parts: dict[str, list[np.ndarray]] = {}
    for shard in resolve_npz_paths(path, split=split):
        with np.load(shard, allow_pickle=True) as data:
            for key in data.files:
                parts.setdefault(key, []).append(data[key])
    out = {key: np.concatenate(values, axis=0) for key, values in parts.items()}
    out["feats"] = coerce_feats(out["feats"])
    if scaler is not None:
        scaler_obj = scaler if isinstance(scaler, FeatureZScore) else FeatureZScore(scaler)
        out["feats"] = scaler_obj.transform(out["feats"])
    return out


def _import_tf():
    import tensorflow as tf

    from classification import gpu_config  # noqa: F401

    return tf


def make_dataset(
    path: str | Path | Sequence[str | Path],
    scaler: FeatureZScore | str | Path,
    *,
    split: str | None = None,
    label_key: str = "y",
    batch_size: int = 128,
    shuffle: bool = False,
    seed: int = 42,
    shuffle_buffer: int = 10_000,
    repeat: bool = False,
):
    tf = _import_tf()
    scaler_obj = scaler if isinstance(scaler, FeatureZScore) else FeatureZScore(scaler)
    shards = resolve_npz_paths(path, split=split)
    with np.load(shards[0], allow_pickle=True) as first:
        image_shape = tuple(first["X"].shape[1:])
        label_dtype = tf.as_dtype(first[label_key].dtype)

    def generator():
        rng = np.random.default_rng(seed)
        epoch_shards = list(shards)
        if shuffle:
            rng.shuffle(epoch_shards)
        for shard in epoch_shards:
            with np.load(shard, allow_pickle=True) as data:
                images = np.asarray(data["X"], dtype=np.float32)
                features = scaler_obj.transform(data["feats"])
                labels = data[label_key]
                surveys = np.asarray(data["survey_id"], dtype=np.int32)
                filters = np.asarray(data["filter_id"], dtype=np.int32)
                order = rng.permutation(len(labels)) if shuffle else np.arange(len(labels))
                for index in order:
                    yield (
                        {
                            "image": images[index],
                            "feats": features[index],
                            "survey_id": surveys[index],
                            "filter_id": filters[index],
                        },
                        labels[index],
                    )

    signature = (
        {
            "image": tf.TensorSpec(image_shape, tf.float32),
            "feats": tf.TensorSpec((len(EXPECTED_FEATURE_NAMES),), tf.float32),
            "survey_id": tf.TensorSpec((), tf.int32),
            "filter_id": tf.TensorSpec((), tf.int32),
        },
        tf.TensorSpec((), label_dtype),
    )
    dataset = tf.data.Dataset.from_generator(generator, output_signature=signature)
    if repeat:
        dataset = dataset.repeat()
    if shuffle:
        dataset = dataset.shuffle(shuffle_buffer, seed=seed, reshuffle_each_iteration=True)
    return dataset.batch(batch_size).prefetch(tf.data.AUTOTUNE)


def save_feature_scaler(train_npz_paths: Iterable[str | Path], output: str | Path) -> dict:
    paths = resolve_npz_paths(list(train_npz_paths))
    n_rows = 0
    sum_x = np.zeros(len(EXPECTED_FEATURE_NAMES), dtype=np.float64)
    sum_x2 = np.zeros_like(sum_x)
    for path in paths:
        with np.load(path, allow_pickle=True, mmap_mode="r") as data:
            feats = np.nan_to_num(coerce_feats(data["feats"]).astype(np.float64))
            n_rows += len(feats)
            sum_x += feats.sum(axis=0)
            sum_x2 += np.square(feats).sum(axis=0)
    if n_rows == 0:
        raise ValueError("Training shards contain no rows")
    mean = sum_x / n_rows
    std = np.sqrt(np.maximum(sum_x2 / n_rows - np.square(mean), 0.0))
    std = np.where(std <= 0, 1.0, std)
    mean[FIT_CLEAN_INDEX] = 0.0
    std[FIT_CLEAN_INDEX] = 1.0
    payload = {
        "schema_version": "rubrat_feature_scaler_v1",
        "feature_names": EXPECTED_FEATURE_NAMES,
        "mean": mean.tolist(),
        "std": std.tolist(),
        "n_rows": n_rows,
        "train_npz": [str(path) for path in paths],
    }
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload
