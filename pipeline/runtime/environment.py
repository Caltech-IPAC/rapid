"""
File:    environment.py

The per-invocation environment contract, read once at startup.

design/compute.md § Payload contract: "The submit-time environment carries
per-invocation identity only: the manifest URI, batch identity, and manifest
checksum, plus the scheduler's own job, attempt, array-index, and queue
variables." That is the complete list, and this module is the only place the
pipeline reads it.

**Fail-loud on absence, no defaults.** Every variable here is required, and a
missing one raises `ConfigError` naming it. This is the rev-5/rev-6 failure
class stated directly in the proposal: configuration that "depends on someone
remembering job-definition env entries". A default would convert a
misconfigured job definition into a job that runs against the wrong thing and
reports success — the exact failure the fail-loud posture exists to prevent.
Everything else the job needs comes from the parameter tree at startup, not
from here.

**`AWS_BATCH_JOB_ATTEMPT` is normalized, not trusted blindly.** Batch numbers
attempts from 1 and W1's recorded convention stores them one-based, so the
value passes through unchanged — but it is parsed, range-checked, and rejected
if it is zero or negative. A stored attempt index is a key into the resolver's
uniqueness constraint; a bad one there resolves to the wrong row rather than
failing.

**`AWS_BATCH_JOB_ARRAY_INDEX` is the one conditional variable.** Batch sets it
only for array children, and a non-array job legitimately has no array index.
It is therefore optional-but-typed: absent means "not an array child", which
is a real distinction, not a missing value to default to 0. Defaulting it to 0
would make every non-array job claim to be array child zero.
"""

import dataclasses
import os

from pipeline.runtime.errors import ConfigError

# The RAPID-authored per-invocation variables. The manifest carries everything
# else the invocation needs; these three are what locates and validates it.
ENV_MANIFEST_URI = "RAPID_MANIFEST_URI"
ENV_BATCH_ID = "RAPID_BATCH_ID"
ENV_MANIFEST_CHECKSUM = "RAPID_MANIFEST_CHECKSUM"

# Batch's own variables. Names fixed by the scheduler, not by us.
ENV_JOB_ID = "AWS_BATCH_JOB_ID"
ENV_JOB_ATTEMPT = "AWS_BATCH_JOB_ATTEMPT"
ENV_ARRAY_INDEX = "AWS_BATCH_JOB_ARRAY_INDEX"
ENV_JQ_NAME = "AWS_BATCH_JQ_NAME"

REQUIRED_VARIABLES = (
    ENV_MANIFEST_URI,
    ENV_BATCH_ID,
    ENV_MANIFEST_CHECKSUM,
    ENV_JOB_ID,
    ENV_JOB_ATTEMPT,
    ENV_JQ_NAME,
)

OPTIONAL_VARIABLES = (ENV_ARRAY_INDEX,)


@dataclasses.dataclass(frozen=True)
class JobEnvironment:
    """The complete per-invocation environment contract.

    Frozen: this is what the scheduler handed the container, and nothing in
    the runtime is entitled to change it after the fact. A stage that wants a
    different manifest is a stage with a bug, not one that should be able to
    rewrite the record of what it was asked to do.
    """

    manifest_uri: str
    batch_id: str
    manifest_checksum: str
    scheduler_job_id: str
    attempt_index: int
    queue_name: str
    array_index: int | None = None

    @property
    def is_array_child(self) -> bool:
        return self.array_index is not None

    @property
    def attempt_key(self) -> str:
        """A stable identifier for this attempt, safe as a path component.

        Scheduler job id plus attempt index: unique per attempt by
        construction, since Batch never reuses a job id and the attempt index
        distinguishes the retries of one job. Used for the working-directory
        name and as the identity the bundle and terminal-record keys derive
        from — which is what makes those keys reproducible by a reconciler
        that only has the row.
        """
        return f"{self.scheduler_job_id}-attempt-{self.attempt_index}"

    def as_dict(self) -> dict:
        """The contract as recorded in provenance and logs.

        Note what is NOT here: no full environment dump. The observability
        policy prohibits one, and this method exists partly so there is an
        obvious right thing to log instead of `dict(os.environ)`.
        """
        out = {
            "manifest_uri": self.manifest_uri,
            "batch_id": self.batch_id,
            "manifest_checksum": self.manifest_checksum,
            "scheduler_job_id": self.scheduler_job_id,
            "attempt_index": self.attempt_index,
            "queue_name": self.queue_name,
        }
        if self.array_index is not None:
            out["array_index"] = self.array_index
        return out


def read_environment(env: dict | None = None) -> JobEnvironment:
    """Read and validate the per-invocation contract. Raises `ConfigError`.

    All missing variables are reported together, not one per run. A job
    definition missing three env entries would otherwise take three
    submit-fail-fix cycles to correct, each one costing a container start.
    """
    source = os.environ if env is None else env

    missing = [name for name in REQUIRED_VARIABLES
               if not (source.get(name) or "").strip()]
    if missing:
        raise ConfigError(
            "the per-invocation environment contract is incomplete; missing: "
            + ", ".join(missing)
            + ". These are set by the job definition and the submission "
            "layer; there are no defaults for them, because a default would "
            "let a misconfigured job run against the wrong inputs and report "
            "success.",
            missing=",".join(missing))

    attempt_index = _require_positive_int(source, ENV_JOB_ATTEMPT)
    array_index = _optional_non_negative_int(source, ENV_ARRAY_INDEX)

    return JobEnvironment(
        manifest_uri=source[ENV_MANIFEST_URI].strip(),
        batch_id=source[ENV_BATCH_ID].strip(),
        manifest_checksum=source[ENV_MANIFEST_CHECKSUM].strip(),
        scheduler_job_id=source[ENV_JOB_ID].strip(),
        attempt_index=attempt_index,
        queue_name=source[ENV_JQ_NAME].strip(),
        array_index=array_index,
    )


def _require_positive_int(source: dict, name: str) -> int:
    raw = source[name].strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{name}={raw!r} is not an integer; the scheduler sets it to the "
            f"one-based attempt number", variable=name, value=raw) from exc
    if value < 1:
        raise ConfigError(
            f"{name}={value} is not one-based; Batch numbers attempts from 1 "
            f"and the stored convention is one-based (W1). A zero or negative "
            f"index would key the attempt resolver to the wrong row.",
            variable=name, value=raw)
    return value


def _optional_non_negative_int(source: dict, name: str) -> int | None:
    raw = (source.get(name) or "").strip()
    if not raw:
        # Absent is meaningful: this is not an array child. Not defaulted to
        # 0, which would be a claim rather than an absence.
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{name}={raw!r} is not an integer; Batch sets it to the "
            f"zero-based array child index", variable=name, value=raw) from exc
    if value < 0:
        raise ConfigError(f"{name}={value} is negative; array indexes are "
                          f"zero-based", variable=name, value=raw)
    return value


def describe(env: JobEnvironment) -> str:
    """A one-line, log-safe rendering of the contract."""
    parts = [f"job={env.scheduler_job_id}",
             f"attempt={env.attempt_index}",
             f"queue={env.queue_name}",
             f"batch={env.batch_id}"]
    if env.array_index is not None:
        parts.append(f"array_index={env.array_index}")
    parts.append(f"manifest={env.manifest_uri}")
    return " ".join(parts)


def redacting_environ(env: dict | None = None) -> dict:
    """The environment with sensitive VALUES removed, for a diagnostic dump.

    Provided so that if someone genuinely needs to see the environment while
    debugging, there is a safe way to do it — rather than the unsafe way being
    the only way. Still not called on any normal path: the observability
    policy prohibits complete environment dumps in diagnostics, and this
    exists for interactive use, not for the record.
    """
    # Imported here rather than at module scope: `process` imports nothing
    # from this module today, and keeping the dependency one-directional and
    # local means neither file constrains the other's import order.
    from pipeline.runtime.process import REDACTED, _sensitive_name

    source = os.environ if env is None else env
    return {name: (REDACTED if _sensitive_name(name) else value)
            for name, value in source.items()}
