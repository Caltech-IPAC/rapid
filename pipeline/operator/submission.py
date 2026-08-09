"""The submission-time binding: queue, job definition, and clients.

MOVED FROM `pipeline/virtualPipelineOperator.py` (IR-1a extraction). That
module is a SCRIPT — it reads `sys.argv[1]` and environment variables at
import time via `_startup()`, called from its own `__main__` block — so
importing anything from it runs a hazard `pipeline/operator/gathering.py`
already documents in full: the restructured service's first live
rehearsal died mid-run because an import of one pure helper re-read the
NEW operator's argv and demanded the old operator's environment
interface. `submission_env` and `production_registrar` (in
`pipeline.operator.registrar`) are the monolith's only two functions the
deployed service still calls, so they move to where importing them is
safe. The monolith itself has since been retired (IR-2).

Split from `production_registrar`: this module resolves what a
submission binds to (queue, job definition, clients); `registrar` builds
what registers the products a submission's jobs produce. They are
different responsibilities with different lifetimes in the operator's
pass — bound once per phase before anything runs, versus built once and
then re-bound per registration connection — and `production_registrar`
already documents its own reasoning at that length, so keeping it in its
own module keeps each docstring next to only the logic it explains.
"""

import os

import boto3
from dataclasses import dataclass

from submission import routes


def active_definition(batch_client, family):

    '''
    The one ACTIVE revisioned job-definition ARN for a definition family.

    THE EXECUTION BINDING MUST NAME WHAT ACTUALLY RAN (round-5 finding). The
    parameter tree carries a definition FAMILY — `rapid-pipeline-science` —
    and submitting that bare name lets Batch resolve whichever revision is
    ACTIVE at the instant of submission. Nothing records which one that was:
    the revision was carried separately, as a process-wide
    `RAPID_JOB_DEFINITION_REV`, and a single integer cannot be right for two
    independently revisioned families at once. The science and bulk
    definitions revise on their own schedules, so whichever number the
    environment held, at least one class recorded a revision it did not run.

    That is not a bookkeeping detail. `ExecutionBinding.definition_identity`
    synthesizes `<name>:<rev>` from the recorded pair, and the reconciler
    compares its observation of the real job against it — so a binding whose
    revision came from the environment makes the reconciler record DRIFT on
    attempts that ran under exactly the definition they were submitted to. At
    ramp scale that is a false-positive per attempt, against a gate that
    requires zero unexplained terminal records.

    So the revision is RESOLVED, not declared, and resolved once per family at
    env build. The `describe_job_definitions` call filters to ACTIVE and the
    exact family name; Batch returns revisions oldest-first, so the last is
    the one a bare-name submission would have reached — the same revision,
    now named explicitly and recorded.

    AMBIGUITY IS REFUSED rather than resolved by guessing. `jobDefinitionName`
    is an exact-match filter, so more than one distinct family coming back
    means the account holds something this code does not model, and picking
    one would submit real work under a definition nobody chose. None coming
    back means the family does not exist and every submission under it would
    fail at Batch with a far less legible error.

    Parameters
    ----------
    batch_client : botocore client
        Batch client, injected so the tests can drive this without AWS.
    family : str
        The definition family name from the parameter tree.

    Returns
    -------
    dict
        `arn` (the versioned ARN), `revision` (int), and `image_digest`
        (the digest the definition's container actually names).

    Raises
    ------
    RuntimeError
        No ACTIVE revision, or more than one family in the response.
    '''

    described = batch_client.describe_job_definitions(
        jobDefinitionName=family, status="ACTIVE")
    definitions = described.get("jobDefinitions", [])

    if not definitions:
        raise RuntimeError(
            "no ACTIVE revision of job definition family {!r}; a submission "
            "under it could not run, and binding it to a revision that does "
            "not exist would record a job that never was".format(family))

    names = {definition["jobDefinitionName"] for definition in definitions}
    if len(names) > 1:
        raise RuntimeError(
            "job definition family {!r} resolved to more than one "
            "definition ({}); refusing to choose, because submitting real "
            "work under a definition nobody selected is worse than not "
            "submitting it".format(family, ", ".join(sorted(names))))

    # Batch returns revisions in ascending order; the last ACTIVE one is what
    # a bare-name submission would have resolved to.
    latest = definitions[-1]
    image = latest.get("containerProperties", {}).get("image", "")

    return {
        "arn": latest["jobDefinitionArn"],
        "revision": int(latest["revision"]),
        "image_digest": image.split("@", 1)[-1] if "@" in image else "",
    }


#-------------------------------------------------------------------------------------------------------------
# The submission-time execution binding and its clients.
#-------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SubmissionBinding:

    '''
    The four binding facts the OPERATOR knows, before there is a manifest.

    NOT an `ExecutionBinding`, and that is the point. `ExecutionBinding`
    requires `manifest_checksum` and refuses to be constructed without it —
    deliberately, because an attempt row must always name the manifest it was
    submitted under. But the checksum is a property of a BATCH, and the
    operator resolves its binding once per phase, before any batch has been
    assembled. `submission_env` used to build an `ExecutionBinding` with
    `manifest_checksum=None` anyway, which raised `ValueError` on every
    production call — the operator could not submit anything at all. (Found
    while writing the round-4 finding #1 routing tests, which construct this
    binding for real rather than stubbing it.)

    `submit_gathered` is where the two meet: it publishes the manifest, reads
    these four fields off whatever it was handed, and builds the real
    `ExecutionBinding` with the checksum it now has. So this carries exactly
    what the operator can know and nothing it cannot, and the validation
    stays where the complete fact exists.
    '''

    job_definition_arn: str
    image_digest: str
    job_definition_rev: int
    release_identity: str

    def __post_init__(self) -> None:
        missing = [name for name in
                   ("job_definition_arn", "image_digest",
                    "job_definition_rev", "release_identity")
                   if getattr(self, name) in (None, "")]
        if missing:
            raise ValueError(
                "the submission binding is incomplete; missing: "
                + ", ".join(missing)
                + ". These are the facts the CI pipeline produces and the "
                "attempt row must record to be reproducible.")


def submission_env(job_type, parameters=None, batch_client=None,
                   s3_client=None):

    '''
    The queue, job definition, binding and clients one submission needs.

    THE QUEUE AND DEFINITION ARE PER JOB TYPE (round-4 finding #1), and they
    come from the route matrix rather than from the environment. This function
    used to take `job_type` and ignore it, returning one singular
    `RAPID_JOB_QUEUE`/`RAPID_JOB_DEFINITION` pair to all three phases. The
    matrix does not allow that: reference-image runs on the BULK class and
    science and post-process on PROMPT, so whichever single pair was
    configured, at least one phase was submitted to a queue whose job
    definition names the other class — and `validate_route` rejects it at the
    entrypoint, before any processing, exactly as it is designed to.

    `routes.Route` names the parameter-tree KEYS (`batch/queue-bulk`,
    `batch/job-definition-science`, ...) and deliberately does not carry the
    names themselves, so this resolves them through `fetch_parameters` — the
    same read the entrypoint validates against. One fact, one home: were the
    names duplicated into the environment they could disagree with the tree
    the entrypoint checks, and a disagreement there is a rejected submission.

    The rest stay in the ENVIRONMENT, because they are deployment facts that
    change with every image build: the image digest and the release identity
    are what the CI pipeline produces and what the attempt row must record to
    be reproducible.

    THE REVISION IS NOT AMONG THEM (round-5 finding). It used to be, as a
    process-wide `RAPID_JOB_DEFINITION_REV`, and that is exactly the defect:
    one integer declared for two independently revisioned families, recorded
    beside a bare family name that Batch resolved to whatever was ACTIVE.
    `active_definition` resolves it per route class instead, and the SAME
    versioned ARN is both submitted and recorded — which is the property that
    makes the reconciler's comparison meaningful rather than a coin flip.

    Every one is REQUIRED. A submission that cannot name its own binding is
    exactly what migration 013's amended submitted-state constraint refuses,
    and defaulting any of them would create rows whose binding does not
    describe the job that ran.

    Parameters
    ----------
    job_type : str
        The phase being submitted. Selects the route, and through it the
        queue and job definition.
    parameters : dict, optional
        Parameter-tree values, relative-keyed, as `fetch_parameters`
        returns them. Injected by the tests and by a caller that has
        already read the tree; fetched here when omitted.
    batch_client : botocore client, optional
        Batch client, used to resolve the ACTIVE revision and returned for
        the submission itself. Injected by the tests; built here when
        omitted, so one client serves both.
    s3_client : botocore client, optional
        S3 client for the manifest write. Injected by the tests for the same
        reason as `batch_client`: resolving a binding is a decision, and a
        test of that decision should not need AWS credentials or a region to
        construct a client the resolution never calls.
    '''

    required = ("RAPID_IMAGE_DIGEST",
                "RAPID_RELEASE_IDENTITY", "RAPID_MANIFEST_BUCKET")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print("*** Error: the submission environment is incomplete; "
              "missing {}; quitting...".format(", ".join(missing)))
        exit(64)

    from submission.startup import fetch_parameters

    if parameters is None:
        parameters = fetch_parameters()

    route = routes.route_for(job_type)

    # A tree that does not carry this route's keys cannot bind the phase, and
    # guessing one would submit to whatever the last phase happened to use —
    # which is the defect this replaces. One clear message, before submission.
    binding_names = {}
    for kind, key in (("queue", route.queue_parameter),
                      ("job_definition", route.definition_parameter)):
        value = parameters.get(key)
        if not value:
            print("*** Error: the parameter tree does not carry {}, so the "
                  "{} for job type {} cannot be resolved; quitting...".format(
                      key, kind.replace("_", " "), job_type))
            exit(64)
        binding_names[kind] = value

    if batch_client is None:
        batch_client = boto3.client('batch')
    if s3_client is None:
        s3_client = boto3.client('s3')

    # The family from the tree, resolved to the one ACTIVE revision. What is
    # submitted and what is recorded are now the same string by construction,
    # rather than two values that agree only while an env var happens to be
    # right.
    active = active_definition(batch_client, binding_names["job_definition"])
    job_definition = active["arn"]

    return {
        "queue": binding_names["queue"],
        "job_definition": job_definition,
        "workload_class": route.workload_class,
        "binding": SubmissionBinding(
            job_definition_arn=job_definition,
            job_definition_rev=active["revision"],
            image_digest=os.environ['RAPID_IMAGE_DIGEST'],
            release_identity=os.environ['RAPID_RELEASE_IDENTITY']),
        "manifest_bucket": os.environ['RAPID_MANIFEST_BUCKET'],
        "manifest_prefix": os.environ.get('RAPID_MANIFEST_PREFIX',
                                          'submissions'),
        "s3_client": s3_client,
        "batch_client": batch_client,
    }
