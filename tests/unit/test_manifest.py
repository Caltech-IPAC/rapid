"""Tests for rapidpipe.products.manifest: round-trip and validation failures.

The products-page round-trip test below copies the "A complete manifest"
JSON from https://roman-rapid.readthedocs.io/en/latest/system/products.html
verbatim, except the four SHA-256 digests: the page's example digests are
each one hex character too long (65 hex digits, not 64) -- illustrative
placeholder text, not real digest output -- so this fixture trims each to
the last 64 hex characters. Every other character, key and structural
choice is copied as published.
"""

from __future__ import annotations

import json

import pytest

from rapidpipe.products.manifest import (
    Inputs,
    Manifest,
    ManifestError,
    Member,
    OutputEntry,
    Unit,
    hash_file,
    member_for_file,
)

# The products page's "A complete manifest" example, verbatim except the
# four SHA-256 digests trimmed from 65 to 64 hex characters (see module
# docstring).
PRODUCTS_PAGE_EXAMPLE = """
{
  "schema_version": "1",
  "run": "r-2026-09-21-0007",
  "unit": {"kind": "detector-image", "id": "e20260821001234/SCA07"},
  "stage": "difference",
  "attempt": "a-01J8Y6QZ3M",
  "execution_record": "exec/a-01J8Y6QZ3M.json",
  "inputs": {
    "manifest": "s3://<inputs-prefix>/manifest.json",
    "products": {
      "l2-image": "pi-l2-0000123456",
      "reference-image": "pi-ref-0000004321",
      "psf": "pi-psf-0000000789"
    },
    "result_sets": []
  },
  "outputs": [
    {
      "kind": "difference-image",
      "format_version": "1",
      "instance": "pi-diff-0000998877",
      "key": {
        "l2": "pi-l2-0000123456",
        "reference": "pi-ref-0000004321",
        "differencer": "zogy",
        "settings_hash": "sha256:4f2a9c0e1b7d6a5f3e2c1d0b9a8f7e6d5c4b3a291807f6e5d4c3b2a1f0e9d8c7"
      },
      "primary": "diff/e20260821001234_SCA07_zogy.fits",
      "members": [
        {"role": "difference",   "path": "diff/e20260821001234_SCA07_zogy.fits",       "bytes": 201326592, "sha256": "sha256:a1f0e2d3c4b5a69788796a5b4c3d2e1f00112233445566778899aabbccddeeff"},
        {"role": "uncertainty",  "path": "diff/e20260821001234_SCA07_zogy_unc.fits",   "bytes": 201326592, "sha256": "sha256:b2e1f3d4c5a6b7988a7b6c5d4e3f2019900aabbccddeeff00112233445566778"},
        {"role": "significance", "path": "diff/e20260821001234_SCA07_zogy_scorr.fits", "bytes": 201326592, "sha256": "sha256:c3f2e4d5b6a7c8a99b8c7d6e5f4a3b20aa11bbccddeeff001122334455667789"}
      ],
      "registration": {
        "centre": {"ra": 269.4521, "dec": -28.7710},
        "corners": [[269.39, -28.83], [269.51, -28.83], [269.51, -28.71], [269.39, -28.71]],
        "infobits_science": 0,
        "infobits_reference": 0,
        "source_counts": {"sextractor": {"positive": 412, "negative": 388}, "photutils": {"positive": 405, "negative": 391}},
        "registration_residual": {"x_rms": 0.031, "y_rms": 0.029, "x_median": 0.004, "y_median": -0.002},
        "reference_scale_factor": 0.998
      }
    },
    {
      "kind": "source-catalog",
      "format_version": "1",
      "instance": "pi-cat-0000998878",
      "key": {"difference": "pi-diff-0000998877", "catalog_type": "sextractor", "sign": "positive"},
      "primary": "cat/e20260821001234_SCA07_zogy_pos.sexcat",
      "members": [{"role": "catalog", "path": "cat/e20260821001234_SCA07_zogy_pos.sexcat", "bytes": 88214, "sha256": "sha256:d4a3f5e6c7b8d9aaac9d8e7f6a5b4c31bb22ccddeeff0011223344556677889a"}],
      "registration": {"source_count": 412}
    }
  ]
}
"""


def test_products_page_example_round_trips():
    example = json.loads(PRODUCTS_PAGE_EXAMPLE)
    manifest = Manifest.from_dict(example)
    manifest.validate()
    restored = manifest.to_dict()
    assert restored == example


def test_products_page_example_result_set_output_has_no_members_or_primary():
    example = json.loads(PRODUCTS_PAGE_EXAMPLE)
    manifest = Manifest.from_dict(example)
    for output in manifest.outputs:
        assert output.members
        assert output.primary is not None


def _manifest(**overrides):
    defaults = dict(
        run="r1",
        unit=Unit(kind="detector-image", id="u1"),
        stage="difference",
        attempt="a1",
        execution_record="exec/a1.json",
        inputs=Inputs(manifest="s3://bucket/inputs/manifest.json",
                      products={"l2-image": "pi-l2-1"}, result_sets=()),
        outputs=(
            OutputEntry(
                kind="difference-image",
                format_version="1",
                instance="pi-diff-1",
                key={"l2": "pi-l2-1"},
                primary="diff/x.fits",
                members=(Member("difference", "diff/x.fits", 10, "sha256:" + "a" * 64),),
            ),
        ),
    )
    defaults.update(overrides)
    return Manifest(**defaults)


def test_round_trip_dict():
    manifest = _manifest()
    manifest.validate()
    restored = Manifest.from_dict(manifest.to_dict())
    assert restored == manifest


def test_round_trip_json_file(tmp_path):
    manifest = _manifest()
    path = tmp_path / "manifest.json"
    manifest.write(path)
    restored = Manifest.read(path)
    assert restored == manifest


def test_write_creates_valid_json_with_schema_version(tmp_path):
    manifest = _manifest()
    path = tmp_path / "manifest.json"
    manifest.write(path)
    text = path.read_text()
    assert '"schema_version": "1"' in text


def test_write_is_atomic_no_temp_file_left_behind(tmp_path):
    manifest = _manifest()
    path = tmp_path / "manifest.json"
    manifest.write(path)
    leftovers = [p for p in tmp_path.iterdir() if p != path]
    assert leftovers == []


@pytest.mark.parametrize("field_name", [
    "run", "stage", "attempt", "execution_record",
])
def test_missing_required_field_fails_validation(field_name):
    manifest = _manifest(**{field_name: ""})
    with pytest.raises(ManifestError):
        manifest.validate()


def test_missing_unit_id_fails_validation():
    manifest = _manifest(unit=Unit(kind="detector-image", id=""))
    with pytest.raises(ManifestError):
        manifest.validate()


def test_unknown_unit_kind_fails_validation():
    with pytest.raises(ManifestError):
        Unit(kind="not-a-real-unit", id="u1").validate()


@pytest.mark.parametrize(
    "kind", ["exposure", "detector-image", "field", "processing-date", "detector-date"])
def test_each_contract_unit_kind_is_valid(kind):
    Unit(kind=kind, id="u1").validate()  # does not raise


def test_missing_inputs_manifest_fails_validation():
    manifest = _manifest(inputs=Inputs(manifest="", products={}, result_sets=()))
    with pytest.raises(ManifestError):
        manifest.validate()


def test_empty_inputs_products_value_fails_validation():
    manifest = _manifest(inputs=Inputs(
        manifest="s3://bucket/inputs/manifest.json",
        products={"l2-image": ""}, result_sets=()))
    with pytest.raises(ManifestError):
        manifest.validate()


def test_unsupported_schema_version_fails_validation():
    manifest = _manifest(schema_version="999")
    with pytest.raises(ManifestError):
        manifest.validate()


def test_duplicate_output_instance_fails_validation():
    entry = OutputEntry(
        kind="k", format_version="1", instance="dup", key={"a": "b"},
        primary="p", members=(Member("role", "p", 1, "sha256:" + "a" * 64),))
    manifest = _manifest(outputs=(entry, entry))
    with pytest.raises(ManifestError):
        manifest.validate()


def test_output_missing_kind_fails():
    entry = OutputEntry(kind="", format_version="1", instance="i1", key={"a": "b"})
    with pytest.raises(ManifestError):
        entry.validate()


def test_output_missing_format_version_fails():
    entry = OutputEntry(kind="k", format_version="", instance="i1", key={"a": "b"})
    with pytest.raises(ManifestError):
        entry.validate()


def test_output_missing_instance_fails():
    entry = OutputEntry(kind="k", format_version="1", instance="", key={"a": "b"})
    with pytest.raises(ManifestError):
        entry.validate()


def test_output_empty_key_fails():
    entry = OutputEntry(kind="k", format_version="1", instance="i1", key={})
    with pytest.raises(ManifestError):
        entry.validate()


def test_result_set_output_has_no_members_and_no_primary():
    entry = OutputEntry(
        kind="association-set", format_version="1", instance="pi-assoc-1",
        key={"field": "f1"})
    entry.validate()  # does not raise
    assert entry.is_result_set()


def test_result_set_output_with_primary_fails():
    entry = OutputEntry(
        kind="association-set", format_version="1", instance="pi-assoc-1",
        key={"field": "f1"}, primary="not-allowed")
    with pytest.raises(ManifestError):
        entry.validate()


def test_file_output_without_primary_fails():
    entry = OutputEntry(
        kind="difference-image", format_version="1", instance="pi-diff-1",
        key={"a": "b"},
        members=(Member("difference", "diff/x.fits", 1, "sha256:" + "a" * 64),))
    with pytest.raises(ManifestError):
        entry.validate()


def test_primary_not_a_member_path_fails():
    entry = OutputEntry(
        kind="difference-image", format_version="1", instance="pi-diff-1",
        key={"a": "b"}, primary="not/a/member/path",
        members=(Member("difference", "diff/x.fits", 1, "sha256:" + "a" * 64),))
    with pytest.raises(ManifestError):
        entry.validate()


def test_duplicate_member_paths_fail():
    entry = OutputEntry(
        kind="difference-image", format_version="1", instance="pi-diff-1",
        key={"a": "b"}, primary="diff/x.fits",
        members=(
            Member("difference", "diff/x.fits", 1, "sha256:" + "a" * 64),
            Member("uncertainty", "diff/x.fits", 2, "sha256:" + "b" * 64),
        ))
    with pytest.raises(ManifestError):
        entry.validate()


@pytest.mark.parametrize("path", ["/abs/path", "../escape.fits", "diff/../../escape.fits"])
def test_member_path_must_be_relative_and_contained(path):
    member = Member("role", path, 1, "sha256:" + "a" * 64)
    with pytest.raises(ManifestError):
        member.validate()


def test_member_relative_path_with_internal_dotdot_that_stays_contained_still_rejected():
    # ".." anywhere in the path is rejected outright, even if the resolved
    # path would stay under the base -- the rule is syntactic, not resolved.
    member = Member("role", "a/../b.fits", 1, "sha256:" + "a" * 64)
    with pytest.raises(ManifestError):
        member.validate()


@pytest.mark.parametrize("sha", [
    "not-even-hex",
    "sha256:" + "a" * 63,   # too short
    "sha256:" + "a" * 65,   # too long
    "sha256:" + "A" * 64,   # uppercase not allowed
    "a" * 64,               # missing prefix
])
def test_member_sha256_must_match_pattern(sha):
    member = Member("role", "diff/x.fits", 1, sha)
    with pytest.raises(ManifestError):
        member.validate()


def test_member_missing_bytes_or_negative_fails():
    with pytest.raises(ManifestError):
        Member("role", "diff/x.fits", -1, "sha256:" + "a" * 64).validate()


def test_from_dict_rejects_unknown_top_level_field():
    d = _manifest().to_dict()
    d["bogus"] = "surprise"
    with pytest.raises(ManifestError):
        Manifest.from_dict(d)


def test_from_dict_rejects_unknown_output_field():
    d = _manifest().to_dict()
    d["outputs"][0]["bogus"] = "surprise"
    with pytest.raises(ManifestError):
        Manifest.from_dict(d)


def test_from_dict_rejects_unknown_member_field():
    d = _manifest().to_dict()
    d["outputs"][0]["members"][0]["bogus"] = "surprise"
    with pytest.raises(ManifestError):
        Manifest.from_dict(d)


def test_from_dict_rejects_unknown_unit_field():
    d = _manifest().to_dict()
    d["unit"]["bogus"] = "surprise"
    with pytest.raises(ManifestError):
        Manifest.from_dict(d)


def test_from_dict_rejects_unknown_inputs_field():
    d = _manifest().to_dict()
    d["inputs"]["bogus"] = "surprise"
    with pytest.raises(ManifestError):
        Manifest.from_dict(d)


def test_read_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        Manifest.read(tmp_path / "does-not-exist.json")


def test_read_invalid_json_raises_manifest_error(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text("{not json")
    with pytest.raises(ManifestError):
        Manifest.read(path)


def test_hash_file_matches_known_digest(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"hello world")
    size, sha256 = hash_file(path)
    assert size == 11
    assert sha256 == (
        "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    )


def test_member_for_file_computes_hash_size_and_relative_path(tmp_path):
    output_dir = tmp_path / "outputs"
    (output_dir / "diff").mkdir(parents=True)
    file_path = output_dir / "diff" / "f.bin"
    file_path.write_bytes(b"payload")

    member = member_for_file("difference", file_path, relative_to=output_dir)
    size, sha256_hex = hash_file(file_path)

    assert member.role == "difference"
    assert member.path == "diff/f.bin"
    assert member.bytes == size
    assert member.sha256 == f"sha256:{sha256_hex}"
    member.validate()  # does not raise
