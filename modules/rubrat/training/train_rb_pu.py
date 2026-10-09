#!/usr/bin/env python
"""Train an RB model with non-negative positive-unlabeled risk."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import tensorflow as tf
from tensorflow import keras

from classification import gpu_config  # noqa: F401
from classification.model_factory import build_rb_model, load_config
from classification.pu import controlled_scar_labels, make_pu_dataset, nnpu_risk, validate_pu_shards
from classification.training import (
    WarmupCosineDecay,
    git_commit,
    prefer_gpu_memory_growth,
    save_history,
    set_reproducible,
    write_json,
)
from rubrat.provenance import run_context

SURVEY_IDS = {"hltds": 0, "gbtds": 1}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--survey", choices=sorted(SURVEY_IDS), required=True)
    p.add_argument("--train", nargs="+", required=True)
    p.add_argument("--val", nargs="+", required=True)
    p.add_argument("--config", default="configs/rb_model.yaml")
    p.add_argument("--feats-scaler", required=True)
    p.add_argument("--positive-prior", type=float, required=True)
    p.add_argument("--risk-mode", choices=["nnpu", "naive_bce", "supervised_subset"], default="nnpu")
    p.add_argument("--labeled-positive-fraction", type=float, default=None,
                   help="Construct a controlled SCAR PU pool from truth y instead of stored PU labels.")
    p.add_argument("--selection-seed", type=int, default=0, help="Independent PU label-selection draw seed.")
    p.add_argument("--output", required=True)
    p.add_argument("--epochs", type=int, default=None)
    p.add_argument("--batch-size", type=int, default=None)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--max-skipped-fraction", type=float, default=0.05)
    p.add_argument("--progress-steps", type=int, default=25, help="Print progress every N train/val steps.")
    p.add_argument("-v", "--verbose", action="count", default=0)
    return p.parse_args()


def _float(value) -> float:
    if hasattr(value, "numpy"):
        value = value.numpy()
    return float(value)


def _should_print_step(step: int, steps: int, progress_steps: int) -> bool:
    return step == 1 or step == steps or (progress_steps > 0 and step % progress_steps == 0)


@tf.function(reduce_retracing=True)
def _train_batch(model, optimizer, inputs, y_pu, positive_prior, risk_mode):
    """Graph-compiled PU update; semantics match the original eager loop."""
    with tf.GradientTape() as tape:
        preds = model(inputs, training=True)["rb"]
        risk = nnpu_risk(y_pu, preds, positive_prior)
        if risk_mode != "nnpu":
            risk["risk"] = tf.reduce_mean(keras.losses.binary_crossentropy(tf.reshape(y_pu, (-1, 1)), preds))
    valid = tf.logical_or(
        tf.constant(risk_mode != "nnpu"),
        tf.logical_and(risk["n_positive"] > 0, risk["n_unlabeled"] > 0),
    )
    grads = tape.gradient(risk["risk"], model.trainable_variables)

    def apply():
        optimizer.apply_gradients(zip(grads, model.trainable_variables))
        return tf.constant(1, tf.int32)

    applied = tf.cond(valid, apply, lambda: tf.constant(0, tf.int32))
    return risk, applied


@tf.function(reduce_retracing=True)
def _eval_batch(model, inputs, y_pu, positive_prior, risk_mode):
    preds = tf.reshape(model(inputs, training=False)["rb"], (-1,))
    risk = nnpu_risk(y_pu, preds, positive_prior)
    if risk_mode != "nnpu":
        risk["risk"] = tf.reduce_mean(keras.losses.binary_crossentropy(tf.reshape(y_pu,(-1,1)),tf.reshape(preds,(-1,1))))
    return preds, risk


def _train_epoch(
    model, optimizer, ds, *, epoch: int, epochs: int, steps: int, positive_prior: float, progress_steps: int,
    risk_mode: str = "nnpu"
) -> dict[str, float]:
    metrics = {
        "risk": [],
        "positive_risk": [],
        "negative_risk": [],
        "raw_negative_risk": [],
    }
    skipped = 0
    for step_i, (inputs, y_pu) in enumerate(ds.take(steps), start=1):
        if _should_print_step(step_i, steps, progress_steps):
            print(
                f"epoch {epoch}/{epochs} train step {step_i}/{steps} batch={int(tf.shape(y_pu)[0].numpy())}", flush=True
            )
        risk, applied = _train_batch(model, optimizer, inputs, y_pu, positive_prior, risk_mode)
        if int(applied.numpy()) == 0:
            skipped += 1
            continue
        for key in metrics:
            metrics[key].append(_float(risk[key]))
    out = {key: float(np.mean(vals)) if vals else float("nan") for key, vals in metrics.items()}
    out["skipped_batches"] = float(skipped)
    out["skipped_fraction"] = float(skipped / max(1, len(metrics["risk"]) + skipped))
    return out


def _eval_epoch(
    model, ds, *, epoch: int, epochs: int, steps: int, positive_prior: float, progress_steps: int,
    risk_mode: str = "nnpu"
) -> dict[str, float]:
    metrics = {
        "val_risk": [],
        "val_positive_risk": [],
        "val_negative_risk": [],
        "val_raw_negative_risk": [],
        "val_positive_recall_at_0_5": [],
        "val_mean_positive_score": [],
        "val_mean_unlabeled_score": [],
    }
    for step_i, (inputs, y_pu) in enumerate(ds.take(steps), start=1):
        if _should_print_step(step_i, steps, progress_steps):
            print(
                f"epoch {epoch}/{epochs} val step {step_i}/{steps} batch={int(tf.shape(y_pu)[0].numpy())}", flush=True
            )
        preds, risk = _eval_batch(model, inputs, y_pu, positive_prior, risk_mode)
        y = tf.reshape(tf.cast(y_pu, tf.float32), (-1,))
        pos_mask = tf.equal(y, 1.0)
        unl_mask = tf.equal(y, 0.0)
        metrics["val_risk"].append(_float(risk["risk"]))
        metrics["val_positive_risk"].append(_float(risk["positive_risk"]))
        metrics["val_negative_risk"].append(_float(risk["negative_risk"]))
        metrics["val_raw_negative_risk"].append(_float(risk["raw_negative_risk"]))
        if int(risk["n_positive"].numpy()) > 0:
            pos_scores = tf.boolean_mask(preds, pos_mask)
            metrics["val_positive_recall_at_0_5"].append(_float(tf.reduce_mean(tf.cast(pos_scores >= 0.5, tf.float32))))
            metrics["val_mean_positive_score"].append(_float(tf.reduce_mean(pos_scores)))
        if int(risk["n_unlabeled"].numpy()) > 0:
            metrics["val_mean_unlabeled_score"].append(_float(tf.reduce_mean(tf.boolean_mask(preds, unl_mask))))
    return {key: float(np.mean(vals)) if vals else float("nan") for key, vals in metrics.items()}


def main() -> int:
    args = parse_args()
    if not 0.0 < args.positive_prior < 1.0:
        raise ValueError("--positive-prior must be in (0, 1)")
    set_reproducible(args.seed)
    gpus = prefer_gpu_memory_growth()
    expected_survey_id = SURVEY_IDS[args.survey]
    print(f"GPUs visible: {gpus if gpus else 'none'}", flush=True)
    train_labels = val_labels = None
    train_pu_report = val_pu_report = None
    if args.labeled_positive_fraction is not None:
        train_labels, train_pu_report = controlled_scar_labels(
            args.train, labeled_positive_fraction=args.labeled_positive_fraction, selection_seed=args.selection_seed
        )
        val_labels, val_pu_report = controlled_scar_labels(
            args.val, labeled_positive_fraction=args.labeled_positive_fraction, selection_seed=args.selection_seed + 1000003
        )
        train_counts = {"rows": train_pu_report["known_positive_rows"] + train_pu_report["unlabeled_rows"],
                        "positive": train_pu_report["known_positive_rows"], "unlabeled": train_pu_report["unlabeled_rows"],
                        "trusted_negative": 0}
        val_counts = {"rows": val_pu_report["known_positive_rows"] + val_pu_report["unlabeled_rows"],
                      "positive": val_pu_report["known_positive_rows"], "unlabeled": val_pu_report["unlabeled_rows"],
                      "trusted_negative": 0}
        if args.risk_mode == "supervised_subset":
            train_counts["rows"] -= train_pu_report["hidden_positive_rows"]
            train_counts["unlabeled"] = train_pu_report["truth_negative_rows"]
            val_counts["rows"] -= val_pu_report["hidden_positive_rows"]
            val_counts["unlabeled"] = val_pu_report["truth_negative_rows"]
    else:
        print("validating training shards", flush=True)
        train_counts = validate_pu_shards(args.train, expected_survey_id=expected_survey_id, verbose=True)
        print("validating validation shards", flush=True)
        val_counts = validate_pu_shards(args.val, expected_survey_id=expected_survey_id, verbose=True)
    if train_counts["positive"] == 0 or train_counts["unlabeled"] == 0:
        raise ValueError(f"Training shards need both PU positives and unlabeled rows; got {train_counts}")

    cfg = load_config(args.config)
    batch_size = min(256, int(args.batch_size or cfg.get("batch_size", 64)))
    epochs = int(args.epochs or cfg.get("max_epochs", 50))
    steps_per_epoch = max(1, math.ceil(train_counts["rows"] / batch_size))
    validation_steps = max(1, math.ceil(val_counts["rows"] / batch_size))
    total_steps = max(1, epochs * steps_per_epoch)
    lr = float(cfg.get("lr", 1e-4))
    warmup_epochs = int(cfg.get("warmup_epochs", 3))
    warmup_steps = min(total_steps - 1, max(0, warmup_epochs * steps_per_epoch)) if total_steps > 1 else 0
    schedule = WarmupCosineDecay(
        initial_learning_rate=lr,
        total_steps=total_steps,
        warmup_steps=warmup_steps,
        min_learning_rate=lr * float(cfg.get("min_lr_ratio", 0.05)),
    )
    optimizer = keras.optimizers.AdamW(
        learning_rate=schedule,
        weight_decay=float(cfg.get("weight_decay", 1e-4)),
        clipnorm=float(cfg.get("clipnorm", 1.0)),
    )
    print("building RB model", flush=True)
    model = build_rb_model(cfg)
    print(
        f"training rows={train_counts['rows']} val rows={val_counts['rows']} "
        f"batch_size={batch_size} epochs={epochs} "
        f"steps_per_epoch={steps_per_epoch} validation_steps={validation_steps} "
        f"positive_prior={args.positive_prior}",
        flush=True,
    )
    print("creating training dataset", flush=True)
    train_ds = make_pu_dataset(
        args.train,
        args.feats_scaler,
        batch_size=batch_size,
        shuffle=True,
        seed=args.seed,
        repeat=True,
        expected_survey_id=expected_survey_id,
        verbose=bool(args.verbose),
        controlled_labels=train_labels,
        pool_mode="supervised_subset" if args.risk_mode == "supervised_subset" else "pu",
    )
    print("creating validation dataset", flush=True)
    val_ds = make_pu_dataset(
        args.val,
        args.feats_scaler,
        batch_size=batch_size,
        shuffle=False,
        seed=args.seed,
        repeat=True,
        expected_survey_id=expected_survey_id,
        verbose=bool(args.verbose),
        controlled_labels=val_labels,
        pool_mode="supervised_subset" if args.risk_mode == "supervised_subset" else "pu",
    )
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    write_json(
        output / "run_metadata.json",
        {
            "task": "rb_pu",
            "survey": args.survey,
            "survey_id": expected_survey_id,
            "positive_prior": float(args.positive_prior),
            "controlled_pu": args.labeled_positive_fraction is not None,
            "risk_mode": args.risk_mode,
            "labeled_positive_fraction": args.labeled_positive_fraction,
            "selection_seed": int(args.selection_seed),
            "train_pu_report": train_pu_report,
            "val_pu_report": val_pu_report,
            "config": str(args.config),
            "feats_scaler": str(args.feats_scaler),
            "train": args.train,
            "val": args.val,
            "train_counts": train_counts,
            "val_counts": val_counts,
            "batch_size": batch_size,
            "epochs": epochs,
            "steps_per_epoch": steps_per_epoch,
            "validation_steps": validation_steps,
            "git_commit": git_commit(ROOT),
            "gpus": gpus,
            "resolved_config": cfg,
            **run_context(seed=args.seed, train=args.train, val=args.val),
        },
    )
    history: dict[str, list[float]] = {}
    best_val = np.inf
    for epoch in range(epochs):
        print(f"starting epoch {epoch + 1}/{epochs}", flush=True)
        train_metrics = _train_epoch(
            model,
            optimizer,
            train_ds,
            epoch=epoch + 1,
            epochs=epochs,
            steps=steps_per_epoch,
            positive_prior=args.positive_prior,
            progress_steps=args.progress_steps,
            risk_mode=args.risk_mode,
        )
        if train_metrics["skipped_fraction"] > float(args.max_skipped_fraction):
            raise RuntimeError(
                f"Skipped {train_metrics['skipped_fraction']:.3f} of PU batches; "
                "use a more balanced PU batch loader or larger batch size."
            )
        val_metrics = _eval_epoch(
            model,
            val_ds,
            epoch=epoch + 1,
            epochs=epochs,
            steps=validation_steps,
            positive_prior=args.positive_prior,
            progress_steps=args.progress_steps,
            risk_mode=args.risk_mode,
        )
        row = {"epoch": float(epoch + 1), **train_metrics, **val_metrics}
        for key, value in row.items():
            history.setdefault(key, []).append(float(value))
        if val_metrics["val_risk"] < best_val:
            best_val = val_metrics["val_risk"]
            model.save(output / "best.keras")
        print(
            f"epoch {epoch + 1}/{epochs} "
            f"risk={train_metrics['risk']:.5f} "
            f"val_risk={val_metrics['val_risk']:.5f} "
            f"skipped={train_metrics['skipped_batches']:.0f}",
            flush=True,
        )
    model.save(output / "final.keras")
    save_history(history, output)
    write_json(output / "final_metrics.json", {key: values[-1] for key, values in history.items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
