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

import hashlib
import json
import logging
import os
from typing import Any, Mapping

logger = logging.getLogger(__name__)

# The tree, and the only tree: the job role's parameter read is scoped to
# exactly this path (rapid_systems cloudformation/rapid-batch.yaml).
PIPELINE_PARAMETER_PATH = "/rapid/pipeline"

# D9 (throughput sitting): measured live 2026-09-10 — a 1,000-job start
# burst had 218 jobs die at start-up because every container reads this
# tree with ONE GetParametersByPath call, no retry Config, no jitter, and
# SSM throttled the herd. `mode="adaptive"` is botocore's own client-side
# rate limiter, which is exactly the right shape for a self-inflicted
# thundering herd (it backs off the CLIENT's own request rate, not just the
# individual call) rather than `"standard"`, which retries but does not
# throttle the caller. 10 is the retry floor D9 sets; adaptive mode's own
# backoff schedule (not a fixed one this module controls) is what stretches
# that to a few minutes of possible total elapsed time under real
# throttling. Every boto3 client this module creates carries this Config —
# built lazily inside each function (deferred import, matching this
# module's own existing `import boto3` inside `fetch_parameters`) so a
# caller that never hits AWS never needs botocore importable.
def _retry_config():
    from botocore.config import Config
    return Config(retries={"max_attempts": 10, "mode": "adaptive"})

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
        # D9: adaptive retry mode, >= 10 attempts -- see `_retry_config`'s
        # header comment for the 2026-09-10 measurement this answers.
        client = boto3.client("ssm", config=_retry_config())

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
