"""Production validators for RAPID products, NPZ datasets, and run artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from classification.data_utils import EXPECTED_FEATURE_NAMES, resolve_npz_paths
from classification.rb_manifest import row_id_from_metadata

HLTDS_REQUIRED = (
    "bkg_subbed_science_image.fits",
    "awaicgen_output_mosaic_image_resampled_gainmatched.fits",
    "sfftdiffimage_dconv_masked.fits",
    "sfftdiffimage_masked_psfcat.parquet",
)
GBTDS_REQUIRED = (
    "bkg_subbed_science_image.fits",
    "awaicgen_output_mosaic_image_resampled_gainmatched.fits",
    "sfftdiffimage_masked.fits",
    "sfftdiffimage_masked_psfcat_finder.txt",
)
NPZ_REQUIRED = ("X", "feats", "y", "survey_id", "filter_id", "metadata")


def sha256_file(path: str | Path, *, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _truth_present(jid: Path, survey: str) -> bool:
    if survey == "hltds":
        return any(jid.glob("Roman_TDS_index_*.txt")) or any(jid.glob("Roman_TDS_*_lite_inject.txt"))
    return True


def validate_products(root: str | Path, survey: str, *, checksum: bool = False) -> dict[str, Any]:
    root = Path(root)
    if survey not in {"hltds", "gbtds"}:
        raise ValueError("survey must be 'hltds' or 'gbtds'")
    required = HLTDS_REQUIRED if survey == "hltds" else GBTDS_REQUIRED
    jids = sorted(path for path in root.glob("jid*") if path.is_dir())
    if not jids:
        raise ValueError(f"No jid* directories under {root}")
    missing: list[dict[str, Any]] = []
    inventory: list[dict[str, Any]] = []
    for jid in jids:
        absent = [name for name in required if not (jid / name).is_file()]
        if not _truth_present(jid, survey):
            absent.append("Roman_TDS truth/index or injection sidecar")
        if absent:
            missing.append({"jid": jid.name, "missing": absent})
            continue
        entry: dict[str, Any] = {"jid": jid.name, "files": {}}
        for name in required:
            path = jid / name
            meta = {"size": path.stat().st_size}
            if checksum:
                meta["sha256"] = sha256_file(path)
            entry["files"][name] = meta
        inventory.append(entry)
    return {
        "schema_version": "rubrat_product_inventory_v1",
        "survey": survey,
        "root": str(root.resolve()),
        "n_jids": len(jids),
        "n_valid": len(inventory),
        "n_invalid": len(missing),
        "valid": not missing,
        "missing": missing,
        "inventory": inventory,
    }


def _metadata_rows(values: np.ndarray) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for value in np.asarray(values, dtype=object).reshape(-1):
        if not isinstance(value, dict):
            raise ValueError(f"metadata entry is not a dict: {type(value).__name__}")
        rows.append(dict(value))
    return rows


def validate_dataset(paths: str | Path | Iterable[str | Path], *, checksum: bool = False) -> dict[str, Any]:
    shards = resolve_npz_paths(paths)
    errors: list[str] = []
    shard_rows: list[dict[str, Any]] = []
    all_ids: set[str] = set()
    duplicate_ids: set[str] = set()
    for shard in shards:
        with np.load(shard, allow_pickle=True) as data:
            missing = [key for key in NPZ_REQUIRED if key not in data.files]
            if missing:
                errors.append(f"{shard}: missing arrays {missing}")
                continue
            n = len(data["y"])
            for key in NPZ_REQUIRED:
                if len(data[key]) != n:
                    errors.append(f"{shard}: {key} length {len(data[key])} != y length {n}")
            if data["X"].ndim != 4 or data["X"].shape[-1] != 3:
                errors.append(f"{shard}: X must have shape (N,H,W,3), got {data['X'].shape}")
            if data["feats"].shape != (n, len(EXPECTED_FEATURE_NAMES)):
                errors.append(f"{shard}: feats must have shape ({n},9), got {data['feats'].shape}")
            for key in ("X", "feats"):
                if not np.isfinite(data[key]).all():
                    errors.append(f"{shard}: {key} contains NaN or Inf")
            labels = np.asarray(data["y"], dtype=np.int64)
            if not set(np.unique(labels)).issubset({0, 1}):
                errors.append(f"{shard}: y contains values outside {{0,1}}")
            surveys = np.asarray(data["survey_id"], dtype=np.int64)
            if not set(np.unique(surveys)).issubset({0, 1}):
                errors.append(f"{shard}: survey_id contains values outside {{0,1}}")
            rows = _metadata_rows(data["metadata"])
            for row in rows:
                try:
                    row_id = row_id_from_metadata(row)
                except ValueError as exc:
                    errors.append(f"{shard}: {exc}")
                    continue
                if row_id in all_ids:
                    duplicate_ids.add(row_id)
                all_ids.add(row_id)
            item: dict[str, Any] = {
                "path": str(shard),
                "n": n,
                "class_counts": {str(label): int((labels == label).sum()) for label in (0, 1)},
                "survey_counts": {str(s): int((surveys == s).sum()) for s in (0, 1)},
                "size": shard.stat().st_size,
            }
            if checksum:
                item["sha256"] = sha256_file(shard)
            shard_rows.append(item)
    if duplicate_ids:
        errors.append(f"duplicate row IDs: {sorted(duplicate_ids)[:20]}")
    return {
        "schema_version": "rubrat_dataset_validation_v1",
        "valid": not errors,
        "errors": errors,
        "n_shards": len(shards),
        "n_rows": sum(row.get("n", 0) for row in shard_rows),
        "n_unique_row_ids": len(all_ids),
        "feature_names": EXPECTED_FEATURE_NAMES,
        "shards": shard_rows,
    }


def validate_split_disjointness(split_paths: dict[str, Iterable[str | Path]]) -> dict[str, Any]:
    row_sets: dict[str, set[str]] = {}
    for split, items in split_paths.items():
        ids: set[str] = set()
        for shard in resolve_npz_paths(list(items)):
            with np.load(shard, allow_pickle=True) as data:
                ids.update(row_id_from_metadata(row) for row in _metadata_rows(data["metadata"]))
        row_sets[split] = ids
    overlaps: dict[str, list[str]] = {}
    names = sorted(row_sets)
    for i, left in enumerate(names):
        for right in names[i + 1 :]:
            overlap = row_sets[left] & row_sets[right]
            if overlap:
                overlaps[f"{left}:{right}"] = sorted(overlap)[:100]
    return {"valid": not overlaps, "counts": {k: len(v) for k, v in row_sets.items()}, "overlaps": overlaps}


def write_report(report: dict[str, Any], output: str | Path | None) -> None:
    text = json.dumps(report, indent=2, sort_keys=True)
    if output is None:
        print(text)
    else:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
