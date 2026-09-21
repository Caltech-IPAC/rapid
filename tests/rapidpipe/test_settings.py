"""Tests for rapidpipe.stages.settings: merge rules and hash stability."""

from __future__ import annotations

import pytest

from rapidpipe.stages.settings import (
    SettingsError,
    canonical_hash,
    load_toml,
    merge_settings,
    resolve_settings,
)


def test_scalar_overlay_replaces_default():
    merged = merge_settings({"a": 1}, {"a": 2})
    assert merged == {"a": 2}


def test_array_overlay_replaces_rather_than_concatenates():
    merged = merge_settings({"a": [1, 2, 3]}, {"a": [9]})
    assert merged == {"a": [9]}


def test_nested_tables_merge_recursively():
    defaults = {"a": 1, "b": {"c": 2, "d": 3}}
    overlay = {"b": {"c": 99}}
    merged = merge_settings(defaults, overlay)
    assert merged == {"a": 1, "b": {"c": 99, "d": 3}}


def test_deeply_nested_tables_merge_recursively():
    defaults = {"a": {"b": {"c": 1, "d": 2}}}
    overlay = {"a": {"b": {"c": 99}}}
    merged = merge_settings(defaults, overlay)
    assert merged == {"a": {"b": {"c": 99, "d": 2}}}


def test_empty_overlay_returns_defaults_unchanged():
    defaults = {"a": 1, "b": {"c": 2}}
    merged = merge_settings(defaults, {})
    assert merged == defaults


def test_unknown_key_at_top_level_raises():
    with pytest.raises(SettingsError):
        merge_settings({"a": 1}, {"z": 2})


def test_unknown_key_in_nested_table_raises():
    with pytest.raises(SettingsError):
        merge_settings({"a": {"b": 1}}, {"a": {"z": 2}})


def test_shape_mismatch_table_to_scalar_raises():
    with pytest.raises(SettingsError):
        merge_settings({"a": {"b": 1}}, {"a": 5})


def test_shape_mismatch_scalar_to_table_raises():
    with pytest.raises(SettingsError):
        merge_settings({"a": 1}, {"a": {"b": 5}})


def test_defaults_not_mutated_by_merge():
    defaults = {"a": {"b": 1}}
    merge_settings(defaults, {"a": {"b": 2}})
    assert defaults == {"a": {"b": 1}}


def test_hash_is_stable_across_key_order():
    assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})


def test_hash_is_stable_across_nested_key_order():
    left = {"a": {"x": 1, "y": 2}, "b": 3}
    right = {"b": 3, "a": {"y": 2, "x": 1}}
    assert canonical_hash(left) == canonical_hash(right)


def test_hash_differs_for_different_content():
    assert canonical_hash({"a": 1}) != canonical_hash({"a": 2})


def test_load_toml_reads_a_file(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text("a = 1\n[b]\nc = 2\n")
    assert load_toml(path) == {"a": 1, "b": {"c": 2}}


def test_resolve_settings_with_no_schema_and_no_overlay():
    assert resolve_settings(None, None) == {}


def test_resolve_settings_with_schema_and_no_overlay(tmp_path):
    schema = tmp_path / "defaults.toml"
    schema.write_text("a = 1\n")
    assert resolve_settings(schema, None) == {"a": 1}


def test_resolve_settings_with_schema_and_overlay(tmp_path):
    schema = tmp_path / "defaults.toml"
    schema.write_text("a = 1\nb = 2\n")
    overlay = tmp_path / "overlay.toml"
    overlay.write_text("b = 99\n")
    assert resolve_settings(schema, overlay) == {"a": 1, "b": 99}


def test_resolve_settings_overlay_with_unknown_key_raises(tmp_path):
    schema = tmp_path / "defaults.toml"
    schema.write_text("a = 1\n")
    overlay = tmp_path / "overlay.toml"
    overlay.write_text("z = 99\n")
    with pytest.raises(SettingsError):
        resolve_settings(schema, overlay)
