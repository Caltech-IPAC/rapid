"""Shared helpers for Phase 0 CNN input verification scripts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import yaml

EXPECTED_PSF_COLUMNS = [
    "flux_fit",
    "flux_err",
    "x_err",
    "y_err",
    "reduced_chi2",
    "cfit",
    "flags",
    "sharpness",
    "roundness1",
    "roundness2",
    "npixfit",
]

REQUIRED_LABEL_COLUMNS = [
    "truth_obj_type",
    "truth_id",
    "xcentroid",
    "ycentroid",
    "flags",
    "flux_fit",
    "flux_err",
    "cfit",
    "reduced_chi2",
    "x_err",
    "y_err",
    "sharpness",
    "roundness1",
    "roundness2",
]

PSF_ALIASES = {
    "n_pixels_fit": "npixfit",
    "n_pixels": "npixfit",
}


def normalize_psf_aliases(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with PSF schema aliases normalized."""
    out = df.copy()
    for src, dst in PSF_ALIASES.items():
        if src in out.columns and dst not in out.columns:
            out[dst] = out[src]
    return out


def missing_columns(df: pd.DataFrame, required: Iterable[str]) -> list[str]:
    return [col for col in required if col not in df.columns]


def read_json(path: str | Path) -> dict:
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def extract_filter_from_summary(entry: dict, labels_dir: Path | None = None) -> str:
    """Infer filter from summary truth_source or local Roman_TDS_index file."""
    truth_source = str(entry.get("truth_source", ""))
    match = re.search(r"Roman_TDS_index_([A-Za-z]\d{3})_", truth_source)
    if match:
        return match.group(1).upper()

    jid = str(entry.get("jid", ""))
    if labels_dir is not None and jid:
        for path in sorted((labels_dir / jid).glob("truth_used.csv")):
            try:
                first = pd.read_csv(path, nrows=1)
            except Exception:
                continue
            for col in ("filter", "bandpass"):
                if col in first.columns and len(first):
                    return str(first[col].iloc[0]).upper()
    return "UNKNOWN"


def convert_truth_id(value: object) -> int:
    """Convert labeled parquet truth IDs like '30480035.0' to int IDs."""
    if value is None:
        raise ValueError("truth_id is missing")
    if isinstance(value, str) and not value.strip():
        raise ValueError("truth_id is blank")
    try:
        return int(float(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"cannot convert truth_id {value!r}") from exc


def load_sn_class_map(config_path: str | Path) -> dict[int, int]:
    with Path(config_path).open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    class_map = cfg.get("sn_class_map")
    if not isinstance(class_map, dict):
        raise ValueError(f"{config_path} must define sn_class_map")
    return {int(k): int(v) for k, v in class_map.items()}


def map_gentype_to_y_sn(
    gentype: object,
    class_map: dict[int, int],
    *,
    other_class: int = 4,
) -> int:
    if pd.isna(gentype):
        return other_class
    return int(class_map.get(int(gentype), other_class))


def read_detection_table(path: str | Path) -> pd.DataFrame:
    """Read a parquet or whitespace-delimited RAPID detection table."""
    path = Path(path)
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path, sep=r"\s+", comment="#", engine="python")


def jid_sort_key(path: Path) -> tuple[int, str]:
    match = re.search(r"(\d+)$", path.name)
    return (int(match.group(1)) if match else -1, path.name)


def choose_psf_catalog(jid_dir: Path) -> Path:
    candidates = [
        jid_dir / "sfftdiffimage_masked_psfcat.parquet",
        jid_dir / "sfftdiffimage_masked_psfcat.txt",
        jid_dir / "zogy_diffimage_masked_psfcat.parquet",
        jid_dir / "zogy_diffimage_masked_psfcat.txt",
    ]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(f"No PSF catalog found in {jid_dir}")


def finite_percentiles(values: np.ndarray, percentiles=(5, 25, 50, 75)) -> dict[str, float]:
    finite = np.asarray(values, dtype=float)
    finite = finite[np.isfinite(finite)]
    if len(finite) == 0:
        return {f"p{p}": float("nan") for p in percentiles}
    vals = np.percentile(finite, percentiles)
    return {f"p{p}": float(v) for p, v in zip(percentiles, vals)}


# Labeled-catalog stems produced by scripts/label_detection_catalogs.py
HLTDS_LABEL_CATALOG_STEMS = (
    "sfft_psfcat",
    "sfft_finder",
    "zogy_psfcat",
    "zogy_finder",
)


def labeled_catalog_filename(catalog_stem: str, *, kind: str = "parquet") -> str:
    """Return ``{stem}_labeled.parquet`` or ``{stem}_labeled.csv``."""
    if catalog_stem not in HLTDS_LABEL_CATALOG_STEMS:
        raise ValueError(f"Unknown catalog stem {catalog_stem!r}; choose from {HLTDS_LABEL_CATALOG_STEMS}")
    ext = "parquet" if kind == "parquet" else "csv"
    return f"{catalog_stem}_labeled.{ext}"


def labeled_parquet_path(labels_dir: Path, jid: str, catalog_stem: str = "sfft_psfcat") -> Path:
    return labels_dir / jid / labeled_catalog_filename(catalog_stem, kind="parquet")


def discover_labeled_jids(labels_dir: Path, catalog_stem: str = "sfft_psfcat") -> list[str]:
    """Sorted jid names that have a labeled PSF parquet for *catalog_stem*."""
    fname = labeled_catalog_filename(catalog_stem, kind="parquet")
    jids = [p.parent.name for p in labels_dir.glob(f"jid*/{fname}")]
    return sorted(jids, key=lambda j: int(j.replace("jid", "")))
