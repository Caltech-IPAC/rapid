"""Tests for the manifest's W4 vocabulary: job type, route, unit facts.

The existing test_manifest.py covers sizing, binding, and integrity — the
properties the manifest had before W4. This file covers what W4 added:
the job type that fixes the route, and the per-invocation fact set that
used to ride a per-job .ini.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from submission import payloads  # noqa: E402
from submission.manifest import Manifest, ProcessingUnit, UnitFacts  # noqa: E402
from submission.routes import (  # noqa: E402
    CLASS_BULK, CLASS_PROMPT, JOB_TYPE_CROSSMATCH, JOB_TYPE_REFERENCE_IMAGE,
    JOB_TYPE_SCIENCE, LANE_SESSION, LANE_TRANSACTION, RouteError,
)


def units(count=2, exposure=90210, job_type=JOB_TYPE_SCIENCE):
    return [ProcessingUnit(payload=payloads.build(job_type, exposure=exposure,
                                                  sca=n + 1))
           for n in range(count)]


def science_facts(**overrides):
    base = dict(
        rid=4242, fid=3, filter_name="F184", field=511, rtid=511,
        expid=90210, mjdobs=60553.25, exptime=140.25, infobits=0, status=1,
        science_image_uri="s3://sims/l2/f184/exp90210_sca1.fits",
        psfid=77, psf_uri="s3://sims/psf/f184_sca1.fits",
        reference_image_id=1201,
        reference_image_uri="s3://products/ref/511_f184.fits",
        reference_image_infobits=0, reference_image_ppid=12,
        images_to_coadd=-1,
        sky_position={"ra0": 10.5, "dec0": -20.25},
        tile_position={"ra0": 10.4, "dec0": -20.2},
        overlapping_fields=[511, 512, 513],
    )
    base.update(overrides)
    return UnitFacts(**base)


# --- job type and route ----------------------------------------------

def test_a_manifest_names_its_job_type():
    manifest = Manifest(units(job_type=JOB_TYPE_REFERENCE_IMAGE), batch_id="b1",
                        job_type="reference-image")
    assert manifest.job_type == "reference-image"


def test_the_job_type_fixes_the_route():
    manifest = Manifest(units(job_type=JOB_TYPE_REFERENCE_IMAGE),
                        job_type="reference-image")
    assert manifest.workload_class == CLASS_BULK
    assert manifest.db_lane == LANE_TRANSACTION
    assert manifest.ppid == 12


def test_a_session_lane_job_type_carries_the_session_lane():
    # Crossmatch units are DATE_FIELD-grain (co-design ruling 2): their
    # declared subject is `(job_type, proc_date, field)`, not exposure/SCA,
    # so a manifest of them needs those fields named to have a real dedup
    # identity — a bare `units()` here would have every unit collide on
    # the same missing-field subject.
    crossmatch_units = [
        ProcessingUnit(payload=payloads.build(
            JOB_TYPE_CROSSMATCH, proc_date="20260808", field=field,
            target_tables=("xmatch_20260808",)))
        for field in (101, 202)
    ]
    manifest = Manifest(crossmatch_units, job_type="crossmatch")
    assert manifest.db_lane == LANE_SESSION
    assert manifest.ppid is None


def test_an_unknown_job_type_is_refused_at_construction():
    # Before it can reach S3 in a written manifest — the entrypoint's
    # startup check is the second line of defence, not the first.
    with pytest.raises(RouteError, match="not a known job type"):
        Manifest(units(), job_type="telescope-repair")


def test_the_job_type_survives_a_round_trip():
    # JUDGEMENT CALL: was job_type="registration". Registration has no
    # payload type (`payloads.PAYLOAD_TYPES`), and a unit's payload now
    # dictates the only job type it can belong to (`ProcessingUnit.dedup_key`
    # has "no fallback" for a job type outside the typed-identity registry —
    # "a job type without a payload cannot construct a unit at all"). Swapped
    # to reference-image, a payload-bearing job type distinct from science,
    # to keep exercising the same round-trip-of-job-type property.
    original = Manifest(units(job_type=JOB_TYPE_REFERENCE_IMAGE), batch_id="b1",
                        job_type="reference-image")
    restored = Manifest.from_json(original.to_json())
    assert restored.job_type == "reference-image"
    assert restored == original


def test_a_manifest_without_a_job_type_is_refused():
    # Defaulting would route unknown work onto the prompt queue.
    raw = Manifest(units(), batch_id="b1").to_dict()
    del raw["job_type"]
    with pytest.raises(ValueError, match="does not name a job_type"):
        Manifest.from_dict(raw)


def test_schema_version_1_manifests_are_refused():
    raw = Manifest(units(), batch_id="b1").to_dict()
    raw["schema_version"] = 1
    with pytest.raises(ValueError, match="schema_version"):
        Manifest.from_dict(raw)


def test_two_manifests_differing_only_in_job_type_are_not_equal():
    # JUDGEMENT CALL: was job_type="reprocessing" on the right. Reprocessing
    # has no payload type, so its units cannot be constructed at all (see
    # the round-trip test above) — swapped to reference-image, still a
    # payload-bearing job type distinct from "science" on the left.
    left = Manifest(units(job_type=JOB_TYPE_SCIENCE), batch_id="b1",
                    job_type="science")
    right = Manifest(units(job_type=JOB_TYPE_REFERENCE_IMAGE), batch_id="b1",
                     job_type="reference-image")
    assert left != right


def test_the_job_type_is_in_the_checksum():
    # The checksum is what a starting job proves its manifest by; a
    # rerouted manifest must not check out as the same one.
    # JUDGEMENT CALL: was job_type="reprocessing" on the right — see above.
    left = Manifest(units(job_type=JOB_TYPE_SCIENCE), batch_id="b1",
                    job_type="science")
    right = Manifest(units(job_type=JOB_TYPE_REFERENCE_IMAGE), batch_id="b1",
                     job_type="reference-image")
    assert left.checksum() != right.checksum()


# --- the startup validation the entrypoint calls ----------------------

QUEUE_NAMES = {
    "batch/queue-prompt": "rapid-queue-prompt",
    "batch/queue-bulk": "rapid-queue-bulk",
}


def test_validate_for_accepts_the_matching_class_and_queue():
    manifest = Manifest(units(), job_type="science")
    route = manifest.validate_for(CLASS_PROMPT,
                                  queue_name="rapid-queue-prompt",
                                  queue_names=QUEUE_NAMES)
    assert route.job_type == "science"


def test_validate_for_rejects_an_incompatible_class():
    manifest = Manifest(units(), job_type="science")
    with pytest.raises(RouteError, match="runs on the prompt class"):
        manifest.validate_for(CLASS_BULK)


def test_validate_for_rejects_the_wrong_queue():
    manifest = Manifest(units(), job_type="science")
    with pytest.raises(RouteError, match="submitted to rapid-queue-bulk"):
        manifest.validate_for(CLASS_PROMPT, queue_name="rapid-queue-bulk",
                              queue_names=QUEUE_NAMES)


# --- key zero-padding (catalog co-design, storage.md § Key schema) ----

def test_key_zero_pads_exposure_to_six_digits_and_sca_to_two():
    unit = ProcessingUnit(payload=payloads.build(JOB_TYPE_SCIENCE, exposure=1,
                                                 sca=2))
    assert unit.key == "000001/02"


def test_key_does_not_truncate_a_component_that_already_fills_its_width():
    unit = ProcessingUnit(payload=payloads.build(JOB_TYPE_SCIENCE,
                                                 exposure=123456, sca=42))
    assert unit.key == "123456/42"


def test_key_padding_keeps_two_different_units_distinct():
    assert (ProcessingUnit(payload=payloads.build(
                JOB_TYPE_SCIENCE, exposure=1, sca=2)).key
           != ProcessingUnit(payload=payloads.build(
                JOB_TYPE_SCIENCE, exposure=12, sca=2)).key)


# --- per-invocation facts ---------------------------------------------

def test_facts_default_to_empty_and_serialize_away():
    unit = ProcessingUnit(payload=payloads.build(JOB_TYPE_SCIENCE, exposure=1,
                                                 sca=2))
    assert unit.to_dict() == {"payload": {"grain": "exposure_sca",
                                          "exposure": 1, "sca": 2}}


def test_facts_round_trip_with_their_types():
    unit = ProcessingUnit(payload=payloads.build(JOB_TYPE_SCIENCE,
                                                 exposure=90210, sca=1),
                          facts=science_facts())
    restored = ProcessingUnit.from_dict(unit.to_dict(), JOB_TYPE_SCIENCE)
    assert restored == unit
    assert restored.facts.mjdobs == 60553.25
    assert restored.facts.overlapping_fields == [511, 512, 513]
    assert restored.facts.sky_position == {"ra0": 10.5, "dec0": -20.25}


def test_absent_facts_are_omitted_not_written_as_null():
    # The adopted absent-not-sentinel rule: never-resolved and
    # resolved-to-nothing are different states.
    facts = UnitFacts(rid=1)
    written = facts.to_dict()
    assert written == {"rid": 1}
    assert "psfid" not in written


def test_unknown_fact_keys_are_refused_rather_than_dropped():
    # A key this schema does not know means a newer submitter wrote the
    # manifest; guessing at the rest would be worse than refusing.
    with pytest.raises(ValueError, match="unknown keys"):
        UnitFacts.from_dict({"rid": 1, "quantum_flux": 9})


def test_require_names_every_missing_fact_at_once():
    facts = UnitFacts(rid=1)
    with pytest.raises(ValueError) as caught:
        facts.require("rid", "psfid", "science_image_uri")
    message = str(caught.value)
    assert "psfid" in message
    assert "science_image_uri" in message
    assert "rid" not in message.split(":")[1]


def test_require_passes_when_every_fact_is_present():
    science_facts().require("rid", "psfid", "science_image_uri", "field")


def test_a_reference_image_id_of_none_means_one_must_be_built():
    # The launcher's rfid=None branch: no existing reference image.
    facts = science_facts(reference_image_id=None, reference_image_uri=None,
                          images_to_coadd=14,
                          coadd_inputs_uri="s3://files/inputs_jid7.csv")
    assert facts.reference_image_id is None
    assert facts.images_to_coadd == 14
    assert "reference_image_id" not in facts.to_dict()


# --- manifest-wide fact requirements ----------------------------------

def test_require_facts_passes_when_every_unit_carries_them():
    manifest = Manifest(
        [ProcessingUnit(payload=payloads.build(JOB_TYPE_SCIENCE,
                                               exposure=90210, sca=n + 1),
                        facts=science_facts())
         for n in range(3)],
        job_type="science")
    manifest.require_facts("rid", "science_image_uri")


def test_require_facts_names_the_offending_indices():
    manifest = Manifest([
        ProcessingUnit(payload=payloads.build(JOB_TYPE_SCIENCE,
                                              exposure=90210, sca=1),
                       facts=science_facts()),
        ProcessingUnit(payload=payloads.build(JOB_TYPE_SCIENCE,
                                              exposure=90210, sca=2),
                       facts=UnitFacts(rid=2)),
        ProcessingUnit(payload=payloads.build(JOB_TYPE_SCIENCE,
                                              exposure=90210, sca=3),
                       facts=UnitFacts(rid=3)),
    ], job_type="science")
    with pytest.raises(ValueError) as caught:
        manifest.require_facts("science_image_uri")
    message = str(caught.value)
    assert "2 of 3 units" in message
    assert "index 1 (090210/02)" in message
    assert "index 2 (090210/03)" in message
    assert "index 0" not in message


def test_require_facts_truncates_a_long_list_but_states_the_total():
    manifest = Manifest(
        [ProcessingUnit(payload=payloads.build(JOB_TYPE_SCIENCE,
                                               exposure=90210, sca=n + 1))
         for n in range(15)],
        job_type="science")
    with pytest.raises(ValueError) as caught:
        manifest.require_facts("rid")
    message = str(caught.value)
    assert "15 of 15 units" in message
    assert message.rstrip().endswith("...")


def test_facts_survive_a_full_manifest_round_trip():
    manifest = Manifest(
        [ProcessingUnit(payload=payloads.build(JOB_TYPE_SCIENCE,
                                               exposure=90210, sca=n + 1),
                        facts=science_facts(rid=n))
         for n in range(3)],
        batch_id="b1", job_type="science")
    restored = Manifest.from_json(manifest.to_json())
    assert restored == manifest
    assert restored.unit_for_index(2).facts.rid == 2
    assert restored.checksum() == manifest.checksum()
