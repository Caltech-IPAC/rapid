"""Cancellation: the operator disposition that is not a failure.

Conformance rule 9, brief C3: "Cancellation becomes an explicit disposition:
an operator- or policy-initiated terminal state distinct from failure, taking
the same lock, recording who/why (compose with the mutation-audit machinery
that migrations 030-031 provide rather than inventing a parallel record)."

Before this, `grep -ri cancel` across `pipeline/` returned nothing: there was
no way to say "this work should not happen" at all. The only terminal
dispositions were `complete` (it happened) and `failed` (RAPID tried and the
retry policy exhausted), so an operator who needed work stopped had a choice
between quarantining it — which means "something is wrong here, hold it for a
human", a different statement — and leaving it to run.

## Where the authority lives

The mutation contract is not this module's to re-implement. Migrations 030 and
031 already own it: `derived.mutation_audit` is the append-only ledger with
actor, dispatcher, action_class, action_tier, target_scope, reason, dry_run
and rows_affected all NOT NULL or CHECK-constrained, and
`derived.write_mutation_audit` is the SECURITY DEFINER writer. The one-path
rule 030 states is strict — "NO role receives INSERT, UPDATE, or DELETE on any
table in this file. Writes arrive exclusively through the SECURITY DEFINER
functions in 031" — so a Python module that wrote its own audit row would need
a grant that does not exist, and inventing a second ledger to route around
that is precisely what the brief forbids.

So cancellation's authoritative implementation is DRAFT migration 046's
`derived.cancel_work_units`, built to 031's own shape: same dry-run semantics,
same reason/policy_citation/dispatcher parameters, same audit row, same
jsonb result. This module is the CALLER of that function, not a parallel
implementation of it — the same relationship `pipeline.intent.
load_definitions_cli` has to migration 039's loader ("Migration 039 shipped
`derived.load_workflow_definition` with no caller; this is the caller").

## What this module does hold

The one thing the SQL function cannot do for a Python caller: present the
result in the vocabulary the intent layer already speaks, and fail in its
typed errors rather than as a bare psycopg2 exception. `CancellationRefused`
is raised where the function reports it refused, so a caller distinguishes
"the unit was not in a cancellable state" from "the call did not run".

## The lock, and why it is the function's rather than this module's

The work-unit lock (rule 9's "same lock in the same order") is taken INSIDE
`derived.cancel_work_units`, per unit, before that unit's state is read —
exactly as `pipeline.intent.writer.transition_unit` takes it for every
Python-side transition. It belongs there rather than here because the SQL
function is where the read-decide-write sequence lives: a lock taken in Python
around a call that does its own reading would be a lock held over the wrong
interval, and the deciding read would still be unserialized. Both paths take
namespace `WU` (`pipeline.intent.lock.WORK_UNIT_NAMESPACE`, 0x5755) on
`work_unit_id`, so a Python transition and a SQL cancellation of one unit
genuinely serialize against each other — which is the property acceptance
criterion 3 asserts by racing them.
"""

import json
import logging

logger = logging.getLogger("rapid.intent.cancellation")

#: The action class this disposition records in `derived.mutation_audit`.
#: Named here so the Python side and DRAFT migration 046 cannot drift on the
#: string an operator queries the ledger by.
ACTION_CLASS = "cancel_work_units"

#: The draft function this module calls. Absent from the deployed schema until
#: DRAFT 046 lands as a rapid_systems change request — `is_available` is how a
#: caller (and the contract tier) asks before calling.
FUNCTION_NAME = "derived.cancel_work_units"

_AVAILABLE_SQL = (
    "SELECT 1 FROM pg_proc p"
    " JOIN pg_namespace n ON n.oid = p.pronamespace"
    " WHERE n.nspname = 'derived' AND p.proname = 'cancel_work_units'"
    " LIMIT 1"
)

_CANCEL_SQL = (
    "SELECT derived.cancel_work_units(%s, %s, %s, %s, %s)"
)


class CancellationError(RuntimeError):
    """Base class for this module's errors."""


class CancellationRefused(CancellationError):
    """The units named could not be cancelled in the state they were found.

    Distinct from a failed call: the function ran, took each unit's lock, read
    its state under that lock and declined to transition it. A unit already
    `complete`, `failed`, `quarantined` or `cancelled` is not cancellable —
    see `pipeline.intent.writer._TRANSITION_GRAPH` for which from-states the
    machine admits and why the terminal ones are excluded.
    """

    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result or {}


def is_available(execute):
    """Is DRAFT 046's cancellation function present in this database?

    PROBED, NEVER ASSUMED (brief: "Contract tests that need draft schema must
    SKIP cleanly when it is absent — probe the schema, don't assume"). The
    drafts on this branch are not in the deployed migration stream, so both a
    contract test and a production caller must be able to ask. Asking
    `pg_proc` rather than trying the call and interpreting the failure keeps
    "the feature is not deployed" apart from "the call went wrong", which is
    the same distinction `submission.gathering` draws between `NotReadyYet`
    and `GatheringError`.
    """
    rows = execute(_AVAILABLE_SQL, [])
    return bool(rows)


def cancel_work_units(execute, work_unit_ids, *, reason, dry_run=False,
                      policy_citation=None, dispatcher=None):
    """Cancel the named units through the audited mutation API.

    Returns the function's own jsonb result as a dict: the action, the dry-run
    flag, the units requested, the units actually transitioned, and the units
    refused with the state each was found in.

    `reason` is REQUIRED and is not defaulted, here or in the SQL function.
    Migration 030's `mutation_audit` CHECKs `length(btrim(reason)) > 0`, and
    the mutation contract names reason as one of the six things every mutation
    supplies. A cancellation whose reason is "" is a record that answers "why
    did this work not happen" with silence, which is the question the ledger
    exists for.

    **DRY RUN IS A REHEARSAL, NOT A QUERY.** `dry_run=True` reports what would
    transition without transitioning anything, and still writes an audit row —
    031's own posture, and 030's CHECK enforces `NOT dry_run OR
    coalesce(rows_affected, 0) = 0` so a rehearsal cannot claim effects.
    """
    if not reason or not reason.strip():
        raise ValueError(
            "cancellation needs a reason: derived.mutation_audit CHECKs "
            "length(btrim(reason)) > 0, and the mutation contract requires "
            "one for every mutation")

    ids = [int(unit_id) for unit_id in work_unit_ids]
    if not ids:
        raise ValueError("cancellation needs at least one work unit id")

    rows = execute(_CANCEL_SQL,
                   [ids, reason, bool(dry_run), policy_citation, dispatcher])
    result = _single_result(rows)

    refused = result.get("refused") or []
    if refused and not result.get("units_cancelled"):
        raise CancellationRefused(
            f"none of the {len(ids)} unit(s) named could be cancelled; "
            f"refused: {refused}", result=result)

    logger.info(
        "cancellation %s: %s of %s unit(s) cancelled, %s refused (reason=%r)",
        "rehearsed" if dry_run else "applied",
        result.get("units_cancelled"), len(ids), len(refused), reason)
    return result


def _single_result(rows):
    """The one jsonb value the function returns, as a dict.

    psycopg2 decodes `jsonb` to a dict already; a driver or an executor that
    hands back the raw text is decoded here rather than assumed away, because
    the contract tier's `fixture.executor` and the production
    `ConnectionExecutor` are two different shapes reaching this same code.
    """
    if not rows:
        raise CancellationError(
            "derived.cancel_work_units returned no row; the call did not run")
    value = rows[0][0] if isinstance(rows[0], (list, tuple)) else rows[0]
    if isinstance(value, (str, bytes)):
        return json.loads(value)
    return value or {}
