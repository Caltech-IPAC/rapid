from __future__ import annotations

import csv
import json
import sys
from types import ModuleType

import numpy as np
import pytest

from rubrat.inference import infer_npz


def _install_fake_predictor(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("classification.evaluation")

    def predict_model(checkpoint, paths, scaler, **kwargs):
        del checkpoint, paths, scaler, kwargs
        data = {
            "metadata": np.asarray(
                [
                    {"row_id": "jid1:4", "jid": "jid1", "row_index": 4},
                    {"row_id": "jid2:9", "jid": "jid2", "row_index": 9},
                ],
                dtype=object,
            )
        }
        return data, {"rb": np.asarray([[0.25], [0.75]], dtype=np.float32)}

    module.predict_model = predict_model
    monkeypatch.setitem(sys.modules, "classification.evaluation", module)


def test_infer_npz_writes_scores_labels_and_provenance(tmp_path, monkeypatch):
    _install_fake_predictor(monkeypatch)
    checkpoint = tmp_path / "best.keras"
    scaler = tmp_path / "scaler.json"
    shard = tmp_path / "test_0.npz"
    output = tmp_path / "results" / "predictions.csv"
    checkpoint.write_bytes(b"checkpoint")
    scaler.write_text("{}", encoding="utf-8")
    np.savez(shard, y=np.asarray([0, 1]))

    result = infer_npz(checkpoint, [shard], scaler, output, threshold=0.5)

    with output.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == [
        {"row_id": "jid1:4", "jid": "jid1", "row_index": "4", "rb_score": "0.25", "rb_label": "0"},
        {"row_id": "jid2:9", "jid": "jid2", "row_index": "9", "rb_score": "0.75", "rb_label": "1"},
    ]
    provenance_path = output.with_suffix(".provenance.json")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    assert provenance["n_rows"] == 2
    assert provenance["threshold"] == 0.5
    assert result["provenance"] == str(provenance_path.resolve())


def test_infer_npz_protects_existing_output(tmp_path):
    checkpoint = tmp_path / "best.keras"
    scaler = tmp_path / "scaler.json"
    shard = tmp_path / "test_0.npz"
    output = tmp_path / "predictions.csv"
    checkpoint.write_bytes(b"checkpoint")
    scaler.write_text("{}", encoding="utf-8")
    np.savez(shard, y=np.asarray([0]))
    output.write_text("existing\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="--overwrite"):
        infer_npz(checkpoint, [shard], scaler, output)

    assert output.read_text(encoding="utf-8") == "existing\n"
