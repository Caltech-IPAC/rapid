"""Typed payload construction and validation (brief D, criterion 8).

"Subject derivation with a missing required component fails loud at
construction."

The emphasis is on AT CONSTRUCTION. The pre-D3 code validated a subject at
DERIVATION time — `JobTypeSubject.subject_for` raised when a declared
component was missing from the open `fields` dict — which meant a unit
missing its identity could be built, put in a manifest, submitted, and
scheduled, and only failed when something asked what it was. Rule 11 says
subjects are "typed and validated at creation", and these tests assert the
difference: the invalid unit cannot exist.

A stub-tier test: pure Python, no database.
"""

import pytest

from submission import payloads
from submission.test import payload_fixtures as fixtures
from submission.manifest import ProcessingUnit
from submission.routes import (JOB_TYPE_ALERT_PRODUCTION,
                               JOB_TYPE_CATALOG_LOAD, JOB_TYPE_CROSSMATCH,
                               JOB_TYPE_MERGE_DEDUP,
                               JOB_TYPE_REFERENCE_IMAGE, JOB_TYPE_SCIENCE,
                               JOB_TYPE_STATISTICS)
from submission.subjects import SubjectError, subject_for


# ---------------------------------------------------------------------------
# Fail-loud at construction
# ---------------------------------------------------------------------------

#: One missing-component case per job type, naming the component omitted.
#: Parameterized rather than spot-checked so no payload type can quietly
#: acquire an unvalidated component.
MISSING_CASES = [
    (JOB_TYPE_SCIENCE, dict(sca=3), "exposure"),
    (JOB_TYPE_SCIENCE, dict(exposure=1), "sca"),
    # The imaging FACTS, not just the subject components. Each is dropped
    # from an otherwise-complete fixture, so the case exercises exactly one
    # absence — and covers the required set D4 moved off `UnitFacts`, where
    # every one of these was `X | None = None` and none was checked at all.
] + [
    (JOB_TYPE_SCIENCE,
     dict({k: v for k, v in fixtures.IMAGING_FACTS.items() if k != name},
          exposure=90001, sca=3),
     name)
    for name in ("rid", "fid", "field", "rtid", "expid")
] + [
    (JOB_TYPE_CATALOG_LOAD,
     dict(sca=3, target_table="t"), "proc_date"),
    (JOB_TYPE_CATALOG_LOAD,
     dict(proc_date="20260812", target_table="t"), "sca"),
    (JOB_TYPE_CATALOG_LOAD,
     dict(proc_date="20260812", sca=3), "target_table"),
    (JOB_TYPE_CROSSMATCH,
     dict(field=1, target_tables=("t",)), "proc_date"),
    (JOB_TYPE_CROSSMATCH,
     dict(proc_date="20260812", target_tables=("t",)), "field"),
    (JOB_TYPE_CROSSMATCH,
     dict(proc_date="20260812", field=1), "target_tables"),
    (JOB_TYPE_STATISTICS, dict(target_table="t"), "field"),
    (JOB_TYPE_STATISTICS, dict(field=1), "target_table"),
    (JOB_TYPE_MERGE_DEDUP, dict(target_table="t"), "field"),
    (JOB_TYPE_ALERT_PRODUCTION,
     dict(exposure=1, sca=2, release_identity="r",
          difference_image_pid=3), "promoted_attempt_id"),
    (JOB_TYPE_ALERT_PRODUCTION,
     dict(exposure=1, sca=2, promoted_attempt_id=1,
          difference_image_pid=3), "release_identity"),
]


@pytest.mark.parametrize("job_type,components,missing", MISSING_CASES)
def test_a_missing_component_fails_at_construction(job_type, components,
                                                   missing):
    """Building a payload without a required component raises, naming it.

    Three things asserted together: that it raises AT ALL (rather than
    building a unit with a None component that fails later), that it raises
    ONE exception type whichever way the component is missing, and that the
    message names the component — because the operator reading it needs to
    know which fact the submission was missing, not merely that one was.

    The single type matters more than it looks. An omitted keyword reaches
    the dataclass constructor as `TypeError` while an explicit `None`
    reaches `__post_init__` as `PayloadError`; `payloads.build` translates
    the first so a caller writing `except PayloadError` catches both. Before
    that translation it caught half the cases silently.
    """
    with pytest.raises(payloads.PayloadError) as raised:
        payloads.build(job_type, **components)
    assert missing in str(raised.value)


@pytest.mark.parametrize("job_type,components,missing", MISSING_CASES)
def test_no_unit_can_be_built_around_an_invalid_payload(job_type, components,
                                                        missing):
    """The failure happens before a `ProcessingUnit` exists.

    The distinction from the pre-D3 behaviour, asserted directly: there is
    no moment at which an identity-less unit is a live object that could be
    put in a manifest.
    """
    with pytest.raises(payloads.PayloadError):
        ProcessingUnit(payload=payloads.build(job_type, **components))


def test_a_none_component_is_refused_as_firmly_as_an_absent_one():
    """Passing `sca=None` explicitly is the same failure as omitting it.

    Worth its own test because the dataclass would happily accept `None` —
    the constructor's signature is satisfied — and only `__post_init__`
    catches it. A submitter that resolved a fact to nothing and passed it
    through is exactly how an identity-less unit used to get built.
    """
    with pytest.raises(payloads.PayloadError) as raised:
        payloads.build(JOB_TYPE_SCIENCE, exposure=90001, sca=None)
    assert "sca" in str(raised.value)


# ---------------------------------------------------------------------------
# Subject derivation reads only declared typed components
# ---------------------------------------------------------------------------


def test_subject_derivation_reads_the_typed_payload():
    """`subject_for(...).subject_for(unit)` returns the payload's subject."""
    unit = ProcessingUnit(payload=payloads.build(
        JOB_TYPE_CROSSMATCH, proc_date="20260812", field=4242,
        target_tables=("t",)))
    assert (subject_for(JOB_TYPE_CROSSMATCH).subject_for(unit)
            == (JOB_TYPE_CROSSMATCH, "20260812", 4242))
    assert unit.dedup_key() == (JOB_TYPE_CROSSMATCH, "20260812", 4242)


def test_asking_for_the_wrong_job_types_subject_is_an_error():
    """A caller bug the old signature could not detect.

    `subject_for(job_type).subject_for(unit)` used to take whatever unit it
    was handed and read the components of the job type ASKED FOR out of its
    dict — so asking a crossmatch unit for a science subject returned a
    plausible tuple built from the wrong thing. A payload declares its own
    job type, so the mismatch is now visible.
    """
    unit = ProcessingUnit(payload=payloads.build(
        JOB_TYPE_CROSSMATCH, proc_date="20260812", field=4242,
        target_tables=("t",)))
    with pytest.raises(SubjectError):
        subject_for(JOB_TYPE_SCIENCE).subject_for(unit)
    with pytest.raises(SubjectError):
        unit.dedup_key(JOB_TYPE_SCIENCE)


@pytest.mark.parametrize("job_type,components", [
    (JOB_TYPE_CROSSMATCH, dict(proc_date="20260812", field=1,
                               target_tables=("t",))),
    (JOB_TYPE_STATISTICS, dict(field=1, target_table="t")),
])
def test_a_non_exposure_grain_unit_has_no_exposure_at_all(job_type,
                                                          components):
    """Asking a field- or date-grained unit for its exposure RAISES.

    Not "returns 0", not "returns a date ordinal" — raises. That is the
    whole of rule 11's first clause, and the old representation answered
    the question with a placeholder that looked like data.
    """
    unit = ProcessingUnit(payload=payloads.build(job_type, **components))
    with pytest.raises(SubjectError):
        unit.exposure
    if "sca" not in payloads.payload_type(job_type).COMPONENTS:
        with pytest.raises(SubjectError):
            unit.sca


def test_an_exposure_grain_unit_still_answers_normally():
    """The complement: where the grain declares them, they are plain data."""
    unit = fixtures.science_unit(exposure=90001, sca=3)
    assert unit.exposure == 90001
    assert unit.sca == 3
    assert unit.job_type == JOB_TYPE_SCIENCE


def test_a_date_sca_unit_has_an_sca_but_no_exposure():
    """Date/SCA declares one of the two, which is the interesting case."""
    unit = ProcessingUnit(payload=payloads.build(
        JOB_TYPE_CATALOG_LOAD, proc_date="20260812", sca=6,
        target_table="sources_20260812_6"))
    assert unit.sca == 6
    with pytest.raises(SubjectError):
        unit.exposure


# ---------------------------------------------------------------------------
# The registry is closed
# ---------------------------------------------------------------------------


def test_a_reference_candidate_may_lack_its_coadd_facts_but_a_submission_may_not():
    """The one optionality that is a STAGE distinction, asserted both ways.

    Reference-image gathering is two passes: the first yields candidates
    from the L2 rows, the second aggregates the overlapping frames and
    completes them. So a candidate legitimately has no `coadd_inputs_uri` —
    the overlap query has not run — while a unit about to be SUBMITTED must
    have all three, because `coadd_input_identities` is an input component
    of the product key (rule 10).

    Both halves are asserted here so the optionality cannot quietly widen
    into "these are never checked". This is the shape rule 11 asks for:
    optional for a stated reason, with the requirement enforced where it is
    actually true.
    """
    from submission.manifest import Manifest

    candidate = payloads.build(
        JOB_TYPE_REFERENCE_IMAGE, exposure=90002, sca=4,
        **fixtures.IMAGING_FACTS)
    assert candidate.coadd_inputs_uri is None
    assert candidate.coadd_input_identities == ()

    manifest = Manifest([ProcessingUnit(payload=candidate)],
                        batch_id="run-1", job_type=JOB_TYPE_REFERENCE_IMAGE)
    with pytest.raises(ValueError) as raised:
        manifest.require_facts(*candidate.SUBMITTABLE_FACTS)
    assert "coadd_inputs_uri" in str(raised.value)

    # The completed unit the second gathering stage yields passes.
    completed = Manifest([ProcessingUnit(payload=fixtures.reference_payload())],
                         batch_id="run-1",
                         job_type=JOB_TYPE_REFERENCE_IMAGE)
    completed.require_facts(*candidate.SUBMITTABLE_FACTS)


def test_an_undeclared_job_type_cannot_build_a_payload():
    """D4's surrendered convenience, asserted.

    "A new job type henceforth declares its payload type; the 'no schema
    change to carry one extra value' convenience is surrendered." So an
    unknown job type FAILS rather than falling back to an exposure/SCA
    shape — which is the re-examined fallback brief D asks about, resolved
    in favour of failing loud.
    """
    with pytest.raises(payloads.UnknownPayloadType) as raised:
        payloads.build("no_such_job_type", exposure=1, sca=2)
    assert "no_such_job_type" in str(raised.value)


def test_every_gathered_job_type_declares_a_payload():
    """The registry covers every job type the subject registry declares.

    The two declarations must agree: a job type with a subject grain but no
    payload type could be gathered and then not constructed, and one with a
    payload but no grain could be constructed and then not identified.
    """
    from submission.subjects import SUBJECTS

    declared_subjects = {s.job_type for s in SUBJECTS}
    declared_payloads = set(payloads.PAYLOAD_TYPES)
    assert declared_subjects == declared_payloads, (
        f"subjects-only: {sorted(declared_subjects - declared_payloads)}; "
        f"payloads-only: {sorted(declared_payloads - declared_subjects)}")


def test_each_payload_type_agrees_with_its_declared_grain():
    """Payload grain and registry grain match, per job type.

    Two independent statements that must agree — `subjects.SUBJECTS` and the
    payload class — so a mismatch is caught rather than one being silently
    derived from the other.
    """
    from submission.subjects import SUBJECTS

    for declared in SUBJECTS:
        payload_class = payloads.payload_type(declared.job_type)
        assert payload_class.GRAIN == declared.grain, (
            f"{declared.job_type}: registry says {declared.grain}, payload "
            f"says {payload_class.GRAIN}")
        if declared.components:
            assert payload_class.COMPONENTS == declared.components, (
                f"{declared.job_type}: registry components "
                f"{declared.components}, payload {payload_class.COMPONENTS}")


# ---------------------------------------------------------------------------
# The wire form refuses what it does not declare
# ---------------------------------------------------------------------------


def test_an_unknown_payload_key_is_refused_not_dropped():
    """A key this payload type does not declare refuses the whole unit.

    Dropping it would run the job against a subset of what the submitter
    described — the same position `UnitFacts.from_dict` already took for
    facts, applied to components.
    """
    with pytest.raises(payloads.PayloadError) as raised:
        payloads.from_dict(JOB_TYPE_SCIENCE,
                           {"grain": "exposure_sca", "exposure": 1, "sca": 2,
                            "smuggled": "value"})
    assert "smuggled" in str(raised.value)


def test_a_disagreeing_grain_in_the_wire_form_is_refused():
    """A manifest claiming a grain the payload type does not have."""
    with pytest.raises(payloads.PayloadError) as raised:
        payloads.from_dict(JOB_TYPE_SCIENCE,
                           {"grain": "field", "exposure": 1, "sca": 2})
    assert "grain" in str(raised.value)


@pytest.mark.parametrize("build", [
    fixtures.science_payload,
    fixtures.reference_payload,
    fixtures.alert_payload,
    fixtures.crossmatch_payload,
    fixtures.catalog_load_payload,
])
def test_a_payload_round_trips_through_its_wire_form(build):
    """Serialize a payload, parse it back, and get an EQUAL object.

    Equality, not just an equal subject: the facts D4 moved onto the payload
    have to survive the round trip too, and the sequence members are the ones
    that would not (JSON returns every sequence as a list) without
    `_freeze`'s normalization.
    """
    original = build()
    restored = payloads.from_dict(original.JOB_TYPE, original.to_dict())
    assert restored.subject() == original.subject()
    assert restored == original


# ---------------------------------------------------------------------------
# CrossmatchPayload.source_tables (2026-08-14): the typed home for the
# crossmatch source-tables fix. Optional (no gatherer populates it yet — see
# the field's own docstring and this wave's ledger for the integration
# request against submission/gathering.py), unlike target_tables which
# stays required.
# ---------------------------------------------------------------------------


def test_source_tables_defaults_to_empty_when_omitted():
    payload = fixtures.crossmatch_payload()
    assert payload.source_tables == ()


def test_source_tables_is_accepted_and_frozen():
    payload = fixtures.crossmatch_payload(
        source_tables=["sources_20260812_1", "sources_20260812_2"])
    assert payload.source_tables == (
        "sources_20260812_1", "sources_20260812_2")
    assert isinstance(payload.source_tables, tuple)


def test_source_tables_absent_from_target_tables_construction_still_works():
    """target_tables stays REQUIRED; source_tables being new and optional
    must not change that — a unit with no declared targets still refuses."""
    with pytest.raises(payloads.PayloadError):
        payloads.build(JOB_TYPE_CROSSMATCH, proc_date="20260812", field=1,
                       target_tables=())


def test_source_tables_is_omitted_from_the_wire_form_when_empty():
    """The absent-not-sentinel rule `to_dict`'s own docstring states:
    an empty sequence is omitted, exactly like target_tables would be if it
    were ever empty (it cannot be — required) and like product_inputs is
    on CatalogLoadPayload."""
    payload = fixtures.crossmatch_payload()
    assert "source_tables" not in payload.to_dict()


def test_source_tables_round_trips_through_the_wire_form_when_present():
    original = fixtures.crossmatch_payload(
        source_tables=("sources_20260812_1", "sources_20260812_2"))
    restored = payloads.from_dict(original.JOB_TYPE, original.to_dict())
    assert restored == original
    assert restored.source_tables == (
        "sources_20260812_1", "sources_20260812_2")
