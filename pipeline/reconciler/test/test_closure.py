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

        read = closure.read_predecessor(self.store, self.key, 1)

        self.assertIsNone(read.reason)
        self.assertTrue(read.usable)
        self.assertEqual("failure", read.body["rapid_outcome"])

    def test_a_usable_record_carries_its_own_key_and_checksum(self):
        # Review finding #14: in the record-written/row-not-closed crash state
        # neither the row nor the body holds these, and `application_closed`
        # requires a non-null key. The reader has just read and checksummed the
        # bytes, so it is the one component that knows both.
        put_record(self.store, self.key, application_record())

        read = closure.read_predecessor(self.store, self.key, 1)

        self.assertEqual(self.key, read.key)
        self.assertEqual(closure.body_checksum(self.store.objects[self.key]["body"]),
                         read.checksum)

    def test_absent_is_reported_as_absent_not_as_an_error(self):
        read = closure.read_predecessor(self.store, self.key, 1)

        self.assertIsNone(read.body)
        self.assertEqual(closure.REJECTED_ABSENT, read.reason)
        self.assertFalse(read.deferred)

    def test_a_record_for_a_different_attempt_is_rejected(self):
        # Validation is by identity, never by mere presence.
        put_record(self.store, self.key, application_record(attempt_id=999))

        read = closure.read_predecessor(self.store, self.key, 1)

        self.assertIsNone(read.body)
        self.assertEqual(closure.REJECTED_IDENTITY, read.reason)

    def test_a_checksum_mismatch_is_rejected(self):
        put_record(self.store, self.key, application_record())
        # Corrupt the stored bytes while leaving the recorded checksum alone:
        # exactly the state a truncated or partially-overwritten object is in,
        # and the reason validation is by checksum rather than by presence.
        stored = self.store.objects[self.key]
        stored["body"] = b'{"attempt_id": 1, "tampered": true}'

        read = closure.read_predecessor(self.store, self.key, 1)

        self.assertIsNone(read.body)
        self.assertEqual(closure.REJECTED_CHECKSUM, read.reason)

    def test_unreadable_json_is_rejected(self):
        body = b"this is not json"
        self.store.put_if_absent(self.key, body)

        read = closure.read_predecessor(self.store, self.key, 1)

        self.assertIsNone(read.body)
        self.assertEqual(closure.REJECTED_UNREADABLE, read.reason)

    def test_a_store_fault_defers_rather_than_rejecting(self):
        # Review finding #16. A HEAD or GET that raises says nothing about the
        # attempt — the record may be sitting there intact. Rejecting on it
        # published a lossy authoritative record and terminalized the row.
        put_record(self.store, self.key, application_record())

        def explode(_key):
            raise RuntimeError("AccessDenied")

        self.store.head = explode

        read = closure.read_predecessor(self.store, self.key, 1)

        self.assertIsNone(read.body)
        self.assertTrue(read.deferred)
        self.assertEqual(closure.DEFERRED_STORE_FAULT, read.reason)

    def test_a_get_fault_defers_too(self):
        put_record(self.store, self.key, application_record())

        def explode(_key):
            raise RuntimeError("connection reset")

        self.store.get = explode

        read = closure.read_predecessor(self.store, self.key, 1)

        self.assertTrue(read.deferred)


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

    def test_the_same_classification_rebuilds_byte_identically(self):
        # The record store is create-once, so a replayed lease must re-derive
        # the SAME object or the conditional put fails as "already exists with
        # different content". Found live: a wall-clock `reconciled_at` made
        # every replay a different object and turned idempotence into an
        # error. Two builds an hour apart must still be identical.
        first = closure.build_closure_record(
            self.row, None, sequence=1, predecessor=None,
            classification="never_resolved", now=utc(2026, 8, 6, 12, 0, 0))
        second = closure.build_closure_record(
            self.row, None, sequence=1, predecessor=None,
            classification="never_resolved", now=utc(2026, 8, 6, 13, 0, 0))

        self.assertEqual(first.to_bytes(), second.to_bytes())

    def test_republishing_after_a_later_build_still_dedupes(self):
        first = closure.build_closure_record(
            self.row, None, sequence=1, predecessor=None,
            classification="never_resolved", now=utc(2026, 8, 6, 12, 0, 0))
        closure.publish_closure_record(self.store, PREFIX, self.row, first)

        later = closure.build_closure_record(
            self.row, None, sequence=1, predecessor=None,
            classification="never_resolved", now=utc(2026, 8, 6, 14, 0, 0))
        result = closure.publish_closure_record(
            self.store, PREFIX, self.row, later)

        self.assertFalse(result.created)

    def test_a_divergent_existing_record_is_superseded_not_overwritten(self):
        # Records are immutable. When a sequence already holds a DIFFERENT
        # account — an older reconciler's, or one built before a fix — the
        # new account goes to the next free sequence rather than overwriting.
        # Every record is a complete snapshot, so the highest sequence is
        # still the full terminal account.
        stale = closure.build_closure_record(
            self.row, None, sequence=1, predecessor=None,
            classification="never_resolved")
        stale.body["a_stale_field"] = "from an older build"
        closure.publish_closure_record(self.store, PREFIX, self.row, stale)

        fresh = closure.build_closure_record(
            self.row, None, sequence=1, predecessor=None,
            classification="never_resolved")
        result = closure.publish_closure_record(
            self.store, PREFIX, self.row, fresh)

        self.assertTrue(result.created)
        self.assertTrue(result.key.endswith("seq-0002.json"))
        # The stale record was left exactly as it was.
        stale_body = json.loads(self.store.get(
            "attempts/records/run-1/90000_1/attempt-1/seq-0001.json"))
        self.assertEqual("from an older build", stale_body["a_stale_field"])

    def test_supersession_also_fires_on_the_S3_stores_error_shape(self):
        # The two stores report divergence differently: InMemoryObjectStore
        # attaches existing/new checksums, S3ObjectStore attaches only key and
        # bucket and says it in the message. Matching on the details alone
        # passed the suite and did nothing in production — this pins the real
        # S3 shape so that cannot recur.
        from pipeline.runtime.errors import StorageError

        class S3Shaped:
            def __init__(self):
                self.keys = []

            def put_if_absent(self, key, body, content_type=None):
                self.keys.append(key)
                if key.endswith("seq-0001.json"):
                    raise StorageError(
                        f"object s3://bucket/{key} already exists with "
                        f"different content: two writers under one attempt "
                        f"identity", key=key, bucket="bucket")
                from pipeline.runtime.boundaries import PutResult
                return PutResult(key=key, checksum="x", created=True, size=1)

        store = S3Shaped()
        record = closure.build_closure_record(
            self.row, None, sequence=1, predecessor=None,
            classification="never_resolved")

        result = closure.publish_closure_record(store, PREFIX, self.row, record)

        self.assertTrue(result.key.endswith("seq-0002.json"))

    def test_the_supersession_climb_is_bounded(self):
        class AlwaysDivergent:
            def put_if_absent(self, key, body, content_type=None):
                from pipeline.runtime.errors import StorageError
                raise StorageError("already exists with different content",
                                   key=key, existing_checksum="a",
                                   new_checksum="b")

        record = closure.build_closure_record(
            self.row, None, sequence=1, predecessor=None,
            classification="never_resolved")

        with self.assertRaises(Exception) as caught:
            closure.publish_closure_record(
                AlwaysDivergent(), PREFIX, self.row, record)
        self.assertIn("all", str(caught.exception).lower())

    def test_the_body_round_trips_through_the_store(self):
        record = closure.build_closure_record(
            self.row, None, sequence=1, predecessor=None,
            classification="never_resolved")
        result = closure.publish_closure_record(
            self.store, PREFIX, self.row, record)

        raw = self.store.get(result.key)
        self.assertEqual(checksum(raw), result.checksum)
        self.assertEqual(1, json.loads(raw)["record_sequence"])

    def test_a_superseded_record_declares_the_sequence_it_landed_at(self):
        # REVIEW FINDING #15. The body was serialized ONCE before the climb
        # loop, so when sequence 1 already held different bytes the new
        # account was written at the sequence-2 KEY while its
        # `record_sequence` field still said 1 — and the row stored the stale
        # sequence too. A consumer selecting "the highest sequence" would read
        # a record that says it is a lower one.
        occupied = closure.build_closure_record(
            self.row, None, sequence=1, predecessor=None,
            classification="never_resolved")
        closure.publish_closure_record(self.store, PREFIX, self.row, occupied)

        # A DIFFERENT account for the same attempt at the same sequence.
        superseding = closure.build_closure_record(
            self.row, None, sequence=1, predecessor=None,
            classification="abrupt_loss", error_category="scheduler_reclaimed")
        result = closure.publish_closure_record(
            self.store, PREFIX, self.row, superseding)

        self.assertEqual(2, result.sequence)
        self.assertIn("seq-0002", result.key)
        body = json.loads(self.store.get(result.key))
        self.assertEqual(2, body["record_sequence"],
                         "the record must declare the sequence it is stored "
                         "at, not the one it was built for")

    def test_the_original_record_at_the_lower_sequence_is_untouched(self):
        # Records are immutable: superseding publishes alongside, never over.
        first = closure.build_closure_record(
            self.row, None, sequence=1, predecessor=None,
            classification="never_resolved")
        first_result = closure.publish_closure_record(
            self.store, PREFIX, self.row, first)

        second = closure.build_closure_record(
            self.row, None, sequence=1, predecessor=None,
            classification="abrupt_loss", error_category="scheduler_reclaimed")
        closure.publish_closure_record(self.store, PREFIX, self.row, second)

        kept = json.loads(self.store.get(first_result.key))
        self.assertEqual(1, kept["record_sequence"])
        self.assertEqual("never_resolved",
                         kept["reconciliation_classification"])


class ReadAttemptStagesTests(unittest.TestCase):
    """The stage read must name columns `attempt_stages` actually has.

    W9 ramp, live: the query selected `error_category`, which that table has
    never had — it lives on `attempts` and on `attempt_error_categories`. Two
    consequences, and the second is the worse one:

    1. Every reconciliation of a started attempt failed, so 36 attempts stayed
       open and the service reached 4 consecutive unproductive polls against a
       health threshold of 5.
    2. The `except` around the query looked like it made the failure safe. It
       does not: PostgreSQL aborts the WHOLE transaction on any statement
       error, so every later statement in the same cycle raised
       `InFailedSqlTransaction`. One real error became thirty-six misleading
       ones — the same shape as the numpy-repr defect in gathering, where a
       single bad bind made a whole pass report "no work found".

    There was no coverage of this function at all, which is how a column name
    that never existed survived to be found by a live run.
    """

    # Migration 011's column list, which is what the live table has.
    ACTUAL_COLUMNS = {"stage_record_id", "attempt_id", "stage_name",
                      "started_at", "duration_ms", "outcome"}

    class _Cursor:
        def __init__(self, owner):
            self.owner = owner
            self.description = [("stage_name",), ("outcome",),
                                ("started_at",), ("duration_ms",)]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            self.owner.statements.append(sql)
            if sql.strip().upper().startswith(("SAVEPOINT", "ROLLBACK",
                                               "RELEASE")):
                return
            self.owner.query = sql

        def fetchall(self):
            return [("build_reference_image", "success", None, 145310.0)]

    class _Conn:
        def __init__(self):
            self.statements = []
            self.query = None

        def cursor(self):
            return ReadAttemptStagesTests._Cursor(self)

    def test_it_selects_only_columns_the_table_has(self):
        conn = self._Conn()
        closure.read_attempt_stages(conn, 158)

        self.assertIsNotNone(conn.query)
        select = conn.query.split("FROM")[0]
        named = {token.strip().strip(",")
                 for token in select.replace("SELECT", "").split()
                 if token.strip().strip(",")}
        unknown = named - self.ACTUAL_COLUMNS
        self.assertEqual(set(), unknown,
                         "the stage read names columns attempt_stages "
                         "does not have")

    def test_it_does_not_select_error_category(self):
        """The specific column, named, because this is the live regression."""
        conn = self._Conn()
        closure.read_attempt_stages(conn, 158)
        self.assertNotIn("error_category", conn.query)

    def test_a_failing_read_rolls_back_to_a_savepoint(self):
        """The caught exception must not leave the transaction aborted."""
        class Failing(self._Conn):
            def cursor(self):
                cursor = ReadAttemptStagesTests._Cursor(self)
                original = cursor.execute

                def execute(sql, params=None):
                    original(sql, params)
                    if sql.strip().upper().startswith("SELECT"):
                        raise RuntimeError("column does not exist")
                cursor.execute = execute
                return cursor

        conn = Failing()
        self.assertIsNone(closure.read_attempt_stages(conn, 158))
        joined = " ".join(conn.statements).upper()
        self.assertIn("SAVEPOINT", joined)
        self.assertIn("ROLLBACK TO SAVEPOINT", joined)

    def test_a_successful_read_releases_its_savepoint(self):
        conn = self._Conn()
        rows = closure.read_attempt_stages(conn, 158)

        self.assertEqual(1, len(rows))
        self.assertEqual("build_reference_image", rows[0]["stage_name"])
        joined = " ".join(conn.statements).upper()
        self.assertIn("RELEASE SAVEPOINT", joined)
        self.assertNotIn("ROLLBACK", joined)

    def test_absence_and_failure_are_still_distinct(self):
        """None means the read failed; [] means the attempt recorded none."""
        class Empty(self._Conn):
            def cursor(self):
                cursor = ReadAttemptStagesTests._Cursor(self)
                cursor.fetchall = lambda: []
                return cursor

        self.assertEqual([], closure.read_attempt_stages(Empty(), 158))
        self.assertIsNone(closure.read_attempt_stages(None, 158))


if __name__ == "__main__":
    unittest.main()
