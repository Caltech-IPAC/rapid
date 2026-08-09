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

from submission.manifest import ProcessingUnit
from submission.routes import (JOB_TYPE_ALERT_PRODUCTION,
                               JOB_TYPE_CATALOG_LOAD, JOB_TYPE_CROSSMATCH,
                               JOB_TYPE_MERGE_CURRENCY, JOB_TYPE_MERGE_DEDUP,
                               JOB_TYPE_POST_PROCESS, JOB_TYPE_REFERENCE_IMAGE,
                               JOB_TYPE_SCIENCE, JOB_TYPE_SOURCE_CURRENCY,
                               JOB_TYPE_STATISTICS)
from submission.subjects import (GRAIN_DATE_FIELD, GRAIN_DATE_SCA,
                                 GRAIN_EXPOSURE_SCA, GRAIN_FIELD,
                                 SubjectError, attempt_identity_fields,
                                 is_product_producing, subject_for)


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

    def test_post_process_is_deliberately_unregistered(self):
        # Co-design ruling 9: post-process's disposition is undecided, so it
        # stays out of this registry rather than being guessed into a grain.
        with self.assertRaises(SubjectError):
            subject_for(JOB_TYPE_POST_PROCESS)

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
        a = ProcessingUnit(exposure=20260808, sca=0,
                           fields={"proc_date": "20260808", "field": 101})
        b = ProcessingUnit(exposure=20260808, sca=0,
                           fields={"proc_date": "20260808", "field": 202})
        self.assertNotEqual(a.dedup_key(JOB_TYPE_CROSSMATCH),
                            b.dedup_key(JOB_TYPE_CROSSMATCH))

    def test_a_missing_declared_component_is_refused(self):
        # A crossmatch unit missing "field" is not degraded identity, it is
        # no identity — silently omitting it would let two units with
        # different missing components collide.
        bare = ProcessingUnit(exposure=20260808, sca=0,
                              fields={"proc_date": "20260808"})
        with self.assertRaises(SubjectError):
            bare.dedup_key(JOB_TYPE_CROSSMATCH)

    def test_science_dedup_key_is_the_storage_key_shape(self):
        unit = ProcessingUnit(exposure=90000, sca=1)
        self.assertEqual(unit.dedup_key(JOB_TYPE_SCIENCE),
                         (JOB_TYPE_SCIENCE, 90000, 1))

    def test_an_unregistered_job_type_falls_back_to_exposure_sca(self):
        # post-process and any job type the registry does not cover keep
        # the pre-ruling exposure/SCA identity rather than raising.
        unit = ProcessingUnit(exposure=42, sca=3)
        self.assertEqual(unit.dedup_key(JOB_TYPE_POST_PROCESS),
                         (JOB_TYPE_POST_PROCESS, 42, 3))


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
