"""
File:    writer.py

Writing work_units, unit_events, and campaigns: migration 036's tables.

design/operations.md § Workflow schema makes this module's shape explicit
in a way `observability.attempts` had to earn through review findings:
"Writers are exclusive per transition class: validation/ingest creates,
the orchestrator submits and applies retry dispositions, the reconciler
closes, the mutation API does operator overrides." This module accepts a
`writer` argument on every call precisely because the SAME Python function
is invoked by more than one transition-class writer in practice (a
campaign-staging transformer creates units under 'validation_ingest'; the
submission seam transitions the same unit under 'orchestrator') and the
design's exclusivity rule is a CODE discipline over WHO passes which
value, not a fact this module can enforce by construction. Migration 036's
`unit_events_writer_ck` bounds the vocabulary; this module does not
re-narrow it per function, because more than one writer identity is
legitimately reachable from more than one call site.

**Two state machines, two writer classes.**

`WorkUnitWriter` implements the six-state work-unit machine: create,
transition (CAS-guarded, transition-graph-checked), and supersede
(set-once, separate from ordinary transition per the design's "Supersession
is a set-once pointer, not a state").

`CampaignWriter` implements the five-state campaign machine: defined ->
active <-> paused -> complete | abandoned. Campaigns have no unit_events
analogue — the design gives campaigns no event history, only the state
column itself ("progress is never stored, always derived from its
units" — the SAME principle extends to campaign STATE history, which this
v1 does not audit-trail beyond the plain column migration 036 provides).

**The database boundary is the same injected `execute(sql, params)`
callable `observability.attempts` uses** — not a live connection, not
psycopg2 imported here. This is what lets the whole module be tested
without a database, exactly as that module's own docstring states the
rule, and it is what lets the reconciler and the submission seam share
one transaction with THIS module's writes when they need to (see
`pipeline/reconciler/service.py`'s `_Executor(conn)` and this module's own
`intent_events_written_in_the_same_transaction` test note in
`pipeline/intent/test/test_writer.py`).

**Transition legality is enforced in Python, not SQL.** Migration 036's
header says so explicitly: "Writer exclusivity is a code rule, not a
schema one" — and the same is true one level down, for transition
legality. The DDL's `work_units_state_ck` bounds the six-value
vocabulary; it has no CHECK on which (from_state, to_state) pairs are
legal, because encoding a state machine's edges in a CHECK constraint
over a self-referencing UPDATE is exactly the kind of code-level dispatch
discipline 036's header says this schema deliberately leaves to callers.
`_TRANSITION_GRAPH` below is therefore the one place the graph is written
down, and `transition_unit` refuses an edge that graph does not name
BEFORE issuing any SQL.

**THE CHOSEN TRANSITION GRAPH (v1 judgment call — the design states
states, not the full edge list).** The design's own text names four edges
explicitly: blocked->ready ("park-until-change folds in here" implies
resumption into ready), ready->submitted, submitted->complete,
submitted->failed, and "failed -> ready and quarantined -> ready pass
only through the audited mutation API." Quarantine's entry edges are not
named at all beyond "quarantined" appearing in the state list, so this
module admits quarantine from every non-terminal state that a mutation-API
override might reasonably need to interrupt (blocked, ready, submitted) —
"any -> quarantined" per the task brief's own suggested shape — since an
operator override quarantining a unit must be able to interrupt it at
whatever state it is caught in, not just one. complete and failed are
terminal for ORDINARY (writer != mutation_api) transitions; only
failed->ready and quarantined->ready are legal, and ONLY for
writer='mutation_api', matching "pass only through the audited mutation
API" verbatim. This graph is a v1 judgment call, stated here so a later
ruling can revise it without spelunking through call sites.
"""

import dataclasses
import datetime
import json
import logging
from typing import Any, Protocol, Sequence

logger = logging.getLogger(__name__)

# -- the six work-unit states, verbatim from migration 036 -------------------
BLOCKED = "blocked"
READY = "ready"
SUBMITTED = "submitted"
COMPLETE = "complete"
FAILED = "failed"
QUARANTINED = "quarantined"

WORK_UNIT_STATES = frozenset({
    BLOCKED, READY, SUBMITTED, COMPLETE, FAILED, QUARANTINED,
})

# -- the four writer classes migration 036's unit_events_writer_ck admits ----
WRITER_VALIDATION_INGEST = "validation_ingest"
WRITER_ORCHESTRATOR = "orchestrator"
WRITER_RECONCILER = "reconciler"
WRITER_MUTATION_API = "mutation_api"

WRITERS = frozenset({
    WRITER_VALIDATION_INGEST, WRITER_ORCHESTRATOR, WRITER_RECONCILER,
    WRITER_MUTATION_API,
})

# -- the five campaign states, verbatim from migration 036 -------------------
DEFINED = "defined"
ACTIVE = "active"
PAUSED = "paused"
CAMPAIGN_COMPLETE = "complete"
ABANDONED = "abandoned"

CAMPAIGN_STATES = frozenset({
    DEFINED, ACTIVE, PAUSED, CAMPAIGN_COMPLETE, ABANDONED,
})

#: The legal (from_state, to_state) edges of the work-unit machine, keyed by
#: the writer class permitted to fire them. `None` as the writer means "any
#: writer may fire this edge" — used only for the ordinary forward edges,
#: never for the mutation-only recoveries. See the module docstring for the
#: reasoning behind each edge.
_TRANSITION_GRAPH: dict[tuple[str, str], str | None] = {
    (BLOCKED, READY): None,
    (READY, SUBMITTED): None,
    (SUBMITTED, COMPLETE): None,
    (SUBMITTED, FAILED): None,
    # THE TWO EDGES RETRY POLICY v1 NEEDS AND v1's FIRST GRAPH OMITTED
    # (rule 4 repair). The graph above was written when every closure site
    # hard-coded "terminal and not SUCCESS -> failed", so the only edges out
    # of `submitted` were the two verdicts. `pipeline.intent.retry_policy`
    # makes the policy executable, and it yields two more outcomes that were
    # unreachable:
    #
    #   submitted -> blocked  park-until-change. The policy says application
    #                         failures are "never tombstoned"; parking is
    #                         how a unit survives one. Without this edge the
    #                         only way to record an application failure was
    #                         the tombstone the policy forbids.
    #   submitted -> ready    a scheduler-visible loss (Spot reclaim, OOM
    #                         kill) under the retry ceiling. Nothing was
    #                         learned about the work, so the unit returns to
    #                         the queue for a NEW attempt (rule 5) rather
    #                         than inheriting a verdict from a container's
    #                         death.
    #
    # Both are ORDINARY forward edges (writer None): the reconciler fires
    # them as part of closing an attempt, exactly as it already fires
    # submitted->complete/failed. Neither is an operator override, so
    # neither is gated to the mutation API.
    (SUBMITTED, BLOCKED): None,
    (SUBMITTED, READY): None,
    (BLOCKED, QUARANTINED): None,
    (READY, QUARANTINED): None,
    (SUBMITTED, QUARANTINED): None,
    # "Failed -> ready and quarantined -> ready pass only through the
    # audited mutation API" -- quoted exactly, and enforced exactly: these
    # two edges exist in the graph but are refused for any writer other than
    # WRITER_MUTATION_API (see `transition_unit`'s writer check below).
    (FAILED, READY): WRITER_MUTATION_API,
    (QUARANTINED, READY): WRITER_MUTATION_API,
}


class IntentError(RuntimeError):
    """Base class for this module's errors."""


class IllegalTransition(IntentError):
    """A transition was requested that the state machine does not admit.

    Raised BEFORE any SQL is issued (module docstring: "Python, not SQL") —
    the DDL's vocabulary CHECK would not catch this at all, since
    `to_state` alone is always a legal vocabulary member; only the (from,
    to) PAIR can be illegal, and nothing in the schema encodes pairs.
    """


class WrongWriterForTransition(IllegalTransition):
    """The transition is legal in the abstract, but not for this writer.

    Raised for failed->ready and quarantined->ready when the caller is not
    `mutation_api` — the one pair of edges the design restricts to a named
    writer class rather than leaving open to whichever writer reaches the
    state machine first.
    """


class WorkUnitNotFound(IntentError):
    """A CAS-guarded transition matched no row.

    Mirrors `observability.attempts.AttemptNotFound` exactly: either the
    work unit does not exist, or — the interesting case — it has already
    left the expected `from_state`, meaning a concurrent or replayed writer
    reached it first and this compare-and-set correctly refused to
    overwrite that writer's account.
    """


class SupersessionConflict(IntentError):
    """`supersede_unit` was called on a unit that already has a successor.

    "Supersession is a set-once pointer" (design, quoted in the module
    docstring) — once `superseded_by_unit_id` is non-NULL it is never
    expected to change, and this is the guard that makes "set-once" true
    rather than aspirational.
    """


class Executor(Protocol):
    """The database boundary — identical contract to `observability.
    attempts.Executor`: one callable, rows for a statement with a result
    set, an affected-row count otherwise. Repeated here rather than
    imported so this module has no import-time dependency on
    `observability` beyond what it actually reuses (none) — the two
    modules are siblings under the same convention, not one importing the
    other's plumbing.
    """

    def __call__(self, sql: str, params: Sequence[Any]) -> Any:
        ...


@dataclasses.dataclass(frozen=True)
class WorkUnitIdentity:
    """The work-unit identity quadruple migration 036's schema names.

    (job_type, input_scope, operational_class, definition_version) — the
    partial unique index enforces one NON-SUPERSEDED unit per
    (job_type, input_scope), so a caller that finds an existing unit
    matches on those two columns alone; operational_class and
    definition_version are carried at creation but are not part of the
    uniqueness the index enforces (a definition-version migration is
    explicitly a mutation-API action per the design, not a fresh unit
    under a new identity).
    """

    job_type: str
    input_scope: str
    operational_class: str
    definition_version: int


def _require_known_state(state: str, *, param_name: str) -> None:
    if state not in WORK_UNIT_STATES:
        raise ValueError(
            f"{param_name}={state!r} is not one of the six work-unit "
            f"states: {', '.join(sorted(WORK_UNIT_STATES))}")


def _require_known_writer(writer: str) -> None:
    if writer not in WRITERS:
        raise ValueError(
            f"writer={writer!r} is not one of the four transition-class "
            f"writers migration 036 admits: {', '.join(sorted(WRITERS))}")


class WorkUnitWriter:
    """Creates and transitions work_units rows, with their unit_events.

    Parameters
    ----------
    execute : callable
        ``execute(sql, params)`` against work_units/unit_events. The SAME
        callable a caller passes to `observability.attempts.AttemptWriter`
        when the two need to share a transaction (the reconciler's closure
        integration does exactly this — see `pipeline/reconciler/service.py`).
    """

    def __init__(self, execute: Executor):
        self._execute = execute

    # -- creation -------------------------------------------------------

    def create_work_unit(self, identity: WorkUnitIdentity, *, writer: str,
                         state: str = READY,
                         blocked_reason: str | None = None,
                         campaign_id: int | None = None,
                         reason: str | None = None,
                         detail: dict | None = None,
                         now: Any = None) -> int:
        """Create one work_units row and its creation unit_event.

        `writer` is parameterized rather than fixed to
        `WRITER_VALIDATION_INGEST` because campaign staging (the mock
        transformer, part 5) is ALSO a creator under the design's writer
        model — "Mock inputs enter through the same validation/ingest
        writer as real inputs" makes validation_ingest the default, but a
        caller creating campaign-scoped units under a different declared
        creator class may need to say so explicitly rather than this
        module silently assuming one identity for every creator.

        `state` defaults to READY (the common case: a unit is
        immediately workable) but accepts BLOCKED, in which case
        `blocked_reason` is REQUIRED — migration 036's own CHECK
        (`work_units_blocked_reason_ck`) enforces this at the database, but
        it is validated here first so the failure names the missing reason
        at the caller rather than arriving as a bare constraint violation.

        Both writes — the work_units INSERT and its unit_events creation
        row (from_state NULL, per migration 036: "one row per transition;
        from_state NULL on the unit's first event (creation)") — are
        issued through the SAME `execute` call sequence a caller's own
        transaction wraps; this module never opens or closes a transaction
        itself, matching `AttemptWriter`'s convention exactly.
        """
        _require_known_writer(writer)
        _require_known_state(state, param_name="state")
        if state == BLOCKED and not blocked_reason:
            raise ValueError(
                "blocked_reason is required when state='blocked' "
                "(migration 036: \"blocked (always with a "
                "machine-readable reason)\")")
        if state != BLOCKED and blocked_reason is not None:
            raise ValueError(
                "blocked_reason must be None unless state='blocked' — "
                "migration 036's CHECK forbids a reason on any other state")

        moment = now or datetime.datetime.now(datetime.timezone.utc)

        sql = (
            "INSERT INTO work_units ("
            "  job_type, input_scope, operational_class, definition_version,"
            "  state, blocked_reason, campaign_id, created_at, updated_at"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " RETURNING work_unit_id"
        )
        rows = self._execute(sql, [
            identity.job_type, identity.input_scope,
            identity.operational_class, identity.definition_version,
            state, blocked_reason, campaign_id, moment, moment,
        ])
        work_unit_id = _single_value(rows)

        self._record_event(work_unit_id, from_state=None, to_state=state,
                           writer=writer, reason=reason, detail=detail,
                           now=moment)

        logger.info("created work unit %s (%s/%s) in state %s",
                    work_unit_id, identity.job_type, identity.input_scope,
                    state)
        return work_unit_id

    def find_current_unit(self, job_type: str, input_scope: str
                          ) -> dict | None:
        """The non-superseded work_units row for (job_type, input_scope), if any.

        Reads through the partial unique index's own predicate
        (`superseded_by_unit_id IS NULL`) — the SELECT half of the
        find-or-create shape `pipeline.seams._precreate` uses (see that
        module's docstring for the full race-tolerant sequence: SELECT
        first, INSERT, re-SELECT on conflict).
        """
        sql = (
            "SELECT work_unit_id, job_type, input_scope, operational_class,"
            "  definition_version, state, blocked_reason, campaign_id"
            " FROM work_units"
            " WHERE job_type = %s AND input_scope = %s"
            "   AND superseded_by_unit_id IS NULL"
        )
        rows = self._execute(sql, [job_type, input_scope])
        return _single_row_or_none(rows, columns=(
            "work_unit_id", "job_type", "input_scope", "operational_class",
            "definition_version", "state", "blocked_reason", "campaign_id"))

    # -- transitions ------------------------------------------------------

    def transition_unit(self, work_unit_id: int, from_state: str,
                        to_state: str, *, writer: str,
                        blocked_reason: str | None = None,
                        reason: str | None = None,
                        detail: dict | None = None,
                        now: Any = None) -> None:
        """CAS-guarded state transition, refused if the graph does not admit it.

        The Python-side legality check runs BEFORE any SQL — see the
        module docstring for why the DDL cannot catch an illegal (from,
        to) pair on its own. The SQL itself is still a real
        compare-and-set (`WHERE work_unit_id = %s AND state = %s`,
        verifying exactly one row updated): the Python check rejects an
        edge the machine never admits, and the SQL guard rejects a race
        where another writer already moved the row off `from_state` — two
        different failure modes, both real, neither substitutable for the
        other.
        """
        _require_known_writer(writer)
        _require_known_state(from_state, param_name="from_state")
        _require_known_state(to_state, param_name="to_state")

        edge = (from_state, to_state)
        if edge not in _TRANSITION_GRAPH:
            raise IllegalTransition(
                f"{from_state!r} -> {to_state!r} is not a legal work-unit "
                f"transition; the declared graph is "
                + ", ".join(f"{a}->{b}" for a, b in sorted(_TRANSITION_GRAPH)))
        required_writer = _TRANSITION_GRAPH[edge]
        if required_writer is not None and writer != required_writer:
            raise WrongWriterForTransition(
                f"{from_state!r} -> {to_state!r} passes only through the "
                f"audited mutation API (writer={required_writer!r}); got "
                f"writer={writer!r}")

        if to_state == BLOCKED and not blocked_reason:
            raise ValueError(
                "blocked_reason is required when transitioning to 'blocked'")
        if to_state != BLOCKED and blocked_reason is not None:
            raise ValueError(
                "blocked_reason must be None unless to_state='blocked'")

        moment = now or datetime.datetime.now(datetime.timezone.utc)

        sql = (
            "UPDATE work_units SET state = %s, blocked_reason = %s,"
            "  updated_at = %s"
            " WHERE work_unit_id = %s AND state = %s"
        )
        result = self._execute(sql, [
            to_state, blocked_reason, moment, work_unit_id, from_state,
        ])
        _require_one_row(result, "transition_unit", work_unit_id,
                         expected_state=from_state)

        self._record_event(work_unit_id, from_state=from_state,
                           to_state=to_state, writer=writer, reason=reason,
                           detail=detail, now=moment)

        logger.info("work unit %s: %s -> %s (writer=%s)",
                    work_unit_id, from_state, to_state, writer)

    def amend_blocked_reason(self, work_unit_id: int, blocked_reason: str, *,
                             now: Any = None) -> bool:
        """Refresh the reason of a unit that stays `blocked`. NOT a transition.

        A repeated gathering pass over a dependency that is still unsatisfied
        has nothing to transition — the unit was blocked and remains blocked —
        but the reason's DETAIL can legitimately sharpen between passes (a
        dependency named more precisely once more is known about it). Writing
        that through `transition_unit` would be wrong twice over: `blocked ->
        blocked` is not an edge the graph admits, and recording a
        `unit_events` row for it would claim a transition that did not happen,
        against migration 036's "one row per transition" and invariant 1's
        append-only history being a history OF TRANSITIONS.

        So this updates the reason column and records no event, and it is
        CAS-guarded on `state = 'blocked'` so it cannot resurrect a reason
        onto a unit another writer has since released — which
        `work_units_blocked_reason_ck` would refuse anyway, but refusing here
        names the race instead of surfacing a constraint violation.

        Returns True when a row was updated, False when the unit was no longer
        blocked. False is an ordinary race, not an error: the caller wanted the
        unit's reason current, and a unit that is no longer blocked has no
        reason to keep current.
        """
        if not blocked_reason:
            raise ValueError(
                "amend_blocked_reason needs a reason; a blocked unit must "
                "always carry one (work_units_blocked_reason_ck)")

        moment = now or datetime.datetime.now(datetime.timezone.utc)
        sql = (
            "UPDATE work_units SET blocked_reason = %s, updated_at = %s"
            " WHERE work_unit_id = %s AND state = %s"
        )
        result = self._execute(sql, [
            blocked_reason, moment, work_unit_id, BLOCKED,
        ])
        if _rowcount(result, "amend_blocked_reason") == 0:
            logger.debug(
                "work unit %s is no longer blocked; reason not amended",
                work_unit_id)
            return False
        logger.info("work unit %s blocked_reason amended to %s",
                    work_unit_id, blocked_reason)
        return True

    def supersede_unit(self, old_work_unit_id: int, new_work_unit_id: int, *,
                       writer: str, reason: str | None = None,
                       now: Any = None) -> None:
        """Set the set-once supersession pointer. Refuses if already set.

        Deliberately NOT folded into `transition_unit`: "Supersession is a
        set-once pointer, not a state" (design, quoted above) — a unit can
        be superseded from ANY state, including one still `ready`, so this
        is not a state-machine edge at all and does not go through the
        transition graph. Migration 036 leaves the set-once half to code
        ("enforcement of the 'set-once' half is a code/trigger concern the
        design does not ask this migration to add") — this is that code.
        """
        _require_known_writer(writer)
        if old_work_unit_id == new_work_unit_id:
            raise ValueError(
                "a work unit cannot supersede itself "
                "(work_units_not_self_superseding_ck)")

        moment = now or datetime.datetime.now(datetime.timezone.utc)
        sql = (
            "UPDATE work_units SET superseded_by_unit_id = %s,"
            "  updated_at = %s"
            " WHERE work_unit_id = %s AND superseded_by_unit_id IS NULL"
        )
        result = self._execute(sql, [
            new_work_unit_id, moment, old_work_unit_id,
        ])
        if _rowcount(result, "supersede_unit") == 0:
            raise SupersessionConflict(
                f"work unit {old_work_unit_id} already has a successor, or "
                f"does not exist; supersession is a set-once pointer and "
                f"cannot be reassigned")

        # Not a state transition (from_state/to_state unchanged), so this
        # is recorded with detail carrying the pointer rather than as a
        # to_state the six-value CHECK would reject.
        current = self._current_state(old_work_unit_id)
        self._record_event(
            old_work_unit_id, from_state=current, to_state=current,
            writer=writer, reason=reason or "superseded",
            detail={"superseded_by_unit_id": new_work_unit_id}, now=moment)

        logger.info("work unit %s superseded by %s",
                    old_work_unit_id, new_work_unit_id)

    def _current_state(self, work_unit_id: int) -> str:
        rows = self._execute(
            "SELECT state FROM work_units WHERE work_unit_id = %s",
            [work_unit_id])
        value = _single_value(rows)
        return value

    def _record_event(self, work_unit_id: int, *, from_state: str | None,
                      to_state: str, writer: str, reason: str | None,
                      detail: dict | None, now: Any) -> None:
        """Append one unit_events row. Never called except from this class.

        Same-transaction with the work_units write it accompanies by
        construction: both go through the one `execute` callable a caller
        wraps in a single transaction, exactly the discipline
        `AttemptWriter` documents ("unit_events (append-only, same
        transaction as every transition)").
        """
        sql = (
            "INSERT INTO unit_events ("
            "  work_unit_id, occurred_at, from_state, to_state, writer,"
            "  reason, detail"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s)"
        )
        self._execute(sql, [
            work_unit_id, now, from_state, to_state, writer, reason,
            _as_jsonb(detail),
        ])


class CampaignWriter:
    """Creates and transitions campaigns rows.

    No unit_events analogue: campaigns carry no append-only history table
    in migration 036 — only the plain `state` column, matching the
    design's "progress is never stored, always derived from its units"
    extended one step further to campaign lifecycle bookkeeping itself,
    which this v1 does not audit-trail beyond what the column shows.
    """

    def __init__(self, execute: Executor):
        self._execute = execute

    def create_campaign(self, campaign_name: str, operational_class: str,
                        definition: dict | None = None,
                        now: Any = None) -> int:
        """INSERT one campaigns row in state 'defined'.

        `campaign_name` is UNIQUE (migration 036); a caller that races on
        the same name gets a database-level conflict, which this method
        does not catch or retry — campaign names are human-chosen and a
        collision is a real naming error, not a concurrency case to paper
        over the way `find_current_unit`/create races are.
        """
        moment = now or datetime.datetime.now(datetime.timezone.utc)
        sql = (
            "INSERT INTO campaigns ("
            "  campaign_name, operational_class, state, defined_at, definition"
            ") VALUES (%s, %s, %s, %s, %s)"
            " RETURNING campaign_id"
        )
        rows = self._execute(sql, [
            campaign_name, operational_class, DEFINED, moment,
            _as_jsonb(definition),
        ])
        campaign_id = _single_value(rows)
        logger.info("created campaign %s (%s, class=%s)",
                    campaign_id, campaign_name, operational_class)
        return campaign_id

    def activate_campaign(self, campaign_id: int, now: Any = None) -> None:
        """defined -> active."""
        self._transition(campaign_id, DEFINED, ACTIVE,
                         stamp_started=True, now=now)

    def pause_campaign(self, campaign_id: int, now: Any = None) -> None:
        """active -> paused."""
        self._transition(campaign_id, ACTIVE, PAUSED, now=now)

    def resume_campaign(self, campaign_id: int, now: Any = None) -> None:
        """paused -> active."""
        self._transition(campaign_id, PAUSED, ACTIVE, now=now)

    def complete_campaign(self, campaign_id: int, now: Any = None) -> None:
        """active -> complete."""
        self._transition(campaign_id, ACTIVE, CAMPAIGN_COMPLETE,
                         stamp_completed=True, now=now)

    def abandon_campaign(self, campaign_id: int, from_state: str = ACTIVE,
                         now: Any = None) -> None:
        """{active, paused, defined} -> abandoned.

        `from_state` is a parameter (defaulting to the common case,
        active) rather than a fixed edge, because "complete | abandoned"
        both terminate the run and the design does not restrict WHICH
        live state a campaign may be abandoned from — a campaign
        defined-but-never-activated can be abandoned outright, same as
        one that ran and was called off mid-flight.
        """
        _require_known_campaign_state(from_state, param_name="from_state")
        if from_state not in (DEFINED, ACTIVE, PAUSED):
            raise IllegalTransition(
                f"a campaign cannot be abandoned from {from_state!r}; "
                f"legal sources are defined, active, paused")
        self._transition(campaign_id, from_state, ABANDONED,
                         stamp_completed=True, now=now)

    def _transition(self, campaign_id: int, from_state: str, to_state: str,
                    *, stamp_started: bool = False,
                    stamp_completed: bool = False, now: Any = None) -> None:
        _require_known_campaign_state(from_state, param_name="from_state")
        _require_known_campaign_state(to_state, param_name="to_state")
        edge = (from_state, to_state)
        if edge not in _CAMPAIGN_GRAPH:
            raise IllegalTransition(
                f"{from_state!r} -> {to_state!r} is not a legal campaign "
                f"transition; the declared graph is defined->active, "
                f"active<->paused, active->complete, "
                f"{{defined,active,paused}}->abandoned")

        moment = now or datetime.datetime.now(datetime.timezone.utc)
        set_clauses = ["state = %s"]
        params: list[Any] = [to_state]
        if stamp_started:
            set_clauses.append("started_at = %s")
            params.append(moment)
        if stamp_completed:
            set_clauses.append("completed_at = %s")
            params.append(moment)
        sql = (
            f"UPDATE campaigns SET {', '.join(set_clauses)}"
            f" WHERE campaign_id = %s AND state = %s"
        )
        result = self._execute(sql, [*params, campaign_id, from_state])
        _require_one_row(result, "campaign transition", campaign_id,
                         expected_state=from_state)
        logger.info("campaign %s: %s -> %s", campaign_id, from_state, to_state)


#: defined -> active <-> paused -> complete | abandoned, exactly the design's
#: quoted machine. Abandon's three source states are enumerated separately
#: in `CampaignWriter.abandon_campaign` above (not folded in here) because
#: `_transition`'s generic edge check is shared with the non-abandon calls
#: and abandon is the only verb with more than one legal source.
_CAMPAIGN_GRAPH = {
    (DEFINED, ACTIVE), (ACTIVE, PAUSED), (PAUSED, ACTIVE),
    (ACTIVE, CAMPAIGN_COMPLETE),
    (DEFINED, ABANDONED), (ACTIVE, ABANDONED), (PAUSED, ABANDONED),
}


def _require_known_campaign_state(state: str, *, param_name: str) -> None:
    if state not in CAMPAIGN_STATES:
        raise ValueError(
            f"{param_name}={state!r} is not one of the five campaign "
            f"states: {', '.join(sorted(CAMPAIGN_STATES))}")


def _as_jsonb(value: dict | None) -> Any:
    """Serialize a dict for a jsonb column, or pass None through as NULL.

    A JSON STRING, not the dict itself: psycopg2 does not adapt a bare
    dict on write (`can't adapt type 'dict'` — found live on the first
    real-driver campaign creation, 2026-08-09; the fake executor happily
    accepted the dict the real driver refuses). PostgreSQL casts the text
    parameter to jsonb from the column context. `sort_keys` keeps the
    serialization deterministic, matching the registration outcome
    writer's own convention.
    """
    if value is None:
        return None
    return json.dumps(value, sort_keys=True)


def _rowcount(result: Any, operation: str) -> int:
    """Read the affected-row count from an executor result.

    Identical contract to `observability.attempts._rowcount` (see that
    function's docstring for the full reasoning); duplicated rather than
    imported because the two writer modules are siblings under one
    convention, not one depending on the other's internals.
    """
    if isinstance(result, bool):
        raise TypeError(
            f"{operation}: executor returned a bool where a row count was "
            f"expected")
    if isinstance(result, int):
        if result < 0:
            raise RuntimeError(
                f"{operation}: executor reported row count {result}; a "
                f"negative count means the driver did not track the "
                f"statement, which cannot be distinguished from no rows "
                f"matching")
        return result
    if isinstance(result, (list, tuple)):
        return len(result)
    raise RuntimeError(
        f"{operation}: executor returned {type(result).__name__}, which "
        f"carries no affected-row count.")


def _require_one_row(result: Any, operation: str, identifier: Any,
                     expected_state: str | None = None) -> None:
    count = _rowcount(result, operation)
    if count == 0:
        if expected_state is not None:
            raise WorkUnitNotFound(
                f"{operation}: no row with id={identifier!r} in state "
                f"{expected_state!r}. Either it does not exist, or it has "
                f"already left that state — a concurrent or replayed "
                f"writer reached it first. Nothing was written, which is "
                f"the compare-and-set holding.")
        raise WorkUnitNotFound(
            f"{operation}: no row with id={identifier!r}. A transition "
            f"against a nonexistent row is a caller bug; nothing was "
            f"written.")
    if count > 1:
        raise RuntimeError(
            f"{operation}: {count} rows matched id={identifier!r}, which "
            f"is impossible under the primary key")


def _single_value(rows: Any) -> Any:
    """Pull the one value out of a RETURNING result. Mirrors AttemptWriter's."""
    if rows is None:
        raise RuntimeError("INSERT ... RETURNING produced no result")
    if isinstance(rows, (list, tuple)):
        if not rows:
            raise RuntimeError("INSERT ... RETURNING produced no rows")
        first = rows[0]
        if isinstance(first, (list, tuple)):
            return first[0]
        if isinstance(first, dict):
            return next(iter(first.values()))
        return first
    return rows


def _single_row_or_none(rows: Any, *, columns: tuple[str, ...]) -> dict | None:
    """Normalize a SELECT result to a dict, or None if it matched nothing."""
    if not rows:
        return None
    first = rows[0]
    if isinstance(first, dict):
        return first
    if isinstance(first, (list, tuple)):
        return dict(zip(columns, first))
    raise RuntimeError(
        f"unexpected row shape from executor: {type(first).__name__}")
