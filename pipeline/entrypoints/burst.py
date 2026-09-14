"""
File:    burst.py

A Batch job whose entire purpose is to hold one pooled database connection
open, let go, and take another — at fleet scale. Three thousand of these,
submitted as one array, exercise exactly the things a real payload's startup
connection exercises (the pooler's client ceiling, the STARTUP retry policy's
horizon, Batch's own consumable-resource gate on how many run at once) without
running a single line of science. It is a load-generator, not a payload.

    burst: connect (transaction lane, STARTUP horizon) -> SELECT 1
    burst: hold the connection for RAPID_BURST_HOLD_S
    burst: disconnect
    burst: print "burst: gap entered <UTC ISO-8601>"
    burst: sleep RAPID_BURST_GAP_S
    burst: print "burst: reconnecting <UTC ISO-8601>"
    burst: connect again (same policy) -> SELECT 1
    burst: exit 0

**Why NOT routed through `job.py`'s `--class` dispatch.** `job.py`'s
`parse_arguments` requires `--class` from a FIXED vocabulary
(`submission.routes.WORKLOAD_CLASSES`, currently `("prompt", "bulk")`) and
`main` runs the full attempt protocol on it — manifest fetch, route
validation, ownership resolution, a persisted configuration snapshot, a
started/terminated attempt row. None of that exists for this job: a burst run
has no manifest, no route, no attempt identity, and nothing worth recording as
a science outcome. Forcing it through that dispatch would mean either adding a
fake workload class the route matrix has to carry forever, or short-circuiting
`_run` before most of its own steps — both bend a single-purpose protocol
around a caller it was never meant to serve. This module is therefore its own
process entrypoint, run directly as
`python -m pipeline.entrypoints.burst`,
and a burst job definition's container command names this module rather than
`job` with a `--burst` flag `job.py` has no branch for.

**Exit codes.** `DBUnavailable` from either connect — the retry horizon
exhausted — exits `EXIT_UNRECORDABLE` (70), the SAME number
`pipeline.entrypoints.job` uses for its own "nothing below this point can
record anything" case: a burst job never creates a database row of its own to
record a categorized failure INTO, so a database that never became reachable
within its horizon is exactly that unrecordable case, not a classified
application failure. Any other unexpected exception also exits 70, for the
same reason job.py's outermost handler does: there is no attempt record to
carry a taxonomy category, so the only thing left to do is fail loud on the
safety stream and return nonzero so Batch reports FAILED.

**Configuration shape.** Environment variables only
(`RAPID_BURST_HOLD_S`, `RAPID_BURST_GAP_S`), no argv. `job.py`'s own
per-invocation configuration (`pipeline.runtime.environment.read_environment`)
is entirely environment-driven — Batch job definitions and array-child
overrides are both environment mechanisms, and `job.py` carries no numeric
`--flag` of its own for anything a manifest or the parameter tree does not
already supply. A burst job definition has no manifest and needs exactly two
tunables that differ only by scale (a fleet-wide soak wants a long hold and a
short gap; a churn test wants the reverse), so following the same shape means
one job definition can vary them per submission via the container's
environment overrides without a second argument-parsing path to keep in sync
with `job.py`'s. Both are read with a bounded default and a clear parse
failure rather than silently coercing garbage to zero.
"""

import datetime
import os
import sys

from database.modules.utils.rapid_db_connect import (
    LANE_TRANSACTION,
    STARTUP_BACKOFF_CAP_S,
    STARTUP_BACKOFF_INITIAL_S,
    STARTUP_BACKOFF_MULTIPLIER,
    STARTUP_CONNECT_ATTEMPTS,
    STARTUP_HORIZON_S,
    DBUnavailable,
    connection,
)
from pipeline.runtime import logging_setup

#: Same code `pipeline.entrypoints.job.main` returns for a failure with
#: nowhere of its own to record an outcome into (see the module docstring).
#: A burst job never has an attempt row, so every one of ITS failures is that
#: case, not merely the retry-horizon one.
EXIT_UNRECORDABLE = 70

#: Defaults match the requirement this job exists to prove: a five-minute
#: hold is long enough to occupy a pooler slot through several other jobs'
#: entire startup burst, and a gap of a bit over three minutes is short
#: enough that 3,000 of these cycling independently keep the door under
#: continuous churn rather than all going idle at once.
DEFAULT_HOLD_S = 300
DEFAULT_GAP_S = 200

_logger = logging_setup.get_logger("burst")


def _env_seconds(name: str, default: int) -> float:
    """Read a non-negative duration from the environment, or use the default.

    Fails loud on a value that does not parse rather than coercing it to the
    default — a typo'd job-definition override should be visible as a
    startup failure, not as this job silently running with the number nobody
    actually set.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return float(default)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{name}={raw!r} is not a number of seconds") from exc
    if value < 0:
        raise ValueError(f"{name}={raw!r} must not be negative")
    return value


def _utc_now_iso(clock=datetime.datetime.utcnow) -> str:
    """The exact timestamp shape the acceptance script parses.

    `datetime.datetime.utcnow()` (injected as `clock` for tests) rather than
    `now(tz=utc)`: this process runs on UTC-only infrastructure and the
    contract wants a bare `Z` suffix, not a `+00:00` offset — appending `Z`
    to a naive UTC timestamp is the whole job, and doing that to an
    already-aware one would need stripping the offset first for no benefit.
    """
    return clock().strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect_kwargs(application_name: str) -> dict:
    """The STARTUP retry policy, exactly as `job.py`'s own first connection
    uses it (see `pipeline.entrypoints.job._database`). Same lane, same
    horizon, same jittered backoff: a burst job is standing in for that
    connection at scale, so it has to retry on the identical policy or it
    would prove the wrong thing about the door it is hammering.
    """
    return dict(
        lane=LANE_TRANSACTION,
        attempts=STARTUP_CONNECT_ATTEMPTS,
        backoff_initial=STARTUP_BACKOFF_INITIAL_S,
        backoff_multiplier=STARTUP_BACKOFF_MULTIPLIER,
        backoff_cap=STARTUP_BACKOFF_CAP_S,
        horizon=STARTUP_HORIZON_S,
        jitter=True,
    )


def run(hold_s: float, gap_s: float, *, connect_fn=connection,
        sleep=None, clock=None) -> int:
    """The burst sequence. Returns the process exit code.

    `connect_fn` is `database.modules.utils.rapid_db_connect.connection` by
    default — the context-manager form, so the held connection is closed on
    every exit path including a `SELECT 1` that somehow raises, exactly the
    guarantee that module's own docstring gives for a leaked pooled slot.
    `sleep` and `clock` default to the real `time.sleep` /
    `datetime.datetime.utcnow` only when this runs as a process (see `main`);
    tests inject doubles here instead of patching module globals, matching
    `rapid_db_connect.connect`'s own injection-point convention.
    """
    import time as _time

    sleep = sleep or _time.sleep
    clock = clock or datetime.datetime.utcnow

    try:
        with connect_fn("rapid-burst", **_connect_kwargs("rapid-burst")) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            _logger.info("burst: holding the connection for %.0fs", hold_s)
            sleep(hold_s)
        # THE CONTRACT LINE. Exact format: lowercase "burst: ", the word,
        # a space, an ISO-8601 UTC timestamp ending in "Z" — an acceptance
        # script parses this, so it is printed verbatim rather than built
        # through the logger's own line format.
        print(f"burst: gap entered {_utc_now_iso(clock)}")

        sleep(gap_s)

        print(f"burst: reconnecting {_utc_now_iso(clock)}")

        with connect_fn("rapid-burst", **_connect_kwargs("rapid-burst")) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
    except DBUnavailable as exc:
        # The retry horizon on either connect was exhausted. No attempt row
        # exists to record this into (see the module docstring), so this is
        # the unrecordable case and gets that case's exit code.
        _logger.error("burst: database unavailable within the startup "
                     "horizon: %s", exc)
        return EXIT_UNRECORDABLE
    except Exception:  # noqa: BLE001 - the last resort, matching job.py's own
        _logger.exception("burst: unexpected failure")
        return EXIT_UNRECORDABLE

    return 0


def main(argv=None) -> int:  # noqa: ARG001 - no argv; env-only (see docstring)
    """Process entrypoint: read configuration, run the sequence, return exit code.

    `argv` is accepted and ignored rather than omitted from the signature, so
    this still matches the `main(argv=None) -> int` shape every entrypoint in
    this package uses (`job.main`) and remains callable the same way from a
    test or a wrapper that always passes one.
    """
    logging_setup.configure()

    try:
        hold_s = _env_seconds("RAPID_BURST_HOLD_S", DEFAULT_HOLD_S)
        gap_s = _env_seconds("RAPID_BURST_GAP_S", DEFAULT_GAP_S)
    except ValueError as exc:
        _logger.error("burst: %s", exc)
        return EXIT_UNRECORDABLE

    return run(hold_s, gap_s)


if __name__ == "__main__":
    sys.exit(main())
