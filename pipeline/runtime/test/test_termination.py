"""
File:    test_termination.py

The termination protocol, and crash-at-every-boundary recovery.

The deliverable's hardest requirement, stated in the proposal as: "Crash-at-
every-boundary recovery must be unit-proven: for each step boundary, simulate
death and assert the resulting state is exactly one of the design's legal
recovery states."

`TestCrashAtEveryBoundary` is that proof. It enumerates the protocol's step
boundaries, kills the process at each one (by raising from the `on_step` hook,
which fires BEFORE the named step), and asserts the resulting
(database state, S3 objects present) pair against the design's table of legal
recovery states. A crash that produced a state outside the table would be a
state no reconciler rule covers — which is the failure mode the ordering
exists to prevent, and the reason "we wrote the steps in that order" is not by
itself evidence.

The state table, from design/observability.md and the proposal's Termination
protocol section:

    crash before build_bundle          -> started row, no bundle, no record
    crash before upload_bundle         -> started row, no bundle, no record
    crash before write_terminal_record -> started row, bundle present, no record
    crash before mark_application_closed -> started row, VALID record present
    crash after mark_application_closed  -> application_closed row, record present

The fourth is the one the ordering was chosen for: the record is authoritative
and the reconciler materializes the row transition from it. The unrecoverable
inverse — a row citing a record that does not exist — is proven impossible by
`test_row_never_cites_a_record_that_does_not_exist`.
"""

import datetime
import decimal
import json
import os
import shutil
import tarfile
import tempfile
import unittest

from observability.attempts import AttemptWriter, LifecycleState
from pipeline.runtime import termination
from pipeline.runtime.boundaries import InMemoryObjectStore, checksum
from pipeline.runtime.errors import RecordsError, StorageError, ToolError
from pipeline.runtime.errors import serialize_error
from pipeline.runtime.test.stubs import (
    RecordingExecutor,
    make_job_environment,
    make_ownership,
    make_provenance,
)
from pipeline.runtime.workdir import WorkingDirectory

PREFIX = "records"


class _Harness:
    """One attempt, wired end to end, with every boundary substituted.

    Built as a plain helper rather than a fixture base class so each test can
    say exactly which parts it wants and a reader can see the whole setup in
    one place.
    """

    def __init__(self, tmpdir, digest="d" * 64):
        self.executor = RecordingExecutor()
        self.writer = AttemptWriter(self.executor)
        self.store = InMemoryObjectStore()
        self.ownership = make_ownership()
        self.job_env = make_job_environment()
        self.workdir = WorkingDirectory.create("job-abc123-attempt-1",
                                               work_root=tmpdir)
        self.digest = digest
        self.provenance = make_provenance(config_digest=digest)
        self.started_at = datetime.datetime(2026, 8, 6, 3, 0, 0,
                                            tzinfo=datetime.timezone.utc)
        self.snapshot_key = termination.snapshot_key(PREFIX, digest)

        # The attempt exists and is started — every termination test begins
        # from there, because termination is by definition what a started
        # attempt does.
        self.executor.rows[self.ownership.attempt_id] = {
            "attempt_id": self.ownership.attempt_id,
            "logical_job_id": self.ownership.logical_job_id,
            "lifecycle_state": LifecycleState.STARTED.value,
        }

        # One stage log, so the bundle has a member to carry.
        with open(self.workdir.stage_log_path("difference"), "w",
                  encoding="utf-8") as handle:
            handle.write("stage log line\n")

    def terminate(self, outcome="success", disposition="published",
                  error=None, on_step=None, science_provenance=None,
                  products=None):
        return termination.terminate(
            self.writer, self.store, self.ownership, self.job_env,
            self.workdir, PREFIX,
            outcome=outcome, product_disposition=disposition,
            started_at=self.started_at, config_digest=self.digest,
            snapshot_key_value=self.snapshot_key,
            stages=[{"stage_name": "difference", "outcome": "success",
                     "duration_ms": 12.5}],
            provenance=self.provenance, error=error, on_step=on_step,
            science_provenance=science_provenance, products=products)

    @property
    def bundle_key(self):
        return termination.bundle_key(PREFIX, self.ownership.run_id,
                                      self.ownership.logical_job_id,
                                      self.ownership.attempt_id)

    @property
    def record_key(self):
        return termination.terminal_record_key(
            PREFIX, self.ownership.run_id, self.ownership.logical_job_id,
            self.ownership.attempt_id, 0)

    def state(self):
        return self.executor.state_of(self.ownership.attempt_id)

    def has(self, key):
        return key in self.store.objects


class TestKeyDerivation(unittest.TestCase):
    """Keys derive from immutable attempt identity, and say nothing else."""

    def test_bundle_key_is_classification_neutral(self):
        """No success/ or failure/ segment anywhere in a bundle key.

        The third ratification amendment replaced classification-carrying key
        prefixes with a reconciler-stamped object tag, precisely so a
        reclassification retags instead of stranding the bundle at a key that
        says the wrong thing. A key that encoded the class would reintroduce
        the problem the amendment removed.
        """
        key = termination.bundle_key(PREFIX, "run-1", "job-1", 42)
        self.assertNotIn("success", key)
        self.assertNotIn("failure", key)
        self.assertIn("attempt-42", key)

    def test_bundle_key_is_derivable_from_identity_alone(self):
        """Same identity, same key — which is how the reconciler finds it."""
        first = termination.bundle_key(PREFIX, "run-1", "job-1", 42)
        second = termination.bundle_key(PREFIX, "run-1", "job-1", 42)
        self.assertEqual(first, second)

    def test_record_key_carries_a_zero_padded_sequence(self):
        """Lexical order is numeric order, so listing gives sequence order."""
        zero = termination.terminal_record_key(PREFIX, "r", "j", 1, 0)
        ten = termination.terminal_record_key(PREFIX, "r", "j", 1, 10)
        self.assertIn("seq-0000", zero)
        self.assertIn("seq-0010", ten)
        self.assertLess(zero, ten)

    def test_record_sequence_cannot_be_negative(self):
        with self.assertRaises(ValueError):
            termination.terminal_record_key(PREFIX, "r", "j", 1, -1)

    def test_a_decimal_sequence_from_the_database_keys_the_same_object(self):
        """`attempts.terminal_record_sequence` is a numeric column and psycopg2
        hands numerics back as `Decimal`, which passes the negativity check and
        then fails `:04d` with "invalid format string". Same family as the
        Decimal defect already fixed in `ClosureRecord.to_bytes`."""
        import decimal

        self.assertEqual(
            termination.terminal_record_key(PREFIX, "r", "j", 1, 2),
            termination.terminal_record_key(PREFIX, "r", "j", 1,
                                            decimal.Decimal("2")))

    def test_snapshot_key_is_content_addressed(self):
        """Keyed by digest, not by attempt — identical config dedupes."""
        digest = "a" * 64
        self.assertIn(digest, termination.snapshot_key(PREFIX, digest))
        self.assertEqual(termination.snapshot_key(PREFIX, digest),
                         termination.snapshot_key(PREFIX, digest))

    def test_unsafe_identity_components_are_sanitized(self):
        key = termination.bundle_key(PREFIX, "run/../1", "job 1", "4/2")
        self.assertNotIn("..", key)
        self.assertNotIn(" ", key)


class TestConfigurationSnapshot(unittest.TestCase):

    def setUp(self):
        self.store = InMemoryObjectStore()

    def test_canonical_bytes_are_order_independent(self):
        """Two processes resolving the same config must produce one object.

        If serialization depended on dict insertion order, content-addressing
        would degenerate into one snapshot per process and the digest would
        stop identifying the configuration.
        """
        first, digest_a = termination.canonical_snapshot_bytes(
            {"b": 2, "a": 1})
        second, digest_b = termination.canonical_snapshot_bytes(
            {"a": 1, "b": 2})
        self.assertEqual(first, second)
        self.assertEqual(digest_a, digest_b)

    def test_persist_creates_once_and_dedupes(self):
        config = {"pipeline": {"threads": 4}}
        digest, key, created = termination.persist_configuration_snapshot(
            self.store, PREFIX, config)
        self.assertTrue(created)

        digest2, key2, created2 = termination.persist_configuration_snapshot(
            self.store, PREFIX, config)
        self.assertEqual(digest, digest2)
        self.assertEqual(key, key2)
        self.assertFalse(created2, "an identical snapshot must dedupe, not "
                                   "create a second object")

    def test_persisted_bytes_verify_against_the_digest(self):
        """A reader can prove the object is the configuration claimed."""
        digest, key, _ = termination.persist_configuration_snapshot(
            self.store, PREFIX, {"a": 1})
        self.assertEqual(checksum(self.store.get(key)), digest)

    def test_snapshot_failure_is_a_records_error_before_any_work(self):
        """Failing here is the cheap failure — nothing has run yet."""
        body, digest = termination.canonical_snapshot_bytes({"a": 1})
        self.store.fail_on_put.add(termination.snapshot_key(PREFIX, digest))
        with self.assertRaises(RecordsError) as caught:
            termination.persist_configuration_snapshot(self.store, PREFIX,
                                                       {"a": 1})
        self.assertEqual(caught.exception.error_category, "records_error")


class TestStartAttempt(unittest.TestCase):

    def setUp(self):
        self.executor = RecordingExecutor()
        self.writer = AttemptWriter(self.executor)
        self.executor.rows[1000] = {"attempt_id": 1000,
                                    "lifecycle_state": "submitted"}

    def test_binding_is_one_write_with_the_started_transition(self):
        """No bound-but-unpersisted or worked-but-unbound state can exist.

        The design makes the digest binding part of the same compare-and-set
        that marks the attempt started, so there is exactly one write and
        therefore no intermediate state between them to recover from.
        """
        result = termination.start_attempt(
            self.writer, 1000, make_provenance(), "d" * 64, "snap/key")
        self.assertEqual(self.executor.state_of(1000),
                         LifecycleState.STARTED.value)
        self.assertEqual(result.config_digest, "d" * 64)

        updates = self.executor.statements_matching("SET lifecycle_state")
        self.assertEqual(len(updates), 1,
                         "the start and the binding are one statement")
        self.assertIn("config_digest", updates[0][0])

    def test_a_digest_mismatch_refuses_to_bind(self):
        """The row must never bind a digest whose object is not what was read."""
        with self.assertRaises(RecordsError):
            termination.start_attempt(
                self.writer, 1000, make_provenance(config_digest="x" * 64),
                "d" * 64, "snap/key")

    def test_a_failed_start_is_a_records_error(self):
        self.executor.missing_attempt_ids.add(1000)
        with self.assertRaises(RecordsError):
            termination.start_attempt(self.writer, 1000, make_provenance(),
                                      "d" * 64, "snap/key")


class TestBundle(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workdir = WorkingDirectory.create("attempt-1",
                                               work_root=self._tmp.name)

    def test_bundle_contains_the_staged_members(self):
        with open(self.workdir.stage_log_path("calibrate"), "w",
                  encoding="utf-8") as handle:
            handle.write("hello\n")
        body = termination.build_bundle(self.workdir.bundle_dir)
        with tarfile.open(fileobj=__import__("io").BytesIO(body), mode="r:gz") as tar:
            names = tar.getnames()
        self.assertIn("stage-logs/calibrate.log", names)

    def test_bundle_is_deterministic_across_rebuilds(self):
        """A rebuild after a crash must not differ merely because time passed.

        The conditional create compares content: if a rebuild produced
        different bytes for the same evidence, a replay would look like two
        attempts colliding under one identity rather than the same attempt
        retrying.
        """
        with open(self.workdir.stage_log_path("calibrate"), "w",
                  encoding="utf-8") as handle:
            handle.write("hello\n")
        first = termination.build_bundle(self.workdir.bundle_dir)
        second = termination.build_bundle(self.workdir.bundle_dir)
        self.assertEqual(checksum(first), checksum(second))

    def test_missing_staging_directory_raises(self):
        with self.assertRaises(StorageError):
            termination.build_bundle(os.path.join(self._tmp.name, "nope"))

    def test_build_bundle_leaves_no_spool_file_behind(self):
        """`build_bundle` spools the tar to a temp file beside `bundle_dir`
        while building it (finding 17: an unbounded `BytesIO` would hold the
        whole archive in memory during the walk, at exactly the moment a
        crashing attempt is trying to record its own failure). The spool
        must not survive the call, on success or on failure.
        """
        with open(self.workdir.stage_log_path("calibrate"), "w",
                  encoding="utf-8") as handle:
            handle.write("hello\n")
        parent = os.path.dirname(os.path.normpath(self.workdir.bundle_dir))
        before = set(os.listdir(parent))
        termination.build_bundle(self.workdir.bundle_dir)
        after = set(os.listdir(parent))
        self.assertEqual(before, after)

    def test_build_bundle_leaves_no_spool_file_on_a_missing_directory(self):
        parent = self._tmp.name
        before = set(os.listdir(parent))
        with self.assertRaises(StorageError):
            termination.build_bundle(os.path.join(parent, "nope"))
        after = set(os.listdir(parent))
        self.assertEqual(before, after)

    def test_replayed_upload_is_accepted_not_a_collision(self):
        store = InMemoryObjectStore()
        body = b"bundle-bytes"
        first = termination.upload_bundle(store, "k", body)
        second = termination.upload_bundle(store, "k", body)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["checksum"], second["checksum"])

    def test_a_replayed_upload_keeps_the_stored_bundle(self):
        """Exactly one bundle per attempt, and the first one written wins.

        A retry that got further before dying has more stage logs, so its
        rebuilt bundle differs byte-wise from the first upload. That is the
        ordinary crash-and-retry case, not a collision: the key derives from
        immutable attempt identity, so only this attempt can be writing here.
        The stored object is kept and its checksum returned, because a
        terminal record already written may already cite it.
        """
        store = InMemoryObjectStore()
        first = termination.upload_bundle(store, "k", b"one")
        second = termination.upload_bundle(store, "k", b"two-is-longer")

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["checksum"], first["checksum"],
                         "the checksum returned must be the STORED bundle's, "
                         "since a record may already cite it")
        self.assertEqual(store.get("k"), b"one")


class TestTerminalRecord(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.harness = _Harness(self._tmp.name)

    def test_record_is_self_contained(self):
        """One object answers "what did this attempt do" with no joins."""
        self.harness.terminate()
        record = json.loads(self.harness.store.get(self.harness.record_key))
        for field in ("run_id", "logical_job_id", "attempt_id",
                      "scheduler_job_id", "rapid_outcome",
                      "product_disposition", "application_intended_exit",
                      "config_digest", "config_snapshot_key", "bundle_key",
                      "bundle_checksum", "stages", "provenance"):
            self.assertIn(field, record, f"{field} missing from the record")

    def test_record_cites_the_bundle_by_checksum(self):
        """Presence is not validation — the reconciler checks the checksum."""
        result = self.harness.terminate()
        record = json.loads(self.harness.store.get(self.harness.record_key))
        self.assertEqual(record["bundle_checksum"], result.bundle_checksum)
        self.assertEqual(
            record["bundle_checksum"],
            checksum(self.harness.store.get(self.harness.bundle_key)))

    def test_application_always_writes_sequence_zero(self):
        """Only the reconciler writes higher; that is what makes supersession
        deterministic."""
        result = self.harness.terminate()
        self.assertEqual(result.record_sequence, 0)
        record = json.loads(self.harness.store.get(self.harness.record_key))
        self.assertEqual(record["record_sequence"], 0)
        self.assertEqual(record["record_author"], "application")

    def test_a_failed_attempt_records_its_category_and_still_intends_exit_zero(self):
        """Scheduler-SUCCEEDED with application-failure is the representable
        combination the schema was built for.

        The whole fail-loud posture rests on this: a classified failure is
        recorded and exits cleanly, so a nonzero exit stays reserved for the
        unrecordable.
        """
        error = serialize_error(ToolError("sfft exited 1", returncode=1),
                                redactor=None)
        result = self.harness.terminate(outcome="failure",
                                        disposition="none", error=error)
        self.assertEqual(result.intended_exit, 0)
        self.assertEqual(result.error_category, "tool_failure")

        record = json.loads(self.harness.store.get(self.harness.record_key))
        self.assertEqual(record["error_category"], "tool_failure")
        self.assertEqual(record["rapid_outcome"], "failure")
        self.assertEqual(record["application_intended_exit"], 0)

    def test_a_successful_record_carries_no_error_category(self):
        """Absent, not null-valued — fields a state has not reached are absent."""
        self.harness.terminate()
        record = json.loads(self.harness.store.get(self.harness.record_key))
        self.assertNotIn("error_category", record)
        self.assertNotIn("error", record)


class TestCrashAtEveryBoundary(unittest.TestCase):
    """Kill the process at each step boundary; assert a legal recovery state.

    See the module docstring for the state table this enumerates. Each test
    asserts the FULL resulting state — database lifecycle plus which S3
    objects exist — because a recovery rule keys on the combination, and
    asserting only one half would pass for states the design does not cover.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.harness = _Harness(self._tmp.name)

    def _die_before(self, step):
        def hook(name):
            if name == step:
                raise _SimulatedDeath(f"killed before {step}")
        return hook

    def test_crash_before_building_the_bundle(self):
        """Legal state: started row, no bundle, no record.

        The reconciler's case: a started attempt with neither artifact. It
        builds the bundle from the stream and writes a reconciler-first
        record.
        """
        with self.assertRaises(_SimulatedDeath):
            self.harness.terminate(on_step=self._die_before("build_bundle"))

        self.assertEqual(self.harness.state(), LifecycleState.STARTED.value)
        self.assertFalse(self.harness.has(self.harness.bundle_key))
        self.assertFalse(self.harness.has(self.harness.record_key))

    def test_crash_before_uploading_the_bundle(self):
        """Legal state: started row, no bundle, no record.

        Same recovery as the previous boundary — the bundle exists only as
        local bytes, which die with the container.
        """
        with self.assertRaises(_SimulatedDeath):
            self.harness.terminate(on_step=self._die_before("upload_bundle"))

        self.assertEqual(self.harness.state(), LifecycleState.STARTED.value)
        self.assertFalse(self.harness.has(self.harness.bundle_key))
        self.assertFalse(self.harness.has(self.harness.record_key))

    def test_crash_before_writing_the_terminal_record(self):
        """Legal state: started row, bundle PRESENT, no record.

        The reconciler validates the bundle by key and checksum — never by
        presence — and writes a reconciler-first record citing it. The bundle
        is not rebuilt: it is already there and valid.
        """
        with self.assertRaises(_SimulatedDeath):
            self.harness.terminate(
                on_step=self._die_before("write_terminal_record"))

        self.assertEqual(self.harness.state(), LifecycleState.STARTED.value)
        self.assertTrue(self.harness.has(self.harness.bundle_key))
        self.assertFalse(self.harness.has(self.harness.record_key))

    def test_crash_before_the_application_closed_cas(self):
        """Legal state: started row beside a VALID terminal record.

        THE case the ordering was chosen for. The record is the authoritative
        application account, so the reconciler materializes the
        application-closed transition from it — values verbatim, row marked
        reconciler-materialized. Writing the row first would produce the
        unrecoverable inverse instead.
        """
        with self.assertRaises(_SimulatedDeath):
            self.harness.terminate(
                on_step=self._die_before("mark_application_closed"))

        self.assertEqual(self.harness.state(), LifecycleState.STARTED.value)
        self.assertTrue(self.harness.has(self.harness.bundle_key))
        self.assertTrue(self.harness.has(self.harness.record_key))

        # And the record left behind is complete and valid — not a partial
        # write the reconciler would have to reject.
        record = json.loads(self.harness.store.get(self.harness.record_key))
        self.assertEqual(record["attempt_id"], self.harness.ownership.attempt_id)
        self.assertEqual(record["bundle_checksum"],
                         checksum(self.harness.store.get(self.harness.bundle_key)))

    def test_crash_after_the_application_closed_cas(self):
        """Legal state: application_closed row, record present.

        The normal successful path's end state, before the reconciler adds
        the scheduler-observed facts. Reached by running the protocol to
        completion — the process then dies on its way to exit, which changes
        nothing that was already durable.
        """
        self.harness.terminate()
        self.assertEqual(self.harness.state(),
                         LifecycleState.APPLICATION_CLOSED.value)
        self.assertTrue(self.harness.has(self.harness.bundle_key))
        self.assertTrue(self.harness.has(self.harness.record_key))

    def test_row_never_cites_a_record_that_does_not_exist(self):
        """The unrecoverable inverse is structurally impossible.

        There is no crash point that leaves an application_closed row citing
        a missing record, because the record write strictly precedes the row
        transition. Proven by exhaustion over every boundary: wherever death
        occurs, if the row is application_closed then the record it cites
        exists.
        """
        boundaries = ["build_bundle", "upload_bundle", "write_terminal_record",
                      "mark_application_closed"]
        for step in boundaries:
            with self.subTest(step=step):
                harness = _Harness(self._tmp.name + f"/{step}")
                with self.assertRaises(_SimulatedDeath):
                    harness.terminate(on_step=self._die_before(step))
                if harness.state() == LifecycleState.APPLICATION_CLOSED.value:
                    self.assertTrue(
                        harness.has(harness.record_key),
                        f"death before {step} left a closed row citing a "
                        f"record that was never written")

    def test_store_unreachable_at_the_bundle_step_raises_records_error(self):
        """A records-path failure is not swallowed into a clean exit."""
        self.harness.store.fail_on_put.add(self.harness.bundle_key)
        with self.assertRaises(RecordsError) as caught:
            self.harness.terminate()
        self.assertEqual(caught.exception.error_category, "records_error")
        self.assertEqual(self.harness.state(), LifecycleState.STARTED.value)

    def test_store_unreachable_at_the_record_step_raises_records_error(self):
        self.harness.store.fail_on_put.add(self.harness.record_key)
        with self.assertRaises(RecordsError):
            self.harness.terminate()
        self.assertEqual(self.harness.state(), LifecycleState.STARTED.value)
        self.assertTrue(self.harness.has(self.harness.bundle_key))

    def test_database_unreachable_at_the_closing_cas_leaves_a_valid_record(self):
        """The record is durable; only the row transition failed.

        The process still exits nonzero — it could not confirm closure — but
        the state it leaves is the recoverable one, and the raised error names
        the record so an operator does not go looking for it.
        """
        self.harness.executor.fail_on["lifecycle_state = %s, ended_at"] = \
            RuntimeError("connection lost")
        with self.assertRaises(RecordsError) as caught:
            self.harness.terminate()

        self.assertTrue(self.harness.has(self.harness.record_key))
        self.assertIn(self.harness.record_key, str(caught.exception))
        self.assertEqual(self.harness.state(), LifecycleState.STARTED.value)


class TestReplayIsIdempotent(unittest.TestCase):
    """Replaying the whole protocol after a crash re-derives, never overwrites."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_full_replay_after_a_record_step_crash(self):
        """The rerun validates what is there and completes what is missing.

        The realistic replay: the first run wrote both artifacts and died
        before the closing transition. The second run must NOT mutate either
        artifact — a terminal record is immutable, and a correction is a
        superseding record only the reconciler writes — and must complete the
        one step that is missing.

        Note the second run's record would not be byte-identical if it were
        rebuilt and written: its `ended_at` is a later moment. That is exactly
        why the protocol validates and keeps rather than comparing content and
        raising; treating this as a collision would turn every crash-and-retry
        into a false "two writers under one identity".
        """
        harness = _Harness(self._tmp.name)
        with self.assertRaises(_SimulatedDeath):
            harness.terminate(on_step=_die_at("mark_application_closed"))

        bundle_before = harness.store.objects[harness.bundle_key]["checksum"]
        record_before = harness.store.objects[harness.record_key]["checksum"]

        # A second runtime, same attempt identity, a later clock.
        result = harness.terminate()

        self.assertEqual(harness.store.objects[harness.bundle_key]["checksum"],
                         bundle_before, "the bundle must not be replaced")
        self.assertEqual(harness.store.objects[harness.record_key]["checksum"],
                         record_before, "a terminal record is immutable")
        self.assertEqual(result.record_key, harness.record_key)
        self.assertEqual(result.record_checksum, record_before,
                         "the row must cite the checksum of the record that "
                         "is actually stored")
        self.assertEqual(harness.state(),
                         LifecycleState.APPLICATION_CLOSED.value)

    def test_a_foreign_record_at_this_key_is_a_collision(self):
        """Validation is by identity, not by presence.

        Keeping whatever is there would be wrong if it belonged to a
        different attempt — that would mean two attempts derived one key, and
        this attempt would cite an account of someone else's work.
        """
        harness = _Harness(self._tmp.name)
        foreign = json.dumps({"attempt_id": 999999, "record_sequence": 0}
                             ).encode("utf-8")
        harness.store.put_if_absent(harness.record_key, foreign)

        with self.assertRaises(RecordsError) as caught:
            harness.terminate()
        self.assertIn("two attempts derived one record key",
                      str(caught.exception))

    def test_an_unparseable_object_at_the_record_key_raises(self):
        """Not a record, so nothing here can be validated or trusted."""
        harness = _Harness(self._tmp.name)
        harness.store.put_if_absent(harness.record_key, b"\xff\xfenot json")
        with self.assertRaises(RecordsError):
            harness.terminate()


def _die_at(step):
    def hook(name):
        if name == step:
            raise _SimulatedDeath(f"killed before {step}")
    return hook


class _SimulatedDeath(BaseException):
    """Stands in for the process dying.

    Derived from `BaseException`, not `Exception`, deliberately: the protocol
    must not be able to catch it and turn a simulated death into a handled
    error. If a step ever grew a bare `except Exception`, this class is what
    would keep the crash tests honest.
    """


if __name__ == "__main__":
    unittest.main()


class TerminalRecordAndBundleStoresTest(unittest.TestCase):
    """The two artifacts go to two stores.

    Regression for the first W5 canary (2026-08-06): the entrypoint passed
    only `store`, so the terminal record was written into the DIAGNOSTICS
    bucket under the right key. The log line said "terminal record
    attempts/records/.../seq-0000.json (written)" and was true — the key was
    correct and the object existed. Only the BUCKET was wrong, which no key
    assertion could see, and which the live canary found by listing both.

    They are different buckets because their lifecycles differ: a bundle
    expires on a reconciled retention class, a terminal record is provenance
    kept at least product lifetime. One bucket would force one policy on both.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def test_record_and_bundle_go_to_their_own_stores(self):
        harness = _Harness(self.tmpdir)
        records = InMemoryObjectStore()

        result = termination.terminate(
            harness.writer, harness.store, harness.ownership, harness.job_env,
            harness.workdir, PREFIX,
            outcome="success", product_disposition="published",
            started_at=harness.started_at, config_digest=harness.digest,
            snapshot_key_value=harness.snapshot_key, stages=[],
            provenance=harness.provenance, record_store=records)

        self.assertIsNotNone(harness.store.head(result.bundle_key))
        self.assertIsNone(
            harness.store.head(result.record_key),
            "the terminal record must not land in the diagnostics store")
        self.assertIsNotNone(records.head(result.record_key))
        self.assertIsNone(
            records.head(result.bundle_key),
            "the bundle must not land in the records store")

    def test_one_store_still_serves_both(self):
        # The default keeps every single-store caller working, which is what
        # every other test in this module is.
        harness = _Harness(self.tmpdir)
        result = harness.terminate()
        self.assertIsNotNone(harness.store.head(result.bundle_key))
        self.assertIsNotNone(harness.store.head(result.record_key))


class ScienceProvenanceAndProductsTests(unittest.TestCase):
    """The stages' own account reaches the record (implementation review #6).

    Stages accumulate checksums, source counts and product facts into
    `StageContext`, and the entrypoint passed only the runtime `Provenance` —
    so files were uploaded but sequence 0 carried no authoritative product
    list, no URIs, no checksums and no input or reference identities. A
    registration callback cannot register from a record that lacks them; it
    would have to guess from mutable external state, which is what the record
    exists to replace.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.harness = _Harness(self._tmp.name)

    def test_the_product_list_reaches_the_record(self):
        self.harness.terminate(products={
            "difference_image": {"uri": "s3://p/diff.fits",
                                 "checksum": "sha256:diff"},
            "psf_catalog": {"uri": "s3://p/psfcat.parquet",
                            "checksum": "sha256:psf"},
        })
        record = json.loads(self.harness.store.get(self.harness.record_key))

        self.assertIn("products", record)
        names = [entry["name"] for entry in record["products"]]
        self.assertEqual(["difference_image", "psf_catalog"], names)
        first = record["products"][0]
        self.assertEqual("s3://p/diff.fits", first["uri"])
        self.assertEqual("sha256:diff", first["checksum"])

    def test_science_provenance_reaches_the_record(self):
        self.harness.terminate(science_provenance={
            "release_content_digest": "sha256:release",
            "tessellation_version": "nside512-v2",
            "n_sources": 4242,
        })
        record = json.loads(self.harness.store.get(self.harness.record_key))

        self.assertEqual("sha256:release",
                         record["science_provenance"]["release_content_digest"])
        self.assertEqual("nside512-v2",
                         record["science_provenance"]["tessellation_version"])

    def test_a_job_with_no_products_says_nothing_rather_than_nothing_found(self):
        # Absent, not empty: a registration job has no science products, and
        # `products: []` would claim it looked and found none where the truth
        # is that the question does not apply (the absent-not-sentinel rule).
        self.harness.terminate()
        record = json.loads(self.harness.store.get(self.harness.record_key))

        self.assertNotIn("products", record)
        self.assertNotIn("science_provenance", record)

class NumpyScalarsInTheRecordTests(unittest.TestCase):
    """A numpy scalar must not make an attempt unrecordable (W9 ramp, live).

    `coverage_and_uncertainty_statistics` records `reference_cov5percent` as a
    `numpy.float32`, because the extracted stage bodies compute in numpy.
    `json.dumps` raised `TypeError: Object of type float32 is not JSON
    serializable` inside `write_terminal_record` — which runs on the FAILURE
    path as well as the success path, so an attempt that failed for an
    unrelated reason (a missing sextractor key, in the run that found this)
    could not write the record saying so. All 18 children of the ramp's first
    step ended non-terminal with no terminal record: the exact state the
    attempt-record contract exists to make impossible.

    These tests use a stand-in scalar rather than importing numpy, for the
    same reason the fix is duck-typed — the runtime does not depend on the
    science stack, and the contract being proven is "carries .item()", not
    "is a numpy type".
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.harness = _Harness(self._tmp.name)

    def test_a_numpy_like_float_is_written_as_a_number(self):
        self.harness.terminate(
            science_provenance={"reference_cov5percent": _Float32(0.8671875)})
        record = json.loads(self.harness.store.get(self.harness.record_key))

        value = record["science_provenance"]["reference_cov5percent"]
        self.assertIsInstance(value, float)
        self.assertAlmostEqual(0.8671875, value)

    def test_it_is_not_stringified(self):
        """`default=str` would keep the write working and corrupt the type."""
        self.harness.terminate(
            science_provenance={"reference_medncov": _Float32(12.5)})
        record = json.loads(self.harness.store.get(self.harness.record_key))

        self.assertNotIsInstance(
            record["science_provenance"]["reference_medncov"], str)

    def test_an_integer_scalar_stays_an_integer(self):
        """`.item()` preserves the type where `float()` would flatten it."""
        self.harness.terminate(science_provenance={"n_sources": _Int64(4242)})
        record = json.loads(self.harness.store.get(self.harness.record_key))

        value = record["science_provenance"]["n_sources"]
        self.assertIsInstance(value, int)
        self.assertNotIsInstance(value, float)
        self.assertEqual(4242, value)

    def test_a_value_with_no_scalar_equivalent_still_raises(self):
        """The coercion is a boundary fix, not a silent swallow-everything."""
        with self.assertRaises(TypeError) as caught:
            termination._json_default(object())
        self.assertIn("not JSON-serializable", str(caught.exception))


class DecimalsInTheRecordTests(unittest.TestCase):
    """`decimal.Decimal` must coerce too (W9 ramp, defect 8, live).

    `Decimal` has no `.item()`, so the duck-typed branch that handles numpy
    scalars does not reach it and `json.dumps` raised `TypeError: Object of
    type Decimal is not JSON serializable`. psycopg2 maps every PostgreSQL
    `numeric` column to `Decimal`, and `attempt_stages.duration_ms` is
    `numeric NOT NULL` (migration 011) — so the reconciler's closure record
    carried one for every attempt that recorded any stage, and none of those
    records could be published.

    Tested here rather than only at the closure site because this helper is
    now the project's single coercion policy for both record writers.
    """

    def test_a_decimal_is_written_as_a_number(self):
        self.assertEqual(1250.0,
                         termination._json_default(decimal.Decimal("1250")))

    def test_a_fractional_decimal_keeps_its_value(self):
        self.assertAlmostEqual(
            145.25, termination._json_default(decimal.Decimal("145.25")))

    def test_it_is_not_stringified(self):
        """A duration is a number to every consumer that reads it."""
        self.assertNotIsInstance(
            termination._json_default(decimal.Decimal("4.51")), str)

    def test_a_decimal_survives_a_full_dumps(self):
        """The end-to-end contract: the encoder no longer refuses the body."""
        body = {"stages": [{"stage_name": "build_reference_image",
                            "duration_ms": decimal.Decimal("145300")}]}
        loaded = json.loads(json.dumps(body,
                                       default=termination._json_default))
        self.assertEqual(145300.0, loaded["stages"][0]["duration_ms"])


class _Float32(float):
    """Stands in for `numpy.float32`: a float carrying `.item()`."""

    def item(self):
        return float(self)


class _Int64(int):
    """Stands in for `numpy.int64`: an int carrying `.item()`."""

    def item(self):
        return int(self)
