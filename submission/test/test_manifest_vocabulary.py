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
from submission.manifest import Manifest, ProcessingUnit  # noqa: E402
from submission import payloads  # noqa: E402
from submission.test import payload_fixtures as fixtures  # noqa: E402
from submission.routes import (  # noqa: E402
    CLASS_BULK, CLASS_PROMPT, JOB_TYPE_CROSSMATCH, JOB_TYPE_REFERENCE_IMAGE,
    JOB_TYPE_SCIENCE, LANE_SESSION, LANE_TRANSACTION, RouteError,
)


def units(count=2, exposure=90210, job_type=JOB_TYPE_SCIENCE):
    # A unit's payload must carry every required fact its job type declares
    # (D4) — `payloads.build` with only `exposure`/`sca` can no longer
    # construct a science or reference-image unit, so this helper goes
    # through the fixtures, which resolve the rest. `field_payload`-style
    # bare `payloads.build` remains fine for the other job types, which have
    # no required facts beyond their grain components.
    builder = (fixtures.reference_payload if job_type == JOB_TYPE_REFERENCE_IMAGE
              else fixtures.science_payload)
    return [ProcessingUnit(payload=builder(exposure=exposure, sca=n + 1))
           for n in range(count)]


def science_facts(**overrides):
    """A fully-resolved science payload — what `science_facts` gathers.

    `status` and `images_to_coadd` are gone from this list because D4 dropped
    them: neither was read by any consumer, and one was never even written.
    The rest moved onto `ImagingPayload` unchanged.
    """
    base = dict(
        rid=4242, fid=3, filter_name="F184", field=511, rtid=511,
        expid=90210, mjdobs=60553.25, exptime=140.25, infobits=0,
        science_image_uri="s3://sims/l2/f184/exp90210_sca1.fits",
        psfid=77, psf_uri="s3://sims/psf/f184_sca1.fits",
        reference_image_id=1201,
        reference_image_uri="s3://products/ref/511_f184.fits",
        reference_image_infobits=0, reference_image_ppid=12,
        sky_position={"ra0": 10.5, "dec0": -20.25},
        tile_position={"ra0": 10.4, "dec0": -20.2},
        overlapping_fields=(511, 512, 513),
    )
    base.update(overrides)
    # `exposure`/`sca` are subject COMPONENTS rather than facts, so they are
    # split out and passed positionally — `science_payload` takes them as
    # its first two arguments and the rest as fact overrides.
    exposure = base.pop("exposure", 90210)
    sca = base.pop("sca", 1)
    return fixtures.science_payload(exposure=exposure, sca=sca, **base)


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
    unit = ProcessingUnit(payload=fixtures.science_payload(exposure=1, sca=2))
    assert unit.key == "000001/02"


def test_key_does_not_truncate_a_component_that_already_fills_its_width():
    unit = ProcessingUnit(payload=fixtures.science_payload(exposure=123456,
                                                            sca=42))
    assert unit.key == "123456/42"


def test_key_padding_keeps_two_different_units_distinct():
    assert (ProcessingUnit(payload=fixtures.science_payload(
                exposure=1, sca=2)).key
           != ProcessingUnit(payload=fixtures.science_payload(
                exposure=12, sca=2)).key)


# --- per-invocation facts ---------------------------------------------

def test_facts_default_to_empty_and_serialize_away():
    # JUDGEMENT CALL: the original built a bare science payload with only
    # exposure/sca and asserted NO facts serialize at all. D4 made the
    # eleven imaging facts required at construction, so that bare payload
    # can no longer be built — there is no science unit with zero facts to
    # retarget onto. What "absent facts serialize away" still means for a
    # fully-resolved payload is that a fact nobody resolved (here `psfid`/
    # `psf_uri`, both genuinely optional per `ImagingPayload`'s docstring)
    # is omitted from the wire form rather than written as null, while every
    # required fact IS present. That is the same absent-not-sentinel rule
    # the original test exercised, now demonstrated on the one class of
    # fact that can still be absent.
    unit = ProcessingUnit(payload=fixtures.science_payload(exposure=1, sca=2))
    written = unit.to_dict()["payload"]
    assert "psfid" not in written and "psf_uri" not in written, (
        "the fixture resolves no PSF, so no psfid/psf_uri key may appear")
    assert written["exposure"] == 1
    assert written["sca"] == 2


def test_facts_round_trip_with_their_types():
    unit = ProcessingUnit(payload=science_facts())
    restored = ProcessingUnit.from_dict(unit.to_dict(), JOB_TYPE_SCIENCE)
    assert restored == unit
    assert restored.facts.mjdobs == 60553.25
    # `_freeze` normalizes sequence members to tuples on construction, but
    # `overlapping_fields` round-trips through `unit.to_dict()` -> JSON-
    # shaped dict -> `from_dict` in this test WITHOUT an actual JSON
    # encode/decode pass (no `json.dumps`/`json.loads` in between), so it
    # is still the tuple `payloads.build` normalized it to, not a list.
    assert restored.facts.overlapping_fields == (511, 512, 513)
    assert restored.facts.sky_position == {"ra0": 10.5, "dec0": -20.25}


def test_absent_facts_are_omitted_not_written_as_null():
    # The adopted absent-not-sentinel rule: never-resolved and
    # resolved-to-nothing are different states. Unchanged by D4 — the rule
    # moved onto the payloads with the members it governs. What DID change
    # is that the required facts can no longer be absent at all, so the
    # omission is demonstrated on an optional one.
    written = fixtures.science_payload().to_dict()
    assert "psfid" not in written, (
        "the fixture resolves no PSF, so no psfid key may appear")
    assert written["rid"] == 101


def test_unknown_fact_keys_are_refused_rather_than_dropped():
    # A key this schema does not know means a newer submitter wrote the
    # manifest; guessing at the rest would be worse than refusing.
    with pytest.raises(payloads.PayloadError, match="unknown keys"):
        payloads.from_dict(
            JOB_TYPE_SCIENCE,
            dict(fixtures.science_payload().to_dict(), quantum_flux=9))


def test_require_names_every_missing_fact_at_once():
    # `require` survives D4 with its contract intact: it names EVERY absent
    # fact rather than the first, so one startup failure tells the operator
    # everything the submission was missing.
    payload = fixtures.science_payload()      # resolves no PSF
    with pytest.raises(ValueError) as caught:
        payload.require("rid", "psfid", "psf_uri")
    message = str(caught.value)
    assert "psfid" in message
    assert "psf_uri" in message
    assert "rid" not in message.split(":")[1]


def test_require_passes_when_every_fact_is_present():
    science_facts().require("rid", "psfid", "science_image_uri", "field")


def test_a_reference_image_id_of_none_means_one_must_be_built():
    # The launcher's rfid=None branch: no existing reference image.
    # JUDGEMENT CALL: the original also set `images_to_coadd=14` and
    # `coadd_inputs_uri=...` on the science payload and asserted
    # `facts.images_to_coadd == 14`. Both members are gone: manifest.py's
    # own retirement note says `images_to_coadd` and `reference_position`
    # were declared and documented but never written by any gatherer and
    # never read by any consumer, so D4 dropped them rather than moving
    # them onto a payload — and `coadd_inputs_uri` is a
    # `ReferenceImagePayload`-only member, invalid on `SciencePayload`
    # (`science_facts()` builds the latter). Retargeted at
    # `overlapping_fields`, a genuinely optional science fact
    # (`ImagingPayload`'s docstring: "a submit-time convenience for the
    # coadd path"), to keep exercising "an optional fact besides the
    # reference-image ones round-trips to None/absent same as before."
    facts = science_facts(reference_image_id=None, reference_image_uri=None,
                          overlapping_fields=())
    assert facts.reference_image_id is None
    assert facts.overlapping_fields == ()
    assert "reference_image_id" not in facts.to_dict()
    assert "overlapping_fields" not in facts.to_dict()


# --- manifest-wide fact requirements ----------------------------------

def test_require_facts_passes_when_every_unit_carries_them():
    manifest = Manifest(
        [ProcessingUnit(payload=science_facts(sca=n + 1))
         for n in range(3)],
        job_type="science")
    manifest.require_facts("rid", "science_image_uri")


def test_require_facts_names_the_offending_indices():
    # THE FACT ASKED FOR IS AN OPTIONAL ONE, and that is a consequence of
    # D4 rather than a weakening. `science_image_uri` — what this asked for
    # before — is now REQUIRED at construction, so a unit lacking it cannot
    # be built and no manifest can contain one. The behaviour under test is
    # unchanged: `require_facts` names every offending index, not just the
    # first. `psf_uri` is genuinely optional (an SCA may have no registered
    # PSF), so it is the fact that can still be absent on some units and
    # present on others.
    manifest = Manifest([
        ProcessingUnit(payload=science_facts(
            exposure=90210, sca=1, psf_uri="s3://sims/psf/a.fits")),
        ProcessingUnit(payload=science_facts(
            exposure=90210, sca=2, psfid=None, psf_uri=None)),
        ProcessingUnit(payload=science_facts(
            exposure=90210, sca=3, psfid=None, psf_uri=None)),
    ], job_type="science")
    with pytest.raises(ValueError) as caught:
        manifest.require_facts("psf_uri")
    message = str(caught.value)
    assert "2 of 3 units" in message
    assert "index 1 (090210/02)" in message
    assert "index 2 (090210/03)" in message
    assert "index 0" not in message


def test_require_facts_truncates_a_long_list_but_states_the_total():
    # JUDGEMENT CALL: the original asked for "rid" — now a REQUIRED fact, so
    # a unit missing it cannot be constructed and no manifest of 15 such
    # units can exist to exercise the truncation. Retargeted at `psf_uri`,
    # genuinely optional (an SCA may have no registered PSF), which the
    # fixture leaves unresolved on every unit by default.
    manifest = Manifest(
        [ProcessingUnit(payload=fixtures.science_payload(exposure=90210,
                                                          sca=n + 1))
         for n in range(15)],
        job_type="science")
    with pytest.raises(ValueError) as caught:
        manifest.require_facts("psf_uri")
    message = str(caught.value)
    assert "15 of 15 units" in message
    assert message.rstrip().endswith("...")


def test_facts_survive_a_full_manifest_round_trip():
    manifest = Manifest(
        [ProcessingUnit(payload=science_facts(sca=n + 1, rid=n))
         for n in range(3)],
        batch_id="b1", job_type="science")
    restored = Manifest.from_json(manifest.to_json())
    assert restored == manifest
    assert restored.unit_for_index(2).facts.rid == 2
    assert restored.checksum() == manifest.checksum()
