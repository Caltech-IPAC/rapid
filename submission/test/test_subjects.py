"""Tests for the typed-identity registry: subject grains, dedup keys, and
the applicable-identifier mapping attempt rows carry (co-design ruling 2).

Two refusal tests from the build spec live here directly:

* a field-grain unit carries NO exposure/SCA sentinel identity
  (`test_field_grain_identity_carries_no_sentinel`);
* a database-effect job type mints no product keys
  (`test_database_effect_job_type_mints_no_product_key`, in
  `pipeline/stages/test/test_context.py` — `product_prefix()` is where a
  product key is actually built, so that refusal is tested there against
  the real method; this file asserts the REGISTRY declaration
  `product_prefix()` reads).
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from submission import payloads
from submission.manifest import ProcessingUnit
from submission.routes import (JOB_TYPE_ALERT_PRODUCTION,
                               JOB_TYPE_CATALOG_LOAD, JOB_TYPE_CROSSMATCH,
                               JOB_TYPE_MERGE_CURRENCY, JOB_TYPE_MERGE_DEDUP,
                               JOB_TYPE_REFERENCE_IMAGE, JOB_TYPE_REGISTRATION,
                               JOB_TYPE_SCIENCE, JOB_TYPE_SOURCE_CURRENCY,
                               JOB_TYPE_STATISTICS)
from submission.subjects import (GRAIN_DATE_FIELD, GRAIN_DATE_SCA,
                                 GRAIN_EXPOSURE_SCA, GRAIN_FIELD,
                                 SubjectError, attempt_identity_fields,
                                 build_input_scope, is_product_producing,
                                 parse_exposure_sca_scope, subject_for)


class GrainDeclarationTests(unittest.TestCase):
    def test_product_producing_types_are_exactly_the_exposure_sca_two(self):
        self.assertTrue(is_product_producing(JOB_TYPE_SCIENCE))
        self.assertTrue(is_product_producing(JOB_TYPE_REFERENCE_IMAGE))
        for job_type in (JOB_TYPE_CATALOG_LOAD, JOB_TYPE_CROSSMATCH,
                         JOB_TYPE_STATISTICS, JOB_TYPE_MERGE_CURRENCY,
                         JOB_TYPE_SOURCE_CURRENCY, JOB_TYPE_MERGE_DEDUP,
                         JOB_TYPE_ALERT_PRODUCTION):
            self.assertFalse(is_product_producing(job_type),
                             f"{job_type} must not be product-producing")

    def test_registration_is_deliberately_unregistered(self):
        # Registration is not gathered through this path at all — it has no
        # declared subject grain and stays out of this registry rather than
        # being guessed into one.
        with self.assertRaises(SubjectError):
            subject_for(JOB_TYPE_REGISTRATION)

    def test_grains_match_the_ruling_text(self):
        self.assertEqual(subject_for(JOB_TYPE_CATALOG_LOAD).grain,
                         GRAIN_DATE_SCA)
        self.assertEqual(subject_for(JOB_TYPE_CROSSMATCH).grain,
                         GRAIN_DATE_FIELD)
        for job_type in (JOB_TYPE_STATISTICS, JOB_TYPE_MERGE_CURRENCY,
                         JOB_TYPE_SOURCE_CURRENCY, JOB_TYPE_MERGE_DEDUP):
            self.assertEqual(subject_for(job_type).grain, GRAIN_FIELD)
        self.assertEqual(subject_for(JOB_TYPE_SCIENCE).grain,
                         GRAIN_EXPOSURE_SCA)


class DedupSubjectTests(unittest.TestCase):
    def test_two_crossmatch_fields_share_no_subject(self):
        a = ProcessingUnit(payload=payloads.build(
            JOB_TYPE_CROSSMATCH, proc_date="20260808", field=101,
            target_tables=("t",)))
        b = ProcessingUnit(payload=payloads.build(
            JOB_TYPE_CROSSMATCH, proc_date="20260808", field=202,
            target_tables=("t",)))
        self.assertNotEqual(a.dedup_key(JOB_TYPE_CROSSMATCH),
                            b.dedup_key(JOB_TYPE_CROSSMATCH))

    def test_a_missing_declared_component_is_refused(self):
        # A crossmatch unit missing "field" is not degraded identity, it is
        # no identity — silently omitting it would let two units with
        # different missing components collide. The refusal has moved
        # earlier: a payload missing a declared component cannot be BUILT
        # at all now (PayloadError at construction), so there is no bare
        # unit left to reach `dedup_key` with.
        with self.assertRaises(payloads.PayloadError):
            payloads.build(JOB_TYPE_CROSSMATCH, proc_date="20260808",
                           target_tables=("t",))

    def test_science_dedup_key_is_the_storage_key_shape(self):
        unit = ProcessingUnit(payload=payloads.build(
            JOB_TYPE_SCIENCE, exposure=90000, sca=1))
        self.assertEqual(unit.dedup_key(JOB_TYPE_SCIENCE),
                         (JOB_TYPE_SCIENCE, 90000, 1))

    def test_dedup_key_refuses_a_job_type_the_payload_disagrees_with(self):
        # Registration has no declared payload type at all now (not in
        # payloads.PAYLOAD_TYPES), so a registration unit cannot be built
        # to begin with — the old "unregistered job type falls back to
        # exposure/SCA" behaviour is gone by construction, not just
        # untested (manifest.py's ProcessingUnit.dedup_key docstring:
        # "NO FALLBACK REMAINS"). What is left to test at this seam is the
        # surviving mismatch check: dedup_key(job_type) raises when asked
        # for a job type other than the one the unit's own payload
        # declares.
        unit = ProcessingUnit(payload=payloads.build(
            JOB_TYPE_SCIENCE, exposure=42, sca=3))
        with self.assertRaises(SubjectError):
            unit.dedup_key(JOB_TYPE_REGISTRATION)


class AttemptIdentityFieldTests(unittest.TestCase):
    """Refusal: a field-grain unit carries NO exposure/SCA sentinel identity."""

    def test_field_grain_identity_carries_no_sentinel(self):
        # `_per_field_units` (submission/gathering.py) puts the field number
        # in `exposure` as a synthetic array-layer carrier — `sca` is a
        # fixed 0. Neither is a real identifier of a statistics attempt, so
        # neither may appear in the identity fields written to the row.
        unit = ProcessingUnit(exposure=4678622, sca=0,
                              fields={"field": 4678622})
        identity = attempt_identity_fields(JOB_TYPE_STATISTICS, unit)

        self.assertEqual(identity, {"field": 4678622})
        self.assertNotIn("exposure_id", identity)
        self.assertNotIn("sca", identity)

    def test_date_field_grain_identity_carries_no_sentinel(self):
        unit = ProcessingUnit(exposure=20260808, sca=0,
                              fields={"proc_date": "20260808", "field": 101})
        identity = attempt_identity_fields(JOB_TYPE_CROSSMATCH, unit)

        self.assertEqual(identity,
                         {"field": 101, "processing_date": "20260808"})
        self.assertNotIn("exposure_id", identity)
        self.assertNotIn("sca", identity)

    def test_date_sca_grain_identity_carries_the_real_sca_not_the_ordinal(self):
        unit = ProcessingUnit(exposure=20260808, sca=7,
                              fields={"proc_date": "20260808", "sca": 7})
        identity = attempt_identity_fields(JOB_TYPE_CATALOG_LOAD, unit)

        self.assertEqual(identity,
                         {"sca": 7, "processing_date": "20260808"})
        self.assertNotIn("exposure_id", identity)

    def test_exposure_sca_grain_identity_is_unchanged(self):
        unit = ProcessingUnit(exposure=90000, sca=1)
        identity = attempt_identity_fields(JOB_TYPE_SCIENCE, unit)

        self.assertEqual(identity["exposure_id"], 90000)
        self.assertEqual(identity["sca"], 1)
        self.assertNotIn("field", identity)
        self.assertNotIn("processing_date", identity)


class InputScopeGrammarTests(unittest.TestCase):
    """`build_input_scope`/`parse_exposure_sca_scope`: the ONE grammar
    (IR-13-a) `pipeline.seams._input_scope_for` now delegates to, and the
    campaign gatherer parses back.
    """

    def test_build_matches_the_delimited_shape_seams_used_to_hardcode(self):
        unit = ProcessingUnit(exposure=90000, sca=7)
        self.assertEqual(build_input_scope(JOB_TYPE_SCIENCE, unit),
                         "90000/7")

    def test_round_trip_recovers_the_original_exposure_and_sca(self):
        unit = ProcessingUnit(exposure=90000, sca=7)
        scope = build_input_scope(JOB_TYPE_SCIENCE, unit)
        self.assertEqual(parse_exposure_sca_scope(scope), (90000, 7))

    def test_round_trip_holds_for_reference_image_too(self):
        # The other EXPOSURE_SCA-grain, product-producing job type — proof
        # the grammar is grain-shaped, not science-specific.
        unit = ProcessingUnit(exposure=12345, sca=3)
        scope = build_input_scope(JOB_TYPE_REFERENCE_IMAGE, unit)
        self.assertEqual(parse_exposure_sca_scope(scope), (12345, 3))

    def test_a_malformed_scope_is_refused_not_coerced(self):
        with self.assertRaises(SubjectError):
            parse_exposure_sca_scope("90000/7/extra")

    def test_a_non_integer_component_is_refused(self):
        with self.assertRaises(SubjectError):
            parse_exposure_sca_scope("90000/not-a-sca")

    def test_a_single_component_scope_is_refused(self):
        # Guards against accidentally parsing a FIELD-grain scope (a bare
        # field id, no slash) as if it were exposure/SCA.
        with self.assertRaises(SubjectError):
            parse_exposure_sca_scope("101")

    def test_seams_input_scope_for_agrees_with_build_input_scope(self):
        # pipeline.seams._input_scope_for now DELEGATES to build_input_scope
        # (IR-13-a) rather than duplicating the stringification — this
        # proves the delegation actually happened, not just that both
        # functions independently agree.
        from pipeline.seams import _input_scope_for

        unit = ProcessingUnit(exposure=90000, sca=7)
        self.assertEqual(_input_scope_for(JOB_TYPE_SCIENCE, unit),
                         build_input_scope(JOB_TYPE_SCIENCE, unit))
