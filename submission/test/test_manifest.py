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

from submission import payloads
from submission.manifest import (MAX_ARRAY_SIZE, OVERRIDE_REFERENCE_WINDOW,
                                 Manifest, ProcessingUnit,
                                 ReferenceObservationWindow)
from submission.routes import JOB_TYPE_REFERENCE_IMAGE, JOB_TYPE_SCIENCE


def unit(exposure, sca):
    return ProcessingUnit(
        payload=payloads.build(JOB_TYPE_SCIENCE, exposure=exposure, sca=sca))


def units(count, exposure=90210):
    return [unit(exposure, i + 1) for i in range(count)]


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
    duplicated = [unit(1, 5), unit(1, 5)]
    with pytest.raises(ValueError, match="duplicate"):
        Manifest(duplicated)


def test_same_sca_on_different_exposures_is_not_a_duplicate():
    Manifest([unit(1, 5), unit(2, 5)])


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


# JUDGEMENT CALL: `test_extra_fields_survive_the_round_trip` tested that the
# open `fields` dict's arbitrary extra keys survived a round trip. `.fields`
# is gone entirely (rule 11) — there is no longer a concept of an untyped
# "extra" key riding along a unit, only a payload's own declared, typed
# components. Rewritten to the closest surviving property: a unit's declared
# payload components survive the round trip.
def test_payload_components_survive_the_round_trip():
    first = unit(7, 3)
    second = unit(7, 4)
    restored = Manifest.from_json(Manifest([first, second]).to_json())
    assert restored.unit_for_index(0).payload == first.payload


# ---------------------------------------------------------------------------
# Integrity: an altered or foreign manifest is detected
# ---------------------------------------------------------------------------

def test_checksum_is_stable_across_equal_manifests():
    assert Manifest(units(5), batch_id="b").checksum() \
        == Manifest(units(5), batch_id="b").checksum()


def test_checksum_changes_when_a_unit_changes():
    before = Manifest(units(5), batch_id="b").checksum()
    altered = units(5)
    altered[2] = unit(90210, 99)
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


# ---------------------------------------------------------------------------
# The enumerated science overrides (O1)
#
# The manifest is the SOLE carrier of a per-run override of a
# science-affecting value, and the reference-image observation window is the
# sole enumerated field. What these pin is the properties the design leans
# on: the override survives a round trip, it is visible to a promotion gate,
# it changes the checksum (which is what "recorded by construction" rests
# on), and an unenumerated or half-specified one is refused rather than
# quietly dropped.
# ---------------------------------------------------------------------------

def test_no_override_is_the_default_and_writes_no_key():
    manifest = Manifest(units(3), batch_id="b")
    assert manifest.reference_observation_window is None
    assert manifest.has_science_override is False
    # Absent, not an empty mapping: two spellings of one absence would give
    # two different checksums for the same submission.
    assert "overrides" not in manifest.to_dict()


def test_the_window_override_round_trips():
    # BUG FOUND DURING MIGRATION: the manifest here is built with
    # job_type="reference-image", but `units(4)` builds SCIENCE-typed
    # payloads. `Manifest.__init__` dedups on `unit.dedup_key(self.job_type)`
    # (co-design ruling 2), which now raises `SubjectError` when a unit's
    # own payload job type disagrees with the manifest's — exactly the
    # mismatch this manifest would have had. The old exposure/SCA-sentinel
    # `ProcessingUnit` carried no job type of its own, so the mismatch was
    # invisible before this refactor; units are now built with the matching
    # reference-image payload instead.
    window = ReferenceObservationWindow(start_mjdobs=60000.0,
                                        end_mjdobs=60100.5)
    reference_image_units = [
        ProcessingUnit(payload=payloads.build(JOB_TYPE_REFERENCE_IMAGE,
                                              exposure=90210, sca=i + 1))
        for i in range(4)]
    manifest = Manifest(reference_image_units, batch_id="b",
                        job_type=JOB_TYPE_REFERENCE_IMAGE,
                        reference_observation_window=window)

    restored = Manifest.from_json(manifest.to_json())

    assert restored.reference_observation_window == window
    assert restored.has_science_override is True
    assert restored == manifest


def test_the_override_is_serialized_under_its_enumerated_name():
    window = ReferenceObservationWindow(start_mjdobs=1.0, end_mjdobs=2.0)
    raw = Manifest(units(2), batch_id="b",
                   reference_observation_window=window).to_dict()
    assert raw["overrides"] == {
        OVERRIDE_REFERENCE_WINDOW: {"start_mjdobs": 1.0, "end_mjdobs": 2.0}}


def test_an_override_changes_the_checksum():
    # The bar on promoting an override-bearing product binds "by
    # construction" because the manifest and its checksum are bound into the
    # attempt record. That only holds if the override is inside the checksum.
    plain = Manifest(units(5), batch_id="b").checksum()
    overridden = Manifest(
        units(5), batch_id="b",
        reference_observation_window=ReferenceObservationWindow(
            start_mjdobs=60000.0, end_mjdobs=60100.0)).checksum()
    assert overridden != plain


def test_a_different_window_is_a_different_checksum():
    def checksum(end):
        return Manifest(
            units(5), batch_id="b",
            reference_observation_window=ReferenceObservationWindow(
                start_mjdobs=60000.0, end_mjdobs=end)).checksum()

    assert checksum(60100.0) != checksum(60200.0)


def test_an_unenumerated_override_is_refused_not_dropped():
    # Dropping it would run the job WITHOUT an override its author asked
    # for, and produce a promotable-looking product from a barred run.
    raw = Manifest(units(3), batch_id="b").to_dict()
    raw["overrides"] = {"sca_gain": 1.5}
    with pytest.raises(ValueError, match="unknown override fields"):
        Manifest.from_dict(raw)


def test_a_half_specified_window_is_refused():
    raw = Manifest(units(3), batch_id="b").to_dict()
    raw["overrides"] = {OVERRIDE_REFERENCE_WINDOW: {"start_mjdobs": 60000.0}}
    with pytest.raises(ValueError, match="end_mjdobs"):
        Manifest.from_dict(raw)


def test_an_empty_window_is_refused():
    # The window is half-open, so end must exceed start; an inverted or
    # equal pair selects no frames at all and would build a reference image
    # from nothing rather than saying so.
    with pytest.raises(ValueError, match="empty"):
        ReferenceObservationWindow(start_mjdobs=60100.0, end_mjdobs=60000.0)
    with pytest.raises(ValueError, match="empty"):
        ReferenceObservationWindow(start_mjdobs=60000.0, end_mjdobs=60000.0)


def test_a_non_numeric_window_bound_is_refused():
    with pytest.raises(ValueError, match="MJD"):
        ReferenceObservationWindow(start_mjdobs="60000", end_mjdobs=60100.0)


def test_a_version_2_manifest_is_refused():
    # A version-2 manifest predates the override vocabulary. Reading one as
    # version 3 would claim it carried no override when the concept did not
    # exist for it to carry.
    raw = Manifest(units(3), batch_id="b").to_dict()
    raw["schema_version"] = 2
    with pytest.raises(ValueError, match="schema_version"):
        Manifest.from_dict(raw)


# JUDGEMENT CALL: no prior test pinned "a version-3 manifest is refused",
# but `Manifest.SCHEMA_VERSION` moving from 3 to 4 (D3) is itself a rule-11
# behaviour change the docstring calls out by name — "there is deliberately
# NO compatibility parser rebuilding a typed subject from a sentinel
# exposure/SCA" — so it is added here rather than left unpinned. A
# version-3 unit is exactly the sentinel shape rule 11 forbids:
# `{"exposure": ..., "sca": ..., "fields": {...}}`, with no `payload` key
# at all.
def test_a_version_3_manifest_is_refused_not_translated():
    raw = Manifest(units(3), batch_id="b").to_dict()
    raw["schema_version"] = 3
    with pytest.raises(ValueError, match="schema_version"):
        Manifest.from_dict(raw)


def test_a_version_3_unit_shape_is_refused_even_at_the_current_version():
    # Even if a caller mislabels a legacy unit as schema_version 4, the
    # per-unit reconstruction refuses it: there is no `payload` key to read,
    # only the old sentinel `exposure`/`sca`/`fields` triple, and
    # `ProcessingUnit.from_dict` raises rather than guessing a typed subject
    # out of it.
    raw = Manifest(units(3), batch_id="b").to_dict()
    raw["units"][0] = {"exposure": 90210, "sca": 1, "fields": {}}
    with pytest.raises(ValueError, match="payload"):
        Manifest.from_dict(raw)
