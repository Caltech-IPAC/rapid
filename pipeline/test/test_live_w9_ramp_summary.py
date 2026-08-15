"""The ramp harness's summary must name fields the submission result HAS.

**THE DEFECT THIS REFUSES.** The summary built its `scheduler_job_id` with
`getattr(submission, "scheduler_job_id", None)` — a field the submission
result does not define; it is called `job_id`. The getattr default turned a
wrong field name into `null` rather than an `AttributeError`, so every
summary this harness ever printed carried a null job id while the id itself
was perfectly well known (`attempts.scheduler_job_id` is populated on the
same submission, which is how the job had to be recovered by name after the
2026-08-15 run). A summary that cannot be correlated to a Batch job is the
one artifact an operator reads first.

The test is written against the SUBMISSION RESULT'S OWN FIELD NAMES rather
than against the summary's expected values, because the bug was never a
wrong value — it was a name that silently resolved to a default. Asserting
"the summary is not null for this fixture" would pass again the moment
someone renamed the field back; asserting the names EXIST on the real class
is what refuses the whole class of getattr-default drift.
"""

import dataclasses
import unittest

from submission.submit import Submission


class SummaryFieldNamesTests(unittest.TestCase):

    def test_the_submission_result_defines_every_field_the_summary_reads(self):
        """Each attribute the summary reads must exist on the real class.

        `live_w9_ramp.main()` reads `submission.job_id` and
        `submission.array_size` when building each batch entry. If either is
        renamed, this fails loudly here rather than emitting a null into an
        operator-facing artifact.
        """
        names = {f.name for f in dataclasses.fields(Submission)}
        for attribute in ("job_id", "array_size"):
            self.assertIn(
                attribute, names,
                "live_w9_ramp's summary reads submission.%s; if that field "
                "has been renamed, the summary must be updated with it "
                "rather than silently emitting null" % attribute)

    def test_scheduler_job_id_is_not_a_field_of_the_submission_result(self):
        """The original wrong name must stay wrong, or the fix is moot.

        Pinned deliberately: if a `scheduler_job_id` field is ever added to
        the submission result, the summary should read THAT rather than
        `job_id`, and this test failing is the prompt to make that choice
        knowingly instead of leaving two plausible sources.
        """
        names = {f.name for f in dataclasses.fields(Submission)}
        self.assertNotIn(
            "scheduler_job_id", names,
            "Submission has gained a scheduler_job_id field — decide "
            "which one live_w9_ramp's summary should report and update it")

    def test_the_summary_reads_the_attribute_directly_not_through_getattr(self):
        """No getattr default may stand between the summary and the id.

        The defect was not the value but the DEFAULT: `getattr(obj, "name",
        None)` converts a typo into a plausible null. Read as source text
        because that is the property under test — a direct attribute access
        raises on a wrong name, which is the whole point.
        """
        import inspect

        from pipeline.test import live_w9_ramp

        source = inspect.getsource(live_w9_ramp.main)
        summary_region = source[source.index("batches = []"):
                                source.index("summary = {")]
        # Comments are stripped before the check: the region deliberately
        # DESCRIBES the old getattr call in prose, and a naive substring
        # scan would match that description and fail on a correct file.
        code = "\n".join(line for line in summary_region.splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertNotIn(
            "getattr(", code,
            "the ramp summary builds a batch entry through getattr with a "
            "default, which is what let a wrong field name emit null "
            "instead of raising")


if __name__ == "__main__":
    unittest.main()
