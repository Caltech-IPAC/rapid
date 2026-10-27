"""Tests for rapidpipe.science.difference.realbogus: the model's location and checksums, the columns.

A faked classifier stands in for `dev`'s ``RealBogusClassifier``: no
TensorFlow and no model here; the proof run scores with the real one.
"""

from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from astropy.io import fits

from rapidpipe.science.difference import realbogus

RUBRAT = {"model_prefix": "", "model_bucket_parameter": "/p/inputs-bucket",
          "model_key": "rubrat/m-1", "checkpoint": "best.keras",
          "feats_scaler": "scaler.json", "threshold_json": "threshold.json",
          "survey_id": 0, "batch_size": 4}


class FakeSsm:
    def get_parameter(self, Name):
        return {"Parameter": {"Value": {"/p/inputs-bucket": "the-bucket"}[Name]}}


class FakeClassifier:
    """`dev`'s ``score_arrays`` contract: one row per source, in order; NaN score off the image."""

    threshold = 0.5

    def __init__(self):
        self.seen = None

    def score_arrays(self, science, reference, difference, detections, *, filter_name):
        self.seen = {"filter": filter_name, "dtype": science.dtype, "frame": detections}
        x = detections["xcentroid"].to_numpy()
        valid = np.isfinite(x) & (x < science.shape[1])
        score = np.where(valid, x / 100.0, np.nan)
        return pd.DataFrame({"rb_score": score, "rb_valid": valid,
                             "rb_label": np.where(valid, (score >= self.threshold).astype(int), -1),
                             "rb_status": np.where(valid, "", "centroid_outside_image")})


def catalog(x):
    n = len(x)
    columns = {column: np.full(n, 1.0) for column in realbogus.CATALOG_COLUMNS.values()}
    columns.update(id=np.arange(1, n + 1), x_fit=np.asarray(x, float), peak=np.full(n, 9.0))
    return pa.table(columns)


def test_the_prefix_is_the_parameters_bucket_and_the_key_unless_a_prefix_is_set():
    assert realbogus.model_prefix(RUBRAT, FakeSsm()) == "s3://the-bucket/rubrat/m-1"
    assert realbogus.model_prefix({**RUBRAT, "model_prefix": "file:///m/x/"}, None) == "file:///m/x"
    assert realbogus.version_of("s3://the-bucket/rubrat/m-1/") == "m-1"


def _model_dir(tmp_path, sums=None):
    files = {"best.keras": b"weights", "scaler.json": b"{}", "threshold.json": b'{"threshold": 0.5}'}
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    sums = sums or {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    (tmp_path / "SHA256SUMS").write_text("".join(f"{d}  {n}\n" for n, d in sums.items()))
    return lambda url: tmp_path / url.rsplit("/", 1)[-1]


def test_fetch_model_returns_the_three_files_when_every_checksum_holds(tmp_path):
    model = realbogus.fetch_model("s3://b/rubrat/m-1", RUBRAT, _model_dir(tmp_path))
    assert sorted(model.files) == sorted(realbogus.MODEL_FILES)
    assert model.version == "m-1"


def test_fetch_model_refuses_a_file_whose_checksum_differs_or_is_not_listed(tmp_path):
    get = _model_dir(tmp_path, sums={"best.keras": "0" * 64, "scaler.json": "0" * 64})
    with pytest.raises(ValueError, match="sha256"):
        realbogus.fetch_model("s3://b/m", RUBRAT, get)
    get = _model_dir(tmp_path, sums={"best.keras": hashlib.sha256(b"weights").hexdigest()})
    with pytest.raises(ValueError, match="lists no"):
        realbogus.fetch_model("s3://b/m", RUBRAT, get)


def test_score_maps_the_catalog_columns_and_leaves_an_unscored_source_nan_and_minus_one():
    image = np.zeros((64, 64))
    classifier = FakeClassifier()
    values = realbogus.score(classifier, image, image, image, catalog([10.0, 60.0, 80.0, np.nan]), "W146")
    np.testing.assert_allclose(values["rb_score"], [0.1, 0.6, np.nan, np.nan])
    assert values["rb_label"].tolist() == [0, 1, -1, -1]
    assert classifier.seen["filter"] == "F146"                 # dev's W146 alias
    assert classifier.seen["dtype"] == np.float32              # dev loads the images as float32
    assert set(classifier.seen["frame"].columns) == set(realbogus.CATALOG_COLUMNS)


def test_add_rb_columns_writes_the_catalog_with_two_columns_appended_and_every_row_kept(tmp_path):
    image = tmp_path / "image.fits"
    fits.writeto(image, np.zeros((64, 64), np.float32))
    path, out = tmp_path / "catalog.parquet", tmp_path / "scored.parquet"
    before = catalog([30.0, 70.0, 50.0])
    pq.write_table(before, path)
    summary = realbogus.add_rb_columns(path, out, image, image, image, "F146", FakeClassifier())
    after = pq.read_table(out)
    assert pq.read_table(path).equals(before)
    assert after.column_names == before.column_names + list(realbogus.RB_COLUMNS)
    assert after.select(before.column_names).equals(before)
    assert after.column("rb_label").to_pylist() == [0, -1, 1]
    assert (summary["rows"], summary["scored"], summary["unscored"]) == (
        3, 2, {"centroid_outside_image": 1})
    with pytest.raises(ValueError, match="already has"):
        realbogus.add_rb_columns(out, tmp_path / "again.parquet", image, image, image, "F146",
                                 FakeClassifier())


def test_the_sidecar_command_scores_the_catalog_into_its_output_with_the_models_files(tmp_path):
    image = tmp_path / "image.fits"
    fits.writeto(image, np.zeros((64, 64), np.float32))
    path, out = tmp_path / "catalog.parquet", tmp_path / "scored.parquet"
    pq.write_table(catalog([30.0, 60.0]), path)
    model = realbogus.Model("s3://b/rubrat/m-1", {key: tmp_path / key for key in realbogus.MODEL_FILES})
    args = realbogus.command("/venv/bin/python", path, out, image, image, image, "F146", model, RUBRAT)
    assert args[:3] == ["/venv/bin/python", "-m", "rapidpipe.science.difference.realbogus"]
    loaded = []

    def load(*files):
        loaded.append(files)
        return FakeClassifier()

    assert realbogus.main(args[3:], load=load) == 0
    assert loaded == [(model.files["checkpoint"], model.files["feats_scaler"],
                       model.files["threshold_json"], 0, 4)]
    assert pq.read_table(out).column("rb_label").to_pylist() == [0, 1]
    assert json.loads(realbogus.summary_path(out).read_text())["scored"] == 2
