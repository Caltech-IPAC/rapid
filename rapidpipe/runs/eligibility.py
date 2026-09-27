"""Acceptance state: one judgement per product instance, computed once
(checks.md §Acceptance).

:func:`acceptance_state` decides the state of one ``product_instances``
row; :func:`acceptance_states` decides many in one pass and
:func:`run_acceptance_states` the candidate and current instances of one
run. The promotion walk (``repository._validate_promotion_eligibility``),
``check accept``, ``check show`` and ``run show`` all read these, so the
states and their wording come from one place.

States, decided in this order of precedence (checks.md §Acceptance):

- ``deleted``: ``deletion_state`` is not ``retained``;
- ``incomplete``: a result set that is not complete;
- ``scratch``: custody ``scratch``;
- ``unselected``: produced by an attempt that is not its unit's selected
  attempt, or its unit has no selected attempt;
- ``current``: custody ``current``;
- ``superseded``: custody ``candidate`` and a ``promotion_changes`` row
  names it as its after instance (it was current before, so it was
  accepted when it was promoted);
- ``accepted``: an ``acceptances`` row exists for it, or every required
  check of its governing policy for its kind has a latest row with
  outcome ``passed`` (a kind with no required check has nothing to pass);
- ``pending``: a required check has no row yet;
- ``rejected``: the latest row of a required check is ``failed``.

The governing policy of an instance is its own run's resolved policy
(:func:`rapidpipe.checks.runner.resolve_run_policy` with no explicit
reference: the run's ``check_policy_ref``, else the default). Requiredness
is the policy's own flag, never the ``checks.required`` column. The
latest row of a check is the promotion gate's rule
(``repository._validate_check_policy``): the same check name, version and
``detail.params`` as the policy's entry, newest by ``happened_at`` then
``id``.

Every function works inside the caller's transaction and never commits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from rapidpipe.checks.policy import Policy, PolicyCheck

#: Every state :func:`acceptance_state` returns.
STATES: tuple[str, ...] = (
    "deleted", "incomplete", "scratch", "unselected", "current", "superseded",
    "accepted", "pending", "rejected",
)

#: The states a promotion accepts in an ancestor (checks.md §The
#: promotion gate).
PROMOTABLE_ANCESTOR_STATES: tuple[str, ...] = ("current", "superseded", "accepted")


@dataclass(frozen=True)
class CheckEvidence:
    """One policy check for an instance's kind and its latest qualifying row."""

    ref: str
    required: bool
    row_id: str | None
    outcome: str | None
    summary: str | None


@dataclass(frozen=True)
class AcceptanceState:
    """The acceptance state of one ``product_instances`` row
    (checks.md §Acceptance)."""

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
    policy_ref: str | None           # None when the state was decided before the policy
    checks: tuple[CheckEvidence, ...] = field(default_factory=tuple)
    accepted_by: str | None = None   # the latest acceptances row id, if any
    reason: str | None = None        # that row's reason

    @property
    def required_checks(self) -> list[tuple[str, str | None]]:
        """``(check ref, outcome or None)`` for every required policy check."""
        return [(c.ref, c.outcome) for c in self.checks if c.required]

    @property
    def check_ids(self) -> list[str]:
        """The latest row id of every policy check for the kind that has one."""
        return [c.row_id for c in self.checks if c.row_id is not None]

    @property
    def failed(self) -> list[CheckEvidence]:
        """The required checks whose latest row failed."""
        return [c for c in self.checks if c.required and c.outcome == "failed"]

    @property
    def missing(self) -> list[CheckEvidence]:
        """The required checks with no row yet."""
        return [c for c in self.checks if c.required and c.row_id is None]

    def detail(self) -> str:
        """What decided the state: the check and outcome, the acceptance
        row, or the custody (the tail of the ``acceptance`` line, checks.md
        §The check commands)."""
        if self.state == "deleted":
            return "deletion_state=deleted"
        if self.state == "incomplete":
            return "complete=false"
        if self.state == "scratch":
            return "custody=scratch"
        if self.state == "unselected":
            return (f"custody={self.custody} attempt={self.producing_attempt} "
                    f"selected_attempt={self.selected_attempt or 'none'}")
        if self.state == "current":
            return "custody=current"
        if self.state == "superseded":
            return "custody=candidate was_current=true"
        if self.state == "accepted":
            if self.accepted_by is not None:
                return f"acceptance={self.accepted_by} policy={self.policy_ref}"
            required = [c for c in self.checks if c.required]
            if not required:
                return f"policy={self.policy_ref} required_checks=none"
            return " ".join(f"check={c.ref} outcome={c.outcome}" for c in required) + \
                f" policy={self.policy_ref}"
        if self.state == "pending":
            return " ".join(f"check={c.ref} outcome=none" for c in self.missing) + \
                f" policy={self.policy_ref}"
        # rejected
        return " ".join(f"check={c.ref} outcome={c.outcome}" for c in self.failed) + \
            f" policy={self.policy_ref}"

    def why(self) -> str:
        """A short phrase for a refusal: ``rejected: <check> failed`` and so on."""
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
        if self.state == "pending":
            return "pending: " + ", ".join(
                f"{c.ref} has not run" for c in self.missing) + f" under {self.policy_ref}"
        if self.state == "rejected":
            return "rejected: " + ", ".join(
                f"{c.ref} {c.outcome}" for c in self.failed) + f" under {self.policy_ref}"
        if self.state == "accepted" and self.accepted_by is not None:
            return f"accepted: acceptance {self.accepted_by}"
        return self.state

    def line(self) -> str:
        """The line ``check show`` and ``run show`` print
        (checks.md §The check commands)."""
        return (f"acceptance instance={self.instance} kind={self.kind} "
                f"state={self.state} {self.detail()}").rstrip()


_BASE_SQL = """
    SELECT pi.id, pi.kind, pi.run, pi.custody, pi.deletion_state,
           rs.instance IS NOT NULL, rs.complete,
           pi.producing_attempt, u.selected_attempt,
           EXISTS (SELECT 1 FROM promotion_changes pc WHERE pc.after_instance = pi.id),
           acc.id, acc.reason
    FROM product_instances pi
    LEFT JOIN result_sets rs ON rs.instance = pi.id
    LEFT JOIN attempts a ON a.id = pi.producing_attempt
    LEFT JOIN units u ON u.id = a.unit
    LEFT JOIN LATERAL (
        SELECT ac.id, ac.reason FROM acceptances ac
        WHERE ac.instance = pi.id
        ORDER BY ac.happened_at DESC, ac.id DESC
        LIMIT 1
    ) acc ON true
    WHERE pi.id = ANY(%s)
"""

_LATEST_SQL = """
    SELECT id, outcome, detail->>'summary'
    FROM checks
    WHERE instance = %s AND check_name = %s AND version = %s
      AND detail->'params' = %s::jsonb
    ORDER BY happened_at DESC, id DESC
    LIMIT 1
"""


def latest_check_row(cur, instance: str, policy_check: PolicyCheck, *,
                     for_share: bool = False) -> tuple[str, str, str | None] | None:
    """``(id, outcome, summary)`` of the latest row of ``policy_check`` on
    ``instance`` whose params equal the policy's, or None: the promotion
    gate's rule."""
    cur.execute(_LATEST_SQL + (" FOR SHARE" if for_share else ""),
                (instance, policy_check.name, policy_check.version,
                 policy_check.params_json()))
    return cur.fetchone()


class _PolicyCache:
    """Each run's resolved policy, resolved once per call."""

    def __init__(self, cur) -> None:
        self._cur = cur
        self._by_run: dict[str, Policy] = {}

    def __call__(self, run_id: str) -> Policy:
        if run_id not in self._by_run:
            from rapidpipe.checks.runner import resolve_run_policy

            self._by_run[run_id] = resolve_run_policy(self._cur.connection, run_id, None)
        return self._by_run[run_id]


def _decide(cur, row: Sequence[Any], policies: _PolicyCache, for_share: bool) -> AcceptanceState:
    (instance, kind, run, custody, deletion_state, is_result_set, complete,
     producing_attempt, selected_attempt, was_after, acceptance_id, acceptance_reason) = row
    selected = selected_attempt is not None and selected_attempt == producing_attempt
    base = dict(
        instance=instance, kind=kind, run=run, custody=custody, selected=selected,
        complete=bool(complete) if is_result_set else None,
        retained=deletion_state == "retained",
        producing_attempt=producing_attempt, selected_attempt=selected_attempt,
    )

    def made(state: str, **extra: Any) -> AcceptanceState:
        return AcceptanceState(state=state, **base, **{"policy_ref": None, **extra})

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

    policy = policies(run)
    evidence = []
    for policy_check in policy.checks_for_kind(kind):
        latest = latest_check_row(cur, instance, policy_check, for_share=for_share)
        evidence.append(CheckEvidence(
            ref=policy_check.ref, required=policy_check.required,
            row_id=latest[0] if latest else None,
            outcome=latest[1] if latest else None,
            summary=latest[2] if latest else None))
    judged = dict(policy_ref=policy.ref, checks=tuple(evidence),
                  accepted_by=acceptance_id, reason=acceptance_reason)
    required = [c for c in evidence if c.required]
    if acceptance_id is not None:
        return made("accepted", **judged)
    if any(c.row_id is None for c in required):
        return made("pending", **judged)
    if any(c.outcome != "passed" for c in required):
        return made("rejected", **judged)
    return made("accepted", **judged)


def acceptance_states(cur, instance_ids: Iterable[str], *,
                      for_share: bool = False) -> dict[str, AcceptanceState]:
    """The state of each named instance that exists, by id (the bulk helper).

    An id naming no ``product_instances`` row is absent from the result.
    ``for_share`` reads the ``checks`` rows relied on ``FOR SHARE`` (the
    promotion walk, under the promotion lock).
    """
    ids = list(dict.fromkeys(instance_ids))
    if not ids:
        return {}
    cur.execute(_BASE_SQL, (ids,))
    rows = cur.fetchall()
    policies = _PolicyCache(cur)
    return {row[0]: _decide(cur, row, policies, for_share) for row in rows}


def acceptance_state(cur, instance_id: str) -> AcceptanceState:
    """The acceptance state of one instance (checks.md §Acceptance);
    :class:`LookupError` if no ``product_instances`` row has that id."""
    states = acceptance_states(cur, [instance_id])
    if instance_id not in states:
        raise LookupError(f"no product instance {instance_id!r}")
    return states[instance_id]


def run_acceptance_states(cur, run_id: str, *,
                          instance: str | None = None) -> list[AcceptanceState]:
    """The states of ``run_id``'s candidate and current instances (or the one
    ``instance`` of them), ordered by kind then id: the lines ``check show``
    and ``run show`` print (checks.md §The check commands)."""
    query = ("SELECT id FROM product_instances WHERE run = %s "
             "AND custody IN ('candidate', 'current')")
    params: list[Any] = [run_id]
    if instance is not None:
        query += " AND id = %s"
        params.append(instance)
    query += " ORDER BY kind, id"
    cur.execute(query, params)
    ids = [row[0] for row in cur.fetchall()]
    states = acceptance_states(cur, ids)
    return sorted(states.values(), key=lambda s: (s.kind, s.instance))


def ancestors(cur, instance_id: str) -> list[str]:
    """Every instance ``instance_id`` depends on, through the whole chain of
    ``dependencies`` (checks.md §The promotion gate): a recursive walk from
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
