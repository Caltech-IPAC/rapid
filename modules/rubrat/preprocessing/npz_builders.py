"""Shared helpers for Phase 1 NPZ builder scripts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from classification.data_utils import EXPECTED_FEATURE_NAMES, coerce_feats
from classification.phase0_utils import convert_truth_id, normalize_psf_aliases
from ingestion.fits_loader import load_fits
from ingestion.rubr_adapter import build_feats_dict, extract_cutout

FILTER_IDS = {
    "R062": 0,
    "Z087": 1,
    "F087": 1,
    "Y106": 2,
    "J129": 3,
    "H158": 4,
    "F184": 5,
    "K213": 6,
    "F213": 6,
    "F146": 7,
    "UNKNOWN": -1,
}

HLTDS_FILES = {
    "sci": "bkg_subbed_science_image.fits",
    "ref": "awaicgen_output_mosaic_image_resampled_gainmatched.fits",
    "diff": "sfftdiffimage_dconv_masked.fits",
}
HLTDS_ZOGY_FILES = {
    **HLTDS_FILES,
    "diff": "zogy_diffimage_masked.fits",
}
GBTDS_FILES = {
    "sci": "bkg_subbed_science_image.fits",
    "ref": "awaicgen_output_mosaic_image_resampled_gainmatched.fits",
    "diff": "sfftdiffimage_masked.fits",
    "detcat": "sfftdiffimage_masked_psfcat_finder.txt",
}


def jid_num(jid: str | Path) -> int:
    match = re.search(r"(\d+)$", Path(str(jid)).name)
    return int(match.group(1)) if match else -1


def infer_filter_from_jid_dir(jid_dir: Path, fallback: str = "UNKNOWN") -> str:
    for path in sorted(jid_dir.glob("Roman_TDS_index_*.txt")):
        match = re.search(r"Roman_TDS_index_([A-Za-z]\d{3})_", path.name)
        if match:
            return match.group(1).upper()
    for path in sorted(jid_dir.glob("rimtimsim_WFI_*.fits*")):
        parts = path.name.split("_")
        if len(parts) >= 3:
            return parts[2].upper()
    return fallback.upper()


def filter_id(name: str) -> int:
    return int(FILTER_IDS.get(str(name).upper(), -1))


def arcsinh_stack_cutout(
    sci: np.ndarray,
    ref: np.ndarray,
    diff: np.ndarray,
    x: float,
    y: float,
    *,
    image_size: int,
    scale: float = 0.01,
) -> np.ndarray | None:
    cuts = [
        extract_cutout(sci, x, y, image_size),
        extract_cutout(ref, x, y, image_size),
        extract_cutout(diff, x, y, image_size),
    ]
    if any(c is None for c in cuts):
        return None
    stack = np.stack(cuts, axis=-1).astype(np.float32)
    if np.isnan(stack).any():
        return None
    return np.arcsinh(stack / np.float32(scale)).astype(np.float32)


def feature_row(row: pd.Series | dict) -> np.ndarray:
    feats = build_feats_dict(row)
    return np.asarray([feats[name] for name in EXPECTED_FEATURE_NAMES], dtype=np.float32)


def save_phase1_npz(path: Path, arrays: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    arrays = dict(arrays)
    arrays["X"] = np.asarray(arrays.get("X", []), dtype=np.float32)
    arrays["feats"] = coerce_feats(arrays.get("feats", np.empty((0, 9), dtype=np.float32)))
    arrays["y"] = np.asarray(arrays.get("y", []), dtype=np.int64)
    arrays["survey_id"] = np.asarray(arrays.get("survey_id", []), dtype=np.int32)
    arrays["filter_id"] = np.asarray(arrays.get("filter_id", []), dtype=np.int32)
    arrays["metadata"] = np.asarray(arrays.get("metadata", []), dtype=object)
    np.savez(path, **arrays)


def parse_size_bytes(value: str | int | float) -> int:
    """Parse sizes like ``10gb``, ``512mb``, or raw bytes."""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    multipliers = {
        "gb": 1024**3,
        "g": 1024**3,
        "mb": 1024**2,
        "m": 1024**2,
        "kb": 1024,
        "k": 1024,
        "b": 1,
    }
    for suffix, mult in multipliers.items():
        if text.endswith(suffix):
            return int(float(text[: -len(suffix)]) * mult)
    return int(float(text))


def empty_phase1_arrays(image_size: int, *, extra_keys: tuple[str, ...] = ()) -> dict:
    arrays = {
        "X": np.empty((0, image_size, image_size, 3), dtype=np.float32),
        "feats": np.empty((0, 9), dtype=np.float32),
        "y": np.empty(0, dtype=np.int64),
        "survey_id": np.empty(0, dtype=np.int32),
        "filter_id": np.empty(0, dtype=np.int32),
        "metadata": np.empty(0, dtype=object),
    }
    for key in extra_keys:
        arrays[key] = np.empty(0, dtype=np.int64)
    return arrays


def finalize_phase1_rows(rows: dict, image_size: int, *, extra_keys: tuple[str, ...] = ()) -> dict:
    if not rows["X"]:
        return empty_phase1_arrays(image_size, extra_keys=extra_keys)
    arrays = {
        "X": np.stack(rows["X"]).astype(np.float32),
        "feats": np.stack(rows["feats"]).astype(np.float32),
        "y": np.asarray(rows["y"], dtype=np.int64),
        "survey_id": np.asarray(rows["survey_id"], dtype=np.int32),
        "filter_id": np.asarray(rows["filter_id"], dtype=np.int32),
        "metadata": np.asarray(rows["metadata"], dtype=object),
    }
    for key in extra_keys:
        arrays[key] = np.asarray(rows[key], dtype=np.int64)
    return arrays


class ShardedNPZWriter:
    """Incrementally write ``<split>_<index>.npz`` shards below a byte cap."""

    def __init__(
        self,
        output_dir: Path,
        *,
        image_size: int,
        max_shard_bytes: int,
        extra_keys: tuple[str, ...] = (),
    ):
        self.output_dir = Path(output_dir)
        self.image_size = int(image_size)
        self.max_shard_bytes = int(max_shard_bytes)
        self.extra_keys = tuple(extra_keys)
        self._rows: dict[str, dict] = {}
        self._bytes: dict[str, int] = {}
        self._counts: dict[str, int] = {}
        self._shards: dict[str, list[str]] = {}

    def _new_rows(self) -> dict:
        rows = {k: [] for k in empty_phase1_arrays(self.image_size, extra_keys=self.extra_keys)}
        return rows

    def _ensure_split(self, split: str) -> None:
        if split not in self._rows:
            self._rows[split] = self._new_rows()
            self._bytes[split] = 0
            self._counts[split] = 0
            self._shards[split] = []

    @staticmethod
    def _sample_nbytes(sample: dict) -> int:
        total = 0
        for key, value in sample.items():
            if key == "metadata":
                # Conservative object/JSON overhead estimate; actual NPZ pickle
                # size varies, but images dominate production shard size.
                total += max(1024, len(json.dumps(value, default=str).encode("utf-8")))
            else:
                total += np.asarray(value).nbytes
        return int(total)

    def add(self, split: str, sample: dict) -> None:
        self._ensure_split(split)
        sample_bytes = self._sample_nbytes(sample)
        if self._counts[split] > 0 and self._bytes[split] + sample_bytes > self.max_shard_bytes:
            self.flush(split)
        rows = self._rows[split]
        for key in rows:
            rows[key].append(sample[key])
        self._bytes[split] += sample_bytes
        self._counts[split] += 1

    def flush(self, split: str) -> Path | None:
        self._ensure_split(split)
        rows = self._rows[split]
        if not rows["X"]:
            return None
        shard_idx = len(self._shards[split])
        path = self.output_dir / f"{split}_{shard_idx}.npz"
        arrays = finalize_phase1_rows(rows, self.image_size, extra_keys=self.extra_keys)
        save_phase1_npz(path, arrays)
        self._shards[split].append(str(path))
        self._rows[split] = self._new_rows()
        self._bytes[split] = 0
        self._counts[split] = 0
        return path

    def close(self, splits: list[str] | tuple[str, ...]) -> dict[str, list[str]]:
        for split in splits:
            self.flush(split)
            self._ensure_split(split)
            if not self._shards[split]:
                path = self.output_dir / f"{split}_0.npz"
                arrays = empty_phase1_arrays(self.image_size, extra_keys=self.extra_keys)
                save_phase1_npz(path, arrays)
                self._shards[split].append(str(path))
        return {split: list(self._shards[split]) for split in splits}


def write_split_report(output_dir: Path, split_rows: dict[str, dict], extra: dict | None = None) -> None:
    report = {"splits": split_rows}
    if extra:
        report.update(extra)
    (output_dir / "split_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


def split_jids(
    jid_to_filter: dict[str, str],
    *,
    seed: int = 42,
    train_frac: float = 0.70,
    val_frac: float = 0.15,
) -> dict[str, str]:
    rng = np.random.default_rng(seed)
    split: dict[str, str] = {}
    for filt in sorted(set(jid_to_filter.values())):
        jids = np.array(sorted([j for j, f in jid_to_filter.items() if f == filt], key=jid_num))
        rng.shuffle(jids)
        n = len(jids)
        n_train = int(round(n * train_frac))
        n_val = int(round(n * val_frac))
        for jid in jids[:n_train]:
            split[str(jid)] = "train"
        for jid in jids[n_train : n_train + n_val]:
            split[str(jid)] = "val"
        for jid in jids[n_train + n_val :]:
            split[str(jid)] = "test"
    return split


def infer_gbtds_image_shape(jid_dirs: list[Path]) -> tuple[int, int]:
    """Return ``(width, height)`` from the first available GBTDS science image."""
    for jid_dir in jid_dirs:
        sci_path = jid_dir / GBTDS_FILES["sci"]
        if sci_path.exists():
            img = load_fits(sci_path)
            height, width = img["data"].shape
            return int(width), int(height)
    raise FileNotFoundError("Could not infer GBTDS image shape from science FITS.")


def spatial_corner_mask_fn(
    corner: str,
    *,
    width: int,
    height: int,
) -> Callable[[np.ndarray, np.ndarray], np.ndarray]:
    """Return a boolean mask over detection centroids in the requested corner."""
    masks: dict[str, Callable[[np.ndarray, np.ndarray], np.ndarray]] = {
        "bottom_right": lambda x, y: (x >= width / 2) & (y >= height / 2),
        "bottom_left": lambda x, y: (x < width / 2) & (y >= height / 2),
        "top_right": lambda x, y: (x >= width / 2) & (y < height / 2),
        "top_left": lambda x, y: (x < width / 2) & (y < height / 2),
    }
    if corner not in masks:
        raise ValueError(f"Unsupported spatial corner {corner!r}; expected one of {sorted(masks)}")
    return masks[corner]


@dataclass(frozen=True)
class SpatialCornerSplit:
    """Detection-level spatial holdout matching the RuBR corner experiment."""

    width: int
    height: int
    corner: str = "bottom_right"

    @classmethod
    def from_jid_dirs(cls, jid_dirs: list[Path], *, corner: str = "bottom_right") -> SpatialCornerSplit:
        width, height = infer_gbtds_image_shape(jid_dirs)
        return cls(width=width, height=height, corner=corner)

    def contains(self, x: float, y: float) -> bool:
        return bool(
            spatial_corner_mask_fn(self.corner, width=self.width, height=self.height)(
                np.asarray([x], dtype=np.float64),
                np.asarray([y], dtype=np.float64),
            )[0]
        )

    def output_split(self, jid: str, x: float, y: float, jid_split: str | None) -> str | None:
        if self.contains(x, y):
            return "test"
        return jid_split


def spatial_corner_jid_split(
    data_dir: Path,
    jid_dirs: list[Path],
    *,
    seed: int = 42,
    test_corner: str = "bottom_right",
    val_frac: float = 0.15,
) -> dict[str, str]:
    """Assign jids to train/val for GBTDS spatial-corner mode.

    Test rows are selected later by detection centroid inside
    :class:`SpatialCornerSplit`; whole jids are never assigned to test.
    """
    del data_dir, test_corner  # train/val only; corner holdout is per detection.
    if not jid_dirs:
        return {}
    eligible = sorted({jid_dir.name for jid_dir in jid_dirs}, key=jid_num)
    rng = np.random.default_rng(seed)
    rest = np.array(eligible, dtype=object)
    rng.shuffle(rest)
    n_val = max(1, int(round(len(rest) * val_frac))) if len(rest) > 1 else 0
    split: dict[str, str] = {}
    for jid in rest[:n_val]:
        split[str(jid)] = "val"
    for jid in rest[n_val:]:
        split[str(jid)] = "train"
    return split


def load_split_json(path: Path, split_key: str) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "jids" in payload and isinstance(payload["jids"], dict):
        rows = payload["jids"].get(split_key, [])
    elif split_key in payload:
        rows = payload[split_key]
    elif "splits" in payload:
        rows = payload["splits"].get(split_key, [])
    else:
        raise ValueError(f"Could not find split key {split_key!r} in {path}")
    return {Path(str(j)).name: split_key for j in rows}


def truth_match_labels(
    det: pd.DataFrame,
    truth: pd.DataFrame,
    *,
    radius_px: float,
) -> tuple[np.ndarray, np.ndarray]:
    labels = np.zeros(len(det), dtype=np.int64)
    match_ids = np.full(len(det), -1, dtype=np.int64)
    if len(det) == 0 or len(truth) == 0:
        return labels, match_ids
    if "sicbro_id" not in truth.columns:
        raise ValueError("truth catalog must include sicbro_id")
    det_xy = np.column_stack([det["xcentroid"].to_numpy(float), det["ycentroid"].to_numpy(float)])
    truth_xy = np.column_stack([truth["x"].to_numpy(float), truth["y"].to_numpy(float)])
    valid_det = np.isfinite(det_xy).all(axis=1)
    valid_truth = np.isfinite(truth_xy).all(axis=1)
    if not valid_det.any() or not valid_truth.any():
        return labels, match_ids
    valid_idx = np.where(valid_det)[0]
    tree = cKDTree(det_xy[valid_det])
    dists, idx = tree.query(truth_xy[valid_truth], distance_upper_bound=radius_px)
    truth_ids = truth.loc[valid_truth, "sicbro_id"].to_numpy(np.int64)
    for order_i in np.argsort(dists):
        if not np.isfinite(dists[order_i]):
            continue
        det_i = int(valid_idx[idx[order_i]])
        if labels[det_i] == 0:
            labels[det_i] = 1
            match_ids[det_i] = int(truth_ids[order_i])
    return labels, match_ids


def load_rts_truth(catalog: Path, filter_name: str = "F213") -> pd.DataFrame:
    usecols = ["sicbro_id", "MEAN_XCOL", "MEAN_YCOL", filter_name]
    df = pd.read_csv(catalog, sep="\t", usecols=usecols, low_memory=False)
    out = df.rename(columns={"MEAN_XCOL": "x", "MEAN_YCOL": "y", filter_name: "mag"}).copy()
    return out[np.isfinite(out["x"]) & np.isfinite(out["y"])].reset_index(drop=True)


def normalize_detection_table(df: pd.DataFrame) -> pd.DataFrame:
    return normalize_psf_aliases(df)


def y_sn_lookup(join_path: Path) -> dict[tuple[str, int], int]:
    join = pd.read_parquet(join_path)
    return {(str(row.jid), int(row.truth_id)): int(row.y_sn) for row in join.itertuples(index=False)}


def load_hltds_injection_truth(path: Path) -> dict[str, pd.DataFrame]:
    """Load enriched HLTDS injection truth and group it by jid name."""
    truth = pd.read_csv(path)
    required = {"jid", "x", "y", "inject_type", "injection_label", "source_id", "rtid"}
    missing = required - set(truth.columns)
    if missing:
        raise ValueError(f"{path} missing required injection columns: {sorted(missing)}")
    truth = truth.copy()
    truth["jid"] = truth["jid"].map(lambda value: f"jid{int(value)}")
    truth["source_id"] = truth["source_id"].astype(str)
    return {str(jid): df.reset_index(drop=True) for jid, df in truth.groupby("jid", sort=False)}


def match_hltds_injection(
    injection_lookup: dict[str, pd.DataFrame] | None,
    *,
    jid: str,
    x: float,
    y: float,
    radius_px: float,
) -> dict:
    """Return enriched injection metadata for the nearest injection in one jid."""
    if not injection_lookup:
        return {}
    truth = injection_lookup.get(str(jid))
    if truth is None or len(truth) == 0:
        return {}
    coords = truth[["x", "y"]].to_numpy(dtype=float)
    finite = np.isfinite(coords).all(axis=1)
    if not finite.any() or not np.isfinite([x, y]).all():
        return {}
    finite_indices = np.where(finite)[0]
    tree = cKDTree(coords[finite])
    distance, tree_index = tree.query([[float(x), float(y)]], k=1)
    distance = float(distance[0])
    if not np.isfinite(distance) or distance > radius_px:
        return {}
    row = truth.iloc[int(finite_indices[int(tree_index[0])])].to_dict()
    row["injection_match_distance_px"] = distance
    return row


def safe_truth_id(value: object) -> int | None:
    try:
        return convert_truth_id(value)
    except ValueError:
        return None
