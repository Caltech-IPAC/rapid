"""
The execution runtime: process execution, stage accounting, attempt ownership,
and the termination protocol.

W2 of the Batch payload co-design. Everything the payload does operationally
goes through this package — external commands, stage spans, the attempt row,
the diagnostics bundle, the terminal record — so that the fail-loud posture is
a property of one module rather than a convention ~60 call sites are each
expected to remember.

Modules
-------
errors
    The v1 error taxonomy: exception classes carrying an explicit category,
    and the one serialization function.
logging_setup
    One logging configuration — UTC, identifiers on every line, stdout plus
    per-stage bundle files. `print()` is banned here and lint-enforced.
process
    `run_tool` (argv, shell=False, checked) and its one named shell variant.
    There is no unchecked variant.
stages
    `run_stage` / `stage_context`: span-shaped stage records, two clocks,
    written once at completion including on failure.
environment
    The per-invocation environment contract, read once, fail-loud on absence.
science_config
    The reader for release-versioned science content (`cdf/science/
    pipeline.toml`) — the third configuration home, carried by the image
    and identified by its digest. No overrides, no defaults, no merge.
ownership
    Attempt resolution at startup through W1's atomic claim-or-create
    resolver.
boundaries
    The narrow S3 interface the protocol writes through, plus an in-memory
    implementation that enforces the real semantics for tests.
termination
    The startup and termination protocols, and the key derivations they use.
workdir
    The per-attempt working directory: derived paths, no cwd dependence.

The database boundary is W1's `ConnectionExecutor` under
`observability.attempts.AttemptWriter`; the S3 boundary is
`boundaries.ObjectStore`. Both are injected, so the whole package is testable
without a database or a bucket — which is what lets crash-at-every-boundary
recovery be unit-proven rather than only observed live.
"""

from pipeline.runtime.errors import (  # noqa: F401
    APPLICATION_ERROR_CATEGORIES,
    ERROR_CATEGORIES,
    RECONCILER_ERROR_CATEGORIES,
    ConfigError,
    DBError,
    InputError,
    RecordsError,
    ResourceError,
    RuntimeErrorBase,
    StorageError,
    ToolError,
    categorize,
    serialize_error,
)
from pipeline.runtime.logging_setup import (  # noqa: F401
    RuntimeLogger,
    StageFileHandler,
    configure,
    get_logger,
)
from pipeline.runtime.process import (  # noqa: F401
    ToolResult,
    redact,
    run_shell,
    run_tool,
)
from pipeline.runtime.science_config import (  # noqa: F401
    auxiliary_identity,
    load_science_config,
    science_config_digest,
)
from pipeline.runtime.stages import (  # noqa: F401
    SKIPPED,
    StageRecord,
    StageRecorder,
    run_stage,
    stage_context,
)
from pipeline.runtime.workdir import WorkingDirectory  # noqa: F401

__all__ = [
    "APPLICATION_ERROR_CATEGORIES",
    "ERROR_CATEGORIES",
    "RECONCILER_ERROR_CATEGORIES",
    "ConfigError",
    "DBError",
    "InputError",
    "RecordsError",
    "ResourceError",
    "RuntimeErrorBase",
    "RuntimeLogger",
    "SKIPPED",
    "StageFileHandler",
    "StageRecord",
    "StageRecorder",
    "StorageError",
    "ToolError",
    "ToolResult",
    "WorkingDirectory",
    "auxiliary_identity",
    "categorize",
    "configure",
    "get_logger",
    "load_science_config",
    "redact",
    "run_shell",
    "run_stage",
    "run_tool",
    "science_config_digest",
    "serialize_error",
    "stage_context",
]
