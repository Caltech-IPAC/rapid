"""Positive-unlabeled training helpers for RB models."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import numpy as np

from classification.data_utils import EXPECTED_FEATURE_NAMES, FeatureZScore, resolve_npz_paths


def _positive_group(meta: object, shard: Path, index: int) -> str:
    row = dict(meta) if isinstance(meta, dict) else {}
    truth_id = row.get("truth_id")
    if truth_id is not None and str(truth_id).lower() not in {"", "nan", "none"}:
        return f"truth:{truth_id}"
    jid = row.get("jid", shard.stem)
    x = row.get("xcentroid", row.get("ra", index))
    y = row.get("ycentroid", row.get("dec", index))
    return f"fallback:{jid}:{x}:{y}"


def controlled_scar_labels(
    paths: str | Path | Sequence[str | Path], *, labeled_positive_fraction: float, selection_seed: int
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    """Hide source groups of truth-positive rows under a reproducible SCAR draw."""
    if not 0.0 < labeled_positive_fraction <= 1.0:
        raise ValueError("labeled_positive_fraction must be in (0, 1]")
    shards = resolve_npz_paths(paths)
    groups: set[str] = set()
    cached: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for shard in shards:
        with np.load(shard, allow_pickle=True) as z:
            if "y" not in z.files or "metadata" not in z.files:
                raise ValueError(f"Controlled PU construction requires y and metadata in {shard}")
            truth = np.asarray(z["y"], dtype=np.int64)
            sidecar = shard.with_suffix(".pu_groups.npy")
            if sidecar.exists():
                group = np.asarray(np.load(sidecar, allow_pickle=True), dtype=object)
                if group.shape != truth.shape:
                    raise ValueError(f"PU group sidecar {sidecar} has shape {group.shape}, expected {truth.shape}")
            else:
                group = np.asarray([_positive_group(m, shard, i) for i, m in enumerate(z["metadata"])], dtype=object)
        groups.update(group[truth == 1].tolist())
        cached[str(shard)] = truth, group
    ordered = np.asarray(sorted(groups), dtype=object)
    rng = np.random.default_rng(selection_seed)
    n_known = max(1, int(round(labeled_positive_fraction * len(ordered))))
    known = set(rng.choice(ordered, size=n_known, replace=False).tolist())
    labels: dict[str, np.ndarray] = {}
    n_pos = n_hidden = n_neg = 0
    for shard, (truth, group) in cached.items():
        pu = ((truth == 1) & np.isin(group, list(known))).astype(np.float32)
        labels[shard] = pu
        n_pos += int(pu.sum())
        n_hidden += int(np.sum((truth == 1) & (pu == 0)))
        n_neg += int(np.sum(truth == 0))
    report = {
        "mechanism": "SCAR source-group draw",
        "selection_seed": int(selection_seed),
        "labeled_positive_fraction_requested": float(labeled_positive_fraction),
        "positive_groups_total": int(len(ordered)),
        "positive_groups_labeled": int(len(known)),
        "known_positive_rows": n_pos,
        "hidden_positive_rows": n_hidden,
        "truth_negative_rows": n_neg,
        "unlabeled_rows": n_hidden + n_neg,
        "true_unlabeled_positive_prior": float(n_hidden / max(1, n_hidden + n_neg)),
    }
    return labels, report


def nnpu_risk(y_pu, y_pred, positive_prior: float, *, eps: float = 1e-7) -> dict[str, Any]:
    """Compute non-negative PU risk with binary cross entropy components.

    ``y_pu`` is ``1`` for confirmed positives and ``0`` for unlabeled rows.
    ``y_pred`` is the model probability for the positive/transient class.
    """
    import tensorflow as tf

    from classification import gpu_config  # noqa: F401

    pi_p = tf.cast(positive_prior, tf.float32)
    y = tf.cast(tf.reshape(y_pu, (-1,)), tf.float32)
    p = tf.clip_by_value(tf.cast(tf.reshape(y_pred, (-1,)), tf.float32), eps, 1.0 - eps)
    pos_mask = tf.equal(y, 1.0)
    unl_mask = tf.equal(y, 0.0)
    n_positive = tf.reduce_sum(tf.cast(pos_mask, tf.int32))
    n_unlabeled = tf.reduce_sum(tf.cast(unl_mask, tf.int32))

    pos_loss = -tf.math.log(p)
    neg_loss = -tf.math.log(1.0 - p)

    positive_loss_on_positive = tf.cond(
        n_positive > 0,
        lambda: tf.reduce_mean(tf.boolean_mask(pos_loss, pos_mask)),
        lambda: tf.constant(0.0, dtype=tf.float32),
    )
    negative_loss_on_positive = tf.cond(
        n_positive > 0,
        lambda: tf.reduce_mean(tf.boolean_mask(neg_loss, pos_mask)),
        lambda: tf.constant(0.0, dtype=tf.float32),
    )
    negative_loss_on_unlabeled = tf.cond(
        n_unlabeled > 0,
        lambda: tf.reduce_mean(tf.boolean_mask(neg_loss, unl_mask)),
        lambda: tf.constant(0.0, dtype=tf.float32),
    )

    positive_risk = pi_p * positive_loss_on_positive
    raw_negative_risk = negative_loss_on_unlabeled - pi_p * negative_loss_on_positive
    negative_risk = tf.maximum(tf.constant(0.0, dtype=tf.float32), raw_negative_risk)
    total = positive_risk + negative_risk
    return {
        "risk": total,
        "positive_risk": positive_risk,
        "negative_risk": negative_risk,
        "raw_negative_risk": raw_negative_risk,
        "n_positive": n_positive,
        "n_unlabeled": n_unlabeled,
    }


def validate_pu_shards(
    paths: str | Path | Sequence[str | Path],
    *,
    expected_survey_id: int | None = None,
    verbose: bool = False,
) -> dict[str, int]:
    """Validate PU arrays and return aggregate status counts."""
    counts = {"rows": 0, "positive": 0, "unlabeled": 0, "trusted_negative": 0}
    shards = resolve_npz_paths(paths)
    for shard_i, shard in enumerate(shards, start=1):
        if verbose:
            print(f"validating PU shard {shard_i}/{len(shards)}: {shard}", flush=True)
        with np.load(shard, allow_pickle=True) as z:
            missing = {"X", "feats", "survey_id", "filter_id", "pu_label", "pu_status"} - set(z.files)
            if missing:
                raise ValueError(f"{shard} missing required PU arrays: {sorted(missing)}")
            if expected_survey_id is not None:
                survey = np.asarray(z["survey_id"], dtype=np.int64)
                bad = survey[survey != int(expected_survey_id)]
                if bad.size:
                    raise ValueError(
                        f"{shard} contains survey_id values outside {expected_survey_id}: "
                        f"{sorted(set(int(x) for x in bad.tolist()))}"
                    )
            labels = np.asarray(z["pu_label"], dtype=np.int64)
            statuses = np.asarray(z["pu_status"], dtype=np.int64)
            if labels.shape[0] != z["X"].shape[0] or statuses.shape[0] != z["X"].shape[0]:
                raise ValueError(f"{shard} has PU arrays with row counts inconsistent with X")
            counts["rows"] += int(labels.shape[0])
            counts["positive"] += int(np.sum(labels == 1))
            counts["unlabeled"] += int(np.sum(statuses == 0))
            counts["trusted_negative"] += int(np.sum(statuses == 2))
    return counts


def make_pu_dataset(
    paths: str | Path | Sequence[str | Path],
    scaler: FeatureZScore | str | Path,
    *,
    batch_size: int,
    shuffle: bool,
    seed: int = 42,
    repeat: bool = True,
    expected_survey_id: int | None = None,
    verbose: bool = False,
    controlled_labels: dict[str, np.ndarray] | None = None,
    pool_mode: str = "pu",
):
    """Create a PU ``tf.data.Dataset`` yielding model inputs and ``pu_label``."""
    import tensorflow as tf

    from classification import gpu_config  # noqa: F401

    if controlled_labels is None:
        validate_pu_shards(paths, expected_survey_id=expected_survey_id, verbose=verbose)
    scaler_obj = scaler if isinstance(scaler, FeatureZScore) else FeatureZScore(scaler)
    shards = resolve_npz_paths(paths)
    with np.load(shards[0], allow_pickle=True) as first:
        image_shape = tuple(first["X"].shape[1:])

    def gen():
        rng = np.random.default_rng(seed)
        while True:
            epoch_shards = list(shards)
            if shuffle:
                rng.shuffle(epoch_shards)
            for shard in epoch_shards:
                if verbose:
                    print(f"loading PU shard: {shard}", flush=True)
                with np.load(shard, allow_pickle=True) as z:
                    X = z["X"].astype(np.float32)
                    feats = scaler_obj.transform(z["feats"])
                    survey_id = z["survey_id"].astype(np.int32)
                    filter_id = z["filter_id"].astype(np.int32)
                    labels = (controlled_labels[str(shard)] if controlled_labels is not None else z["pu_label"]).astype(np.float32)
                    n = int(labels.shape[0])
                    order = rng.permutation(n) if shuffle else np.arange(n)
                    if controlled_labels is not None and pool_mode == "supervised_subset":
                        truth = np.asarray(z["y"], dtype=np.int64)
                        order = order[~((truth[order] == 1) & (labels[order] == 0))]
                    elif pool_mode not in {"pu", "supervised_subset"}:
                        raise ValueError(f"Unsupported controlled PU pool_mode {pool_mode!r}")
                    for i in order:
                        yield (
                            {
                                "images": X[i],
                                "tabular": feats[i].astype(np.float32),
                                "survey_id": np.asarray(survey_id[i], dtype=np.int32),
                                "filter_id": np.asarray(filter_id[i], dtype=np.int32),
                            },
                            labels[i],
                        )
            if not repeat:
                break

    ds = tf.data.Dataset.from_generator(
        gen,
        output_signature=(
            {
                "images": tf.TensorSpec(shape=image_shape, dtype=tf.float32),
                "tabular": tf.TensorSpec(shape=(len(EXPECTED_FEATURE_NAMES),), dtype=tf.float32),
                "survey_id": tf.TensorSpec(shape=(), dtype=tf.int32),
                "filter_id": tf.TensorSpec(shape=(), dtype=tf.int32),
            },
            tf.TensorSpec(shape=(), dtype=tf.float32),
        ),
    )
    return ds.batch(batch_size).prefetch(tf.data.AUTOTUNE)
