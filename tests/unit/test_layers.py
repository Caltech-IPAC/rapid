"""Tests for rapidpipe.foundation.layers: the three layers a run's body is resolved from."""

from __future__ import annotations

import hashlib
import json
import re
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
    the reference bound, e.g. run 17's) with the DQ mask, the WCS
    translation, the HLWAS program id and the cutover's scope cut (no sources records, ZOGY or naive
    subtraction) that followed them, hash included; dev's OpenUniverse SFFT
    block, once the overlay's, is the base's since sfft-block-base."""
    root, bound = "s3://bucket/r00342/l2/", 61591.4056
    hand_built = tomllib.loads(SHIPPED.read_text())
    hand_built["intake"].update(source_root=root, unsigned=True, dq_mask_bits=34671617,
                                crpix_shift=[0.12, -0.99], program_id=-4)
    hand_built["difference"]["skip_loading_sources_records"] = True
    hand_built["difference"]["zogy"]["run_zogy"] = False
    hand_built["difference"]["naive_diffimage"]["naive_diffimage_flag"] = False
    hand_built["reference"]["selection"]["before_mjd"] = bound
    body, record = resolve(SHIPPED, HLWAS, [f'intake.source_root="{root}"',
                                            f"reference.selection.before_mjd={bound}"])
    assert body == hand_built
    assert record["settings_hash"] == body_hash(hand_built)


@pytest.mark.parametrize("path", sorted((SETTINGS / "datasets").glob("*.toml")), ids=lambda p: p.name)
def test_every_dataset_overlay_resolves_and_holds_only_keys_that_differ_from_the_base(path):
    resolve(SHIPPED, path)  # a key the base lacks is refused
    base, overlay = tomllib.loads(SHIPPED.read_text()), tomllib.loads(path.read_text())

    def differing(upper, lower):
        for key, value in upper.items():
            if isinstance(value, dict):
                differing(value, lower[key])
            else:
                assert lower[key] != value, f"{key} repeats the base's value"

    differing(overlay, base)


FLIGHT = SETTINGS / "datasets" / "flight-1049.toml"
FLIGHT_DIFFERENCE = {("sci_image", "repair_extreme_artifact_pixels"): False,
                     ("sfft", "crossconv_flag"): False,
                     ("gainmatch", "magrefthresmin"): 10.0,
                     ("gainmatch", "magrefthresmax"): 30.0,
                     ("gainmatch", "refmagkeep"): 30.0,
                     ("psfcat_diffimage", "sharphi"): 1.8}


def test_the_flight_overlay_carries_commissionings_difference_values():
    """Flight 1049's resolved body holds the flight difference values and crossconv_flag false."""
    body, _ = resolve(SHIPPED, FLIGHT)
    for (table, key), value in FLIGHT_DIFFERENCE.items():
        assert body["difference"][table][key] == value, f"{table}.{key}"


@pytest.mark.parametrize("path", sorted(p for p in (SETTINGS / "datasets").glob("*.toml") if p != FLIGHT),
                         ids=lambda p: p.name)
def test_the_base_and_the_sim_overlays_keep_devs_difference_values(path):
    """A sim overlay resolves the flight keys to its own value where it sets one, else the base's."""
    base = tomllib.loads(SHIPPED.read_text())["difference"]
    own = tomllib.loads(path.read_text()).get("difference", {})
    body, _ = resolve(SHIPPED, path)
    for table, key in FLIGHT_DIFFERENCE:
        expected = own.get(table, {}).get(key, base[table][key])
        assert body["difference"][table][key] == expected, f"{table}.{key}"


def test_the_retired_diff_flavor_cannot_select_zogy(tmp_path):
    """Only SFFT's difference image is registered: no run or overlay names a differencer."""
    assert "diff_flavor" not in resolve(SHIPPED)[0]["difference"]
    with pytest.raises(LayerError, match="'difference.diff_flavor'"):
        resolve(SHIPPED, HLWAS, ['difference.diff_flavor="zogy"'])
    overlay = _toml(tmp_path, "ds.toml", '[difference]\ndiff_flavor = "zogy"\n')
    with pytest.raises(LayerError, match="diff_flavor"):
        resolve(SHIPPED, overlay)

def test_flight_masks_saturated_the_soc_simulations_do_not_and_the_lite_readers_mask_nothing():
    """SATURATED is masked on flight's real cores and left on the SOC simulations, which flag every bright
    star's core; a reader without a usable DQ plane refuses a nonzero mask."""
    from roman_datamodels.dqflags import pixel

    for path in sorted((SETTINGS / "datasets").glob("*.toml")):
        intake = resolve(SHIPPED, path)[0]["intake"]
        saturated = bool(intake["dq_mask_bits"] & int(pixel.SATURATED))
        assert saturated == (path == FLIGHT), path.name
        if intake["reader"] in ("socsims-lite", "rimtimsim"):
            assert intake["dq_mask_bits"] == 0, path.name
    for path in (HLWAS, SETTINGS / "datasets" / "gbtds-r00340.toml"):
        assert resolve(SHIPPED, path)[0]["intake"]["dq_mask_bits"], path.name


@pytest.mark.parametrize("path", [SHIPPED, *sorted((SETTINGS / "datasets").glob("*.toml"))], ids=lambda p: p.name)
def test_every_nonzero_dq_mask_is_decimal_under_a_comment_that_sums_its_named_flags(path):
    from roman_datamodels.dqflags import pixel

    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        match = re.fullmatch(r"dq_mask_bits = (\S+)", line)
        if not match or match.group(1) == "0":
            continue
        assert match.group(1).isdigit(), f"{path.name}: {line} is not decimal"
        terms = re.findall(r"([A-Z_]+) (\d+)", lines[i - 1])
        assert lines[i - 1] == "# " + " + ".join(f"{name} {value}" for name, value in terms), lines[i - 1]
        assert all(int(value) == int(pixel[name]) for name, value in terms), lines[i - 1]
        assert sum(int(value) for _, value in terms) == int(match.group(1))


def test_flight_scales_reference_inputs_by_the_on_orbit_nominal_zptmag_and_the_base_does_not():
    """Commissioning 687c93b2's table and 8a4a172d's flag: on for flight-1049, off in the base (the sims)."""
    from rapidpipe.products.filters import ROMAN_TO_RAPID_FILTER_NAMES

    base, _ = resolve(SHIPPED)
    flight, _ = resolve(SHIPPED, SETTINGS / "datasets" / "flight-1049.toml")
    assert base["reference"]["awaicgen"]["use_nominal_zptmag"] is False
    assert flight["reference"]["awaicgen"]["use_nominal_zptmag"] is True
    assert flight["intake"]["nominal_zptmag"] == {
        "F062": 26.04, "F087": 25.73, "F106": 25.78, "F129": 25.77,
        "F146": 27.01, "F158": 25.80, "F184": 25.34, "F213": 25.29}
    for body in (base, flight):
        assert set(body["intake"]["nominal_zptmag"]) == set(ROMAN_TO_RAPID_FILTER_NAMES)
