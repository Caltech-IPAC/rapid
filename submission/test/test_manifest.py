"""Manifest tests: the index -> SCA binding and its invariants.

The properties under test are the ones the retry contract depends on: an
array index resolves to the same SCA on every attempt, the manifest sizes
the array, and a manifest that has been altered or truncated is detected
rather than silently trusted.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from submission.manifest import (MAX_ARRAY_SIZE, Manifest, ProcessingUnit)


def units(count, exposure=90210):
    return [ProcessingUnit(exposure=exposure, sca=i + 1) for i in range(count)]


# ---------------------------------------------------------------------------
# Sizing: the manifest is what sizes the array
# ---------------------------------------------------------------------------

def test_array_size_is_the_unit_count():
    assert Manifest(units(18)).array_size == 18


def test_single_unit_batch_is_not_an_array():
    # Batch rejects arraySize 1, so a one-unit batch submits as a plain job.
    assert Manifest(units(1)).is_array is False
    assert Manifest(units(2)).is_array is True


def test_empty_manifest_is_rejected():
    with pytest.raises(ValueError, match="at least one"):
        Manifest([])


def test_manifest_over_the_array_ceiling_is_rejected():
    with pytest.raises(ValueError, match="ceiling"):
        Manifest(units(MAX_ARRAY_SIZE + 1))


def test_manifest_at_exactly_the_ceiling_is_allowed():
    assert Manifest(units(MAX_ARRAY_SIZE)).array_size == MAX_ARRAY_SIZE


def test_duplicate_units_are_rejected():
    # Two children on one SCA would write the same products under two
    # attempt identities.
    duplicated = [ProcessingUnit(exposure=1, sca=5),
                  ProcessingUnit(exposure=1, sca=5)]
    with pytest.raises(ValueError, match="duplicate"):
        Manifest(duplicated)


def test_same_sca_on_different_exposures_is_not_a_duplicate():
    Manifest([ProcessingUnit(exposure=1, sca=5),
              ProcessingUnit(exposure=2, sca=5)])


# ---------------------------------------------------------------------------
# Binding: index -> unit, stable and bounded
# ---------------------------------------------------------------------------

def test_index_resolves_to_its_unit():
    manifest = Manifest(units(18))
    assert manifest.unit_for_index(0).sca == 1
    assert manifest.unit_for_index(17).sca == 18


def test_index_binding_survives_a_serialization_round_trip():
    # This is the retry contract: a retried child re-reads the manifest
    # and must resolve the same SCA it did on the first attempt.
    original = Manifest(units(18), batch_id="b-1")
    restored = Manifest.from_json(original.to_json())
    for index in range(18):
        assert restored.unit_for_index(index) == original.unit_for_index(index)


def test_index_outside_the_manifest_raises():
    manifest = Manifest(units(4))
    with pytest.raises(IndexError, match="outside manifest"):
        manifest.unit_for_index(4)
    with pytest.raises(IndexError):
        manifest.unit_for_index(-1)


def test_extra_fields_survive_the_round_trip():
    unit = ProcessingUnit(exposure=7, sca=3,
                          fields={"field": 42, "filter": "F158"})
    restored = Manifest.from_json(Manifest([unit, ProcessingUnit(7, 4)]).to_json())
    assert restored.unit_for_index(0).fields == {"field": 42, "filter": "F158"}


# ---------------------------------------------------------------------------
# Integrity: an altered or foreign manifest is detected
# ---------------------------------------------------------------------------

def test_checksum_is_stable_across_equal_manifests():
    assert Manifest(units(5), batch_id="b").checksum() \
        == Manifest(units(5), batch_id="b").checksum()


def test_checksum_changes_when_a_unit_changes():
    before = Manifest(units(5), batch_id="b").checksum()
    altered = units(5)
    altered[2] = ProcessingUnit(exposure=90210, sca=99)
    assert Manifest(altered, batch_id="b").checksum() != before


def test_unknown_schema_version_is_rejected():
    raw = Manifest(units(3)).to_dict()
    raw["schema_version"] = 99
    with pytest.raises(ValueError, match="schema_version"):
        Manifest.from_dict(raw)


def test_recorded_size_inconsistent_with_the_units_is_rejected():
    # Catches a truncated manifest: the array was sized from a count the
    # unit list no longer supports.
    raw = Manifest(units(5)).to_dict()
    raw["units"] = raw["units"][:3]
    with pytest.raises(ValueError, match="records array_size"):
        Manifest.from_dict(raw)


def test_serialized_form_is_byte_stable():
    # The manifest is checksummed across the submit/startup boundary, so
    # its serialization cannot depend on dict ordering.
    manifest = Manifest(units(6), batch_id="b-9")
    assert manifest.to_json() == Manifest.from_json(manifest.to_json()).to_json()
