"""
File:    retry_policy.py

The retry-policy decision the closure sites were missing: given a terminal
attempt disposition, does its work unit close, park, or wait for a sibling?

**WHY THIS MODULE EXISTS.** `observability.attempts` states the governing
policy in prose at its `RETRY_POLICY_VERSION` (version 1, verbatim): "every
application-failure category is park-until-change — no automatic retry; the
unit waits until a relevant input, configuration, software, or operational
condition changes, and is never tombstoned — while scheduler-visible
failures carry the condition-gated scheduler retry rows, the sole
automatic-retry surface under version 1." That sentence had no executable
counterpart. Every closure site instead hard-coded "any terminal
disposition that is not SUCCESS closes the unit `failed`", which contradicts
the policy twice over: it tombstones application failures the policy says
are never tombstoned, and it treats a Spot reclaim — a scheduler-visible
failure the policy hands to the automatic-retry surface — as a permanent
verdict on the logical work.

The conformance rule this restores (rule 4, minimal-viable target): "A work
unit closes only from an accepted result or explicit retry-policy
exhaustion — never from an intermediate physical failure." An attempt's
terminal failure is an INTERMEDIATE PHYSICAL EVENT. The work unit is the
LOGICAL work, and it outlives any one attempt at it.

**THE POLICY IS RELEASE CONTENT; THIS IS ITS DECISION FUNCTION.** The
category taxonomy itself is not restated here — it is imported from
`observability.attempts`, which holds the v1 allowlist mirroring migration
013's `attempt_error_categories` table. Two sets, two dispositions:

  * `RECONCILER_ERROR_CATEGORIES` (scheduler_reclaimed,
    scheduler_provisioning) — scheduler-visible. The scheduler lost the
    container; nothing was learned about the work. Version 1's "sole
    automatic-retry surface": the unit returns to `ready` so the ordinary
    submission path can author a NEW attempt (rule 5: every retry is a new
    RAPID attempt, never a Batch-internal one).
  * `APPLICATION_ERROR_CATEGORIES` (the eleven: tool_failure,
    input_missing, ... internal_error) — the application ran and reached a
    verdict. Retrying the identical inputs under the identical release would
    produce the identical failure, so version 1 parks: the unit becomes
    `blocked` with a `blocked_reason`, queryable by an operator, and returns
    to `ready` only through the audited mutation API once a condition
    changed. NOT `failed` — "never tombstoned".

**AN ABSENT CATEGORY IS NOT A THIRTEENTH CATEGORY.** A terminal failure
whose `error_category` is NULL is a row that failed to say why. That is
park-shaped, not retry-shaped: an unexplained failure repeated on a timer is
the unbounded-retry loop this policy exists to prevent, and the
conservative reading of "no automatic retry unless the policy names this
category as retryable" is to park it for a human. `UNCLASSIFIED_REASON`
below names that case explicitly in the `blocked_reason` so an operator
reading the parked unit sees "we do not know why", not a fabricated cause.

**EXHAUSTION IS THE ONLY PATH TO `failed`.** Under version 1 no category is
exhaustible by counting — application failures park indefinitely and
scheduler failures retry condition-gated — so `CLOSE_FAILED` is reachable
here only through `SCHEDULER_RETRY_CEILING`, the backstop that keeps a
pathological scheduler (reclaiming every attempt forever) from cycling a
unit ready->submitted->ready without bound. That ceiling is this module's
own v1 judgment call, not a quotation from the policy: the policy calls
scheduler retry "condition-gated" without stating the gate, and a gate that
is never closed is not a gate. Stated as a constant so a policy document
can move it, and deliberately generous — a unit that legitimately lost
twenty containers to Spot reclamation has a fleet problem an operator
should see as an explicit exhaustion, not as silent perpetual motion.
"""

import datetime
import logging

from observability.attempts import (
    APPLICATION_ERROR_CATEGORIES,
    RECONCILER_ERROR_CATEGORIES,
    RETRY_POLICY_VERSION,
)

logger = logging.getLogger(__name__)

# -- the four dispositions a closure site can be handed ----------------------
#
# Named rather than returned as bare strings so a caller cannot silently
# mis-spell one into a no-op branch, and so the set is enumerable in tests.

#: The attempt succeeded and its result was accepted: close `complete`.
#: The ONLY disposition that may close a unit complete (rule 4).
CLOSE_COMPLETE = "close_complete"

#: Retry policy is exhausted for this category: close `failed`. Under
#: version 1 this is reachable only via SCHEDULER_RETRY_CEILING.
CLOSE_FAILED = "close_failed"

#: Park-until-change: transition `blocked` with a reason. The unit is alive;
#: an operator or a changed condition revives it through the mutation API.
PARK_BLOCKED = "park_blocked"

#: A scheduler-visible loss under the retry ceiling: return the unit to
#: `ready` so a NEW attempt can be authored by the ordinary path.
RETRY_READY = "retry_ready"

DISPOSITIONS = frozenset({
    CLOSE_COMPLETE, CLOSE_FAILED, PARK_BLOCKED, RETRY_READY,
})

#: How many scheduler-visible losses one work unit may absorb before its
#: retry surface is declared exhausted. See the module docstring: a v1
#: judgment call standing in for the policy's unstated "condition gate",
#: deliberately generous.
SCHEDULER_RETRY_CEILING = 20

#: The application failure categories a RUN'S RETRY BUDGET may be spent on
#: (migration 122, the 2026-09-13 13:01 ruling). Named as a set rather than
#: left implicit because the ruling's own words are "at most 3 attempts in
#: total for TRANSIENT failures, none for DETERMINISTIC ones", and which
#: category is which is the whole content of that distinction.
#:
#: The three here are the ones where running the same unit again could
#: plausibly succeed without anything else changing:
#:
#:   `db_unavailable`   — the pooler refused, or the database was unreachable.
#:                        The pooler-refusal category the ruling names; the
#:                        client's own fifteen-minute backoff horizon already
#:                        absorbed the short outages, so an attempt spent here
#:                        is one that outlasted it.
#:   `db_error`         — a transaction lost to a conflict or a restart. Same
#:                        reasoning: the input was fine and nothing about the
#:                        unit needs to change.
#:   `resource_exhausted` — memory or disk ran out on THIS placement. Another
#:                        placement may have more, so a retry is not obviously
#:                        pointless. Deliberately included, deliberately
#:                        bounded: 3 attempts is what stops a unit that is
#:                        simply too big from consuming Batch's whole ceiling.
#:
#: EVERY OTHER APPLICATION CATEGORY STILL PARKS, unchanged. `input_missing`,
#: `input_invalid`, `config_invalid`, `reference_missing`, `tool_failure`,
#: `storage_error`, `records_error` and `internal_error` are deterministic:
#: the same unit, the same inputs, the same code, so the second attempt fails
#: exactly as the first did and the retry is pure waste. Parking is right for
#: them because they need a CHANGE — an operator, a fixed input, a new
#: release — not another try.
#:
#: SCHEDULER-VISIBLE LOSSES ARE NOT IN THIS SET, and that is the ruling too:
#: "Spot reclaims never counted". They keep their own path and their own
#: ceiling below, because a reclaim is not a failure of the work at all.
TRANSIENT_APPLICATION_CATEGORIES = frozenset({
    "db_unavailable", "db_error", "resource_exhausted",
})

#: The `blocked_reason` for a terminal failure that never said why. Kept
#: distinct from every real category so an operator can grep for the
#: "we do not know" population specifically.
UNCLASSIFIED_REASON = "unclassified_failure"

#: The `blocked_reason` prefix for a genuine application-failure park. The
#: category is appended, so the parked row names its own cause
#: ("application_failure:input_missing") — migration 036 requires a
#: non-NULL blocked_reason on every blocked unit, and a reason that merely
#: said "blocked" would satisfy the constraint while telling an operator
#: nothing.
APPLICATION_PARK_PREFIX = "application_failure"


def disposition_for_terminal_attempt(*, succeeded, error_category,
                                     scheduler_loss_count=0,
                                     transient_failure_count=0,
                                     retry_attempts=None,
                                     first_submitted_at=None,
                                     retry_wallclock_s=None,
                                     now=None):
    """What a terminal attempt means for its WORK UNIT, per policy v1.

    `succeeded` is the application's own accepted verdict — not the
    scheduler's exit code, and not "the row reached a terminal state".
    Only an accepted result closes a unit complete.

    `error_category` is the attempt row's category, one of the thirteen v1
    values or None. `scheduler_loss_count` is how many scheduler-visible
    losses this unit's attempt series has already absorbed, used only for
    the ceiling.

    THE RUN'S RETRY BUDGET (migration 122, the 2026-09-13 13:01 ruling).
    `transient_failure_count` is how many attempts this unit has already spent
    on `TRANSIENT_APPLICATION_CATEGORIES`; `retry_attempts` and
    `retry_wallclock_s` are the run's own two bounds; `first_submitted_at` is
    when the unit was first submitted, which is what the wall-clock is
    measured from. A transient application failure re-releases the unit while
    both bounds hold and closes it failed — an exhausted unit — when either is
    reached.

    All five are optional and default to the pre-122 behaviour, so every
    existing call site keeps its meaning: with no `retry_attempts` the
    transient branch is not taken at all and every application category parks
    exactly as before. That is deliberate rather than convenient — a run that
    declared no budget has not asked for retries, and inventing one for it
    would start re-running work nobody asked to have re-run.

    SCHEDULER-VISIBLE LOSSES ARE NEVER COUNTED AGAINST THE RUN'S BUDGET, which
    is the ruling's "Spot reclaims never counted". They are handled by the
    branch above, against `SCHEDULER_RETRY_CEILING`, and never reach the
    transient branch — a reclaim says nothing about the work, so spending the
    run's budget on it would let a bad afternoon on the Spot market exhaust
    units whose science never once failed.

    Returns one of `DISPOSITIONS`. The caller performs the transition; this
    function performs no I/O and reads no row, so it is exhaustively
    testable without a database — the same discipline
    `observability.attempts` states for its own validation helpers.
    """
    if succeeded:
        return CLOSE_COMPLETE

    if error_category in RECONCILER_ERROR_CATEGORIES:
        # Scheduler-visible: the container was lost, the work was not judged.
        if scheduler_loss_count >= SCHEDULER_RETRY_CEILING:
            logger.warning(
                "work unit has absorbed %d scheduler-visible losses "
                "(ceiling %d); declaring retry policy exhausted rather than "
                "cycling it ready->submitted again",
                scheduler_loss_count, SCHEDULER_RETRY_CEILING)
            return CLOSE_FAILED
        return RETRY_READY

    # THE RUN'S OWN RETRY BUDGET (migration 122, the 2026-09-13 13:01
    # ruling). A transient application failure may be retried — but only
    # while BOTH bounds hold, and only when the run declared a budget at all.
    #
    # `retry_attempts is None` means the unit's run carries no envelope: a run
    # predating 122, or a unit with no run row. The behaviour there is exactly
    # what it was before this branch existed — park — because inventing a
    # budget for a run that never declared one would start retrying work
    # nobody asked to have retried.
    if (error_category in TRANSIENT_APPLICATION_CATEGORIES
            and retry_attempts is not None):
        exhausted_by = _budget_exhausted_by(
            transient_failure_count=transient_failure_count,
            retry_attempts=retry_attempts,
            first_submitted_at=first_submitted_at,
            retry_wallclock_s=retry_wallclock_s,
            now=now)
        if exhausted_by is None:
            return RETRY_READY
        # AN EXHAUSTED UNIT — the term every record uses. The log names which
        # bound closed it, because "it stopped retrying" and "it ran out of
        # time" and "it ran out of attempts" are three different operational
        # situations and the row alone cannot distinguish them.
        logger.warning(
            "work unit exhausted its run's retry budget on %s failures "
            "(%s); closing it failed rather than re-releasing it",
            error_category, exhausted_by)
        return CLOSE_FAILED

    # Everything else parks. This deliberately catches BOTH the eleven
    # application categories and an absent/unrecognized one: see the module
    # docstring on why an unexplained failure is park-shaped, not
    # retry-shaped.
    return PARK_BLOCKED


def _budget_exhausted_by(*, transient_failure_count, retry_attempts,
                         first_submitted_at, retry_wallclock_s, now):
    """Which bound, if either, has closed this unit's retry budget.

    Returns a short phrase naming the bound that ended it, or None while the
    unit may still be retried.

    TWO BOUNDS, BOTH NECESSARY, AND THE WALL-CLOCK IS NOT DECORATION. The
    2026-09-13 overnight run is why: its body finished at 06:30 and its clock
    ran past 11:00 on a retry loop. An attempt COUNT alone did not stop that,
    because each re-release started a fresh attempt series — so the count kept
    resetting while the wall-clock kept running. The elapsed-time bound is
    measured from the unit's FIRST submission for exactly that reason: it is
    the one quantity a re-release cannot reset.

    A missing `first_submitted_at` or `retry_wallclock_s` disables only the
    wall-clock half; the count still applies. Absent data is not evidence the
    budget is spent, and closing a unit failed on a NULL timestamp would fail
    work for a reason that has nothing to do with the work.
    """
    if transient_failure_count >= retry_attempts:
        return ("%d of %d attempts used"
                % (transient_failure_count, retry_attempts))
    if first_submitted_at is None or retry_wallclock_s is None:
        return None
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    if first_submitted_at.tzinfo is None:
        raise ValueError(
            "first_submitted_at %r is naive; every timestamp in this system "
            "is stored timestamptz and read back aware, and comparing a naive "
            "one against an aware `now` would raise deep inside the "
            "disposition rather than here" % (first_submitted_at,))
    elapsed = (moment - first_submitted_at).total_seconds()
    if elapsed >= retry_wallclock_s:
        return ("%.0fs elapsed of a %ds wall-clock budget"
                % (elapsed, retry_wallclock_s))
    return None


#: The `blocked_reason` for a unit parked because its effect-attempt series
#: exhausted the retry ceiling (ruling R1). Distinct from `UNCLASSIFIED_
#: REASON` and `APPLICATION_PARK_PREFIX`-based reasons: those describe an
#: attempt-level failure category, and an unconfirmed effect is not a
#: failure category at all — it is a claim/confirm protocol outcome with no
#: `error_category` to name. A dedicated reason string keeps an operator
#: from reading "unclassified_failure" and looking for a failed attempt when
#: what actually happened is a series of swallowed confirm-path database
#: errors.
EFFECT_RETRY_EXHAUSTED_REASON = "effect_retry_exhausted"


def disposition_for_unconfirmed_effect(*, effect_attempt_count):
    """What a terminal SKIP with disposition `effect_unconfirmed` means for
    its WORK UNIT (ruling R1, effect-lifecycle completion boundary).

    A SEPARATE decision function from `disposition_for_terminal_attempt`,
    not a branch inside it, because it is invoked from a different call site
    under a different lock: the registration consumer's SKIP-consume
    handler, under the per-attempt lease (`pipeline.registration.consumer.
    ATTEMPT_LEASE_NAMESPACE`), never the reconciler — an `effect_unconfirmed`
    attempt is already `terminal_after_start` by the time registration ever
    sees it, so the reconciler's own closure policy has already run and
    found nothing terminal to do (the attempt's own outcome was `success`,
    which is `CLOSE_COMPLETE`-shaped to `disposition_for_terminal_attempt`
    — it is only the EFFECT that failed to confirm, a fact the reconciler's
    attempt-level policy has no vocabulary for).

    `effect_attempt_count` is how many of the unit's attempts have already
    closed with an `effect_*` disposition — the unit's OWN series count for
    THIS policy, deliberately not `scheduler_loss_count` from the sibling
    function: a scheduler-visible loss and an unconfirmed effect are
    different populations absorbing different ceilings, and conflating them
    would let one exhaust the other's budget. Counted by the caller (a
    `product_disposition = ANY('{effect_confirmed,effect_unconfirmed,
    effect_deferred}')` scan over the unit's attempts), not here — this
    function performs no I/O, matching `disposition_for_terminal_attempt`'s
    own discipline.

    Returns `RETRY_READY` under `SCHEDULER_RETRY_CEILING` (the SAME ceiling
    constant — one v1 judgment call about "how many times is worth trying
    again", not two), or `PARK_BLOCKED` at or over it. Never `CLOSE_COMPLETE`
    or `CLOSE_FAILED` under this function directly: confirmation success
    closes the unit through the ordinary `effect_confirmed` -> `CLOSE_
    COMPLETE` path this function is never consulted for, and version 1 does
    not tombstone an unconfirmed effect any more than it tombstones an
    application failure — see the module docstring's "never tombstoned".
    """
    if effect_attempt_count >= SCHEDULER_RETRY_CEILING:
        logger.warning(
            "work unit has absorbed %d unconfirmed-effect attempts "
            "(ceiling %d); parking rather than re-gathering again",
            effect_attempt_count, SCHEDULER_RETRY_CEILING)
        return PARK_BLOCKED
    return RETRY_READY


def blocked_reason_for(error_category):
    """The `blocked_reason` text for a parked unit.

    Migration 036's `work_units_blocked_reason_ck` makes a non-NULL reason a
    constraint of being blocked, so this never returns None.
    """
    if error_category in APPLICATION_ERROR_CATEGORIES:
        return f"{APPLICATION_PARK_PREFIX}:{error_category}"
    if error_category:
        # A category the database admits but this policy version does not
        # classify — recorded verbatim rather than flattened into
        # "unclassified", so the row keeps the fact it carried.
        return f"{APPLICATION_PARK_PREFIX}:{error_category}"
    return UNCLASSIFIED_REASON


def policy_version():
    """The policy version a closure decision was made under.

    Recorded in unit_event detail by the closure sites so a reader of an old
    event can tell which regime governed it — the same reasoning
    `observability.attempts` gives for stamping `retry_policy_version` on
    every attempt.
    """
    return RETRY_POLICY_VERSION
