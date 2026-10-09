#!/usr/bin/env python3
"""Compute Phase 1 feature z-score statistics from train-split NPZ files only."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from classification.data_utils import resolve_npz_paths, save_feature_scaler  # noqa: E402


def _is_train_npz(path: Path) -> bool:
    return path.name == "train.npz" or path.stem == "train" or path.stem.startswith("train_")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--npz", nargs="+", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument(
        "--allow-non-train",
        action="store_true",
        help="Debug override; do not use for production scaler fitting.",
    )
    args = p.parse_args(argv)

    npz_paths = resolve_npz_paths(args.npz)
    bad = [path for path in npz_paths if not _is_train_npz(path)]
    if bad and not args.allow_non_train:
        joined = ", ".join(str(p) for p in bad)
        raise SystemExit(f"Refusing to fit scaler on non-train NPZ path(s): {joined}")

    payload = save_feature_scaler(npz_paths, args.output)
    print(f"Wrote {args.output} from {payload['n_rows']} rows")
    print("is_fit_clean mean/std forced to 0.0/1.0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
