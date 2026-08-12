"""Step 3 — the safety horizon, which FAILS CLOSED.

**THE ELIGIBILITY CLOCK IS CONTINUOUS ABSENCE FROM EVERY REFERENCE SET, NOT
THE AGE OF ANY PRODUCING ATTEMPT.** This is the single decision most likely to
be got wrong, so the reasoning is recorded rather than assumed:

  * **A terminal attempt does NOT mean retry is over.** A scheduler-visible
    loss closes the ATTEMPT (terminal, `ended_at` set) while returning the
    WORK UNIT to `ready`, so the ordinary submission path authors a NEW
    attempt (`pipeline/intent/retry_policy.py:32-34`;
    `pipeline/reconciler/service.py:1396-1398`). Anchoring eligibility to the
    producing attempt's terminality would make a live, about-to-be-retried
    unit's objects deletable.

  * **Producing-attempt age is not a safety property at all.** A two-year-old
    object whose database reference is dropped today would instantly pass an
    `ended_at` horizon — yet a PITR restore to yesterday revives that
    reference after the bytes are gone.

  * So the clock STARTS WHEN THE LAST REFERENCE DISAPPEARS. An object
    protected for a year while `quarantined` does not become age-eligible the
    instant an operator moves the unit out: a newly-dereferenced object serves
    the FULL horizon from that moment, however old it is.

**THE HORIZON IS AN EXTERNAL INPUT AND THERE IS NO DEFAULT THAT PERMITS
DELETION.** It must exceed the pgBackRest PITR retention and every real
retry/recovery hold. This module does not guess it and does not hard-code a
number as though derived: a GC run with no configured horizon deletes nothing
and says why.

`rapid_systems`'s `cloudformation/rapid-postgres-pgbackrest.conf:32` sets
`repo1-retention-full=4` — four full backups, weekly full plus daily
differential with continuous WAL archiving. That is a COUNT, not a duration,
and the cadence is expressly provisional, so no duration can be derived from
the repository. CR-H4 requests the authoritative value.

**THE EFFECTIVE HORIZON IS THE MAXIMUM OF THE CONFIGURED VALUES, NEVER A SUM
and never less than any one of them.** A sum would be arbitrary; a minimum
would defeat the longest hold, which is the one that matters.
"""

import datetime


class HorizonUnset(Exception):
    """No horizon is configured, so nothing may be deleted.

    Not an error in the run — a REFUSAL to delete, which is a conforming
    outcome. The plan is still computed and recorded; every candidate is
    retained under `no-horizon` and the reason is on the plan.
    """

    error_category = "gc_horizon_unset"


def effective_horizon(*values):
    """The MAXIMUM of the configured horizons, or None if none is set.

    `None` entries are ignored — an unconfigured component is not a zero. If
    every component is unset the result is `None`, which fails closed
    downstream.
    """
    known = [int(value) for value in values
             if value is not None and int(value) > 0]
    if not known:
        return None
    return max(known)


def describe(values):
    """The provenance string recorded on the plan beside the horizon.

    A horizon without a stated provenance is a guess wearing a number, and
    DRAFT 052's CHECK requires both or neither. `values` maps a component name
    to its seconds; unset components are named as unset, because "the PITR
    window was not supplied" is exactly what a reviewer needs to see.
    """
    parts = []
    for name in sorted(values):
        value = values[name]
        parts.append("%s=%s" % (name, "<unset>" if value is None else value))
    return "max(" + ", ".join(parts) + ")"


def elapsed_since(first_seen_absent, horizon_seconds, now=None):
    """Has an object been continuously absent for the full horizon?

    `first_seen_absent` is when the object was FIRST OBSERVED ABSENT from
    every reference surface — not when it was written, and not when its
    attempt ended. That is the whole distinction this module exists to draw.
    """
    if horizon_seconds is None:
        raise HorizonUnset(
            "no safety horizon is configured, so no object can be shown to "
            "have been continuously absent for long enough to delete. There "
            "is deliberately no default that permits deletion: the horizon "
            "must exceed the pgBackRest PITR retention and every real "
            "retry/recovery hold, and that duration is an external input "
            "this code does not guess.")
    if first_seen_absent is None:
        return False
    now = now or datetime.datetime.now(datetime.timezone.utc)
    first = first_seen_absent
    if first.tzinfo is None:
        first = first.replace(tzinfo=datetime.timezone.utc)
    return (now - first).total_seconds() >= horizon_seconds


def continuously_absent(first_pass_absent, second_pass_absent,
                        first_seen_absent, horizon_seconds, now=None):
    """The full eligibility test: absent in BOTH passes, horizon elapsed.

    §4.11 step 5 is MANDATORY, not optional: only candidates absent in both
    the plan pass and the recomputation survive. Anything that reappeared is
    excluded from the execution set by status — its row and the plan checksum
    are untouched.
    """
    if not first_pass_absent or not second_pass_absent:
        return False
    return elapsed_since(first_seen_absent, horizon_seconds, now=now)
