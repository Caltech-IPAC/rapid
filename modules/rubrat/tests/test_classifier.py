"""Tests of the real/bogus classifier as the pipeline uses it (inference.classifier.score_arrays).

The feature and cutout tests run anywhere.  The tests that load the trained model need the
checkpoint, scaler, and threshold files, which are not in the repository; point
RUBRAT_MODEL_DIR at a directory holding best.keras, canonical_9.json, and threshold.json
(a copy of s3://rapid-pipeline-files/rubrat/<version>/).  Without it those tests skip,
and test_model_files_available warns once so the skip is visible.

Run from the repository root:  python -m pytest modules/rubrat/tests -q
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from modules.rubrat.preprocessing.data_utils import EXPECTED_FEATURE_NAMES, FeatureZScore
from modules.rubrat.preprocessing.npz_builders import arcsinh_stack_cutout, feature_row
from modules.rubrat.preprocessing.rubr_adapter import build_feats_dict, extract_cutout

MODEL_FILES = {"checkpoint": "best.keras", "feats_scaler": "canonical_9.json", "threshold_json": "threshold.json"}


def model_dir_unavailable_reason() -> str | None:
    model_dir = os.environ.get("RUBRAT_MODEL_DIR")
    if not model_dir:
        return "RUBRAT_MODEL_DIR not set"
    missing = [name for name in MODEL_FILES.values() if not (Path(model_dir) / name).is_file()]
    if missing:
        return f"missing in RUBRAT_MODEL_DIR: {missing}"
    return None


# ---------------------------------------------------------------------------------------------
# Preprocessing, which must match training: these run without the model.

def test_feature_order_and_fit_flag_masking():
    row = {"flux_fit": 3.0, "flux_err": 1.0, "cfit": 0.2, "reduced_chi2": 1.5, "x_err": 0.3, "y_err": 0.4,
           "npixfit": 289, "flags": 0, "sharpness": 0.6, "roundness1": 0.1, "roundness2": -0.1}
    feats = build_feats_dict(row)
    assert list(feats) == EXPECTED_FEATURE_NAMES
    assert feats["arcsinh_snr"] == pytest.approx(np.arcsinh(3.0 / 3.0))
    assert feats["log1p_pos_err"] == pytest.approx(np.log1p(0.5))
    assert feats["log_npixfit"] == pytest.approx(np.log(289))
    assert feats["is_fit_clean"] == 1.0
    # A flagged fit zeroes the four fit-dependent features but keeps the finder morphology.
    flagged = build_feats_dict({**row, "flags": 32})
    assert flagged["is_fit_clean"] == 0.0
    assert all(flagged[k] == 0.0 for k in ("arcsinh_snr", "cfit", "log1p_chi2", "log1p_pos_err"))
    assert flagged["sharpness"] == 0.6 and flagged["log_npixfit"] == pytest.approx(np.log(289))


def test_feature_row_accepts_catalog_aliases():
    row = pd.Series({"flux_fit": 2.0, "flux_err": 0.5, "cfit": 0.0, "reduced_chi2": 1.0, "x_err": 0.1, "y_err": 0.1,
                     "n_pixels_fit": 100, "flags": 0, "sharpness": 0.5, "roundness1": 0.0, "roundness2": 0.0})
    assert feature_row(row)[EXPECTED_FEATURE_NAMES.index("log_npixfit")] == pytest.approx(np.log(100))


def test_cutout_center_padding_and_nan_handling():
    image = np.arange(100 * 100, dtype=np.float32).reshape(100, 100)
    cut = extract_cutout(image, x=50.4, y=20.6, size=64)          # rounds to (50, 21), window [-32, +31]
    assert cut.shape == (64, 64)
    assert cut[32, 32] == image[21, 50]
    edge = extract_cutout(image, x=5.0, y=5.0, size=64)           # overhangs the edge: zero padded, not None
    assert edge.shape == (64, 64) and edge[0, 0] == 0.0 and edge[32, 32] == image[5, 5]
    assert extract_cutout(image, x=-200.0, y=50.0, size=64) is None
    bad = image.copy(); bad[21, 50] = np.nan
    assert arcsinh_stack_cutout(bad, image, image, 50.4, 20.6, image_size=64) is None
    stack = arcsinh_stack_cutout(image, image, image, 50.4, 20.6, image_size=64)
    assert stack.shape == (64, 64, 3) and stack[32, 32, 0] == pytest.approx(np.arcsinh(image[21, 50] / 0.01))


def test_cutout_nan_zero_fill_and_fraction():
    """Live scoring zero-fills NaN pixels (zero_nans=True) and reports the filled fraction;
    the training rule (default) still drops the stack."""
    from modules.rubrat.preprocessing.npz_builders import arcsinh_stack_cutout_with_nan_fraction
    image = np.ones((100, 100), dtype=np.float32)
    bad = image.copy(); bad[10:18, 40:56] = np.nan              # 8 x 16 = 128 NaN pixels inside the 64 x 64 window
    stack, frac = arcsinh_stack_cutout_with_nan_fraction(image, bad, image, 50.0, 20.0, image_size=64, zero_nans=True)
    assert stack is not None and frac == pytest.approx(128 / 4096)
    # The window spans image rows -12..51 and columns 18..81, so the NaN block sits at cutout
    # rows 22..29 and columns 22..37: zero-filled in the reference channel, untouched in the others.
    assert np.isfinite(stack).all()
    assert stack[22, 22, 1] == 0.0 and stack[22, 22, 0] == pytest.approx(np.arcsinh(100.0))
    assert stack[22, 40, 1] == pytest.approx(np.arcsinh(100.0))
    dropped, frac2 = arcsinh_stack_cutout_with_nan_fraction(image, bad, image, 50.0, 20.0, image_size=64)
    assert dropped is None and frac2 == pytest.approx(128 / 4096)
    off, frac3 = arcsinh_stack_cutout_with_nan_fraction(image, image, image, -200.0, 20.0, image_size=64, zero_nans=True)
    assert off is None and np.isnan(frac3)


def test_feature_scaler_keeps_clean_flag_and_masks_fit_features(tmp_path):
    scaler_path = tmp_path / "scaler.json"
    scaler_path.write_text(
        '{"feature_names": %s, "mean": %s, "std": %s}'
        % (str(EXPECTED_FEATURE_NAMES).replace("'", '"'), [1.0] * 9, [2.0] * 9), encoding="utf-8")
    scaler = FeatureZScore(scaler_path)
    clean = np.array([[3.0, 3.0, 1.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0]], dtype=np.float32)
    out = scaler.transform(clean)[0]
    assert out[2] == 1.0 and np.allclose(out[[0, 1, 3, 4, 5, 6, 7, 8]], 1.0)
    flagged = clean.copy(); flagged[0, 2] = 0.0
    out = scaler.transform(flagged)[0]
    assert out[2] == 0.0 and np.all(out[[0, 1, 3, 4]] == 0.0) and np.allclose(out[[5, 6, 7, 8]], 1.0)


# ---------------------------------------------------------------------------------------------
# The trained model.  Skipped, with one visible warning, when the model files are not available.

def test_model_files_available():
    """Canary: warns when the model-dependent tests below cannot run, so the skip is attributed here."""
    reason = model_dir_unavailable_reason()
    if reason:
        warnings.warn(f"model tests NOT run: {reason}")
        pytest.skip(reason)


@pytest.fixture(scope="module")
def classifier():
    reason = model_dir_unavailable_reason()
    if reason:
        pytest.skip(reason)
    from modules.rubrat.inference.classifier import RealBogusClassifier, load_validation_threshold

    model_dir = Path(os.environ["RUBRAT_MODEL_DIR"])
    return RealBogusClassifier(model_dir / MODEL_FILES["checkpoint"], model_dir / MODEL_FILES["feats_scaler"],
                               threshold=load_validation_threshold(model_dir / MODEL_FILES["threshold_json"]),
                               survey_id=0, batch_size=64)


def _synthetic_chip(n: int = 24, seed: int = 3):
    rng = np.random.default_rng(seed)
    height = width = 200
    yy, xx = np.mgrid[:height, :width]
    sci = rng.normal(0, 0.02, (height, width)).astype(np.float32)
    ref = rng.normal(0, 0.02, (height, width)).astype(np.float32)
    diff = rng.normal(0, 0.03, (height, width)).astype(np.float32)
    xs = rng.uniform(40, width - 40, n); ys = rng.uniform(40, height - 40, n)
    for x0, y0 in zip(xs[: n // 2], ys[: n // 2]):          # first half: point sources in the difference image
        diff += (2.0 * np.exp(-((xx - x0) ** 2 + (yy - y0) ** 2) / (2 * 1.2 ** 2))).astype(np.float32)
    det = pd.DataFrame({"xcentroid": xs, "ycentroid": ys, "flux_fit": rng.uniform(0.5, 5, n),
                        "flux_err": rng.uniform(0.05, 0.3, n), "cfit": rng.normal(0, 0.1, n),
                        "reduced_chi2": rng.uniform(0.5, 2, n), "x_err": rng.uniform(0.01, 0.3, n),
                        "y_err": rng.uniform(0.01, 0.3, n), "npixfit": np.full(n, 289), "flags": np.zeros(n, int),
                        "sharpness": rng.uniform(0.3, 0.9, n), "roundness1": rng.uniform(-0.5, 0.5, n),
                        "roundness2": rng.uniform(-0.5, 0.5, n)})
    return sci, ref, diff, det


def test_score_arrays_bookkeeping(classifier):
    sci, ref, diff, det = _synthetic_chip()
    det.loc[2, "xcentroid"] = -500.0                          # off the image
    det.loc[5, ["flux_fit", "cfit"]] = np.nan                 # non-finite features -> neutral defaults, still scored
    out = classifier.score_arrays(sci, ref, diff, det, filter_name="H158")
    assert len(out) == len(det) and list(out.index) == list(range(len(det)))
    assert out.rb_status.iloc[2] == "centroid_outside_image" and not out.rb_valid.iloc[2] and np.isnan(out.rb_score.iloc[2])
    assert out.rb_label.iloc[2] == -1
    assert out.rb_valid.iloc[5] and np.isfinite(out.rb_score.iloc[5])
    valid = out.rb_valid.values
    assert np.all((out.rb_score.values[valid] >= 0) & (out.rb_score.values[valid] <= 1))
    assert np.array_equal(out.rb_label.values[valid], (out.rb_score.values[valid] >= classifier.threshold).astype(int))
    assert (out.rb_threshold == classifier.threshold).all()


def test_score_arrays_is_deterministic_and_filter_aliases_agree(classifier):
    sci, ref, diff, det = _synthetic_chip()
    a = classifier.score_arrays(sci, ref, diff, det, filter_name="H158")
    b = classifier.score_arrays(sci, ref, diff, det, filter_name="F158")   # Roman name for the same filter
    assert np.allclose(a.rb_score.values, b.rb_score.values, atol=1e-6)
    assert a.rb_model_filter_id.iloc[0] == b.rb_model_filter_id.iloc[0]


def test_score_arrays_scores_stamps_with_nan_pixels(classifier):
    """A masked (NaN) region inside a stamp no longer makes the source unscorable; the filled
    fraction is reported in rb_nan_fraction (0 for clean stamps, NaN for off-image ones)."""
    sci, ref, diff, det = _synthetic_chip(n=6)
    x, y = int(round(det.xcentroid.iat[0])), int(round(det.ycentroid.iat[0]))
    diff[y - 2:y + 2, x - 2:x + 2] = np.nan                   # 4 x 4 NaN block at the first source
    det.loc[1, "xcentroid"] = -500.0                          # off the image
    out = classifier.score_arrays(sci, ref, diff, det, filter_name="H158")
    assert out.rb_valid.iloc[0] and np.isfinite(out.rb_score.iloc[0])
    assert out.rb_nan_fraction.iloc[0] == pytest.approx(16 / 4096)
    assert not out.rb_valid.iloc[1] and np.isnan(out.rb_nan_fraction.iloc[1])
    # Every other source is scored, and its fraction is the NaN share of its own 64 x 64 window
    # (nonzero where that window also overlaps the block; edge padding counts as clean).
    nan_mask = np.isnan(diff).astype(np.float32)
    for i in range(2, len(det)):
        expected = extract_cutout(nan_mask, det.xcentroid.iat[i], det.ycentroid.iat[i], size=64).mean()
        assert out.rb_valid.iloc[i] and out.rb_nan_fraction.iloc[i] == pytest.approx(expected)


def test_unknown_filter_is_rejected(classifier):
    sci, ref, diff, det = _synthetic_chip(n=4)
    with pytest.raises(ValueError, match="filter registry"):
        classifier.score_arrays(sci, ref, diff, det, filter_name="NOTAFILTER")
