"""Inference adapter for RAPID per-job products.

This module is deliberately independent of the research NPZ format.  It turns
the products already present in one RAPID ``jid*`` directory into the exact
four tensors consumed by a trained RuBR-AT checkpoint, then writes scores back
in detection-catalog order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

from classification.data_utils import EXPECTED_FEATURE_NAMES, FeatureZScore
from classification.npz_builders import FILTER_IDS, arcsinh_stack_cutout, feature_row, filter_id
from ingestion.fits_loader import load_fits
from ingestion.rubr_adapter import load_detection_catalog, normalize_filter_name
from rubrat.validation import sha256_file

SCIENCE_PRODUCT = "bkg_subbed_science_image.fits"
REFERENCE_PRODUCT = "awaicgen_output_mosaic_image_resampled_gainmatched.fits"
DIFFERENCE_PRODUCTS = (
    "sfftdiffimage_dconv_masked.fits",
    "sfftdiffimage_masked.fits",
)
PSF_CATALOG_PRODUCTS = (
    "sfftdiffimage_masked_psfcat.parquet",
    "sfftdiffimage_masked_psfcat.txt",
)
FINDER_CATALOG_PRODUCTS = (
    "sfftdiffimage_masked_psfcat_finder.parquet",
    "sfftdiffimage_masked_psfcat_finder.txt",
)
DEFAULT_OUTPUT_PRODUCT = "sfftdiffimage_masked_psfcat_rubrat.parquet"

_POSITION_COLUMNS = {"xcentroid", "ycentroid"}
_FIT_COLUMNS = {"flux_fit", "flux_err", "cfit", "reduced_chi2", "x_err", "y_err", "npixfit", "flags"}
_MORPHOLOGY_COLUMNS = {"sharpness", "roundness1", "roundness2"}


@dataclass(frozen=True)
class RapidProductPaths:
    """Resolved products for a single RAPID job."""

    job_dir: Path
    science: Path
    reference: Path
    difference: Path
    psf_catalog: Path
    finder_catalog: Path | None


def _resolve_relative(job_dir: Path, value: str | Path | None, candidates: Sequence[str], label: str) -> Path:
    if value is not None:
        path = Path(value)
        path = path if path.is_absolute() else job_dir / path
        if not path.is_file():
            raise FileNotFoundError(f"{label} not found: {path}")
        return path
    for name in candidates:
        path = job_dir / name
        if path.is_file():
            return path
    raise FileNotFoundError(f"No {label} found in {job_dir}; tried {list(candidates)}")


def resolve_rapid_products(
    job_dir: str | Path,
    *,
    science: str | Path | None = None,
    reference: str | Path | None = None,
    difference: str | Path | None = None,
    psf_catalog: str | Path | None = None,
    finder_catalog: str | Path | None = None,
) -> RapidProductPaths:
    """Resolve canonical RAPID filenames, accepting explicit overrides."""
    root = Path(job_dir).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"RAPID job directory not found: {root}")
    psf = _resolve_relative(root, psf_catalog, PSF_CATALOG_PRODUCTS, "PSF-fit catalog")
    finder: Path | None
    if finder_catalog is not None:
        finder = _resolve_relative(root, finder_catalog, (), "finder catalog")
    else:
        finder = next((root / name for name in FINDER_CATALOG_PRODUCTS if (root / name).is_file()), None)
    return RapidProductPaths(
        job_dir=root,
        science=_resolve_relative(root, science, (SCIENCE_PRODUCT,), "science image"),
        reference=_resolve_relative(root, reference, (REFERENCE_PRODUCT,), "reference image"),
        difference=_resolve_relative(root, difference, DIFFERENCE_PRODUCTS, "difference image"),
        psf_catalog=psf,
        finder_catalog=finder,
    )


def _merge_finder_columns(psf: pd.DataFrame, finder: pd.DataFrame | None) -> pd.DataFrame:
    merged = psf.copy()
    missing = _MORPHOLOGY_COLUMNS.difference(merged.columns)
    if not missing:
        return merged
    if finder is None:
        raise ValueError(
            "PSF-fit catalog lacks finder morphology columns "
            f"{sorted(missing)} and no finder catalog was supplied"
        )
    if "id" in merged.columns and "id" in finder.columns:
        if merged["id"].duplicated().any() or finder["id"].duplicated().any():
            raise ValueError("Catalog id columns must be unique for a one-to-one merge")
        lookup = finder.set_index("id")
        unknown = merged.loc[~merged["id"].isin(lookup.index), "id"].tolist()
        if unknown:
            raise ValueError(f"Finder catalog is missing {len(unknown)} PSF ids; first missing id={unknown[0]!r}")
        for column in sorted(missing):
            if column in lookup.columns:
                merged[column] = lookup.loc[merged["id"], column].to_numpy()
    elif len(merged) == len(finder):
        for column in sorted(missing):
            if column in finder.columns:
                merged[column] = finder[column].to_numpy()
    else:
        raise ValueError(
            "PSF and finder catalogs have no id join key and different row counts "
            f"({len(merged)} != {len(finder)})"
        )
    still_missing = _MORPHOLOGY_COLUMNS.difference(merged.columns)
    if still_missing:
        raise ValueError(f"Finder catalog lacks required morphology columns: {sorted(still_missing)}")
    return merged


def load_rapid_detections(paths: RapidProductPaths) -> pd.DataFrame:
    """Load and strictly validate the feature table used for inference."""
    psf = load_detection_catalog(paths.psf_catalog)
    finder = load_detection_catalog(paths.finder_catalog) if paths.finder_catalog is not None else None
    detections = _merge_finder_columns(psf, finder)
    required = _POSITION_COLUMNS | _FIT_COLUMNS | _MORPHOLOGY_COLUMNS
    missing = required.difference(detections.columns)
    if missing:
        raise ValueError(f"RAPID catalogs do not provide required RuBR-AT columns: {sorted(missing)}")
    if detections.empty:
        return detections.reset_index(drop=True)
    if detections[list(required)].isna().all(axis=0).any():
        empty = detections[list(required)].columns[detections[list(required)].isna().all(axis=0)].tolist()
        raise ValueError(f"RAPID catalog columns contain no usable values: {empty}")
    return detections.reset_index(drop=True)


def load_validation_threshold(path: str | Path) -> float:
    """Read the validation-selected threshold from a RuBR-AT metrics JSON."""
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    value: Any = payload
    for key in ("validation_threshold", "threshold"):
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            break
    if isinstance(value, dict) and "threshold" in value:
        value = value["threshold"]
    try:
        threshold = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Could not find a numeric validation threshold in {source}") from exc
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"Threshold must be in [0, 1], got {threshold}")
    return threshold


def _canonical_filter(name: str) -> str:
    text = str(name).strip().upper()
    try:
        canonical = normalize_filter_name(text)
    except ValueError:
        canonical = text
    if canonical not in FILTER_IDS or filter_id(canonical) < 0:
        raise ValueError(f"Filter {name!r} is not in the trained filter registry")
    return canonical


class RAPIDRealBogusClassifier:
    """Long-lived RuBR-AT scorer suitable for one or many RAPID jobs."""

    def __init__(
        self,
        checkpoint: str | Path,
        feature_scaler: str | Path,
        *,
        threshold: float,
        survey_id: int,
        batch_size: int = 128,
    ) -> None:
        if survey_id not in (0, 1):
            raise ValueError("survey_id must be 0 (HLTDS token) or 1 (GBTDS token)")
        if not 0.0 <= float(threshold) <= 1.0:
            raise ValueError("threshold must be in [0, 1]")
        if int(batch_size) <= 0:
            raise ValueError("batch_size must be positive")
        self.checkpoint = Path(checkpoint).resolve()
        self.feature_scaler_path = Path(feature_scaler).resolve()
        self.threshold = float(threshold)
        self.survey_id = int(survey_id)
        self.batch_size = int(batch_size)
        if not self.checkpoint.is_file():
            raise FileNotFoundError(f"Checkpoint not found: {self.checkpoint}")
        self.scaler = FeatureZScore(self.feature_scaler_path)

        from tensorflow import keras

        from classification import model_factory as _model_factory  # noqa: F401

        self.model = keras.models.load_model(self.checkpoint, compile=False)
        image_input = next((item for item in self.model.inputs if item.name.split(":")[0] == "images"), None)
        if image_input is None or len(image_input.shape) != 4 or image_input.shape[-1] != 3:
            raise ValueError("Checkpoint does not expose the RuBR-AT images input")
        if image_input.shape[1] is None or image_input.shape[1] != image_input.shape[2]:
            raise ValueError(f"Checkpoint image input must have a fixed square shape, got {image_input.shape}")
        self.image_size = int(image_input.shape[1])

    def score_arrays(
        self,
        science: np.ndarray,
        reference: np.ndarray,
        difference: np.ndarray,
        detections: pd.DataFrame,
        *,
        filter_name: str,
    ) -> pd.DataFrame:
        """Score arrays and return catalog-aligned result columns."""
        detections = detections.reset_index(drop=True)
        if science.shape != reference.shape or science.shape != difference.shape:
            raise ValueError(
                "Science, reference, and difference images must be pixel-aligned; "
                f"got {science.shape}, {reference.shape}, {difference.shape}"
            )
        required = _POSITION_COLUMNS | _FIT_COLUMNS | _MORPHOLOGY_COLUMNS
        missing = required.difference(detections.columns)
        if missing:
            raise ValueError(f"Detection table is missing required columns: {sorted(missing)}")
        canonical_filter = _canonical_filter(filter_name)
        fid = filter_id(canonical_filter)
        images: list[np.ndarray] = []
        features: list[np.ndarray] = []
        valid_indices: list[int] = []
        reasons = np.full(len(detections), "nonfinite_or_unusable_cutout", dtype=object)
        scores = np.full(len(detections), np.nan, dtype=np.float64)

        def _flush() -> None:
            if not images:
                return
            raw_features = np.stack(features).astype(np.float32)
            inputs = {
                "images": np.stack(images).astype(np.float32),
                "tabular": self.scaler.transform(raw_features),
                "survey_id": np.full(len(images), self.survey_id, dtype=np.int32),
                "filter_id": np.full(len(images), fid, dtype=np.int32),
            }
            prediction = self.model.predict(inputs, batch_size=self.batch_size, verbose=0)
            if isinstance(prediction, dict):
                if "rb" in prediction:
                    prediction = prediction["rb"]
                elif len(prediction) == 1:
                    prediction = next(iter(prediction.values()))
                else:
                    raise ValueError(f"Checkpoint has ambiguous outputs: {sorted(prediction)}")
            values = np.asarray(prediction, dtype=np.float64).reshape(-1)
            if len(values) != len(valid_indices) or not np.isfinite(values).all():
                raise ValueError("Checkpoint returned invalid or misaligned predictions")
            scores[np.asarray(valid_indices, dtype=np.int64)] = values
            images.clear()
            features.clear()
            valid_indices.clear()

        preprocess_chunk_size = max(self.batch_size, 512)
        for index, row in detections.iterrows():
            try:
                x = float(row["xcentroid"])
                y = float(row["ycentroid"])
            except (TypeError, ValueError):
                reasons[index] = "invalid_centroid"
                continue
            if not np.isfinite(x) or not np.isfinite(y):
                reasons[index] = "nonfinite_centroid"
                continue
            half = self.image_size / 2.0
            if x + half <= 0 or y + half <= 0 or x - half >= science.shape[1] or y - half >= science.shape[0]:
                reasons[index] = "centroid_outside_image"
                continue
            cutout = arcsinh_stack_cutout(
                science,
                reference,
                difference,
                x,
                y,
                image_size=self.image_size,
            )
            feat = feature_row(row)
            if cutout is None:
                reasons[index] = "nonfinite_or_unusable_cutout"
                continue
            if not np.isfinite(feat).all():
                reasons[index] = "nonfinite_features"
                continue
            images.append(cutout)
            features.append(feat)
            valid_indices.append(int(index))
            reasons[index] = ""
            if len(images) >= preprocess_chunk_size:
                _flush()
        _flush()

        output = detections.copy()
        output["rb_score"] = scores
        valid = np.isfinite(scores)
        labels = np.full(len(scores), -1, dtype=np.int8)
        labels[valid] = (scores[valid] >= self.threshold).astype(np.int8)
        output["rb_label"] = labels
        output["rb_valid"] = valid
        output["rb_status"] = reasons
        output["rb_threshold"] = self.threshold
        output["rb_model_filter"] = canonical_filter
        output["rb_model_filter_id"] = fid
        output["rb_model_survey_id"] = self.survey_id
        return output

    def score_job(
        self,
        job_dir: str | Path,
        *,
        filter_name: str | None = None,
        science: str | Path | None = None,
        reference: str | Path | None = None,
        difference: str | Path | None = None,
        psf_catalog: str | Path | None = None,
        finder_catalog: str | Path | None = None,
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        """Score one RAPID job directory and return rows plus provenance."""
        paths = resolve_rapid_products(
            job_dir,
            science=science,
            reference=reference,
            difference=difference,
            psf_catalog=psf_catalog,
            finder_catalog=finder_catalog,
        )
        detections = load_rapid_detections(paths)
        sci = load_fits(paths.science, include_err=False, include_dq=False)
        ref = load_fits(paths.reference, include_err=False, include_dq=False)
        diff = load_fits(paths.difference, include_err=False, include_dq=False)
        selected_filter = filter_name or sci.get("filter", "UNKNOWN")
        rows = self.score_arrays(sci["data"], ref["data"], diff["data"], detections, filter_name=selected_filter)
        canonical_filter = _canonical_filter(selected_filter)
        provenance = {
            "schema_version": "rubrat_rapid_inference_v1",
            "job_dir": str(paths.job_dir),
            "products": {
                "science": str(paths.science),
                "reference": str(paths.reference),
                "difference": str(paths.difference),
                "psf_catalog": str(paths.psf_catalog),
                "finder_catalog": str(paths.finder_catalog) if paths.finder_catalog else None,
            },
            "checkpoint": {"path": str(self.checkpoint), "sha256": sha256_file(self.checkpoint)},
            "feature_scaler": {
                "path": str(self.feature_scaler_path),
                "sha256": sha256_file(self.feature_scaler_path),
                "feature_names": EXPECTED_FEATURE_NAMES,
            },
            "image_size": self.image_size,
            "image_channel_order": ["science", "reference", "difference"],
            "image_transform": "arcsinh(pixel / 0.01)",
            "filter": str(rows["rb_model_filter"].iloc[0]) if len(rows) else canonical_filter,
            "filter_id": int(rows["rb_model_filter_id"].iloc[0]) if len(rows) else filter_id(canonical_filter),
            "survey_id": self.survey_id,
            "threshold": self.threshold,
            "n_detections": int(len(rows)),
            "n_scored": int(rows["rb_valid"].sum()),
            "n_invalid": int((~rows["rb_valid"]).sum()),
        }
        return rows, provenance


def write_scored_catalog(
    rows: pd.DataFrame,
    provenance: dict[str, Any],
    output: str | Path,
    *,
    overwrite: bool = False,
) -> tuple[Path, Path]:
    """Write a scored catalog and adjacent JSON provenance sidecar."""
    destination = Path(output)
    sidecar = destination.with_suffix(destination.suffix + ".provenance.json")
    if not overwrite and (destination.exists() or sidecar.exists()):
        raise FileExistsError(f"Output already exists: {destination} or {sidecar}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.suffix.lower() in {".parquet", ".pqt"}:
        rows.to_parquet(destination, index=False)
    elif destination.suffix.lower() == ".csv":
        rows.to_csv(destination, index=False)
    elif destination.suffix.lower() == ".txt":
        rows.to_csv(destination, index=False, sep=" ")
    else:
        raise ValueError("Output suffix must be .parquet, .pqt, .csv, or .txt")
    payload = dict(provenance)
    payload["output"] = {"path": str(destination.resolve()), "sha256": sha256_file(destination)}
    sidecar.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return destination, sidecar
