"""`pipeline.operator.submission.submission_env`: the per-job-type binding.

PORTED FROM `pipeline/test/test_vpo_phases.py` (`SubmissionEnvRoutingTests`)
when the monolith was retired (IR-2). `submission_env` moved to this module
in IR-1a; these tests now import it directly rather than through
`virtualPipelineOperator`.
"""

import os
import unittest

from pipeline.operator import submission as opsubmission
from submission import routes


class SubmissionEnvRoutingTests(unittest.TestCase):
    """Round-4 finding #1: each phase submits to ITS OWN queue and definition.

    `submission_env` took `job_type` and ignored it, returning one singular
    `RAPID_JOB_QUEUE`/`RAPID_JOB_DEFINITION` pair to all three phases. The
    route matrix does not allow that — reference-image runs on the BULK class
    and science and post-process on PROMPT — so whichever pair was
    configured, at least one phase was submitted to a queue whose job
    definition names the other class, and `validate_route` rejects it at the
    entrypoint before any processing.

    Asserted at the SUBMIT-CALL BOUNDARY: what `submission_env` resolves is
    what the three call sites pass straight into `submit_gathered` as `queue=`
    and `job_definition=`, so nothing has to be submitted to AWS to know which
    queue a phase would reach.
    """

    #: The tree as it really stands (verified live, 2026-08-06:
    #: `aws ssm get-parameters-by-path --path /rapid/pipeline/batch`).
    TREE = {
        "batch/queue-bulk": "rapid-queue-bulk",
        "batch/queue-prompt": "rapid-queue-prompt",
        "batch/job-definition-bulk": "rapid-pipeline-bulk",
        "batch/job-definition-science": "rapid-pipeline-science",
    }

    #: The ACTIVE revision each family really resolves to. DELIBERATELY
    #: DIFFERENT per family and deliberately not 7: the defect this replaces
    #: declared one process-wide revision for both, so a test where the two
    #: families share a number could not tell a resolved revision from a
    #: declared one.
    REVISIONS = {
        "rapid-pipeline-bulk": 11,
        "rapid-pipeline-science": 14,
    }

    ACCOUNT_ARN = "arn:aws:batch:us-east-1:ACCOUNT:job-definition/{}:{}"

    class FakeBatch:
        """`describe_job_definitions`, exact-match on family, ascending.

        Mirrors the two properties the resolver depends on: the name filter
        is exact, and Batch returns revisions oldest-first so the last is the
        ACTIVE one a bare-name submission would have reached.
        """

        def __init__(self, revisions, arn_template):
            self.revisions = revisions
            self.arn_template = arn_template
            self.calls = []

        def describe_job_definitions(self, jobDefinitionName=None,
                                     status=None):
            self.calls.append((jobDefinitionName, status))
            revision = self.revisions.get(jobDefinitionName)
            if revision is None:
                return {"jobDefinitions": []}
            # Two revisions, ascending, so "last is ACTIVE" is exercised
            # rather than accidentally satisfied by a single-element list.
            return {"jobDefinitions": [
                {"jobDefinitionName": jobDefinitionName,
                 "jobDefinitionArn": self.arn_template.format(
                     jobDefinitionName, revision - 1),
                 "revision": revision - 1,
                 "containerProperties": {"image": "repo@sha256:" + "1" * 64}},
                {"jobDefinitionName": jobDefinitionName,
                 "jobDefinitionArn": self.arn_template.format(
                     jobDefinitionName, revision),
                 "revision": revision,
                 "containerProperties": {"image": "repo@sha256:" + "2" * 64}},
            ]}

    def setUp(self):
        self._saved = {name: os.environ.get(name)
                       for name in ("RAPID_JOB_DEFINITION_REV",
                                    "RAPID_IMAGE_DIGEST",
                                    "RAPID_RELEASE_IDENTITY",
                                    "RAPID_MANIFEST_BUCKET",
                                    "RAPID_JOB_QUEUE",
                                    "RAPID_JOB_DEFINITION")}
        # The revision is RESOLVED from Batch now, never declared — and since
        # O1 nothing requires the baked value at all, so this is UNSET rather
        # than set to a deliberately wrong number. A wrong value proved that
        # nothing read it *and got away with it*; an absent one proves the
        # stronger thing, that nothing needs it to be there. The runtime's
        # own read of it (`build_provenance`) is likewise optional now, and
        # `test_job` covers the absent case there.
        os.environ.pop("RAPID_JOB_DEFINITION_REV", None)
        os.environ["RAPID_IMAGE_DIGEST"] = "sha256:" + "0" * 64
        os.environ["RAPID_RELEASE_IDENTITY"] = "w8-test"
        os.environ["RAPID_MANIFEST_BUCKET"] = "rapid-manifests"
        # The env vars this used to read are deliberately left UNSET: a
        # binding resolved from them rather than from the tree would now be
        # a silent regression, so the test would rather fail loudly.
        os.environ.pop("RAPID_JOB_QUEUE", None)
        os.environ.pop("RAPID_JOB_DEFINITION", None)

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _resolve(self, job_type, batch_client=None):
        if batch_client is None:
            batch_client = self.FakeBatch(self.REVISIONS, self.ACCOUNT_ARN)
        # Both clients are injected: resolving a binding must not require AWS
        # credentials or a region, and building a real S3 client here made
        # these tests fail under `unittest discover` (no region in scope)
        # while passing when the module was run alone.
        return opsubmission.submission_env(job_type, parameters=dict(self.TREE),
                                           batch_client=batch_client,
                                           s3_client=object())

    # -- one test per phase, as the direction asks -------------------------

    def test_reference_image_is_submitted_to_the_bulk_class(self):
        context = self._resolve(routes.JOB_TYPE_REFERENCE_IMAGE)

        self.assertEqual("rapid-queue-bulk", context["queue"])
        self.assertEqual(
            self.ACCOUNT_ARN.format("rapid-pipeline-bulk", 11),
            context["job_definition"])
        self.assertEqual(routes.CLASS_BULK, context["workload_class"])

    def test_science_is_submitted_to_the_prompt_class(self):
        context = self._resolve(routes.JOB_TYPE_SCIENCE)

        self.assertEqual("rapid-queue-prompt", context["queue"])
        self.assertEqual(
            self.ACCOUNT_ARN.format("rapid-pipeline-science", 14),
            context["job_definition"])
        self.assertEqual(routes.CLASS_PROMPT, context["workload_class"])

    # -- and the property that makes those a routing test ------------------

    def test_the_three_phases_do_not_share_one_binding(self):
        """The defect stated directly: reference and science must differ.

        Each assertion above would still pass if `submission_env` returned a
        constant that happened to match — this is the one that cannot.
        """
        reference = self._resolve(routes.JOB_TYPE_REFERENCE_IMAGE)
        science = self._resolve(routes.JOB_TYPE_SCIENCE)

        self.assertNotEqual(reference["queue"], science["queue"])
        self.assertNotEqual(reference["job_definition"],
                            science["job_definition"])

    def test_the_binding_recorded_is_the_definition_submitted_to(self):
        """Submitted ARN == recorded ARN == a VERSIONED ARN (round-5).

        The equality alone is not the property. This test used to assert only
        that the two matched, which a bare family name satisfies trivially —
        both sides were `rapid-pipeline-science`, equal and both unpinned,
        while Batch resolved the revision at submission and nothing recorded
        which one it picked. So the versioned-ness is asserted here too, and
        the revision is checked against the one the family really resolves to
        rather than against the environment's declaration.
        """
        expected = {
            routes.JOB_TYPE_REFERENCE_IMAGE: ("rapid-pipeline-bulk", 11),
            routes.JOB_TYPE_SCIENCE: ("rapid-pipeline-science", 14),
        }
        for job_type, (family, revision) in expected.items():
            with self.subTest(job_type=job_type):
                context = self._resolve(job_type)
                binding = context["binding"]

                submitted = context["job_definition"]
                self.assertEqual(submitted, binding.job_definition_arn)

                # ...and it is a versioned ARN, not a family name.
                self.assertEqual(self.ACCOUNT_ARN.format(family, revision),
                                 submitted)
                self.assertTrue(submitted.rpartition(":")[2].isdigit(),
                                "submitted definition is not revision-pinned: "
                                + submitted)
                self.assertEqual(revision, binding.job_definition_rev)

                # The stale env var says 7. Nothing may have read it.
                self.assertNotEqual(7, binding.job_definition_rev)

    def test_the_two_families_resolve_to_their_own_revisions(self):
        """One process-wide revision cannot describe two families.

        The defect stated as a property: bulk and science revise
        independently, so a binding whose revision came from the environment
        was wrong for at least one of them whatever the value.
        """
        reference = self._resolve(routes.JOB_TYPE_REFERENCE_IMAGE)
        science = self._resolve(routes.JOB_TYPE_SCIENCE)

        self.assertNotEqual(reference["binding"].job_definition_rev,
                            science["binding"].job_definition_rev)
        self.assertEqual(11, reference["binding"].job_definition_rev)
        self.assertEqual(14, science["binding"].job_definition_rev)

    def test_the_family_is_selected_by_exact_name_and_active_status(self):
        """The describe call is filtered, not scanned and matched here."""
        batch = self.FakeBatch(self.REVISIONS, self.ACCOUNT_ARN)
        self._resolve(routes.JOB_TYPE_SCIENCE, batch_client=batch)

        self.assertEqual([("rapid-pipeline-science", "ACTIVE")], batch.calls)

    def test_the_recorded_identity_is_what_the_reconciler_will_observe(self):
        """The binding's identity must equal Batch's own report (#11).

        This is what the fix is FOR. `definition_identity` synthesizes
        `<arn>:<rev>` when the recorded ARN carries no revision, so a bare
        name plus an environment revision produced an identity that disagreed
        with the real job — and the reconciler recorded drift on attempts
        that ran under exactly the definition they were submitted to.
        """
        from observability.attempts import ExecutionBinding

        context = self._resolve(routes.JOB_TYPE_SCIENCE)
        submission = context["binding"]

        binding = ExecutionBinding(
            job_definition_arn=submission.job_definition_arn,
            image_digest=submission.image_digest,
            manifest_checksum="c" * 64,
            job_definition_rev=submission.job_definition_rev,
            release_identity=submission.release_identity)

        # What Batch reports for the job it actually ran.
        observed = self.ACCOUNT_ARN.format("rapid-pipeline-science", 14)
        self.assertEqual(observed, binding.definition_identity)

    def test_an_ambiguous_family_is_refused(self):
        """Two definitions behind one name: refuse, do not choose."""
        class Ambiguous:
            def describe_job_definitions(self, jobDefinitionName=None,
                                         status=None):
                return {"jobDefinitions": [
                    {"jobDefinitionName": "rapid-pipeline-science",
                     "jobDefinitionArn": "arn:...science:1", "revision": 1,
                     "containerProperties": {}},
                    {"jobDefinitionName": "rapid-pipeline-science-old",
                     "jobDefinitionArn": "arn:...science-old:2", "revision": 2,
                     "containerProperties": {}},
                ]}

        with self.assertRaises(RuntimeError) as caught:
            self._resolve(routes.JOB_TYPE_SCIENCE,
                          batch_client=Ambiguous())
        self.assertIn("more than one", str(caught.exception))

    def test_a_family_with_no_active_revision_is_refused(self):
        """Better to fail here than to submit under a definition that is
        not there."""
        class Empty:
            def describe_job_definitions(self, jobDefinitionName=None,
                                         status=None):
                return {"jobDefinitions": []}

        with self.assertRaises(RuntimeError) as caught:
            self._resolve(routes.JOB_TYPE_SCIENCE, batch_client=Empty())
        self.assertIn("no ACTIVE revision", str(caught.exception))

    def test_every_resolved_queue_is_the_one_the_entrypoint_will_check(self):
        """`validate_route` re-derives the queue from the SAME tree key.

        Submitting to a queue the entrypoint's own check would reject is the
        failure mode this finding describes, so the two are compared here
        rather than assumed to agree.
        """
        for job_type in (routes.JOB_TYPE_REFERENCE_IMAGE,
                         routes.JOB_TYPE_SCIENCE):
            with self.subTest(job_type=job_type):
                context = self._resolve(job_type)
                route = routes.validate_route(
                    job_type, context["workload_class"],
                    queue_name=context["queue"],
                    queue_names=dict(self.TREE))
                self.assertEqual(job_type, route.job_type)

    def test_a_tree_without_this_route_s_keys_is_refused(self):
        """Guessing would submit to whatever the last phase used — which is
        the defect. One clear message, before any submission."""
        with self.assertRaises(SystemExit) as caught:
            opsubmission.submission_env(routes.JOB_TYPE_REFERENCE_IMAGE,
                                        parameters={"batch/queue-prompt": "q"})

        self.assertEqual(64, caught.exception.code)


if __name__ == "__main__":
    unittest.main()
