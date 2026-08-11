"""Gathering-time BLOCKED work units: the missing dependency, made queryable.

Conformance rule 13: "Missing dependencies (e.g. reference coverage) leave
work `BLOCKED` without consuming attempts". The mechanism for the second half
of that sentence already existed — package A made `blocked` a real, actively
written state (`pipeline.intent.writer`, and the reconciler's park-on-
application-failure edge) — but only for TERMINAL APPLICATION FAILURES, which
is a post-attempt cause. The pre-attempt cause the rule is actually about had
no writer at all: `submission.gathering` raised `NotReadyYet` in memory, its
caller logged it at INFO and continued, and nothing was persisted. An operator
asking "what is blocked, and why" got no answer, because there was no row to
ask.

WHY THIS IS A SEPARATE MODULE RATHER THAN A WRITE INSIDE `gathering`.
`submission.gathering` takes a `UnitSource` — a deliberately narrow read-only
protocol over `RAPIDDB` (see its docstring: "the slice of `rapid_db.RAPIDDB`
gathering actually uses... the tests' stub implements exactly these"). Every
function in that module reads and yields; none writes. Putting an intent-layer
INSERT inside it would widen that protocol from a read slice to a read/write
one and put a work-unit writer in the module whose whole testability story is
that it needs no database to write to. The gatherer keeps raising and yielding;
this module owns the persistence, and the caller composes the two. That is the
same product/database-effect separation rule 8 enforces one layer down — and
repairing rule 13 by violating rule 8's shape would be a poor trade.

THE REASON PREFIX IS THE QUERYABILITY CONTRACT (brief C4: "Blocked-without-
attempt rows are visible to the same queries as A's application-failure parks
(they share the state; keep reasons distinguishable by prefix)"). Both causes
write `work_units.state = 'blocked'`, so one query over that state finds both
— which is the point, an operator wants one blocked list. They stay tellable
apart by the reason's prefix, and neither side invents its own vocabulary:

    application_failure:<category>   the reconciler's park (package A)
    missing_dependency:<dependency>  this module's park (rule 13)

NO ATTEMPT IS CONSUMED, and that is structural rather than promised: nothing
in this module touches `attempts`, `logical_jobs` or the submission path at
all. A unit parked here has never been submitted, so there is no attempt row
to consume; the unit simply sits in `blocked` until a later gathering pass
finds its dependency satisfied and transitions it `blocked -> ready` through
the existing graph edge, which is exactly the edge package A's `_TRANSITION_
GRAPH` already admits for any writer.

IDEMPOTENCE ACROSS PASSES. Gathering runs on a schedule, and a field that is
short of reference coverage tonight is short of it on every pass until new
frames arrive. So `record_blocked` is find-or-create, not create: the first
pass creates the unit `blocked`, and every later pass on the same unresolved
dependency finds the existing row and leaves it alone (refreshing the reason
only if the dependency description changed). Without that, one unripe field
would mint a work unit per poll forever — and migration 036's partial unique
index on `(job_type, input_scope)` would start refusing them, turning an
ordinary early-survey state into a stream of errors.
"""

import logging

from pipeline.intent.errors import is_unique_violation
from pipeline.intent.writer import (
    BLOCKED, READY, WRITER_VALIDATION_INGEST, WorkUnitIdentity, WorkUnitWriter)

logger = logging.getLogger("rapid.submission.blocked")

#: The reason prefix for a dependency that is not yet satisfied, paired with
#: the reconciler's `application_failure:` prefix (`pipeline.intent.
#: retry_policy.blocked_reason_for`). Named here rather than spelled inline at
#: each call site so the two halves of the blocked vocabulary are each defined
#: exactly once.
MISSING_DEPENDENCY_PREFIX = "missing_dependency"

#: The dependency name for rule 13's own worked example. Reference coverage is
#: "this field has not been visited enough times to build a reference image
#: from", which is the ordinary state of a field early in the survey and the
#: case `submission.gathering.coadd_input_rows` raises `NotReadyYet` for.
REFERENCE_COVERAGE = "reference_coverage"


def blocked_reason_for_dependency(dependency):
    """The `blocked_reason` string naming an unsatisfied dependency.

    Deliberately parallel to `pipeline.intent.retry_policy.blocked_reason_for`,
    which builds the reconciler's `application_failure:<category>` half. Two
    prefixes, one state, one query — see the module docstring.
    """
    return f"{MISSING_DEPENDENCY_PREFIX}:{dependency}"


def is_missing_dependency(blocked_reason):
    """Is this park a missing dependency, as opposed to an application failure?

    The prefix test, written once. An operator surface that wants to show the
    two causes apart asks this rather than re-deriving the string shape.
    """
    return bool(blocked_reason) and blocked_reason.startswith(
        MISSING_DEPENDENCY_PREFIX + ":")


def record_blocked(execute, job_type, input_scope, operational_class,
                   dependency, definition_version=1, now=None):
    """Park this unit `blocked` on an unsatisfied dependency. Returns its id.

    Find-or-create, for the reason the module docstring gives: a dependency
    that is not satisfied on this pass is usually not satisfied on the next
    one either, and a create-only writer would mint a unit per poll.

    The three outcomes, all ordinary:

    1. **No current unit** — this pass is the creator. The unit is created
       directly in `blocked` with its reason, which `create_work_unit` already
       supports (package A: "accepts `state=BLOCKED` with mandatory
       `blocked_reason`") and migration 036's `work_units_blocked_reason_ck`
       requires. Note it is created blocked rather than created ready and then
       transitioned: the unit has never been workable, so a `ready` row —
       even momentarily — would be a lie the gatherer could pick up.
    2. **A unit already `blocked`** — the ordinary repeat pass. Nothing is
       written unless the reason has changed (a dependency description that
       sharpened between passes is worth keeping current); no event is
       recorded for an unchanged reason, because nothing transitioned.
    3. **A unit in any other state** — left completely alone. A unit that is
       `ready`, `submitted`, `complete` or `quarantined` is owned by another
       writer right now, and gathering discovering that ITS dependency looks
       unsatisfied is not authority to interrupt work already in flight. The
       most common shape of this is benign and racy: a unit submitted between
       the gather query and this call.

    The unique-violation branch mirrors `pipeline.seams._attach_work_unit`'s
    (rule 6's repair): two gathering passes racing on one identity means the
    other one won, the row this call needed exists, and losing that race is a
    success for this caller's purpose. Recognized by SQLSTATE 23505 through
    `pipeline.intent.errors`, never by matching words in a message.
    """
    reason = blocked_reason_for_dependency(dependency)
    writer = WorkUnitWriter(execute)
    identity = WorkUnitIdentity(
        job_type=job_type, input_scope=input_scope,
        operational_class=operational_class,
        definition_version=definition_version)

    existing = writer.find_current_unit(job_type, input_scope)
    if existing is None:
        try:
            work_unit_id = writer.create_work_unit(
                identity, writer=WRITER_VALIDATION_INGEST, state=BLOCKED,
                blocked_reason=reason, now=now)
        except Exception as exc:  # noqa: BLE001 - re-raised unless 23505
            if not is_unique_violation(exc):
                raise
            existing = writer.find_current_unit(job_type, input_scope)
            if existing is None:
                # A unique violation whose winning row cannot be found is a
                # contradiction, not a race — raise rather than loop, exactly
                # as the submission seam does.
                raise
            return _refresh_existing(writer, existing, reason, job_type,
                                     input_scope, now=now)
        logger.info(
            "work unit %s parked blocked at gathering: %s/%s is %s",
            work_unit_id, job_type, input_scope, reason)
        return work_unit_id

    return _refresh_existing(writer, existing, reason, job_type, input_scope,
                             now=now)


def _refresh_existing(writer, existing, reason, job_type, input_scope,
                      now=None):
    """Outcomes 2 and 3 of `record_blocked`. Returns the work unit id."""
    work_unit_id = existing["work_unit_id"]
    if existing["state"] != BLOCKED:
        logger.debug(
            "work unit %s for %s/%s is %s, not blocked; gathering leaves it "
            "to its owner rather than parking work already in flight",
            work_unit_id, job_type, input_scope, existing["state"])
        return work_unit_id

    if existing["blocked_reason"] != reason:
        writer.amend_blocked_reason(work_unit_id, reason, now=now)
        logger.info(
            "work unit %s stays blocked; reason refreshed to %s",
            work_unit_id, reason)
    return work_unit_id


def release_blocked(execute, job_type, input_scope, writer_identity, now=None):
    """A dependency is satisfied: transition this unit `blocked -> ready`.

    The other half of rule 13's requirement — "a later pass that finds the
    dependency satisfied transitions it blocked->ready through the existing
    graph". `blocked -> ready` is an ordinary forward edge in
    `_TRANSITION_GRAPH` (writer `None`, so any writer may fire it), and this
    goes through `transition_unit` rather than writing the UPDATE itself, so
    the transition gets its `unit_events` row like every other one.

    Returns True when this call transitioned the unit, False when there was
    nothing to transition — no current unit, or a unit not in `blocked`. The
    second is a race, not an error: another pass, or an operator, may have
    released it while this one was gathering. `WorkUnitNotFound` from the CAS
    is caught for the same reason and reported as False.
    """
    from pipeline.intent.writer import WorkUnitNotFound

    work_writer = WorkUnitWriter(execute)
    existing = work_writer.find_current_unit(job_type, input_scope)
    if existing is None or existing["state"] != BLOCKED:
        return False

    try:
        work_writer.transition_unit(
            existing["work_unit_id"], BLOCKED, READY, writer=writer_identity,
            reason="dependency satisfied", now=now)
    except WorkUnitNotFound:
        # Another writer moved it off `blocked` between the SELECT and the
        # CAS. The unit is no longer blocked, which is what this call wanted.
        logger.debug(
            "work unit %s left 'blocked' before this release could fire",
            existing["work_unit_id"])
        return False
    logger.info("work unit %s released blocked->ready: %s/%s",
                existing["work_unit_id"], job_type, input_scope)
    return True
