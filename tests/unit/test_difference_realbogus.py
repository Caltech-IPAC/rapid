"""Tests for rapidpipe.science.difference.realbogus: the model's location and checksums, the columns.

A faked classifier stands in for `dev`'s ``RealBogusClassifier``; the ONNX
classifier is tested on a tiny graph encoded here by hand (the ``onnx``
package is the export side's, not the pipeline's), and the proof run scores
with the real model.
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

from modules.rubrat.preprocessing.npz_builders import feature_row
from rapidpipe.science.difference import realbogus

RUBRAT = {"model_prefix": "", "model_bucket_parameter": "/p/inputs-bucket",
          "model_key": "rubrat/m-1", "model": "best.onnx",
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
    files = {"best.onnx": b"weights", "scaler.json": b"{}", "threshold.json": b'{"threshold": 0.5}'}
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
    get = _model_dir(tmp_path, sums={"best.onnx": "0" * 64, "scaler.json": "0" * 64})
    with pytest.raises(ValueError, match="sha256"):
        realbogus.fetch_model("s3://b/m", RUBRAT, get)
    get = _model_dir(tmp_path, sums={"best.onnx": hashlib.sha256(b"weights").hexdigest()})
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


def test_the_hand_tool_scores_the_catalog_into_its_output_with_the_models_files(tmp_path):
    image = tmp_path / "image.fits"
    fits.writeto(image, np.zeros((64, 64), np.float32))
    path, out = tmp_path / "catalog.parquet", tmp_path / "scored.parquet"
    pq.write_table(catalog([30.0, 60.0]), path)
    files = {key: tmp_path / key for key in realbogus.MODEL_FILES}
    loaded = []

    def load(*args):
        loaded.append(args)
        return FakeClassifier()

    args = ["--catalog", path, "--output", out, "--science", image, "--reference", image,
            "--difference", image, "--filter", "F146", "--model", files["model"],
            "--feats-scaler", files["feats_scaler"], "--threshold-json", files["threshold_json"],
            "--survey-id", "0", "--batch-size", "4"]
    assert realbogus.main([str(a) for a in args], load=load) == 0
    assert loaded == [(files["model"], files["feats_scaler"], files["threshold_json"], 0, 4)]
    assert pq.read_table(out).column("rb_label").to_pylist() == [0, 1]
    assert json.loads(realbogus.summary_path(out).read_text())["scored"] == 2


# A hand-encoded ONNX model (protobuf wire format, onnx.proto's field numbers): rb, of shape
# (N, 1), is the sigmoid of the mean of tabular's row; images, survey_id and filter_id are
# inputs it does not read, declared in another order than the classifier builds them.

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        out.append((n & 0x7F) | (0x80 if n > 0x7F else 0))
        n >>= 7
        if not n:
            return bytes(out)


def _field(number: int, value) -> bytes:
    if isinstance(value, int):
        return _varint(number << 3) + _varint(value)
    data = value.encode() if isinstance(value, str) else value
    return _varint(number << 3 | 2) + _varint(len(data)) + data


def _value_info(name: str, elem_type: int, dims) -> bytes:
    shape = b"".join(_field(1, _field(2, d) if isinstance(d, str) else _field(1, d)) for d in dims)
    return _field(1, name) + _field(2, _field(1, _field(1, elem_type) + _field(2, shape)))


def _onnx_model() -> bytes:
    float32, int32 = 1, 6
    mean = (_field(1, "tabular") + _field(2, "m") + _field(4, "ReduceMean")
            + _field(5, _field(1, "axes") + _field(8, 1) + _field(20, 7)))   # INTS [1]
    sigmoid = _field(1, "m") + _field(2, "rb") + _field(4, "Sigmoid")
    graph = (_field(1, mean) + _field(1, sigmoid) + _field(2, "tiny")
             + _field(11, _value_info("filter_id", int32, ["n"]))
             + _field(11, _value_info("tabular", float32, ["n", 9]))
             + _field(11, _value_info("survey_id", int32, ["n"]))
             + _field(11, _value_info("images", float32, ["n", 64, 64, 3]))
             + _field(12, _value_info("rb", float32, ["n", 1])))
    return _field(1, 8) + _field(7, graph) + _field(8, _field(1, "") + _field(2, 17))


def test_the_onnx_classifier_feeds_the_sessions_inputs_by_name_and_scores_with_its_output(tmp_path):
    pytest.importorskip("onnxruntime")
    model, scaler, threshold = tmp_path / "m.onnx", tmp_path / "scaler.json", tmp_path / "t.json"
    model.write_bytes(_onnx_model())
    from modules.rubrat.preprocessing.data_utils import EXPECTED_FEATURE_NAMES

    names = list(EXPECTED_FEATURE_NAMES)
    scaler.write_text(json.dumps({"feature_names": names, "mean": [0.0] * len(names),
                                  "std": [1.0] * len(names)}))
    threshold.write_text(json.dumps({"threshold": 0.5}))
    classifier = realbogus.load_classifier(model, scaler, threshold, 0, 2)
    assert classifier.image_size == 64

    frame = pd.DataFrame({column: np.full(5, 1.0) for column in realbogus.CATALOG_COLUMNS})
    frame["xcentroid"] = frame["ycentroid"] = np.array([40.0, 50.0, 60.0, 70.0, 500.0])
    frame["flux_fit"] = np.array([2.0, 5.0, 9.0, 18.0, 9.0])
    image = np.ones((128, 128), np.float32)
    scored = classifier.score_arrays(image, image, image, frame, filter_name="F146")
    features = classifier.scaler.transform(np.stack(
        [feature_row(row) for _, row in frame.iloc[:4].iterrows()]).astype(np.float32))
    expected = 1.0 / (1.0 + np.exp(-features.mean(axis=1)))
    np.testing.assert_allclose(scored["rb_score"].to_numpy()[:4], expected, rtol=1e-6)
    assert scored["rb_label"].tolist() == [int(v >= 0.5) for v in expected] + [-1]
    assert scored["rb_status"].tolist()[4] == "centroid_outside_image"


def test_the_job_s_vcpus_are_the_ecs_container_limit_in_units_of_1024_and_1_without_it(tmp_path):
    """The session's threads follow the container's ``Limits.CPU`` at the metadata endpoint,
    never the host's core count; without the endpoint the shipped ask, 1."""
    metadata = tmp_path / "container.json"
    metadata.write_text(json.dumps({"Limits": {"CPU": 5120, "Memory": 16384}}))
    assert realbogus.job_vcpus({"ECS_CONTAINER_METADATA_URI_V4": metadata.as_uri()}) == (5, "ecs")
    metadata.write_text(json.dumps({"Limits": {"CPU": 0}}))
    assert realbogus.job_vcpus({"ECS_CONTAINER_METADATA_URI_V4": metadata.as_uri()}) == (1, "default")
    assert realbogus.job_vcpus({}) == (1, "default")


def test_the_session_runs_on_the_job_s_vcpus_one_inter_op_thread_and_no_spinning():
    onnxruntime = pytest.importorskip("onnxruntime")
    options = realbogus.session_options(onnxruntime, 3)
    assert (options.intra_op_num_threads, options.inter_op_num_threads) == (3, 1)
    assert options.get_session_config_entry("session.intra_op.allow_spinning") == "0"
    assert options.execution_mode == onnxruntime.ExecutionMode.ORT_SEQUENTIAL
