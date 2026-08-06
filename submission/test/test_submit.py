"""Submit tests: one batch, one call, right arguments.

The SubmitJob argument shape is where submission bugs live — an array
sized from the wrong number, configuration smuggled into the container
environment, a single-unit batch sent as an illegal size-1 array. All of
that is assertable without an AWS account, and is asserted here.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from submission.batching import Batch
from submission.manifest import Manifest, ProcessingUnit
from submission.submit import (build_submit_kwargs, publish_manifest,
                               submit_batch)

QUEUE = "rapid-queue-prompt"
DEFINITION = "rapid-pipeline-science"


class FakeStore:
    """In-memory manifest store."""

    def __init__(self):
        self.objects = {}

    def key_for(self, batch_id):
        return f"submissions/{batch_id}/manifest.json"

    def put(self, key, body):
        self.objects[key] = body
        return f"s3://fake-bucket/{key}"

    def get(self, uri):
        return self.objects[uri.removeprefix("s3://fake-bucket/")]


class FakeBatchClient:
    def __init__(self):
        self.calls = []

    def submit_job(self, **kwargs):
        self.calls.append(kwargs)
        return {"jobId": "job-abc123", "jobName": kwargs["jobName"]}


def make_batch(count, batch_id="batch-1"):
    manifest = Manifest(
        [ProcessingUnit(exposure=90210, sca=i + 1) for i in range(count)],
        batch_id=batch_id)
    return Batch(manifest=manifest, reason="size")


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def client():
    return FakeBatchClient()


def env_of(kwargs):
    return {e["name"]: e["value"]
            for e in kwargs["containerOverrides"]["environment"]}


# ---------------------------------------------------------------------------
# Argument construction
# ---------------------------------------------------------------------------

def test_array_size_comes_from_the_manifest():
    kwargs = build_submit_kwargs(make_batch(18), QUEUE, DEFINITION, "s3://m")
    assert kwargs["arrayProperties"] == {"size": 18}


def test_single_unit_batch_is_not_submitted_as_an_array():
    # Batch rejects arraySize 1.
    kwargs = build_submit_kwargs(make_batch(1), QUEUE, DEFINITION, "s3://m")
    assert "arrayProperties" not in kwargs


def test_queue_and_definition_are_passed_through():
    kwargs = build_submit_kwargs(make_batch(4), QUEUE, DEFINITION, "s3://m")
    assert kwargs["jobQueue"] == QUEUE
    assert kwargs["jobDefinition"] == DEFINITION


def test_environment_carries_identifiers_only():
    # The two-tier rule: identifiers in the environment, configuration in
    # the parameter tree. A regression that starts passing tuning values
    # here fails this test.
    batch = make_batch(4)
    kwargs = build_submit_kwargs(batch, QUEUE, DEFINITION, "s3://bucket/m.json")
    assert env_of(kwargs) == {
        "RAPID_MANIFEST_URI": "s3://bucket/m.json",
        "RAPID_BATCH_ID": "batch-1",
        "RAPID_MANIFEST_CHECKSUM": batch.manifest.checksum(),
    }


def test_extra_environment_is_merged():
    kwargs = build_submit_kwargs(make_batch(2), QUEUE, DEFINITION, "s3://m",
                                 environment={"RAPID_PROC_DATE": "2026-08-04"})
    assert env_of(kwargs)["RAPID_PROC_DATE"] == "2026-08-04"


def test_job_name_defaults_to_the_batch_id():
    kwargs = build_submit_kwargs(make_batch(2, batch_id="b-77"), QUEUE,
                                 DEFINITION, "s3://m")
    assert kwargs["jobName"] == "rapid-b-77"


# ---------------------------------------------------------------------------
# Manifest publication
# ---------------------------------------------------------------------------

def test_manifest_is_published_and_readable_back(store):
    batch = make_batch(6)
    uri = publish_manifest(batch.manifest, store)
    assert Manifest.from_json(store.get(uri).decode()) == batch.manifest


def test_manifest_is_keyed_by_batch_id(store):
    publish_manifest(make_batch(2, batch_id="b-42").manifest, store)
    assert "submissions/b-42/manifest.json" in store.objects


def test_publishing_a_manifest_without_a_batch_id_is_an_error(store):
    with pytest.raises(ValueError, match="batch_id"):
        publish_manifest(Manifest([ProcessingUnit(1, 1)]), store)


# ---------------------------------------------------------------------------
# The whole path: one batch, exactly one call
# ---------------------------------------------------------------------------

def test_submit_makes_exactly_one_call(store, client):
    # One call per batch is the throttle property the design rests on.
    submit_batch(make_batch(500), QUEUE, DEFINITION, store, client)
    assert len(client.calls) == 1
    assert client.calls[0]["arrayProperties"]["size"] == 500


def test_submission_carries_what_an_attempt_writer_needs(store, client):
    batch = make_batch(18)
    submission = submit_batch(batch, QUEUE, DEFINITION, store, client)
    assert submission.job_id == "job-abc123"
    assert submission.array_size == 18
    assert submission.batch_id == "batch-1"
    assert submission.manifest_checksum == batch.manifest.checksum()
    assert submission.manifest_uri.startswith("s3://")


def test_child_job_ids_follow_the_batch_convention(store, client):
    submission = submit_batch(make_batch(18), QUEUE, DEFINITION, store, client)
    assert submission.child_job_id(0) == "job-abc123:0"
    assert submission.child_job_id(17) == "job-abc123:17"


def test_a_non_array_submissions_child_is_the_job_itself(store, client):
    submission = submit_batch(make_batch(1), QUEUE, DEFINITION, store, client)
    assert submission.child_job_id(0) == "job-abc123"


def test_the_published_manifest_matches_the_submitted_checksum(store, client):
    # The startup path verifies this checksum; if publication and the
    # environment could disagree, every job would fail that check.
    submission = submit_batch(make_batch(9), QUEUE, DEFINITION, store, client)
    published = json.loads(store.get(submission.manifest_uri).decode())
    assert Manifest.from_dict(published).checksum() \
        == env_of(client.calls[0])["RAPID_MANIFEST_CHECKSUM"]


# --- The scheduler-retry contract (W5) ------------------------------------
#
# "The submission layer never passes a submit-time retryStrategy override
# (validated in code and covered by a test); the job definition is the single
# retry authority" — batch-payload co-design, § Scheduler-retry contract.
#
# The reason this needs a test rather than a reading is that it is an
# invariant about what is ABSENT. Nothing fails today if someone adds a
# retryStrategy here; the job definitions' careful EvaluateOnExit ordering
# would simply stop being what governs retries, silently, and an application
# failure that exits 0 cleanly could start being retried — the 2026-07-22
# failure mode. An assertion on absence is the only thing that notices.

def test_submission_passes_no_retry_strategy_override():
    kwargs = build_submit_kwargs(make_batch(4), QUEUE, DEFINITION,
                                 "s3://bucket/manifest.json")
    assert "retryStrategy" not in kwargs, (
        "the job definition is the single retry authority; a submit-time "
        "override would silently replace its condition-gated EvaluateOnExit "
        "rows and could start retrying clean application failures")


def test_submitted_call_passes_no_retry_strategy_override(store, client):
    # Asserted on the actual submit_job call, not only on the builder: a
    # future submit_batch could add the key after build_submit_kwargs returns.
    submit_batch(make_batch(4), QUEUE, DEFINITION, store, client)
    assert "retryStrategy" not in client.calls[0]


def test_submission_passes_no_command_override(store, client):
    # "No command overrides exist at submit time" — the command is the
    # workload-class discriminator, fixed per job definition, and overriding
    # it at submit time would unbind the route the entrypoint validates.
    submit_batch(make_batch(4), QUEUE, DEFINITION, store, client)
    overrides = client.calls[0].get("containerOverrides", {})
    assert "command" not in overrides


def test_container_overrides_carry_environment_only(store, client):
    # The per-invocation environment is the ONLY submit-time surface. Anything
    # else here — resourceRequirements, command, instanceType — is a second
    # place a job's shape is decided, competing with the job definition.
    submit_batch(make_batch(4), QUEUE, DEFINITION, store, client)
    assert set(client.calls[0]["containerOverrides"]) == {"environment"}
