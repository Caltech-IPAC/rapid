"""
File:    errors.py

The runtime error taxonomy: exception classes carrying an explicit error
category, and the one function that serializes them.

design/observability.md makes the machine-readable error category one of the
five distinct outcome fields, drawn from "an allowlist versioned with the
schema". The Batch payload co-design supplies version 1 of that enumeration —
thirteen categories — and migration 013 enforces it with a foreign key onto
`attempt_error_categories`. This module is the application half of that
contract.

**The category is an attribute set at raise time, not a type lookup.** Each
class below carries a default `error_category`, but the raiser may override it
per-instance, and the serializer reads the instance. This matters because the
mapping is not one-to-one: `InputError` covers both `input_missing` and
`input_invalid`, `ConfigError` covers `config_invalid` and
`reference_missing`, and `DBError` covers `db_unavailable` and `db_error`. The
proposal states this outright — the "Raised as" column of the taxonomy table
is indicative, not the discriminator. A caller that raises `InputError` without
saying which kind gets `input_invalid`, the class default, which is a real
statement rather than a guess: we have the input and it is not usable.

**Two categories are not raisable here.** `scheduler_reclaimed` and
`scheduler_provisioning` are reconciler-authored: they describe attempts the
application never classified, and by construction the application is not
running to raise them. They are in the allowlist this module knows (so
`is_valid_category` accepts them, since one shared vocabulary is the point)
but `RuntimeErrorBase` refuses to carry one, so a runtime path cannot author a
fact whose single writer is the reconciler.

`internal_error` is reached by NOT raising one of these: it is what
`categorize` returns for any exception that is not a `RuntimeErrorBase`, which
is precisely the proposal's "any uncaught" row.
"""

import dataclasses
import traceback
from typing import Any

# The v1 allowlist. Mirrors migration 013's `attempt_error_categories` table
# and `observability.attempts.ERROR_CATEGORIES`; the database remains the
# authority, this is the local copy that lets a typo fail before a round trip.
#
# Deliberately duplicated rather than imported from observability.attempts:
# this module is the runtime's own contract and must be importable without
# pulling in the attempt writer. The two are pinned equal by a test
# (`test_errors.test_allowlist_matches_attempt_writer`), which is what makes
# the duplication safe rather than a second source of truth drifting quietly.
APPLICATION_ERROR_CATEGORIES = frozenset({
    "tool_failure",
    "input_missing",
    "input_invalid",
    "config_invalid",
    "reference_missing",
    "db_unavailable",
    "db_error",
    "storage_error",
    "records_error",
    "resource_exhausted",
    "internal_error",
})

# Authored only by the reconciler, for attempts the application never
# classified. Present here so the vocabulary has one definition; never
# raisable by runtime code.
RECONCILER_ERROR_CATEGORIES = frozenset({
    "scheduler_reclaimed",
    "scheduler_provisioning",
})

ERROR_CATEGORIES = APPLICATION_ERROR_CATEGORIES | RECONCILER_ERROR_CATEGORIES

# What an exception that is not ours classifies as.
UNCLASSIFIED_CATEGORY = "internal_error"


def is_valid_category(category: Any) -> bool:
    """True if `category` is in the v1 allowlist."""
    return category in ERROR_CATEGORIES


class RuntimeErrorBase(Exception):
    """Base for every classified runtime failure.

    Subclasses set `error_category` as a class default; a raiser that knows
    better passes `category=` and the instance carries that instead.

    The constructor validates: a category outside the allowlist, or one of the
    two reconciler-authored categories, raises `ValueError` at the raise site.
    That is deliberate — a mislabeled category discovered at the database
    round trip is a constraint violation with no stack pointing at the code
    that chose it, while this fails where the choice was made.
    """

    error_category = "internal_error"

    def __init__(self, message: str, category: str | None = None, **details: Any):
        super().__init__(message)
        if category is not None:
            if category in RECONCILER_ERROR_CATEGORIES:
                raise ValueError(
                    f"{category!r} is reconciler-authored: the application "
                    f"cannot author a category describing an attempt it never "
                    f"ran. Application categories are "
                    + ", ".join(sorted(APPLICATION_ERROR_CATEGORIES)))
            if not is_valid_category(category):
                raise ValueError(
                    f"{category!r} is not in the v1 error-category allowlist; "
                    f"expected one of " + ", ".join(sorted(ERROR_CATEGORIES))
                    + " (extending the vocabulary is a schema-versioned change)")
            self.error_category = category
        # Structured context for the terminal record and the log line. Kept
        # free-form deliberately: what identifies a tool failure (argv, exit
        # code) is not what identifies a storage failure (bucket, key). The
        # allowlisting that the observability policy requires happens at
        # emission, in `serialize_error`, not here.
        self.details = details


class ToolError(RuntimeErrorBase):
    """An external command exited nonzero, or its binary was not found."""

    error_category = "tool_failure"


class InputError(RuntimeErrorBase):
    """An expected input is absent, unreadable, or failed validation.

    Defaults to `input_invalid`. A caller that knows the input is simply not
    there passes `category="input_missing"` — the distinction is worth
    keeping because the two have different operational responses (re-run
    upstream vs investigate corruption).
    """

    error_category = "input_invalid"


class ConfigError(RuntimeErrorBase):
    """Configuration is missing, inconsistent, or rejects the route.

    Also the class for `reference_missing` — an unavailable reference-data
    version is a configuration fault in the sense that matters here: the job
    was asked to run against something that does not exist.
    """

    error_category = "config_invalid"


class DBError(RuntimeErrorBase):
    """A database query, constraint, or transaction failed."""

    error_category = "db_error"


class StorageError(RuntimeErrorBase):
    """An S3 read or write failed OUTSIDE the records path.

    The records path has its own category (`RecordsError`) because its
    failures have a different meaning: a job that cannot write its terminal
    record cannot record anything at all, and the fail-loud posture sends it
    to a nonzero exit rather than a clean one.
    """

    error_category = "storage_error"


class RecordsError(RuntimeErrorBase):
    """The attempt-record or terminal-record path failed.

    The unrecordable case. Under the adopted posture, a caught application
    failure exits 0 with its outcome recorded; this class is what makes the
    exception — if the records path itself is broken, there is nowhere to put
    the outcome, and the process must exit nonzero so the reconciler treats
    it as a case rather than believing a record that was never written.
    """

    error_category = "records_error"


class ResourceError(RuntimeErrorBase):
    """The application detected disk or memory exhaustion.

    Application-detected only. A container killed by the OOM killer never
    reaches this code — that attempt is reconciler-classified from scheduler
    state, which is the division of labour the two-writer schema exists for.
    """

    error_category = "resource_exhausted"


def categorize(exc: BaseException) -> str:
    """Return the v1 error category for any exception.

    The single mapping point. Our own exceptions answer for themselves; every
    other exception is `internal_error`, which is the allowlist's row for
    "any uncaught". There is no inference from exception type here — no
    treating `OSError` as `storage_error`, no reading `errno` — because a
    guess dressed as a classification is worse than the honest
    `internal_error`: the whole point of the taxonomy is that a category is a
    statement someone made, not one the code inferred.

    One exception to "ours answer for themselves": a `RuntimeErrorBase`
    subclass whose `error_category` has been set to something outside the
    allowlist (only reachable by assigning the attribute directly, bypassing
    the constructor) is reported as `internal_error` rather than propagating
    an invalid value toward the database's foreign key.
    """
    if isinstance(exc, RuntimeErrorBase):
        category = exc.error_category
        if category in APPLICATION_ERROR_CATEGORIES:
            return category
        return UNCLASSIFIED_CATEGORY
    return UNCLASSIFIED_CATEGORY


@dataclasses.dataclass(frozen=True)
class SerializedError:
    """The serialized form of a failure: what goes into the terminal record."""

    error_category: str
    error_type: str
    message: str
    details: dict
    traceback: str | None = None

    def as_dict(self) -> dict:
        out = {
            "error_category": self.error_category,
            "error_type": self.error_type,
            "message": self.message,
            "details": dict(self.details),
        }
        if self.traceback is not None:
            out["traceback"] = self.traceback
        return out


def serialize_error(exc: BaseException, include_traceback: bool = True,
                    redactor=None) -> SerializedError:
    """Serialize an exception for the terminal record and the log.

    The one tested serialization function the proposal calls for. Three things
    it does that a bare `str(exc)` does not:

    **It classifies.** `error_category` comes from `categorize`, so the record
    always carries an allowlist value.

    **It redacts.** Free text from RAPID and external tools is permitted in
    diagnostics (observability policy), but "never credentials, tokens,
    complete environment dumps". A tool's error message can quote its own
    argv, and an argv can carry a secret. `redactor` — normally
    `pipeline.runtime.process.redact` — is applied to the message, to every
    detail value, and to the traceback text. Passing `redactor=None` skips
    redaction and is for tests only; production paths always supply one.

    **It bounds `details` to JSON-safe scalars.** Anything that is not a str,
    int, float, bool, or None is rendered with `repr` — so a detail carrying a
    live socket or a numpy array cannot make the record unserializable at the
    moment the job is trying to report a failure.
    """
    apply = redactor if redactor is not None else (lambda text: text)

    details: dict = {}
    raw_details = getattr(exc, "details", None)
    if isinstance(raw_details, dict):
        for key, value in raw_details.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                safe = value
            else:
                safe = repr(value)
            details[str(key)] = apply(safe) if isinstance(safe, str) else safe

    tb = None
    if include_traceback and exc.__traceback__ is not None:
        tb = apply("".join(traceback.format_exception(
            type(exc), exc, exc.__traceback__)))

    return SerializedError(
        error_category=categorize(exc),
        error_type=type(exc).__name__,
        message=apply(str(exc)),
        details=details,
        traceback=tb,
    )
