"""
File:    registration.py

Registration reads attempt records — it does not parse job output.

decisions.md § Attempt-record schema lists registration among the affected
workstreams, with the change stated exactly: registration "consumes records in
place of stdout parsing". design/observability.md gives the reason the record
exists to serve here — a Batch job that reports SUCCEEDED can still have failed
at the application level, and the outcome taxonomy makes that combination
representable instead of hiding it behind one collapsed status.

So the rule this module implements is a single sentence: an attempt's products
are registered when the ATTEMPT RECORD says the attempt succeeded — never when
the scheduler says the job succeeded, and never because a log line matched a
pattern.

`scheduler_state` is deliberately not consulted for the decision. That is not an
oversight to be "fixed" later by adding it as a second condition: on 2026-07-22
the scheduler's SUCCEEDED was exactly the signal that was wrong, and reinstating
it as a gate would restore the failure mode. It is carried in the decision's
`detail` for operators to read, and nothing more.
"""

import dataclasses
import enum
import logging
from typing import Any, Iterable

from .attempts import LifecycleState, ProductDisposition, RapidOutcome

logger = logging.getLogger(__name__)


class RegistrationDecision(str, enum.Enum):
    """What registration decided about one attempt's products."""

    #: The attempt succeeded at the application level; register its products.
    REGISTER = "register"
    #: The attempt failed, partially succeeded, or never produced products.
    SKIP = "skip"
    #: The record cannot support a decision yet — not terminal, or flagged by
    #: reconciliation. Deliberately distinct from SKIP: skip is a decision,
    #: this is the absence of one, and a caller may retry it later.
    DEFER = "defer"


@dataclasses.dataclass(frozen=True)
class Registrable:
    """One attempt, with registration's verdict on it."""

    attempt_id: int
    decision: RegistrationDecision
    reason: str
    exposure_id: int | None = None
    sca: int | None = None
    sky_tile: str | None = None
    detail: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def should_register(self) -> bool:
        return self.decision is RegistrationDecision.REGISTER


#: States from which no registration decision can be made yet.
#:
#: `application_closed` belongs here even though the application has finished
#: and stated its outcome. Registration consumes *reconciled* outcomes only: an
#: application-closed row has no scheduler truth on it yet, so acting on it
#: would mean registering products for an attempt whose container might have
#: been killed after the record was written. The reconciler moves it to a
#: terminal state within the grace horizon, and registration acts then.
#: (This state was added after this module was first written; leaving it out
#: let an unreconciled row fall through to the outcome branch and register.)
_NON_TERMINAL = frozenset({
    LifecycleState.SUBMITTED.value,
    LifecycleState.STARTED.value,
    LifecycleState.APPLICATION_CLOSED.value,
})


def decide(attempt: Any) -> Registrable:
    """Decide whether one attempt's products should be registered.

    Reads only record fields. No stdout, no log scraping, no exit-code
    interpretation beyond what the record already states.
    """
    state = _as_value(attempt.lifecycle_state)
    outcome = _as_value(getattr(attempt, "rapid_outcome", None))
    disposition = _as_value(getattr(attempt, "product_disposition", None))
    detail = {
        "lifecycle_state": state,
        "rapid_outcome": outcome,
        "product_disposition": disposition,
        # Recorded for operators, never for the decision — see module docstring.
        # The exit code is TWO columns, not one: the application's intended
        # exit and the scheduler's observed exit are separate facts with
        # separate authors, and their disagreement is the thing worth seeing.
        # (`process_exit_code` was the pre-amendment single column and had
        # been reading None ever since the split.)
        "scheduler_state": _as_value(getattr(attempt, "scheduler_state", None)),
        "application_intended_exit": getattr(
            attempt, "application_intended_exit", None),
        "scheduler_observed_exit": getattr(
            attempt, "scheduler_observed_exit", None),
        "error_category": _as_value(getattr(attempt, "error_category", None)),
    }

    def result(decision: RegistrationDecision, reason: str) -> Registrable:
        return Registrable(
            attempt_id=attempt.attempt_id, decision=decision, reason=reason,
            exposure_id=getattr(attempt, "exposure_id", None),
            sca=getattr(attempt, "sca", None),
            sky_tile=getattr(attempt, "sky_tile", None),
            detail=detail)

    if state in _NON_TERMINAL:
        return result(RegistrationDecision.DEFER,
                      f"attempt is {state}; not terminal yet")

    if state == LifecycleState.MISSING_OR_CONTRADICTORY.value:
        return result(RegistrationDecision.DEFER,
                      "record is flagged missing_or_contradictory; "
                      "reconciliation must resolve it first")

    if state == LifecycleState.TERMINAL_WITHOUT_START.value:
        return result(RegistrationDecision.SKIP,
                      "attempt never started; no products exist")

    # terminal_after_start: the application's own verdict decides.
    if outcome == RapidOutcome.SUCCESS.value:
        if disposition == ProductDisposition.SUPERSEDED.value:
            return result(RegistrationDecision.SKIP,
                          "products superseded by a later attempt")
        if disposition == ProductDisposition.NONE.value:
            return result(RegistrationDecision.SKIP,
                          "attempt succeeded but produced no products")
        return result(RegistrationDecision.REGISTER,
                      "application reported success")

    if outcome == RapidOutcome.PARTIAL.value:
        # Partial is not success. Registering half a product set silently is
        # exactly the kind of quiet wrongness the taxonomy exists to prevent.
        return result(RegistrationDecision.SKIP,
                      "application reported partial success; not registered "
                      "without an explicit operator decision")

    return result(RegistrationDecision.SKIP,
                  f"application reported {outcome}")


def decide_all(attempts: Iterable[Any]) -> list[Registrable]:
    """Decide across many attempts, logging the SUCCEEDED-but-failed cases.

    The log line is the operator-visible half of the 2026-07-22 fix: when the
    scheduler and the application disagree, that disagreement gets said out
    loud rather than being resolved into silence.
    """
    decisions = [decide(attempt) for attempt in attempts]
    for item in decisions:
        if (item.detail.get("scheduler_state") == "SUCCEEDED"
                and item.detail.get("rapid_outcome") in
                (RapidOutcome.FAILURE.value, RapidOutcome.PARTIAL.value)):
            logger.warning(
                "attempt %s: scheduler SUCCEEDED but application reported %s "
                "— not registering (error_category=%s)",
                item.attempt_id, item.detail.get("rapid_outcome"),
                item.detail.get("error_category"))
    return decisions


def _as_value(field: Any) -> Any:
    return field.value if isinstance(field, enum.Enum) else field
