"""Closure records: complete canonical snapshots, and the reconciler-first form."""

import json
import unittest

from pipeline.reconciler import closure
from pipeline.reconciler.scheduler import observation_from_job
from pipeline.reconciler.test.stubs import attempt_row, batch_job, utc
from pipeline.runtime.boundaries import InMemoryObjectStore, checksum

PREFIX = "attempts"


def application_record(attempt_id=1, **overrides):
    """A sequence-0 record shaped like the one `terminate()` writes."""
    body = {
        "schema_version": 2,
        "record_sequence": 0,
        "record_author": "application",
        "attempt_id": attempt_id,
        "run_id": "run-1",
        "logical_job_id": "90000/1",
        "scheduler_job_id": "job-abc",
        "application_attempt_index": 1,
        "rapid_outcome": "failure",
        "product_disposition": "none",
        "application_intended_exit": 0,
        "error_category": "config_invalid",
        "config_digest": "digest-1",
        "bundle_key": "attempts/bundles/run-1/90000_1/attempt-1.tar.gz",
        "bundle_checksum": "bundle-sha",
        "stages": [{"stage_name": "one", "outcome": "success"}],
        "a_field_this_module_does_not_know_about": "must survive",
    }
    body.update(overrides)
    return body


def put_record(store, key, body):
    store.put_if_absent(key, json.dumps(body).encode("utf-8"),
                        content_type="application/json")


class PredecessorValidationTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryObjectStore()
        self.key = "attempts/records/run-1/90000_1/attempt-1/seq-0000.json"

    def test_a_valid_record_is_returned(self):
        put_record(self.store, self.key, application_record())

        body, reason = closure.read_predecessor(self.store, self.key, 1)

        self.assertIsNone(reason)
        self.assertEqual("failure", body["rapid_outcome"])

    def test_absent_is_reported_as_absent_not_as_an_error(self):
        body, reason = closure.read_predecessor(self.store, self.key, 1)

        self.assertIsNone(body)
        self.assertEqual(closure.REJECTED_ABSENT, reason)

    def test_a_record_for_a_different_attempt_is_rejected(self):
        # Validation is by identity, never by mere presence.
        put_record(self.store, self.key, application_record(attempt_id=999))

        body, reason = closure.read_predecessor(self.store, self.key, 1)

        self.assertIsNone(body)
        self.assertEqual(closure.REJECTED_IDENTITY, reason)

    def test_a_checksum_mismatch_is_rejected(self):
        put_record(self.store, self.key, application_record())
        # Corrupt the stored bytes while leaving the recorded checksum alone:
        # exactly the state a truncated or partially-overwritten object is in,
        # and the reason validation is by checksum rather than by presence.
        stored = self.store.objects[self.key]
        stored["body"] = b'{"attempt_id": 1, "tampered": true}'

        body, reason = closure.read_predecessor(self.store, self.key, 1)

        self.assertIsNone(body)
        self.assertEqual(closure.REJECTED_CHECKSUM, reason)

    def test_unreadable_json_is_rejected(self):
        body = b"this is not json"
        self.store.put_if_absent(self.key, body)

        result, reason = closure.read_predecessor(self.store, self.key, 1)

        self.assertIsNone(result)
        self.assertEqual(closure.REJECTED_UNREADABLE, reason)


class AgreedClosureTests(unittest.TestCase):
    def setUp(self):
        self.observation = observation_from_job(batch_job(
            status="SUCCEEDED", exit_code=0,
            started=utc(2026, 8, 6, 10, 0, 0),
            stopped=utc(2026, 8, 6, 10, 5, 0)))

    def test_the_snapshot_is_complete_so_consumers_never_chain_fold(self):
        record = closure.build_closure_record(
            attempt_row(), self.observation, sequence=1,
            predecessor=application_record(), classification="agreed")

        body = record.body
        # Application facts, folded in verbatim...
        self.assertEqual("failure", body["rapid_outcome"])
        self.assertEqual(0, body["application_intended_exit"])
        self.assertEqual("bundle-sha", body["bundle_checksum"])
        # ...including one this module has never heard of.
        self.assertEqual("must survive",
                         body["a_field_this_module_does_not_know_about"])
        # ...plus the scheduler's, which only the reconciler can author.
        self.assertEqual("SUCCEEDED", body["scheduler_state"])
        self.assertEqual(0, body["scheduler_observed_exit"])

    def test_an_agreed_record_is_still_written(self):
        # Every classification gets a closure record, agreed included, or the
        # store cannot distinguish "agreed" from "never checked".
        record = closure.build_closure_record(
            attempt_row(), self.observation, sequence=1,
            predecessor=application_record(), classification="agreed")

        self.assertFalse(record.reconciler_first)
        self.assertFalse(record.body["reconstructed"])
        self.assertEqual("reconciler", record.body["record_author"])

    def test_the_sequence_is_carried_into_the_body(self):
        record = closure.build_closure_record(
            attempt_row(), self.observation, sequence=3,
            predecessor=application_record(), classification="agreed")

        self.assertEqual(3, record.body["record_sequence"])


class ReconcilerFirstTests(unittest.TestCase):
    def test_a_never_started_attempt_is_built_from_the_row(self):
        observation = observation_from_job(batch_job(
            status="FAILED", exit_code=None, started=None,
            status_reason="CannotPullContainerError: no such manifest"))

        record = closure.build_closure_record(
            attempt_row(), observation, sequence=1, predecessor=None,
            rejected_key="attempts/records/.../seq-0000.json",
            rejected_reason=closure.REJECTED_ABSENT,
            classification="never_started",
            error_category="scheduler_provisioning")

        body = record.body
        self.assertTrue(record.reconciler_first)
        self.assertTrue(body["reconstructed"])
        self.assertEqual("scheduler_provisioning", body["error_category"])
        # The submission-time binding was copied onto the row at creation, so
        # even an attempt that never ran has its provenance.
        self.assertEqual("sha256:abc", body["provenance"]["image_digest"])
        self.assertEqual(10, body["provenance"]["job_definition_rev"])

    def test_runtime_selected_provenance_is_absent_not_sentinel_valued(self):
        record = closure.build_closure_record(
            attempt_row(), None, sequence=1, predecessor=None,
            classification="never_resolved",
            error_category="scheduler_provisioning")

        # The attempt never started, so it never selected a configuration.
        self.assertNotIn("config_digest", record.body)

    def test_a_rejected_predecessor_is_cited_by_key_and_reason(self):
        observation = observation_from_job(batch_job(
            status="FAILED", exit_code=1,
            started=utc(2026, 8, 6, 10, 0, 0),
            stopped=utc(2026, 8, 6, 10, 1, 0)))

        record = closure.build_closure_record(
            attempt_row(), observation, sequence=1, predecessor=None,
            rejected_key="attempts/records/run-1/90000_1/attempt-1/seq-0000.json",
            rejected_reason=closure.REJECTED_CHECKSUM,
            classification="abrupt_loss", error_category="internal_error")

        rejected = record.body["rejected_predecessor"]
        self.assertEqual(closure.REJECTED_CHECKSUM, rejected["reason"])
        self.assertIn("seq-0000.json", rejected["key"])

    def test_what_was_reconstructed_from_is_recorded(self):
        observation = observation_from_job(batch_job(
            status="FAILED", started=None, exit_code=None))

        record = closure.build_closure_record(
            attempt_row(), observation, sequence=1, predecessor=None,
            classification="never_started",
            error_category="scheduler_provisioning")

        self.assertIn("attempt_row", record.body["reconstructed_from"])
        self.assertIn("scheduler", record.body["reconstructed_from"])


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryObjectStore()
        self.row = attempt_row()

    def test_the_key_carries_the_sequence_zero_padded(self):
        record = closure.build_closure_record(
            self.row, None, sequence=1, predecessor=None,
            classification="never_resolved")

        result = closure.publish_closure_record(
            self.store, PREFIX, self.row, record)

        self.assertTrue(result.key.endswith("seq-0001.json"))
        self.assertTrue(result.created)

    def test_republishing_the_same_record_is_not_an_error(self):
        record = closure.build_closure_record(
            self.row, None, sequence=1, predecessor=None,
            classification="never_resolved", now=utc(2026, 8, 6, 12, 0, 0))

        first = closure.publish_closure_record(
            self.store, PREFIX, self.row, record)
        second = closure.publish_closure_record(
            self.store, PREFIX, self.row, record)

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.checksum, second.checksum)

    def test_the_body_round_trips_through_the_store(self):
        record = closure.build_closure_record(
            self.row, None, sequence=1, predecessor=None,
            classification="never_resolved")
        result = closure.publish_closure_record(
            self.store, PREFIX, self.row, record)

        raw = self.store.get(result.key)
        self.assertEqual(checksum(raw), result.checksum)
        self.assertEqual(1, json.loads(raw)["record_sequence"])


if __name__ == "__main__":
    unittest.main()
