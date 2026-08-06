"""
File:    submit.py

One batch, one SubmitJob call.

design/compute.md § Submission: "one submission call per ready batch of
work, each array child one per-SCA processing unit". That is the whole
contract this module implements — the manifest sizes the array, the
manifest is published where the children can read it, and one API call
goes out.

The Batch client and the manifest store are injected. That is not
ceremony: it is what makes the submit path testable without an AWS
account, and it is the same seam the producer uses. `submit_batch` with a
fake client and an in-memory store exercises the real argument
construction, which is where submission bugs actually live.

Deliberately absent: attempt-record creation. design/compute.md places
the attempt row at submission time, before the scheduler assigns child
identifiers — but the attempt record is its own workstream with its own
schema, and this module stops at the submit boundary. `submit_batch`
returns everything an attempt-record writer needs (the manifest, the job
id, the array size), so the two compose without this module reaching into
the other's tables.
"""

import dataclasses
import logging
from typing import Any, Protocol

from .batching import Batch
from .manifest import Manifest

logger = logging.getLogger(__name__)

# Where a batch's manifest is published for its children to read. The
# jobs reach it under the Batch job role, whose S3 read is already scoped
# to the pipeline's buckets.
DEFAULT_MANIFEST_PREFIX = "submissions"


class ManifestStore(Protocol):
    """Publishes a manifest where the array's children can read it."""

    def put(self, key: str, body: bytes) -> str:
        """Store the manifest and return the URI jobs resolve."""
        ...

    def get(self, uri: str) -> bytes:
        ...


class S3ManifestStore:
    """S3-backed manifest store.

    One object per submission, keyed by batch id, written once and never
    updated — the index binding is immutable for the life of the batch,
    including across retries.
    """

    def __init__(self, bucket: str, prefix: str = DEFAULT_MANIFEST_PREFIX,
                 client: Any = None):
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3
            self._client = boto3.client("s3")
        return self._client

    def key_for(self, batch_id: str) -> str:
        return f"{self.prefix}/{batch_id}/manifest.json"

    def put(self, key: str, body: bytes) -> str:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body,
                               ContentType="application/json")
        return f"s3://{self.bucket}/{key}"

    def get(self, uri: str) -> bytes:
        bucket, _, key = uri.removeprefix("s3://").partition("/")
        response = self.client.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()


@dataclasses.dataclass(frozen=True)
class Submission:
    """The result of submitting one batch.

    Carries what an attempt-record writer needs without this module
    writing any rows itself.
    """

    batch_id: str
    job_id: str
    job_name: str
    array_size: int
    manifest_uri: str
    manifest_checksum: str
    manifest: Manifest

    @property
    def is_array(self) -> bool:
        return self.manifest.is_array

    def child_job_id(self, index: int) -> str:
        """The job id Batch assigns array child `index`.

        Batch names children ``<parent-id>:<index>``. Derived rather than
        looked up so a caller can reconcile children it never saw an API
        response for.
        """
        if not self.is_array:
            return self.job_id
        return f"{self.job_id}:{index}"


def publish_manifest(manifest: Manifest, store: ManifestStore) -> str:
    """Write a batch's manifest and return its URI."""
    if manifest.batch_id is None:
        raise ValueError("manifest has no batch_id; cannot key its object")
    key = (store.key_for(manifest.batch_id)
           if hasattr(store, "key_for")
           else f"{DEFAULT_MANIFEST_PREFIX}/{manifest.batch_id}/manifest.json")
    uri = store.put(key, manifest.to_json().encode("utf-8"))
    logger.info("published manifest for batch %s (%d units) to %s",
                manifest.batch_id, len(manifest), uri)
    return uri


def build_submit_kwargs(batch: Batch, job_queue: str, job_definition: str,
                        manifest_uri: str,
                        environment: dict[str, str] | None = None,
                        job_name: str | None = None) -> dict[str, Any]:
    """Construct the SubmitJob arguments for one batch.

    Split out from `submit_batch` so the argument shape — the part that
    actually breaks — is assertable in a unit test without any client at
    all.

    The container environment carries identifiers only, per the two-tier
    configuration rule (design/security.md): where the manifest is, which
    batch this is. Everything else the job needs it reads from the
    pipeline parameter tree at startup.
    """
    manifest = batch.manifest
    env = {
        "RAPID_MANIFEST_URI": manifest_uri,
        "RAPID_BATCH_ID": str(manifest.batch_id),
        "RAPID_MANIFEST_CHECKSUM": manifest.checksum(),
    }
    if environment:
        env.update(environment)

    kwargs: dict[str, Any] = {
        "jobName": job_name or f"rapid-{manifest.batch_id}",
        "jobQueue": job_queue,
        "jobDefinition": job_definition,
        "containerOverrides": {
            "environment": [{"name": k, "value": v}
                            for k, v in sorted(env.items())],
        },
    }
    # Batch rejects arraySize 1; a single-unit batch is a plain job whose
    # child resolves index 0 through the same startup path.
    if manifest.is_array:
        kwargs["arrayProperties"] = {"size": manifest.array_size}
    return kwargs


def submit_batch(batch: Batch, job_queue: str, job_definition: str,
                 store: ManifestStore, client: Any,
                 environment: dict[str, str] | None = None,
                 job_name: str | None = None) -> Submission:
    """Publish a batch's manifest and submit it as one array job.

    Parameters
    ----------
    batch : batching.Batch
        The cut batch.
    job_queue, job_definition : str
        Batch targets. Which queue and definition a batch goes to is the
        caller's decision (prompt vs bulk); this module submits where told.
    store : ManifestStore
        Where the manifest is published for the children to read.
    client : object
        Batch client exposing ``submit_job(**kwargs)``.
    environment : dict, optional
        Extra container environment. Identifiers only — anything that is
        configuration belongs in the parameter tree.

    Returns
    -------
    Submission
    """
    manifest_uri = publish_manifest(batch.manifest, store)
    kwargs = build_submit_kwargs(batch, job_queue, job_definition,
                                 manifest_uri, environment=environment,
                                 job_name=job_name)
    response = client.submit_job(**kwargs)

    submission = Submission(
        batch_id=str(batch.manifest.batch_id),
        job_id=response["jobId"],
        job_name=response.get("jobName", kwargs["jobName"]),
        array_size=batch.manifest.array_size,
        manifest_uri=manifest_uri,
        manifest_checksum=batch.manifest.checksum(),
        manifest=batch.manifest,
    )
    logger.info("submitted batch %s as job %s (%d children, queue %s)",
                submission.batch_id, submission.job_id,
                submission.array_size, job_queue)
    return submission
