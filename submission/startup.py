"""
File:    startup.py

What a job does in its first seconds: find out what it is, and what it
was configured with.

design/security.md § Job configuration, restated in design/compute.md
§ Job definitions: "Job configuration is two-tier: per-invocation
identifiers arrive in the container environment; everything else —
science tuning, bucket names, mode toggles — is read from the pipeline
parameter tree at startup under the job role and hashed into the attempt
record's configuration digest. The job role's parameter read extends only
over that tree."

The startup sequence that implements it:

1.  Read the identifiers from the environment — where the manifest is,
    which array index this child is.
2.  Fetch `/rapid/pipeline/` from Parameter Store, one recursive call.
3.  Digest the fetched configuration.
4.  Resolve this child's own processing unit from the manifest.

The digest is the load-bearing part. It is what makes a product's
configuration provenance checkable after the fact, so it has to be
computed the same way by every job that reads the same tree. Two
properties are therefore enforced, and tested:

Canonical form. Parameters are sorted by name and serialized with a
fixed separator before hashing, so two jobs that read the same values in
different orders produce the same digest.

Value-complete. The digest covers names AND values — not a version
number or a fetch timestamp. A parameter edited in place changes the
digest, which is exactly the change an operator needs to see reflected in
provenance.

The digest deliberately does NOT cover the environment identifiers: those
are per-invocation (which SCA, which manifest), not configuration, and
folding them in would give every child of one array a different
configuration digest for identical configuration.
"""

import dataclasses
import hashlib
import json
import logging
import os
from typing import Any, Mapping

from .manifest import Manifest, ProcessingUnit

logger = logging.getLogger(__name__)

# The tree, and the only tree: the job role's parameter read is scoped to
# exactly this path (rapid_systems cloudformation/rapid-batch.yaml).
PIPELINE_PARAMETER_PATH = "/rapid/pipeline"

# Environment identifiers the submitter sets (submit.build_submit_kwargs)
# plus the one Batch sets itself.
ENV_MANIFEST_URI = "RAPID_MANIFEST_URI"
ENV_BATCH_ID = "RAPID_BATCH_ID"
ENV_MANIFEST_CHECKSUM = "RAPID_MANIFEST_CHECKSUM"
ENV_ARRAY_INDEX = "AWS_BATCH_JOB_ARRAY_INDEX"


class ParameterFetchError(RuntimeError):
    """The pipeline parameter tree could not be read, or was empty.

    A job that cannot read its configuration must fail loudly at startup
    rather than run on defaults: silent fallback would produce science
    products whose configuration digest describes configuration the job
    never actually used.
    """


def fetch_parameters(path: str = PIPELINE_PARAMETER_PATH,
                     client: Any = None) -> dict[str, str]:
    """Read the pipeline parameter tree.

    One recursive `get_parameters_by_path` walk, paginated. Names are
    returned relative to `path`, so a consumer reads ``kafka/topic``
    rather than the full ``/rapid/pipeline/kafka/topic`` — the tree root
    is a deployment detail, not part of a parameter's identity.

    Parameters
    ----------
    path : str, optional
        Tree root. Defaults to the pipeline tree.
    client : object, optional
        SSM client. Injected in tests; a real boto3 client by default.

    Returns
    -------
    dict
        Relative parameter name -> value.

    Raises
    ------
    ParameterFetchError
        If the read fails, or the tree is empty.
    """
    if client is None:
        import boto3
        client = boto3.client("ssm")

    prefix = path.rstrip("/") + "/"
    parameters: dict[str, str] = {}

    try:
        kwargs: dict[str, Any] = {"Path": path, "Recursive": True,
                                  "WithDecryption": True}
        while True:
            response = client.get_parameters_by_path(**kwargs)
            for parameter in response.get("Parameters", []):
                name = parameter["Name"]
                relative = name[len(prefix):] if name.startswith(prefix) else name
                parameters[relative] = parameter["Value"]
            token = response.get("NextToken")
            if not token:
                break
            kwargs["NextToken"] = token
    except ParameterFetchError:
        raise
    except Exception as exc:                          # noqa: BLE001
        raise ParameterFetchError(
            f"could not read the pipeline parameter tree at {path}: {exc}"
        ) from exc

    if not parameters:
        raise ParameterFetchError(
            f"the pipeline parameter tree at {path} is empty; a job will "
            "not run on defaults")

    logger.info("read %d parameters from %s", len(parameters), path)
    return parameters


def configuration_digest(parameters: Mapping[str, str]) -> str:
    """Hash a configuration mapping into the attempt record's digest.

    Canonical: names sorted, fixed separators, no whitespace variance —
    so the digest depends on the configuration and nothing else.

    Returns
    -------
    str
        Hex SHA-256 over the canonical form.
    """
    canonical = json.dumps(dict(sorted(parameters.items())),
                           sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class JobContext:
    """Everything a starting job resolved about itself.

    What the pipeline code reads instead of poking at the environment and
    Parameter Store from a dozen places.
    """

    batch_id: str
    array_index: int
    unit: ProcessingUnit
    parameters: dict[str, str]
    config_digest: str
    manifest_uri: str
    manifest_checksum: str

    def parameter(self, name: str, default: str | None = None) -> str:
        """Read one configuration parameter.

        Raises
        ------
        KeyError
            If the parameter is absent and no default was given —
            a missing configuration value is a deployment fault.
        """
        if name in self.parameters:
            return self.parameters[name]
        if default is not None:
            return default
        raise KeyError(
            f"parameter {name!r} is not in the pipeline parameter tree "
            f"(read {len(self.parameters)} parameters)")


def resolve_job_context(environ: Mapping[str, str] | None = None,
                        manifest: Manifest | None = None,
                        manifest_loader: Any = None,
                        parameters: Mapping[str, str] | None = None,
                        ssm_client: Any = None,
                        path: str = PIPELINE_PARAMETER_PATH) -> JobContext:
    """The startup sequence: identifiers, parameters, digest, unit.

    Every external dependency has an injection point, so the whole
    sequence runs in a unit test with a dict for the environment, a
    Manifest in memory, and a fake SSM client.

    Parameters
    ----------
    environ : mapping, optional
        Defaults to ``os.environ``.
    manifest : Manifest, optional
        Pre-loaded manifest. If absent, `manifest_loader` fetches it from
        ``RAPID_MANIFEST_URI``.
    manifest_loader : callable, optional
        ``uri -> bytes``; typically ``S3ManifestStore.get``.
    parameters : mapping, optional
        Pre-fetched configuration. If absent, the tree is read.
    ssm_client : object, optional
        SSM client for the parameter read.

    Raises
    ------
    ParameterFetchError
        Configuration unreadable.
    ValueError
        A required identifier is missing, or the manifest does not match
        the checksum the submitter recorded.
    """
    env = os.environ if environ is None else environ

    raw_index = env.get(ENV_ARRAY_INDEX)
    # A plain (non-array) job has no index; it is the single-unit batch
    # case, and index 0 is its unit by construction.
    array_index = int(raw_index) if raw_index is not None else 0

    if manifest is None:
        uri = env.get(ENV_MANIFEST_URI)
        if not uri:
            raise ValueError(
                f"{ENV_MANIFEST_URI} is not set; the job cannot resolve which "
                "processing unit it is")
        if manifest_loader is None:
            raise ValueError(
                "no manifest and no manifest_loader; cannot fetch "
                f"{uri}")
        manifest = Manifest.from_json(manifest_loader(uri).decode("utf-8"))

    expected_checksum = env.get(ENV_MANIFEST_CHECKSUM)
    if expected_checksum and manifest.checksum() != expected_checksum:
        # The submitter recorded a checksum; a mismatch means this job is
        # reading a different manifest than the one that sized its array,
        # so its index binding cannot be trusted.
        raise ValueError(
            f"manifest checksum mismatch: job expected {expected_checksum}, "
            f"read {manifest.checksum()}")

    unit = manifest.unit_for_index(array_index)

    if parameters is None:
        parameters = fetch_parameters(path=path, client=ssm_client)
    parameters = dict(parameters)

    digest = configuration_digest(parameters)
    logger.info("job context: batch=%s index=%d unit=%s config_digest=%s",
                manifest.batch_id, array_index, unit.key, digest)

    return JobContext(
        batch_id=str(manifest.batch_id or env.get(ENV_BATCH_ID, "")),
        array_index=array_index,
        unit=unit,
        parameters=parameters,
        config_digest=digest,
        manifest_uri=env.get(ENV_MANIFEST_URI, ""),
        manifest_checksum=manifest.checksum(),
    )
