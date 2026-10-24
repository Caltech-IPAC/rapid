"""Tests for rapidpipe.foundation.layers: the three layers a run's body is resolved from."""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

import pytest

from rapidpipe.foundation.layers import LayerError, body_hash, parse_set, resolve

SETTINGS = Path(__file__).resolve().parents[2] / "rapidpipe" / "settings"
SHIPPED = SETTINGS / "foundation-thin.toml"
HLWAS = SETTINGS / "datasets" / "hlwas-r00342.toml"


def _toml(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return path


@pytest.fixture()
def base(tmp_path):
    return _toml(tmp_path, "base.toml", "[a]\nx = 1\ny = [1, 2]\n[a.b]\nz = 'dev'\n"
                                        "[intake]\nunsigned = false\n"
                                        "[reference.selection]\nmin_frames = 2\n")


def test_layers_apply_low_to_high_and_tables_merge_key_by_key(tmp_path, base):
    overlay = _toml(tmp_path, "ds.toml", "[a]\nx = 2\n[a.b]\nz = 'ds'\n")
    body, _ = resolve(base, overlay, ["a.x=3", "a.y=[9]"])
    assert body["a"] == {"x": 3, "y": [9], "b": {"z": "ds"}}
    assert body["intake"] == {"unsigned": False}


def test_a_later_set_replaces_an_earlier_one(base):
    body, _ = resolve(base, None, ["a.x=5", "a.x=6"])
    assert body["a"]["x"] == 6


@pytest.mark.parametrize("text, value", [
    ("a.x=61591.4056", 61591.4056), ("a.x=7", 7), ("a.x=true", True),
    ('a.x="s3://bucket/prefix/"', "s3://bucket/prefix/"), ("a.x=[1, 2]", [1, 2]),
])
def test_set_values_are_toml_typed(text, value):
    assert parse_set(text) == (("a", "x"), value)
    assert type(parse_set(text)[1]) is type(value)


@pytest.mark.parametrize("text", ["a.x=s3://unquoted", "ax=1", "a.x", ".x=1"])
def test_a_malformed_set_is_refused(text):
    with pytest.raises(LayerError, match="--set"):
        parse_set(text)


def test_an_overlay_key_the_base_lacks_is_refused_by_name(tmp_path, base):
    overlay = _toml(tmp_path, "ds.toml", "[intake]\nunsinged = true\n")
    with pytest.raises(LayerError, match="'intake.unsinged'"):
        resolve(base, overlay)


def test_a_set_key_the_base_lacks_is_refused_by_name(base):
    with pytest.raises(LayerError, match="'a.b.w'"):
        resolve(base, None, ["a.b.w=1"])


def test_a_value_may_not_replace_a_table(base):
    with pytest.raises(LayerError, match="changes shape"):
        resolve(base, None, ["a.b=1"])


def test_the_documented_optional_keys_may_be_added(base):
    body, _ = resolve(base, None, ['intake.source_root="s3://b/l2/"',
                                   "reference.selection.before_mjd=61591.4056"])
    assert body["intake"]["source_root"] == "s3://b/l2/"
    assert body["reference"]["selection"] == {"min_frames": 2, "before_mjd": 61591.4056}


def test_the_record_names_and_hashes_each_layer_given(tmp_path, base):
    overlay = _toml(tmp_path, "ds.toml", "[a]\nx = 2\n")
    sets = ["a.x=3", "intake.unsigned=true"]
    body, record = resolve(base, overlay, sets)
    assert record == {
        "settings_hash": body_hash(body),
        "layers": [
            {"layer": "base", "name": "base.toml",
             "sha256": hashlib.sha256(base.read_bytes()).hexdigest()},
            {"layer": "overlay", "name": "ds.toml",
             "sha256": hashlib.sha256(overlay.read_bytes()).hexdigest()},
            {"layer": "set", "sets": sets,
             "sha256": hashlib.sha256("\n".join(sets).encode()).hexdigest()},
        ]}
    assert [layer["layer"] for layer in resolve(base)[1]["layers"]] == ["base"]


def test_the_body_hash_ignores_key_order():
    assert body_hash({"a": 1, "b": {"c": 2, "d": 3}}) == body_hash({"b": {"d": 3, "c": 2}, "a": 1})


def test_a_json_base_is_a_stored_body_taken_whole(tmp_path):
    full = tmp_path / "full.json"
    full.write_text(json.dumps({"a": {"x": 1}, "intake": {"source_root": "s3://b/"}}))
    body, record = resolve(full)
    assert body == {"a": {"x": 1}, "intake": {"source_root": "s3://b/"}}
    assert record["layers"][0]["name"] == "full.json"


def test_the_hlwas_overlay_resolves_to_the_hand_built_body():
    """Shipped defaults + the HLWAS overlay + the per-run --sets equals the
    full body earlier runs were created from (base, the two intake keys and
    the reference bound, e.g. run 17's), hash included."""
    root, bound = "s3://bucket/r00342/l2/", 61591.4056
    hand_built = tomllib.loads(SHIPPED.read_text())
    hand_built["intake"].update(source_root=root, unsigned=True)
    hand_built["reference"]["selection"]["before_mjd"] = bound
    body, record = resolve(SHIPPED, HLWAS, [f'intake.source_root="{root}"',
                                            f"reference.selection.before_mjd={bound}"])
    assert body == hand_built
    assert record["settings_hash"] == body_hash(hand_built)


def test_the_hlwas_overlay_holds_only_keys_that_differ_from_the_base():
    base, overlay = tomllib.loads(SHIPPED.read_text()), tomllib.loads(HLWAS.read_text())

    def differing(upper, lower):
        for key, value in upper.items():
            if isinstance(value, dict):
                differing(value, lower[key])
            else:
                assert lower[key] != value, f"{key} repeats the base's value"

    differing(overlay, base)
