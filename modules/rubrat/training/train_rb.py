#!/usr/bin/env python
"""Train the cross-survey real/bogus CNN."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tensorflow import keras

from classification import gpu_config  # noqa: F401
from classification.losses import focal_bce
from classification.model_factory import build_rb_model, load_config
from classification.training import (
    ValidationReportCallback,
    WarmupCosineDecay,
    git_commit,
    make_finite_training_dataset,
    prefer_gpu_memory_growth,
    save_history,
    set_reproducible,
    steps_for,
    write_json,
)
from rubrat.provenance import run_context


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--train",
        nargs="+",
        default=["outputs/npz_hltds_rb_transient/train_*.npz", "outputs/npz_gbtds_rb_transient/train_*.npz"],
    )
    p.add_argument("--val", nargs="+", required=True)
    p.add_argument("--config", default="configs/rb_model.yaml")
    p.add_argument("--feats-scaler", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--resume", default=None, help="Optional .keras checkpoint to resume from.")
    p.add_argument("--epochs", type=int, default=None)
    p.add_argument("--batch-size", type=int, default=None)
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


def compile_rb_model(model: keras.Model, cfg: dict) -> keras.Model:
    weight_decay = float(cfg.get("weight_decay", 1e-4))
    clipnorm = float(cfg.get("clipnorm", 1.0))
    schedule = cfg["learning_rate_schedule"]
    model.compile(
        optimizer=keras.optimizers.AdamW(
            learning_rate=schedule,
            weight_decay=weight_decay,
            clipnorm=clipnorm,
        ),
        loss={"rb": focal_bce(gamma=2.0)},
        metrics={
            "rb": [
                keras.metrics.BinaryAccuracy(name="accuracy"),
                keras.metrics.AUC(name="roc_auc"),
                keras.metrics.AUC(curve="PR", name="pr_auc"),
            ]
        },
    )
    return model


def main() -> int:
    args = parse_args()
    set_reproducible(args.seed)
    gpus = prefer_gpu_memory_growth()
    cfg = load_config(args.config)
    batch_size = min(256, int(args.batch_size or cfg.get("batch_size", 64)))
    epochs = int(args.epochs or cfg.get("max_epochs", 50))
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    steps_per_epoch = steps_for(args.train, "y", batch_size)
    validation_steps = steps_for(args.val, "y", batch_size)
    total_steps = max(1, epochs * steps_per_epoch)
    warmup_epochs = int(cfg.get("warmup_epochs", 3))
    warmup_steps = min(total_steps - 1, max(0, warmup_epochs * steps_per_epoch)) if total_steps > 1 else 0
    lr = float(cfg.get("lr", 1e-4))
    min_lr_ratio = float(cfg.get("min_lr_ratio", 0.05))
    schedule = WarmupCosineDecay(
        initial_learning_rate=lr,
        total_steps=total_steps,
        warmup_steps=warmup_steps,
        min_learning_rate=lr * min_lr_ratio,
    )
    cfg["learning_rate_schedule"] = schedule

    if args.resume:
        model = keras.models.load_model(args.resume, compile=False)
        model = compile_rb_model(model, cfg)
    else:
        model = build_rb_model(cfg)
        model = compile_rb_model(model, cfg)

    train_ds = make_finite_training_dataset(
        args.train,
        args.feats_scaler,
        label_key="y",
        output_name="rb",
        batch_size=batch_size,
        shuffle=True,
        seed=args.seed,
    )
    val_ds = make_finite_training_dataset(
        args.val,
        args.feats_scaler,
        label_key="y",
        output_name="rb",
        batch_size=batch_size,
        shuffle=False,
        seed=args.seed,
    )
    early_stopping_patience = int(cfg.get("early_stopping_patience", 5))

    write_json(
        output / "run_metadata.json",
        {
            "task": "rb",
            "config": str(args.config),
            "feats_scaler": str(args.feats_scaler),
            "train": args.train,
            "val": args.val,
            "batch_size": batch_size,
            "epochs": epochs,
            "steps_per_epoch": steps_per_epoch,
            "validation_steps": validation_steps,
            "dropout": float(cfg.get("dropout", 0.3)),
            "weight_decay": float(cfg.get("weight_decay", 1e-4)),
            "clipnorm": float(cfg.get("clipnorm", 1.0)),
            "warmup_epochs": warmup_epochs,
            "warmup_steps": warmup_steps,
            "lr": lr,
            "min_lr_ratio": min_lr_ratio,
            "early_stopping_patience": early_stopping_patience,
            "git_commit": git_commit(ROOT),
            "gpus": gpus,
            "resolved_config": {k: v for k, v in cfg.items() if k != "learning_rate_schedule"},
            **run_context(seed=args.seed, train=args.train, val=args.val),
        },
    )

    callbacks = [
        ValidationReportCallback(
            args.val, args.feats_scaler, output_name="rb", label_key="y", num_classes=2, output_dir=output
        ),
        keras.callbacks.ModelCheckpoint(
            output / "best.keras", monitor="val_rb_macro_f1", mode="max", save_best_only=True
        ),
        keras.callbacks.CSVLogger(output / "keras_history.csv"),
        keras.callbacks.EarlyStopping(
            monitor="val_rb_macro_f1", mode="max", patience=early_stopping_patience, restore_best_weights=True
        ),
    ]
    history = model.fit(
        train_ds,
        validation_data=val_ds,
        epochs=epochs,
        steps_per_epoch=steps_per_epoch,
        validation_steps=validation_steps,
        callbacks=callbacks,
    )
    model.save(output / "final.keras")
    if "accuracy" in history.history and "rb_accuracy" not in history.history:
        history.history["rb_accuracy"] = history.history["accuracy"]
    if "val_accuracy" in history.history and "val_rb_accuracy" not in history.history:
        history.history["val_rb_accuracy"] = history.history["val_accuracy"]
    save_history(history, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
