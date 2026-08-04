"""
File:    test_registration.py

Tests that registration decides from the attempt record, not from job output.

The decisive test is `test_scheduler_succeeded_does_not_register_a_failed_attempt`:
that combination is the 2026-07-22 failure mode, and registration must skip it.
"""

import types
import unittest

from observability.registration import (
    RegistrationDecision,
    decide,
    decide_all,
)

def attempt(**overrides):
    base = dict(attempt_id=1, lifecycle_state="terminal_after_start",
                rapid_outcome="success", product_disposition="published",
                scheduler_state="SUCCEEDED", process_exit_code=0,
                error_category=None, exposure_id=4242, sca=7, sky_tile=None)
    base.update(overrides)
    return types.SimpleNamespace(**base)


class DecideTests(unittest.TestCase):
    def test_application_success_registers(self):
        result = decide(attempt())
        self.assertIs(result.decision, RegistrationDecision.REGISTER)
        self.assertTrue(result.should_register)

    def test_scheduler_succeeded_does_not_register_a_failed_attempt(self):
        # The whole reason the outcome taxonomy exists. Batch says SUCCEEDED,
        # the application says it failed — registration follows the application.
        result = decide(attempt(rapid_outcome="failure",
                                product_disposition="none",
                                scheduler_state="SUCCEEDED",
                                process_exit_code=0,
                                error_category="science_failure"))
        self.assertIs(result.decision, RegistrationDecision.SKIP)
        self.assertFalse(result.should_register)

    def test_scheduler_failed_does_not_block_a_successful_attempt(self):
        # The converse: the scheduler's view is not a veto either.
        result = decide(attempt(scheduler_state="FAILED"))
        self.assertIs(result.decision, RegistrationDecision.REGISTER)

    def test_partial_is_not_registered(self):
        result = decide(attempt(rapid_outcome="partial"))
        self.assertIs(result.decision, RegistrationDecision.SKIP)
        self.assertIn("partial", result.reason)

    def test_superseded_products_are_skipped(self):
        result = decide(attempt(product_disposition="superseded"))
        self.assertIs(result.decision, RegistrationDecision.SKIP)

    def test_withheld_products_are_skipped(self):
        result = decide(attempt(product_disposition="withheld"))
        self.assertIs(result.decision, RegistrationDecision.SKIP)

    def test_success_with_no_products_is_skipped(self):
        result = decide(attempt(product_disposition="none"))
        self.assertIs(result.decision, RegistrationDecision.SKIP)

    def test_never_started_attempt_is_skipped(self):
        result = decide(attempt(lifecycle_state="terminal_without_start",
                                rapid_outcome=None, product_disposition=None,
                                process_exit_code=None,
                                scheduler_state="FAILED"))
        self.assertIs(result.decision, RegistrationDecision.SKIP)
        self.assertIn("never started", result.reason)

    def test_non_terminal_attempt_defers_rather_than_skipping(self):
        # Defer is not skip: the attempt may still succeed, and a caller can
        # come back to it.
        for state in ("submitted", "started"):
            with self.subTest(state=state):
                result = decide(attempt(lifecycle_state=state,
                                        rapid_outcome=None,
                                        product_disposition=None))
                self.assertIs(result.decision, RegistrationDecision.DEFER)

    def test_flagged_record_defers_to_reconciliation(self):
        result = decide(attempt(lifecycle_state="missing_or_contradictory",
                                rapid_outcome=None, product_disposition=None))
        self.assertIs(result.decision, RegistrationDecision.DEFER)
        self.assertIn("reconciliation", result.reason)

    def test_decision_carries_the_processing_unit_scope(self):
        result = decide(attempt())
        self.assertEqual(result.exposure_id, 4242)
        self.assertEqual(result.sca, 7)

    def test_scheduler_state_is_recorded_but_not_decisive(self):
        result = decide(attempt())
        self.assertEqual(result.detail["scheduler_state"], "SUCCEEDED")


class DecideAllTests(unittest.TestCase):
    def test_decides_across_many_attempts(self):
        results = decide_all([
            attempt(attempt_id=1),
            attempt(attempt_id=2, rapid_outcome="failure",
                    product_disposition="none"),
        ])
        self.assertEqual([r.decision for r in results],
                         [RegistrationDecision.REGISTER,
                          RegistrationDecision.SKIP])

    def test_succeeded_but_failed_is_logged_out_loud(self):
        with self.assertLogs("observability.registration", level="WARNING") as logs:
            decide_all([attempt(rapid_outcome="failure",
                                product_disposition="none",
                                scheduler_state="SUCCEEDED",
                                error_category="science_failure")])
        self.assertIn("scheduler SUCCEEDED", "".join(logs.output))

    def test_ordinary_agreement_is_not_warned_about(self):
        with self.assertNoLogs("observability.registration", level="WARNING"):
            decide_all([attempt()])


if __name__ == "__main__":
    unittest.main()
