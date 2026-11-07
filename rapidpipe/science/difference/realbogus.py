# ported-from: modules/utils/rapid_data_analysis.py, modules/utils/rapid_pipeline_subs.py @ 9ed0e0fc
"""`dev`'s RuBR-AT real/bogus columns of the SFFT PSF-fit catalog: ``rb_score``, ``rb_label``.

`dev`'s ``compute_rb`` (``rapid_data_analysis.py``), as
``_compute_extra_catalog_cols`` (``rapid_pipeline_subs.py``) calls it for the
SFFT PSF-fit catalog (``compute_extra_cols_photutils``): the classifier of
``modules/rubrat`` (vendored verbatim from `dev`) scores every source from a
64 x 64 stack of the background-subtracted science image, the resampled
gain-matched reference and the difference image the catalog was fitted on,
with the nine catalog features ``canonical_9.json`` names. The label is 1 at
or above the model's ``threshold.json``, 0 below, -1 for a source not scored
(its position not finite or off the image, a non-finite cutout pixel or
feature, the classifier's ``rb_status``).

Three things differ from `dev`:

- An unscored source's ``rb_score`` is NaN, where `dev` writes its
  ``fill_value`` -999 into the text catalog and its loader loads NULL; the
  catalog here is the parquet file, as for :mod:`.columns`, whose NaN the
  source set loads as null.
- The features are read from the parquet catalog at full precision, where
  `dev` reads them back from its text catalog (``x_fit`` to 4 decimals,
  ``flux_fit`` to 6).
- A classifier failure fails the job, as every fault in the stage does;
  `dev` skips the columns with a warning.

The classifier runs in the stage's own process on ``onnxruntime``
(:func:`load_classifier`): the model is the training checkpoint exported
to ONNX (``modules/rubrat/training/export_onnx.py``), and TensorFlow and
Keras are not in the pipeline environment (rapid_docs ``decisions.md``,
``decision-reliability-scored``). The difference stage loads it once per
job and scores every catalog with it; :func:`main` is the hand tool over
one job directory's files.

The model is a directory of four files (``SHA256SUMS`` beside the three
`dev`'s ``[RUBRAT]`` names) at the prefix :func:`model_prefix` resolves:
``[difference.rubrat] model_prefix`` when set, else the bucket the SSM
parameter ``model_bucket_parameter`` holds and the key ``model_key``, as
the ``[kafka]`` table names its deployment values. :func:`fetch_model`
verifies every file against ``SHA256SUMS``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from modules.rubrat.inference.classifier import RealBogusClassifier, load_validation_threshold
from modules.rubrat.preprocessing.data_utils import FeatureZScore

#: The columns appended to the catalog, `dev`'s ``rb_catalog_col_names``.
RB_COLUMNS = ("rb_score", "rb_label")

#: The model files, `dev`'s ``[RUBRAT]`` keys but ``model``, the ONNX file where `dev` names its
#: Keras ``checkpoint``.
MODEL_FILES = ("model", "feats_scaler", "threshold_json")

#: `dev`'s ``_rb_filter_aliases``: the database filter name the FITS header carries.
FILTER_ALIASES = {"W146": "F146"}

#: `dev`'s ``_rb_column_aliases``: the classifier's column, the catalog's.
CATALOG_COLUMNS = {
    "xcentroid": "x_fit", "ycentroid": "y_fit", "flux_fit": "flux_fit", "flux_err": "flux_err",
    "cfit": "cfit", "reduced_chi2": "reduced_chi2", "x_err": "x_err", "y_err": "y_err",
    "npixfit": "n_pixels_fit", "flags": "flags", "sharpness": "sharpness",
    "roundness1": "roundness1", "roundness2": "roundness2",
}


@dataclass(frozen=True)
class Model:
    """A fetched, verified model: its prefix, version and local files."""

    prefix: str
    files: dict[str, Path]

    @property
    def version(self) -> str:
        """`dev`'s ``rb_version``: the last path component of the prefix."""
        return version_of(self.prefix)


def version_of(prefix: str) -> str:
    """The model's identifier, `dev`'s rule: the prefix's last path component."""
    return prefix.rstrip("/").rsplit("/", 1)[-1]


def model_prefix(rubrat: dict[str, Any], ssm: Any = None) -> str:
    """The model's prefix: ``model_prefix``, else ``s3://<the parameter's bucket>/<model_key>``."""
    if rubrat["model_prefix"]:
        return rubrat["model_prefix"].rstrip("/")
    if ssm is None:
        import boto3
        ssm = boto3.client("ssm")
    bucket = ssm.get_parameter(Name=rubrat["model_bucket_parameter"])["Parameter"]["Value"]
    return f"s3://{bucket}/{rubrat['model_key'].strip('/')}"


def fetch_model(prefix: str, rubrat: dict[str, Any], get: Callable[[str], Path]) -> Model:
    """The model's files through ``get`` (``rapidpipe.foundation.prefix.get``), each checked
    against the prefix's ``SHA256SUMS``; a missing or mismatched file raises ``ValueError``."""
    sums = {}
    for line in get(f"{prefix}/SHA256SUMS").read_text().splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)
            sums[name.lstrip("*")] = digest
    files = {}
    for key in MODEL_FILES:
        name = rubrat[key]
        path = get(f"{prefix}/{name}")
        if name not in sums:
            raise ValueError(f"{prefix}/SHA256SUMS lists no {name}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != sums[name]:
            raise ValueError(f"{prefix}/{name}: sha256 {digest}, SHA256SUMS says {sums[name]}")
        files[key] = path
    return Model(prefix, files)


def filter_of(l2_image: Path) -> str:
    """The L2 image's ``FILTER``, from its image HDU, else its primary (`dev` ``get_l2_image_metadata``)."""
    from astropy.io import fits

    from rapidpipe.science.limmag import image_hdu_index

    with fits.open(l2_image) as hdul:
        value = hdul[image_hdu_index(hdul)].header.get("FILTER", hdul[0].header.get("FILTER"))
    if value is None:
        raise ValueError(f"no FILTER keyword in {l2_image}")
    return str(value)


class OnnxSession:
    """The two members of a Keras model that ``RealBogusClassifier`` reads, over an
    ``onnxruntime.InferenceSession``: ``inputs`` (``.name``, ``.shape``) and ``predict``."""

    def __init__(self, session):
        self.session = session
        self.inputs = [_Input(i.name, tuple(d if isinstance(d, int) else None for d in i.shape))
                       for i in session.get_inputs()]
        self.outputs = [o.name for o in session.get_outputs()]

    def predict(self, inputs: dict[str, np.ndarray], batch_size: int, verbose: int = 0):
        """The outputs by name, ``inputs`` fed to the session's inputs by name in batches of
        ``batch_size``."""
        names = [i.name for i in self.inputs]
        n = len(inputs[names[0]])
        parts = [self.session.run(None, {name: inputs[name][i:i + batch_size] for name in names})
                 for i in range(0, n, batch_size)]
        return {name: np.concatenate([part[k] for part in parts]) for k, name in enumerate(self.outputs)}


@dataclass(frozen=True)
class _Input:
    name: str
    shape: tuple


class OnnxClassifier(RealBogusClassifier):
    """`dev`'s ``RealBogusClassifier`` on an ONNX model: its ``__init__``'s checks, and an
    :class:`OnnxSession` on the CPU where it loads the Keras checkpoint."""

    def __init__(self, model: str | Path, feature_scaler: str | Path, *, threshold: float,
                 survey_id: int, batch_size: int = 128) -> None:
        import onnxruntime

        if survey_id not in (0, 1):
            raise ValueError("survey_id must be 0 (HLTDS token) or 1 (GBTDS token)")
        if not 0.0 <= float(threshold) <= 1.0:
            raise ValueError("threshold must be in [0, 1]")
        if int(batch_size) <= 0:
            raise ValueError("batch_size must be positive")
        self.checkpoint = Path(model).resolve()
        self.feature_scaler_path = Path(feature_scaler).resolve()
        self.threshold = float(threshold)
        self.survey_id = int(survey_id)
        self.batch_size = int(batch_size)
        if not self.checkpoint.is_file():
            raise FileNotFoundError(f"Model not found: {self.checkpoint}")
        self.scaler = FeatureZScore(self.feature_scaler_path)
        self.model = OnnxSession(onnxruntime.InferenceSession(
            str(self.checkpoint), providers=["CPUExecutionProvider"]))
        image = next((i for i in self.model.inputs if i.name == "images"), None)
        if image is None or len(image.shape) != 4 or image.shape[-1] != 3:
            raise ValueError("Model does not expose the RuBR-AT images input")
        if image.shape[1] is None or image.shape[1] != image.shape[2]:
            raise ValueError(f"Model image input must have a fixed square shape, got {image.shape}")
        self.image_size = int(image.shape[1])


def load_classifier(model: Path, feats_scaler: Path, threshold_json: Path, survey_id: int,
                    batch_size: int) -> OnnxClassifier:
    """The classifier on the model's files (:data:`MODEL_FILES`)."""
    return OnnxClassifier(model, feats_scaler, threshold=load_validation_threshold(threshold_json),
                          survey_id=int(survey_id), batch_size=int(batch_size))


def summary_path(output: Path) -> Path:
    """Where :func:`main` writes its summary: beside the scored catalog."""
    return output.with_name(output.name + ".json")


def score(classifier, science: np.ndarray, reference: np.ndarray, difference: np.ndarray,
          catalog, filter_name: str) -> dict[str, np.ndarray]:
    """`dev` ``compute_rb`` on arrays and a joined PSF-fit catalog (a ``pyarrow.Table``).

    Positions are the catalog's zero-based ``x_fit``, ``y_fit``. Returns
    ``rb_score`` (NaN where not scored), ``rb_label`` and ``rb_status``,
    one value per catalog row, in its order.
    """
    import pandas as pd

    frame = pd.DataFrame({name: catalog.column(column).to_numpy(zero_copy_only=False).astype(float)
                          for name, column in CATALOG_COLUMNS.items()})
    n = len(frame)
    if n == 0:
        return {"rb_score": np.full(0, np.nan), "rb_label": np.full(0, -1, np.int16),
                "rb_status": np.full(0, "", object)}
    filter_name = FILTER_ALIASES.get(str(filter_name).strip().upper(), filter_name)
    scored = classifier.score_arrays(np.asarray(science, np.float32), np.asarray(reference, np.float32),
                                     np.asarray(difference, np.float32), frame,
                                     filter_name=filter_name)
    if len(scored) != n:
        raise RuntimeError(f"the classifier returned {len(scored)} rows for {n} sources")
    valid = scored["rb_valid"].to_numpy(dtype=bool)
    return {"rb_score": np.where(valid, scored["rb_score"].to_numpy(dtype=float), np.nan),
            "rb_label": scored["rb_label"].to_numpy().astype(np.int16),
            "rb_status": scored["rb_status"].to_numpy(dtype=object)}


def add_rb_columns(catalog: Path, output: Path, science: Path, reference: Path, difference: Path,
                   filter_name: str, classifier) -> dict[str, Any]:
    """Write ``catalog`` (a joined PSF-fit catalog, parquet) to ``output`` with ``rb_score`` and
    ``rb_label`` appended.

    Every other column and the row order are kept. Returns what the
    manifest records: the rows, the scored count, the unscored count per
    reason and the seconds.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq
    from astropy.io import fits

    start = time.perf_counter()
    table = pq.read_table(catalog)
    clash = [c for c in RB_COLUMNS if c in table.column_names]
    if clash:
        raise ValueError(f"{catalog} already has {clash}")
    values = score(classifier, fits.getdata(science, 0), fits.getdata(reference, 0),
                   fits.getdata(difference, 0), table, filter_name)
    table = table.append_column("rb_score", pa.array(values["rb_score"], pa.float64()))
    table = table.append_column("rb_label", pa.array(values["rb_label"], pa.int16()))
    pq.write_table(table, output)
    unscored: dict[str, int] = {}
    for status in values["rb_status"]:
        if status:
            unscored[str(status)] = unscored.get(str(status), 0) + 1
    return {"rows": table.num_rows, "scored": int(np.isfinite(values["rb_score"]).sum()),
            "unscored": unscored, "seconds": round(time.perf_counter() - start, 3)}


def main(argv: Sequence[str] | None = None, load: Callable = load_classifier) -> int:
    """The hand tool: score one catalog with the model's files, write it and its summary."""
    parser = argparse.ArgumentParser(prog=f"python -m {__name__}", description=main.__doc__)
    for name in ("catalog", "output", "science", "reference", "difference", "model",
                 "feats-scaler", "threshold-json"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--filter", required=True)
    parser.add_argument("--survey-id", type=int, required=True)
    parser.add_argument("--batch-size", type=int, required=True)
    args = parser.parse_args(argv)
    classifier = load(args.model, args.feats_scaler, args.threshold_json, args.survey_id,
                      args.batch_size)
    summary = add_rb_columns(args.catalog, args.output, args.science, args.reference,
                             args.difference, args.filter, classifier)
    summary_path(args.output).write_text(json.dumps(summary))
    return 0


__all__: Sequence[str] = (
    "CATALOG_COLUMNS", "FILTER_ALIASES", "MODEL_FILES", "Model", "OnnxClassifier", "OnnxSession",
    "RB_COLUMNS", "add_rb_columns", "fetch_model", "load_classifier", "main", "model_prefix",
    "score", "summary_path", "version_of",
)


if __name__ == "__main__":
    raise SystemExit(main())
