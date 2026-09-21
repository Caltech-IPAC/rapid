"""Tests for rapidpipe.products.manifest: round-trip and validation failures."""

from __future__ import annotations

import pytest

from rapidpipe.products.manifest import (
    CompletionManifest,
    ManifestError,
    OutputEntry,
    hash_file,
    output_entry_for_file,
)


def _manifest(**overrides):
    defaults = dict(
        run_id="r1",
        unit_id="u1",
        stage="admit",
        attempt_id="a1",
        execution_record_ref="exec:1",
        input_manifest_ref="in:1",
        outputs=(OutputEntry("out1", "exposure", "1", "s3://bucket/x", 10, "a" * 64),),
    )
    defaults.update(overrides)
    return CompletionManifest(**defaults)


def test_round_trip_dict():
    manifest = _manifest()
    manifest.validate()
    restored = CompletionManifest.from_dict(manifest.to_dict())
    assert restored == manifest


def test_round_trip_json_file(tmp_path):
    manifest = _manifest()
    path = tmp_path / "manifest.json"
    manifest.write(path)
    restored = CompletionManifest.read(path)
    assert restored == manifest


def test_write_creates_valid_json_with_schema_version(tmp_path):
    manifest = _manifest()
    path = tmp_path / "manifest.json"
    manifest.write(path)
    text = path.read_text()
    assert '"schema_version": 1' in text


@pytest.mark.parametrize("field_name", [
    "run_id", "unit_id", "stage", "attempt_id",
    "execution_record_ref", "input_manifest_ref",
])
def test_missing_required_field_fails_validation(field_name):
    manifest = _manifest(**{field_name: ""})
    with pytest.raises(ManifestError):
        manifest.validate()


def test_unsupported_schema_version_fails_validation():
    manifest = _manifest(schema_version=999)
    with pytest.raises(ManifestError):
        manifest.validate()


def test_duplicate_output_identity_fails_validation():
    manifest = _manifest(outputs=(
        OutputEntry("dup", "kind", "1", "loc1"),
        OutputEntry("dup", "kind", "1", "loc2"),
    ))
    with pytest.raises(ManifestError):
        manifest.validate()


def test_output_with_byte_size_but_no_sha256_fails():
    entry = OutputEntry("id1", "kind", "1", "loc", byte_size=5, sha256=None)
    with pytest.raises(ManifestError):
        entry.validate()


def test_output_with_sha256_but_no_byte_size_fails():
    entry = OutputEntry("id1", "kind", "1", "loc", byte_size=None, sha256="a" * 64)
    with pytest.raises(ManifestError):
        entry.validate()


def test_output_with_neither_size_nor_hash_is_a_valid_db_result_set():
    entry = OutputEntry("id1", "association-set", "1", "run-scoped:r1:crossmatch")
    entry.validate()  # does not raise
    assert not entry.is_file()


def test_output_missing_required_field_fails():
    with pytest.raises(ManifestError):
        OutputEntry("", "kind", "1", "loc").validate()


def test_from_dict_rejects_unknown_top_level_field():
    d = _manifest().to_dict()
    d["bogus"] = "surprise"
    with pytest.raises(ManifestError):
        CompletionManifest.from_dict(d)


def test_from_dict_rejects_unknown_output_field():
    d = _manifest().to_dict()
    d["outputs"][0]["bogus"] = "surprise"
    with pytest.raises(ManifestError):
        CompletionManifest.from_dict(d)


def test_read_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        CompletionManifest.read(tmp_path / "does-not-exist.json")


def test_read_invalid_json_raises_manifest_error(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text("{not json")
    with pytest.raises(ManifestError):
        CompletionManifest.read(path)


def test_hash_file_matches_known_digest(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"hello world")
    size, sha256 = hash_file(path)
    assert size == 11
    assert sha256 == (
        "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    )


def test_output_entry_for_file_computes_hash_and_size(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"payload")
    entry = output_entry_for_file("id1", "kind", "1", path)
    size, sha256 = hash_file(path)
    assert entry.byte_size == size
    assert entry.sha256 == sha256
    assert entry.is_file()
