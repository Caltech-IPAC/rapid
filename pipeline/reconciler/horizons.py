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

**THE SUBMISSION-ANCHORED HORIZON IS NOW A BACKSTOP, NOT THE TRUTH** (rule 7,
brief C1: "The time horizon may remain as a backstop for scheduler-side
silence, but it acts on a record that says CALLING/UNKNOWN — the state
machine, not the timestamp, is the truth"). It used to be the ENTIRE
resolution of an ambiguous submission: a pre-created child with a NULL
scheduler id waited out thirty minutes and was then classified, without anyone
ever asking Batch whether the job existed. That made two genuinely different
situations — a job that was accepted and is running, and a request that never
arrived — indistinguishable, because a clock cannot tell them apart.

`submission.protocol` now carries the durable record (PREPARED -> CALLING ->
BOUND / UNKNOWN -> FOUND / LOST) and resolves ambiguity by positively
re-querying Batch for the submission's deterministic job name. This horizon
survives underneath that, doing the narrower job it is actually suited to:
bounding how long a NEGATIVE re-query keeps meaning "not visible yet" before
it is allowed to mean "absent" (`protocol.RESOLUTION_HORIZON_SECONDS`, kept
equal to the value below so the two mechanisms cannot disagree while the
protocol is being adopted), and classifying rows for which no submission
record exists at all — every attempt predating DRAFT migration 044.

So the duration was never the defect and is unchanged. What changed is what
elapsed time is permitted to CONCLUDE: it no longer decides what happened to a
submission, it only bounds how long the evidence is allowed to stay silent.
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
