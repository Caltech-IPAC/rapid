"""The two horizons, which are not the same clock.

A scheduler-terminal observation and a missing record is not immediately a
fault: the job may be mid-termination, writing its bundle and record right now.
The **grace horizon** is how long the reconciler waits after the scheduler says
the attempt ended before classifying what it finds.

A pre-created child whose scheduler identifier never resolves has no scheduler
observation to be graceful about — there is nothing to wait *after*. It is
bounded instead by the **submission-anchored horizon**, measured from the
submission timestamp the row already carries.

Both starting values are replaceable by evidence without re-ratification. They
are stated here, once, rather than spelled inline at the call sites, so that
changing one is a one-line change with a test that names the value.
"""

import datetime

# Between a scheduler-terminal observation and classifying a missing or
# contradictory record.
GRACE_HORIZON_SECONDS = 10 * 60

# Between submission and classifying a child whose scheduler identifier never
# resolved. Deliberately longer: it covers queue time, and a queue that is
# merely slow must not be read as a lost child.
SUBMISSION_HORIZON_SECONDS = 30 * 60


def _elapsed(since, now):
    """Seconds between two aware datetimes, or None if `since` is absent.

    A naive datetime is a bug in the caller, not something to paper over with
    an assumed timezone: every timestamp in this system is stored timestamptz
    and read back aware.
    """
    if since is None:
        return None
    if since.tzinfo is None:
        raise ValueError(
            f"naive timestamp {since!r}: horizons compare aware datetimes only")
    return (now - since).total_seconds()


def beyond_grace_horizon(scheduler_stopped_at, now=None,
                         horizon=GRACE_HORIZON_SECONDS):
    """Has the grace period after a scheduler-terminal observation elapsed?

    False when the stop time is unknown — an attempt the scheduler has not
    reported stopped is not inside *or* past this horizon; it is not yet
    subject to it at all.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    elapsed = _elapsed(scheduler_stopped_at, now)
    if elapsed is None:
        return False
    return elapsed >= horizon


def beyond_submission_horizon(submitted_at, now=None,
                              horizon=SUBMISSION_HORIZON_SECONDS):
    """Has a never-resolved child outlived its submission-anchored horizon?

    False when the submission time is unknown, for the same reason: with no
    anchor there is no horizon to be past. Such a row is a different fault —
    a submitted attempt with no submitted_at — and is not this predicate's to
    classify.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    elapsed = _elapsed(submitted_at, now)
    if elapsed is None:
        return False
    return elapsed >= horizon
