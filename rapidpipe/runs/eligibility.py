"""Instance state: one fact per product instance, computed once
(products.md §Reading across runs; runs.md §Rules).

:func:`instance_state` decides the state of one ``product_instances``
row; :func:`instance_states` decides many in one pass and
:func:`run_instance_states` the candidate and current instances of one
run. The promotion walk (``repository._validate_promotion_eligibility``)
and ``run show`` read these, so the states and their wording come from
one place.

States are facts about custody, completeness and selection, never a
judgement from check results, decided in this order of precedence:

- ``deleted``: ``deletion_state`` is not ``retained``;
- ``incomplete``: a result set that is not complete;
- ``scratch``: custody ``scratch``;
- ``unselected``: produced by an attempt that is not its unit's selected
  attempt, or its unit has no selected attempt;
- ``current``: custody ``current``;
- ``superseded``: custody ``candidate`` and a ``promotion_changes`` row
  names it as its after instance (it was current before);
- ``candidate``: custody ``candidate``, never current.

Every function works inside the caller's transaction and never commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

#: Every state :func:`instance_state` returns, in order of precedence.
STATES: tuple[str, ...] = (
    "deleted", "incomplete", "scratch", "unselected", "current", "superseded",
    "candidate",
)

#: The states a promotion accepts in an ancestor outside its own request
#: (runs.md §Rules).
PROMOTABLE_ANCESTOR_STATES: tuple[str, ...] = ("current", "superseded")


@dataclass(frozen=True)
class InstanceState:
    """The state of one ``product_instances`` row (runs.md §Rules)."""

    instance: str
    kind: str
    run: str
    state: str
    custody: str
    selected: bool
    complete: bool | None           # None when the instance is not a result set
    retained: bool
    producing_attempt: str | None
    selected_attempt: str | None

    def detail(self) -> str:
        """What decided the state: the custody, completeness or selection
        fact behind it."""
        if self.state == "deleted":
            return "deletion_state=deleted"
        if self.state == "incomplete":
            return "complete=false"
        if self.state == "unselected":
            return (f"custody={self.custody} attempt={self.producing_attempt} "
                    f"selected_attempt={self.selected_attempt or 'none'}")
        if self.state == "superseded":
            return "custody=candidate was_current=true"
        return f"custody={self.custody}"

    def why(self) -> str:
        """A short phrase for a refusal: ``scratch: not project custody`` and so on."""
        if self.state == "deleted":
            return "deleted: not retained"
        if self.state == "incomplete":
            return "incomplete: an incomplete result set"
        if self.state == "scratch":
            return "scratch: not project custody"
        if self.state == "unselected":
            return ("unselected: not produced by its unit's selected attempt"
                    if self.selected_attempt else
                    "unselected: its unit has no selected attempt")
        if self.state == "candidate":
            return "candidate: not current or superseded"
        return self.state

    def line(self) -> str:
        """The line ``run show`` prints under ``state:``."""
        return (f"instance={self.instance} kind={self.kind} "
                f"state={self.state} {self.detail()}").rstrip()


_BASE_SQL = """
    SELECT pi.id, pi.kind, pi.run, pi.custody, pi.deletion_state,
           rs.instance IS NOT NULL, rs.complete,
           pi.producing_attempt, u.selected_attempt,
           EXISTS (SELECT 1 FROM promotion_changes pc WHERE pc.after_instance = pi.id)
    FROM product_instances pi
    LEFT JOIN result_sets rs ON rs.instance = pi.id
    LEFT JOIN attempts a ON a.id = pi.producing_attempt
    LEFT JOIN units u ON u.id = a.unit
    WHERE pi.id = ANY(%s)
"""


def _decide(row: Sequence[Any]) -> InstanceState:
    (instance, kind, run, custody, deletion_state, is_result_set, complete,
     producing_attempt, selected_attempt, was_after) = row
    selected = selected_attempt is not None and selected_attempt == producing_attempt

    def made(state: str) -> InstanceState:
        return InstanceState(
            instance=instance, kind=kind, run=run, state=state, custody=custody,
            selected=selected, complete=bool(complete) if is_result_set else None,
            retained=deletion_state == "retained",
            producing_attempt=producing_attempt, selected_attempt=selected_attempt)

    if deletion_state != "retained":
        return made("deleted")
    if is_result_set and not complete:
        return made("incomplete")
    if custody == "scratch":
        return made("scratch")
    if not selected:
        return made("unselected")
    if custody == "current":
        return made("current")
    if was_after:
        return made("superseded")
    return made("candidate")


def instance_states(cur, instance_ids: Iterable[str]) -> dict[str, InstanceState]:
    """The state of each named instance that exists, by id (the bulk helper).
    An id naming no ``product_instances`` row is absent from the result."""
    ids = list(dict.fromkeys(instance_ids))
    if not ids:
        return {}
    cur.execute(_BASE_SQL, (ids,))
    return {row[0]: _decide(row) for row in cur.fetchall()}


def instance_state(cur, instance_id: str) -> InstanceState:
    """The state of one instance; :class:`LookupError` if no
    ``product_instances`` row has that id."""
    states = instance_states(cur, [instance_id])
    if instance_id not in states:
        raise LookupError(f"no product instance {instance_id!r}")
    return states[instance_id]


def run_instance_states(cur, run_id: str) -> list[InstanceState]:
    """The states of ``run_id``'s candidate and current instances, ordered by
    kind then id: the lines ``run show`` prints."""
    cur.execute("SELECT id FROM product_instances WHERE run = %s "
                "AND custody IN ('candidate', 'current')", (run_id,))
    ids = [row[0] for row in cur.fetchall()]
    states = instance_states(cur, ids)
    return sorted(states.values(), key=lambda s: (s.kind, s.instance))


def ancestors(cur, instance_id: str) -> list[str]:
    """Every instance ``instance_id`` depends on, through the whole chain of
    ``dependencies`` (runs.md §Rules): a recursive walk from
    the instance up through ``producer_instance``, cycle-guarded by UNION (a
    row already found is never walked again) and otherwise unbounded.
    Excludes ``instance_id`` itself; ordered by id."""
    cur.execute(
        """
        WITH RECURSIVE walk (id) AS (
            SELECT d.producer_instance FROM dependencies d
            WHERE d.consumer_instance = %s
            UNION
            SELECT d.producer_instance FROM walk w
            JOIN dependencies d ON d.consumer_instance = w.id
        )
        SELECT id FROM walk WHERE id <> %s ORDER BY id
        """,
        (instance_id, instance_id))
    return [row[0] for row in cur.fetchall()]
