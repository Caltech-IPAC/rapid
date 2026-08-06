"""
File:    test_stages.py

Stage accounting: two clocks, written once, including on failure.
"""

import datetime
import os
import tempfile
import time
import unittest
from unittest import mock

from pipeline.runtime.errors import ToolError
from pipeline.runtime.stages import (
    SKIPPED,
    StageRecord,
    StageRecorder,
    run_stage,
    stage_context,
)
from pipeline.runtime.test.stubs import FakeLogger
from pipeline.runtime.workdir import WorkingDirectory


class TestRunStage(unittest.TestCase):

    def setUp(self):
        self.recorder = StageRecorder()

    def test_returns_the_stage_result(self):
        self.assertEqual(run_stage("s", lambda: 42, recorder=self.recorder),
                         42)

    def test_records_one_span_at_completion(self):
        run_stage("difference", lambda: None, recorder=self.recorder)
        self.assertEqual(len(self.recorder.records), 1)
        record = self.recorder.records[0]
        self.assertEqual(record.stage_name, "difference")
        self.assertEqual(record.outcome, "success")

    def test_a_failing_stage_still_gets_its_record(self):
        """A stage whose failure left no row would be indistinguishable from
        one that never ran — which is the silence the design refuses."""
        with self.assertRaises(ToolError):
            run_stage("difference",
                      lambda: (_ for _ in ()).throw(ToolError("boom")),
                      recorder=self.recorder)

        self.assertEqual(len(self.recorder.records), 1)
        record = self.recorder.records[0]
        self.assertEqual(record.outcome, "failure")
        self.assertEqual(record.error_category, "tool_failure")
        self.assertIn("boom", record.error_message)

    def test_the_original_exception_propagates(self):
        """Accounting never swallows the failure it is accounting for."""
        with self.assertRaises(ToolError) as caught:
            run_stage("s", lambda: (_ for _ in ()).throw(ToolError("keep me")),
                      recorder=self.recorder)
        self.assertIn("keep me", str(caught.exception))

    def test_a_stage_is_recorded_exactly_once(self):
        run_stage("s", lambda: None, recorder=self.recorder)
        self.assertEqual(len([r for r in self.recorder.records
                              if r.stage_name == "s"]), 1)

    def test_skipped_is_a_third_real_outcome(self):
        """"Nothing to do" and "silently failed" must not look the same."""
        result = run_stage("s", lambda: SKIPPED, recorder=self.recorder)
        self.assertIsNone(result)
        self.assertEqual(self.recorder.records[0].outcome, "skipped")

    def test_duration_comes_from_the_monotonic_clock(self):
        """A wall-clock subtraction can go negative under an NTP step, and the
        attempt writer rejects a negative duration — so the failure would
        surface as a crash while recording a stage that succeeded."""
        with mock.patch("pipeline.runtime.stages.time.monotonic",
                        side_effect=[100.0, 100.25]):
            run_stage("s", lambda: None, recorder=self.recorder)
        self.assertAlmostEqual(self.recorder.records[0].duration_ms, 250.0,
                               places=3)

    def test_started_at_is_an_aware_utc_wall_clock(self):
        run_stage("s", lambda: None, recorder=self.recorder)
        started = self.recorder.records[0].started_at
        self.assertIsNotNone(started.tzinfo)
        self.assertEqual(started.utcoffset(), datetime.timedelta(0))

    def test_duration_is_never_negative(self):
        run_stage("s", lambda: time.sleep(0.001), recorder=self.recorder)
        self.assertGreaterEqual(self.recorder.records[0].duration_ms, 0.0)


class TestStageContext(unittest.TestCase):

    def test_the_block_may_declare_skipped(self):
        recorder = StageRecorder()
        with stage_context("s", recorder=recorder) as state:
            state["outcome"] = "skipped"
        self.assertEqual(recorder.records[0].outcome, "skipped")

    def test_declaring_failure_is_refused(self):
        """A failure is raised so it carries a category; declaring one would
        produce a failed stage with no classification."""
        recorder = StageRecorder()
        with self.assertRaises(ValueError):
            with stage_context("s", recorder=recorder) as state:
                state["outcome"] = "failure"

    def test_stage_logs_land_in_their_own_bundle_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = WorkingDirectory.create("a1", work_root=tmp)
            import logging

            logger = logging.getLogger("rapid.test.stage")
            with stage_context("calibrate", workdir=workdir):
                logger.warning("inside the stage")
            logger.warning("after the stage")

            path = workdir.stage_log_path("calibrate")
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("inside the stage", text)
            self.assertNotIn(
                "after the stage", text,
                "the handler must be removed at stage exit, or a later "
                "stage's lines land in an earlier stage's file")


class TestStageRecorder(unittest.TestCase):

    def test_writes_each_span_through_the_writer(self):
        written = []
        recorder = StageRecorder(write=written.append)
        run_stage("s", lambda: None, recorder=recorder)
        self.assertEqual(len(written), 1)

    def test_a_failed_span_write_does_not_fail_the_stage(self):
        """The span is a latency artifact, not the outcome authority.

        Failing a successful stage because a diagnostic row would not insert
        would trade a real product for a metric. The terminal record and the
        application-closed transition are NOT best-effort in this way — those
        raise — and that asymmetry is deliberate.
        """
        def explode(_record):
            raise RuntimeError("insert failed")

        logger = FakeLogger()
        recorder = StageRecorder(write=explode)
        with mock.patch("pipeline.runtime.stages._logger", logger):
            result = run_stage("s", lambda: "product", recorder=recorder)

        self.assertEqual(result, "product")
        self.assertIn("could not record the span", logger.text())

    def test_records_are_kept_in_memory_for_the_terminal_record(self):
        recorder = StageRecorder()
        run_stage("a", lambda: None, recorder=recorder)
        run_stage("b", lambda: None, recorder=recorder)
        self.assertEqual([r["stage_name"] for r in recorder.as_list()],
                         ["a", "b"])

    def test_failed_lists_only_failures(self):
        recorder = StageRecorder()
        run_stage("ok", lambda: None, recorder=recorder)
        with self.assertRaises(ToolError):
            run_stage("bad", lambda: (_ for _ in ()).throw(ToolError("x")),
                      recorder=recorder)
        self.assertEqual([r.stage_name for r in recorder.failed], ["bad"])


class TestStageRecordConversion(unittest.TestCase):

    def test_converts_to_the_attempt_writers_stage(self):
        record = StageRecord(
            stage_name="s",
            started_at=datetime.datetime.now(datetime.timezone.utc),
            duration_ms=12.5, outcome="success")
        stage = record.to_attempt_stage()
        self.assertEqual(stage.stage_name, "s")
        self.assertEqual(stage.duration_ms, 12.5)
        self.assertEqual(stage.outcome.value, "success")

    def test_as_dict_omits_absent_error_fields(self):
        record = StageRecord(
            stage_name="s",
            started_at=datetime.datetime.now(datetime.timezone.utc),
            duration_ms=1.0, outcome="success")
        self.assertNotIn("error_category", record.as_dict())

    def test_as_dict_carries_the_failure_classification(self):
        record = StageRecord(
            stage_name="s",
            started_at=datetime.datetime.now(datetime.timezone.utc),
            duration_ms=1.0, outcome="failure",
            error_category="tool_failure", error_message="boom")
        as_dict = record.as_dict()
        self.assertEqual(as_dict["error_category"], "tool_failure")
        self.assertEqual(as_dict["error_message"], "boom")


if __name__ == "__main__":
    unittest.main()
