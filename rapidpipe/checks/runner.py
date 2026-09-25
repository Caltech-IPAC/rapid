"""Running checks and recording their results; automatic promotion
(supervisor step 6, 2026-09-24, R1, R5, R6).

Every function here works inside the caller's transaction and never
commits, like :mod:`rapidpipe.runs.repository`: the CLI commits around
the call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from rapidpipe.checks.policy import (
    DEFAULT_POLICY,
    Policy,
    PolicyCheck,
    load_policy,
    policy_permits_auto_promote,
)
from rapidpipe.checks.registry import CheckResult, get_check
from rapidpipe.db.ids import new_ulid


class CheckUsageError(Exception):
    """A check request names something that does not apply (an instance
    that is not a candidate of the run, a check the policy does not name,
    a parameter the check does not take)."""


@dataclass(frozen=True)
class Candidate:
    """A product instance of a run produced by its unit's selected attempt."""

    id: str
    kind: str
    logical_key: dict[str, Any]


@dataclass(frozen=True)
class RecordedCheck:
    """One ``checks`` row, as recorded or read back."""

    id: str
    instance: str
    kind: str
    logical_key: dict[str, Any]
    check_name: str
    version: str
    required: bool
    outcome: str
    detail: dict[str, Any]
    happened_at: datetime | None = None

    @property
    def check_ref(self) -> str:
        return f"{self.check_name}@{self.version}"

    @property
    def summary(self) -> str:
        return str(self.detail.get("summary", ""))

    def line(self) -> str:
        """The one-line form ``check run`` and ``check show`` print (R6)."""
        key = json.dumps(self.logical_key, sort_keys=True, separators=(",", ":"))
        return (f"instance={self.instance} kind={self.kind} key={key} "
                f"check={self.check_ref} required={'true' if self.required else 'false'} "
                f"outcome={self.outcome} {self.summary}").rstrip()


def resolve_run_policy(conn, run_id: str, explicit: str | None = None) -> Policy:
    """The policy for ``run_id``: ``explicit`` > the run's
    ``check_policy_ref`` > :data:`DEFAULT_POLICY` (R4)."""
    if explicit:
        return load_policy(explicit)
    with conn.cursor() as cur:
        cur.execute("SELECT check_policy_ref FROM runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
    return load_policy((row[0] if row else None) or DEFAULT_POLICY)


def run_candidates(conn, run_id: str) -> list[Candidate]:
    """Every product instance of ``run_id`` whose producing attempt is its
    unit's selected attempt, any custody (scratch runs may be checked
    too), ordered by kind then id -- the same selection
    :func:`rapidpipe.runs.repository.promote_run` promotes from."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pi.id, pi.kind, pi.logical_key
            FROM product_instances pi
            JOIN attempts a ON a.id = pi.producing_attempt
            JOIN units u ON u.id = a.unit
            WHERE pi.run = %s
              AND u.selected_attempt = pi.producing_attempt
            ORDER BY pi.kind, pi.id
            """,
            (run_id,),
        )
        return [Candidate(i, k, key) for i, k, key in cur.fetchall()]


def run_check(conn, candidate: Candidate, policy_check: PolicyCheck, *,
              policy_ref: str, params: dict[str, Any] | None = None,
              who: str | None = None) -> RecordedCheck:
    """Run one check over one instance and record its ``checks`` row (R1).

    ``params`` defaults to the policy's params for the check. The row's
    ``required`` is the policy's flag; its ``detail`` holds the check's own
    detail plus ``policy``, ``params`` (what the promotion gate matches,
    A2), ``summary`` and ``who``. A check that raises is recorded as
    ``failed`` with the error in detail -- never nothing. The function
    runs under a savepoint so a database error inside it does not abort
    the caller's transaction.
    """
    registered = get_check(policy_check.ref)
    params = dict(policy_check.params if params is None else params)
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT rapid_check")
        try:
            result = registered.function(conn, candidate.id, params)
            if not isinstance(result, CheckResult):
                raise TypeError(f"check {registered.ref} returned {type(result).__name__}, "
                                "not CheckResult")
            cur.execute("RELEASE SAVEPOINT rapid_check")
        except Exception as exc:  # noqa: BLE001 - recorded, never lost (R1)
            cur.execute("ROLLBACK TO SAVEPOINT rapid_check")
            cur.execute("RELEASE SAVEPOINT rapid_check")
            message = f"{type(exc).__name__}: {exc}"
            result = CheckResult("failed", {"error": message}, f"check raised {message}")

        detail = dict(result.detail)
        detail.update(policy=policy_ref, params=params, summary=result.summary)
        if who:
            detail["who"] = who
        check_id = new_ulid()
        cur.execute(
            """
            INSERT INTO checks (id, instance, check_name, version, required, outcome, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING happened_at
            """,
            (check_id, candidate.id, policy_check.name, policy_check.version,
             policy_check.required, result.outcome, json.dumps(detail, default=str)),
        )
        (happened_at,) = cur.fetchone()
    return RecordedCheck(check_id, candidate.id, candidate.kind, candidate.logical_key,
                         policy_check.name, policy_check.version, policy_check.required,
                         result.outcome, detail, happened_at)


def run_policy_checks(conn, run_id: str, policy: Policy, *, instance: str | None = None,
                      check: str | None = None,
                      param_overrides: dict[str, Any] | None = None,
                      who: str | None = None) -> list[RecordedCheck]:
    """Run every applicable policy check over the run's candidates (or the
    one ``instance`` / the one ``check``), record each, return them (R6).

    ``param_overrides`` replace the policy's params for ``check`` only,
    and only names that check takes; the recorded params then differ from
    the policy's, so the promotion gate does not count that row (A2).
    Raises :class:`CheckUsageError` for an instance that is not a
    candidate of the run, a check the policy does not name, overrides
    without ``check``, or an unknown parameter.
    """
    candidates = run_candidates(conn, run_id)
    if instance is not None:
        candidates = [c for c in candidates if c.id == instance]
        if not candidates:
            raise CheckUsageError(
                f"instance {instance} is not a product of run {run_id} from a "
                "selected attempt")
    selected_checks = list(policy.checks)
    if check is not None:
        found = policy.find_check(check)
        if found is None:
            raise CheckUsageError(
                f"check policy {policy.ref} names no check {check} (it names "
                f"{', '.join(c.ref for c in policy.checks) or 'none'})")
        selected_checks = [found]
    if param_overrides and check is None:
        raise CheckUsageError("--param needs --check: overrides apply to one named check")
    if param_overrides:
        allowed = set(get_check(check).params)
        unknown = sorted(set(param_overrides) - allowed)
        if unknown:
            raise CheckUsageError(
                f"check {check} takes no parameter {', '.join(unknown)} "
                f"(it takes {', '.join(sorted(allowed))})")

    results = []
    for candidate in candidates:
        for pc in selected_checks:
            if pc.kind != candidate.kind:
                continue
            params = None
            if param_overrides:
                params = {**pc.params, **param_overrides}
            results.append(run_check(conn, candidate, pc, policy_ref=policy.ref,
                                     params=params, who=who))
    return results


def recorded_checks(conn, run_id: str, *, instance: str | None = None) -> list[RecordedCheck]:
    """Recorded ``checks`` rows of the run's instances, newest first (R6)."""
    query = """
        SELECT c.id, c.instance, pi.kind, pi.logical_key, c.check_name, c.version,
               c.required, c.outcome, c.detail, c.happened_at
        FROM checks c
        JOIN product_instances pi ON pi.id = c.instance
        WHERE pi.run = %s
    """
    params: list[Any] = [run_id]
    if instance is not None:
        query += " AND c.instance = %s"
        params.append(instance)
    query += " ORDER BY c.happened_at DESC, c.id DESC"
    with conn.cursor() as cur:
        cur.execute(query, params)
        return [RecordedCheck(*row[:9], happened_at=row[9]) for row in cur.fetchall()]


@dataclass(frozen=True)
class AutoPromoteOutcome:
    """What :func:`maybe_auto_promote` did: ``status`` is ``off``,
    ``skipped``, ``refused`` or ``promoted``; ``message`` is the line
    ``run start`` prints."""

    status: str
    message: str
    promotion_id: str | None = None
    checks: Sequence[RecordedCheck] = ()


def maybe_auto_promote(conn, run_id: str, *, who: str = "auto-promote") -> AutoPromoteOutcome:
    """Automatic promotion at the end of a ``run start`` walk (R5).

    With ``runs.auto_promote`` false (every run today), returns ``off``
    with the message ``auto-promote off (policy <ref>)``. Otherwise,
    when every unit of the run is complete and the run's policy permits
    automatic promotion (:func:`~rapidpipe.checks.policy.
    policy_permits_auto_promote`), runs the policy's checks over the
    run's candidates, records them, and promotes through
    :func:`rapidpipe.runs.repository.promote_run` under that policy; a
    refused promotion is rolled back to a savepoint, so the recorded
    check rows stay in the caller's transaction either way.
    """
    from rapidpipe.runs import repository

    with conn.cursor() as cur:
        cur.execute("SELECT auto_promote, check_policy_ref FROM runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
    auto, policy_ref = (row if row else (False, None))
    policy_ref = policy_ref or DEFAULT_POLICY
    if not auto:
        return AutoPromoteOutcome("off", f"auto-promote off (policy {policy_ref})")

    policy = load_policy(policy_ref)
    if not policy_permits_auto_promote(policy):
        return AutoPromoteOutcome(
            "refused", f"auto-promote refused: policy {policy.ref} does not permit "
                       "automatic promotion; lead approval pending")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), count(*) FILTER (WHERE state <> 'complete') "
            "FROM units WHERE run = %s", (run_id,))
        total, incomplete = cur.fetchone()
    if total == 0 or incomplete:
        return AutoPromoteOutcome(
            "skipped", f"auto-promote skipped: {incomplete} of {total} units not complete "
                       f"(policy {policy.ref})")

    checks = run_policy_checks(conn, run_id, policy, who=who)
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT rapid_auto_promote")
        try:
            promotion_id = repository.promote_run(
                conn, run_id, who, f"automatic promotion under check policy {policy.ref}",
                check_policy=policy)
        except repository.RunModelError as exc:
            cur.execute("ROLLBACK TO SAVEPOINT rapid_auto_promote")
            return AutoPromoteOutcome(
                "refused", f"auto-promote refused (policy {policy.ref}): {exc}", checks=checks)
        cur.execute("RELEASE SAVEPOINT rapid_auto_promote")
    return AutoPromoteOutcome(
        "promoted", f"auto-promote promoted promotion={promotion_id} (policy {policy.ref})",
        promotion_id=promotion_id, checks=checks)
