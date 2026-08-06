"""Retention tagging: full-set rewrite, monotonic toward longer retention."""

import unittest

from pipeline.reconciler import retention
from pipeline.reconciler.test.stubs import FakeS3Tagging, attempt_row

BUCKET = "roman-rapid-diagnostics"
KEY = "attempts/bundles/run-1/90000_1/attempt-1.tar.gz"


class RetentionClassTests(unittest.TestCase):
    def test_success_only_when_both_agree(self):
        self.assertEqual("success",
                         retention.retention_class_for("success", "SUCCEEDED"))

    def test_application_failure_under_scheduler_success_retains_longer(self):
        # The representable combination the whole taxonomy exists for: Batch
        # says SUCCEEDED, the application says it failed. The diagnostics are
        # evidence about a failure and must not expire on the success clock.
        self.assertEqual("failure",
                         retention.retention_class_for("failure", "SUCCEEDED"))

    def test_no_application_outcome_at_all_retains_longer(self):
        # Never started, or dead before classifying itself: the bundle is the
        # only evidence there is.
        self.assertEqual("failure", retention.retention_class_for(None, "FAILED"))


class MonotonicTests(unittest.TestCase):
    def test_absent_accepts_anything(self):
        self.assertTrue(retention.is_monotonic(None, "success"))

    def test_lengthening_is_allowed(self):
        self.assertTrue(retention.is_monotonic("success", "failure"))

    def test_equal_is_allowed_so_replay_succeeds(self):
        self.assertTrue(retention.is_monotonic("failure", "failure"))

    def test_shortening_is_refused(self):
        self.assertFalse(retention.is_monotonic("failure", "success"))

    def test_unknown_class_raises_rather_than_sorting_arbitrarily(self):
        with self.assertRaises(retention.RetentionError):
            retention.rank("forever")


class TagSetTests(unittest.TestCase):
    def test_canonical_set_carries_the_release_tag(self):
        tags = retention.canonical_tag_set(attempt_row(), "failure")

        self.assertEqual("failure", tags[retention.TAG_RETENTION])
        self.assertEqual("rel-1", tags[retention.TAG_RELEASE])
        self.assertEqual("1", tags[retention.TAG_ATTEMPT])

    def test_unknown_class_is_rejected_before_anything_is_written(self):
        with self.assertRaises(retention.RetentionError):
            retention.canonical_tag_set(attempt_row(), "eternal")


class StampTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeS3Tagging()
        self.row = attempt_row()

    def test_stamping_writes_the_whole_set_not_a_delta(self):
        retention.stamp_retention(self.client, BUCKET, KEY, self.row, "success")

        written = self.client.tags[(BUCKET, KEY)]
        self.assertEqual({"retention-class", "producing-release", "attempt-id"},
                         set(written))

    def test_a_correction_toward_longer_retention_rewrites_every_tag(self):
        retention.stamp_retention(self.client, BUCKET, KEY, self.row, "success")
        result = retention.stamp_retention(self.client, BUCKET, KEY, self.row,
                                           "failure")

        self.assertIsNotNone(result)
        written = self.client.tags[(BUCKET, KEY)]
        self.assertEqual("failure", written["retention-class"])
        # The release tag survived the rewrite — the whole point of
        # reconstructing the set rather than patching one key.
        self.assertEqual("rel-1", written["producing-release"])

    def test_a_shortening_correction_is_refused_and_writes_nothing(self):
        retention.stamp_retention(self.client, BUCKET, KEY, self.row, "failure")
        before = len(self.client.put_calls)

        result = retention.stamp_retention(self.client, BUCKET, KEY, self.row,
                                           "success")

        self.assertIsNone(result)
        self.assertEqual(before, len(self.client.put_calls))
        self.assertEqual("failure", self.client.tags[(BUCKET, KEY)][
            "retention-class"])

    def test_replaying_the_same_classification_is_idempotent(self):
        first = retention.stamp_retention(self.client, BUCKET, KEY, self.row,
                                          "failure")
        second = retention.stamp_retention(self.client, BUCKET, KEY, self.row,
                                           "failure")

        self.assertEqual(first, second)
        self.assertEqual("failure",
                         self.client.tags[(BUCKET, KEY)]["retention-class"])

    def test_a_partial_earlier_write_is_repaired_not_carried_forward(self):
        # Someone wrote only the retention tag. The next stamp reconstructs
        # the full set from the row rather than preserving the damage.
        self.client.tags[(BUCKET, KEY)] = {"retention-class": "success"}

        retention.stamp_retention(self.client, BUCKET, KEY, self.row, "failure")

        self.assertEqual("rel-1",
                         self.client.tags[(BUCKET, KEY)]["producing-release"])

    def test_an_absent_object_reads_as_untagged(self):
        # Absence is a real answer: an attempt that died before uploading a
        # bundle has nothing to tag.
        client = FakeS3Tagging(missing={KEY})
        self.assertIsNone(retention.read_retention_class(client, BUCKET, KEY))

    def test_an_unreadable_tag_set_raises_rather_than_reading_as_untagged(self):
        # REVIEW FINDING #16. This used to convert EVERY exception into "no
        # retention tag" — and `stamp_retention` reads that as "nothing to
        # protect" and writes whatever class it was given. So a transient or
        # permission failure reading an existing FAILURE tag permitted it to
        # be replaced with the shorter SUCCESS expiry, silently defeating the
        # monotonic rule under the one condition it most needs to survive.
        client = FakeS3Tagging(unreadable={KEY})
        with self.assertRaises(retention.TagsUnreadable):
            retention.read_retention_class(client, BUCKET, KEY)

    def test_an_unreadable_tag_set_never_permits_a_shortening_rewrite(self):
        # The consequence, end to end: with the read failing, nothing is
        # written at all, so a longer-retention class cannot be shortened by
        # a reader that could not find out what was there.
        client = FakeS3Tagging(unreadable={KEY})
        with self.assertRaises(retention.TagsUnreadable):
            retention.stamp_retention(client, BUCKET, KEY, {"attempt_id": 1},
                                      retention.CLASS_SUCCESS)
        self.assertEqual([], client.put_calls)

    def test_an_absent_bundle_is_not_an_error_to_stamp(self):
        # Nothing to tag is not a failure — it is a recorded fact. The
        # reconciler must still close the attempt.
        client = FakeS3Tagging(missing={KEY})
        self.assertIsNone(
            retention.stamp_retention(client, BUCKET, KEY, {"attempt_id": 1},
                                      retention.CLASS_FAILURE))


if __name__ == "__main__":
    unittest.main()
