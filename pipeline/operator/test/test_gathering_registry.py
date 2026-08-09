"""
File:    test_gathering_registry.py

Tests for the operator's gatherer registry (co-design ruling 1) and the
class-to-job-type fan-out `pipeline.operator.service` builds over it.

What this covers, and why it lives here rather than in `submission/test/`:
`submission/test/test_gathering.py` already exercises each `gather_*`
function's own logic against a `UnitSource` stub. This file is about the
REGISTRY — the mapping from job type to gatherer and operational class —
and the fan-out that turns one running class into several `Operator`
instances, which is the mechanism this ruling adds and nothing upstream
tests.
"""

import dataclasses
import unittest

from pipeline.operator import classes as opclasses
from pipeline.operator.gathering import (REGISTRY, gatherer_for,
                                         job_types_for_class,
                                         processing_date_for)
from submission.routes import (JOB_TYPE_ALERT_PRODUCTION,
                               JOB_TYPE_CATALOG_LOAD, JOB_TYPE_CROSSMATCH,
                               JOB_TYPE_MERGE_CURRENCY, JOB_TYPE_MERGE_DEDUP,
                               JOB_TYPE_REFERENCE_IMAGE, JOB_TYPE_SCIENCE,
                               JOB_TYPE_SOURCE_CURRENCY, JOB_TYPE_STATISTICS)


class RegistryShapeTests(unittest.TestCase):
    """THE ENUMERATION IS THE REGISTRY — no residual class conditional."""

    def test_every_implemented_job_type_is_registered(self):
        # The nine job types `submission.routes.IMPLEMENTED_JOB_TYPES` names
        # minus post-process (co-design ruling 9: undecided disposition,
        # deliberately out of this registry) are exactly what REGISTRY
        # carries.
        registered = {job_type for job_type, _, _ in REGISTRY}
        self.assertEqual(registered, {
            JOB_TYPE_SCIENCE, JOB_TYPE_REFERENCE_IMAGE, JOB_TYPE_CATALOG_LOAD,
            JOB_TYPE_CROSSMATCH, JOB_TYPE_STATISTICS, JOB_TYPE_MERGE_CURRENCY,
            JOB_TYPE_SOURCE_CURRENCY, JOB_TYPE_MERGE_DEDUP,
            JOB_TYPE_ALERT_PRODUCTION,
        })

    def test_reference_construction_keeps_its_one_job_type(self):
        self.assertEqual(
            job_types_for_class(opclasses.REFERENCE_CONSTRUCTION),
            (JOB_TYPE_REFERENCE_IMAGE,))

    def test_prompt_processing_fans_out_to_the_complete_chain(self):
        # The headline defect this ruling closes: before it, prompt
        # processing gathered only science. It now registers science, the
        # six post-DB job types, and alert production — the complete
        # operator-scheduled chain the ADOPTED operations text describes.
        self.assertEqual(
            job_types_for_class(opclasses.PROMPT_PROCESSING), (
                JOB_TYPE_SCIENCE, JOB_TYPE_CATALOG_LOAD, JOB_TYPE_CROSSMATCH,
                JOB_TYPE_STATISTICS, JOB_TYPE_MERGE_CURRENCY,
                JOB_TYPE_SOURCE_CURRENCY, JOB_TYPE_MERGE_DEDUP,
                JOB_TYPE_ALERT_PRODUCTION))

    def test_an_unregistered_job_type_is_refused_not_silently_skipped(self):
        with self.assertRaises(ValueError):
            gatherer_for("not-a-real-job-type", operator_input=None,
                         parameters={}, connection_factory=lambda: None)


class ProcessingDateTests(unittest.TestCase):
    """The named, single function the window-to-date judgment call lives in."""

    def test_the_processing_date_is_the_windows_end_in_utc(self):
        import datetime

        class FakeInput:
            end = datetime.datetime(2026, 8, 9, 3, 0, 0,
                                    tzinfo=datetime.timezone.utc)

        self.assertEqual(processing_date_for(FakeInput()), "20260809")

    def test_a_non_utc_end_is_converted_before_formatting(self):
        import datetime

        tz = datetime.timezone(datetime.timedelta(hours=-7))

        class FakeInput:
            # 2026-08-08 23:30 -07:00 is 2026-08-09 06:30 UTC — the DATE
            # must come from the UTC instant, not the naive wall-clock
            # digits, or a pass near a day boundary would gather the wrong
            # date's chain.
            end = datetime.datetime(2026, 8, 8, 23, 30, 0, tzinfo=tz)

        self.assertEqual(processing_date_for(FakeInput()), "20260809")


class ClassFanOutTests(unittest.TestCase):
    """`service._classes_for_pass`: one running class, several operators."""

    def test_prompt_processing_expands_to_eight_operational_classes(self):
        from pipeline.operator.service import _classes_for_pass

        running = opclasses.class_for(opclasses.PROMPT_PROCESSING)
        expanded = _classes_for_pass(running)

        self.assertEqual(len(expanded), 8)
        self.assertEqual(tuple(c.job_type for c in expanded),
                         job_types_for_class(opclasses.PROMPT_PROCESSING))
        # Every expanded entry is still a real OperationalClass — the exact
        # contract Operator/LiveSubmitter/RehearsalSubmitter already read.
        for c in expanded:
            self.assertTrue(c.implemented)
            self.assertEqual(c.route.job_type, c.job_type)

    def test_the_original_science_class_is_reused_not_rebuilt(self):
        # Science IS the class's own job type, so `_classes_for_pass` must
        # not construct a second, merely-equal OperationalClass for it —
        # returning the original object is what makes this cheap to assert
        # and avoids two frozen instances that compare equal but are not
        # the same object drifting apart under a future edit.
        from pipeline.operator.service import _classes_for_pass

        running = opclasses.class_for(opclasses.PROMPT_PROCESSING)
        expanded = _classes_for_pass(running)
        self.assertIs(expanded[0], running)

    def test_reference_construction_expands_to_itself_alone(self):
        from pipeline.operator.service import _classes_for_pass

        running = opclasses.class_for(opclasses.REFERENCE_CONSTRUCTION)
        expanded = _classes_for_pass(running)
        self.assertEqual(expanded, (running,))
