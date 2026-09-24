"""Runs, units, attempts, instances and promotion: the run-model repository.

Implements the runs page's rules
(https://roman-rapid.readthedocs.io/en/latest/system/runs.html) over the
tables the migration in ``database/migrations/20260921-02-run-model.sql``
creates. Every public function here does its work as one transaction: it
takes an open ``psycopg2`` connection and performs one bounded unit of
work on it, and the caller commits or rolls back around the call --
typically with ``rapidpipe.db.connection.transaction()``, e.g.::

    from rapidpipe.db.connection import transaction
    from rapidpipe.runs.repository import create_run

    with transaction() as conn:
        run_id = create_run(conn, kind="scratch", owner="brusholme", ...)

Each function refuses, by raising one of the named exceptions below,
exactly where the runs page says a refusal is required; raising leaves
the transaction to the caller to roll back (via ``transaction()``'s own
except clause), so a refused call never leaves partial writes committed.

This module composes ``rapidpipe.db`` (connection, ids) and reads manifest
data as a plain ``dict`` rather than importing ``rapidpipe.products.
manifest`` -- another branch is concurrently changing that module's shape,
and the runs page's "A complete manifest" example fully describes the
dict shape ``register_manifest`` needs, so no import is required.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable, Sequence

import psycopg2
import psycopg2.extensions

from rapidpipe.db.ids import new_ulid

#: One fixed advisory-lock key for every promotion, per the runs page,
#: "Promotion": "All promotions take one transaction-scoped advisory
#: lock". A single arbitrary 63-bit constant -- there is exactly one
#: promotion lock in the whole system, not one per kind or run, because
#: promotion changes may span kinds and the runs page describes one lock
#: guarding the whole check-then-write sequence.
_PROMOTION_ADVISORY_LOCK_KEY = 0x52415049445F5052  # "RAPID_PR" in ASCII, as an int

_TERMINAL_UNIT_STATES = ("complete", "failed", "cancelled")
_TERMINAL_RUN_STATES = ("deleting", "deleted")


class RunModelError(Exception):
    """Base class for every exception this module raises."""


class RunNotFound(RunModelError):
    """No run exists with the given id."""


class UnitNotFound(RunModelError):
    """No unit exists with the given (run, stage, unit_id)."""


class AttemptNotFound(RunModelError):
    """No attempt exists with the given id."""


class AttemptAlreadyResolved(RunModelError):
    """record_scheduler_job was called on an attempt with a disposition."""


class RunDeletingOrDeleted(RunModelError):
    """The run is in state 'deleting' or 'deleted'.

    Raised by add_unit, bind_unit_inputs and allocate_attempt: "Attempt
    allocation, input binding and result acceptance use the same run
    fence and refuse a deleting or deleted run." (runs page, "Deletion").
    """


class UnitTerminal(RunModelError):
    """The unit is already complete, failed or cancelled."""


class AttemptAllowanceExhausted(RunModelError):
    """The run's max_attempts_per_unit is already reached for this unit."""


class AttemptNotSucceeded(RunModelError):
    """select_attempt was called on an attempt without disposition='succeeded'."""


class UnitAlreadySelected(RunModelError):
    """The unit already has a selected attempt; selection never changes."""


class ManifestConflict(RunModelError):
    """A manifest entry conflicts with an already-registered instance id."""


class PromotionRefused(RunModelError):
    """A promotion request failed validation; the whole request is refused."""


class DeletionRefused(RunModelError):
    """mark_run_deleting's preconditions were not met."""


class RunNotFinishable(RunModelError):
    """finish_run was called on a run that is not open with every unit terminal."""


#: Product kinds whose promotion also maintains ``dev``'s ``vbest`` column,
#: and the ``dev`` table holding each kind's row (found through that
#: table's ``instance`` column). The kind strings are the ones the
#: registration writers and stages use (``rapidpipe/stages/register.py``
#: ``_KNOWN_KINDS``; ``rapidpipe/stages/difference.py`` for
#: ``reference-image``). Result sets (``source-catalog`` and the like) and
#: any other kind have no ``vbest`` and are skipped (supervisor ruling,
#: step 3, 2026-09-24: "promotion maintains them", products page,
#: "Registration metadata").
_VBEST_TABLES = {
    "l2-image": "l2files",
    "reference-image": "refimages",
    "difference-image": "diffimages",
    "psf": "psfs",
}

#: How long a scratch run lives before :func:`rapidpipe.runs.cleanup.
#: expire_runs` may delete it, when ``create_run`` is given no explicit
#: ``expires_at`` (runs page, "Deletion": scratch runs expire unless
#: pinned).
_SCRATCH_DEFAULT_LIFETIME = "14 days"


# ======================================================================
# create_run
# ======================================================================

def create_run(
    conn: psycopg2.extensions.connection,
    kind: str,
    owner: str,
    purpose: str | None,
    selected_stages: Sequence[str],
    code_revision: str,
    image_digest: str | None,
    schema_version: str,
    settings_overlay_ref: str | None,
    input_selection_ref: str | None,
    lane: str,
    resource_profile: str,
    database_target: str,
    max_attempts_per_unit: int,
    auto_promote: bool,
    check_policy_ref: str | None,
    seed_run: str | None = None,
    expires_at: datetime | None = None,
) -> str:
    """Create a run and return its id.

    A run's kind is fixed at creation and decides the custody of
    everything it makes (runs page, "Runs"). ``seed_run`` copies
    configuration only -- this function does not copy or authorise reuse
    of another run's scratch outputs (runs page: "Seeding a new run
    copies configuration and permitted input selections; it does not
    authorise reuse of another run's scratch outputs."); the caller
    supplies whatever configuration it wants copied through the ordinary
    parameters, this function only records the ``seed_run`` lineage.

    ``expires_at`` is when an unpinned scratch run becomes eligible for
    :func:`rapidpipe.runs.cleanup.expire_runs`. When ``None``, a scratch
    run expires 14 days after creation (``now() + interval '14 days'``,
    evaluated in the same statement that sets ``created``) and a
    production run never expires (NULL).
    """
    if kind not in ("scratch", "production"):
        raise ValueError(f"kind must be 'scratch' or 'production', got {kind!r}")

    run_id = new_ulid()
    with conn.cursor() as cur:
        if seed_run is not None:
            cur.execute(
                "SELECT 1 FROM runs WHERE id = %s", (seed_run,))
            if cur.fetchone() is None:
                raise RunNotFound(f"seed_run {seed_run!r} does not exist")

        cur.execute(
            """
            INSERT INTO runs (
                id, kind, owner, purpose, selected_stages, code_revision,
                image_digest, schema_version, settings_overlay_ref,
                input_selection_ref, lane, resource_profile,
                database_target, max_attempts_per_unit, auto_promote,
                check_policy_ref, seed_run, expires_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                COALESCE(
                    %s::timestamptz,
                    CASE WHEN %s = 'scratch'
                         THEN now() + %s::interval END)
            )
            """,
            (
                run_id, kind, owner, purpose, list(selected_stages),
                code_revision, image_digest, schema_version,
                settings_overlay_ref, input_selection_ref, lane,
                resource_profile, database_target, max_attempts_per_unit,
                auto_promote, check_policy_ref, seed_run,
                expires_at, kind, _SCRATCH_DEFAULT_LIFETIME,
            ),
        )
    return run_id


# ======================================================================
# add_unit / bind_unit_inputs
# ======================================================================

def _fetch_run_state(cur, run_id: str) -> str:
    cur.execute("SELECT state FROM runs WHERE id = %s FOR SHARE", (run_id,))
    row = cur.fetchone()
    if row is None:
        raise RunNotFound(f"run {run_id!r} does not exist")
    return row[0]


def _refuse_if_run_deleting_or_deleted(cur, run_id: str) -> None:
    state = _fetch_run_state(cur, run_id)
    if state in _TERMINAL_RUN_STATES:
        raise RunDeletingOrDeleted(
            f"run {run_id!r} is {state!r}; refusing")


def add_unit(
    conn: psycopg2.extensions.connection,
    run_id: str,
    stage: str,
    unit_kind: str,
    unit_id: str,
) -> None:
    """Add a unit of work to a run.

    Refuses if the run is deleting or deleted (runs page, "Deletion":
    "Attempt allocation, input binding and result acceptance use the same
    run fence and refuse a deleting or deleted run."). Unit identity is
    unique within a run and stage (runs page, "Identifiers"); adding the
    same (run, stage, unit_id) twice is a no-op rather than an error,
    since re-declaring the same piece of work is not itself a conflict
    the way a conflicting manifest replay is.
    """
    with conn.cursor() as cur:
        _refuse_if_run_deleting_or_deleted(cur, run_id)
        cur.execute(
            """
            INSERT INTO units (id, run, stage, unit_kind, unit_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (run, stage, unit_id) DO NOTHING
            """,
            (new_ulid(), run_id, stage, unit_kind, unit_id),
        )


def bind_unit_inputs(
    conn: psycopg2.extensions.connection,
    run_id: str,
    stage: str,
    unit_id: str,
    producer_instances: Iterable[str],
) -> None:
    """Freeze this unit's input bindings.

    "Inputs are bound in unit_inputs before execution and retained for
    retries." (runs page, "Units"). Refuses on a deleting or deleted run,
    same fence as add_unit. Idempotent per (unit, producer_instance): a
    retry that rebinds the same inputs is a no-op, matching "retained for
    retries" rather than an error on re-bind.
    """
    with conn.cursor() as cur:
        _refuse_if_run_deleting_or_deleted(cur, run_id)
        cur.execute(
            "SELECT id FROM units WHERE run = %s AND stage = %s AND unit_id = %s",
            (run_id, stage, unit_id),
        )
        row = cur.fetchone()
        if row is None:
            raise UnitNotFound(
                f"unit (run={run_id!r}, stage={stage!r}, unit_id={unit_id!r}) "
                "does not exist")
        unit_row_id = row[0]

        for producer_instance in producer_instances:
            cur.execute(
                """
                INSERT INTO unit_inputs (id, unit, producer_instance)
                VALUES (%s, %s, %s)
                ON CONFLICT (unit, producer_instance) DO NOTHING
                """,
                (new_ulid(), unit_row_id, producer_instance),
            )


# ======================================================================
# allocate_attempt
# ======================================================================

def allocate_attempt(
    conn: psycopg2.extensions.connection,
    run_id: str,
    stage: str,
    unit_id: str,
) -> str:
    """Allocate a new attempt for a unit and set the unit state to running.

    Refuses (runs page, "Attempts" and "Deletion"):
      - if the unit is terminal (complete, failed or cancelled) --
        UnitTerminal;
      - if the run is deleting or deleted -- RunDeletingOrDeleted;
      - if the attempt allowance (``runs.max_attempts_per_unit``,
        "counting the first attempt and all Batch retries") is already
        exhausted for this unit -- AttemptAllowanceExhausted.

    Locks the unit row for the duration of the check-and-allocate so two
    concurrent callers cannot both allocate past the allowance.
    """
    with conn.cursor() as cur:
        run_state = _fetch_run_state(cur, run_id)
        if run_state in _TERMINAL_RUN_STATES:
            raise RunDeletingOrDeleted(f"run {run_id!r} is {run_state!r}; refusing")

        cur.execute(
            """
            SELECT id, state, run
            FROM units
            WHERE run = %s AND stage = %s AND unit_id = %s
            FOR UPDATE
            """,
            (run_id, stage, unit_id),
        )
        row = cur.fetchone()
        if row is None:
            raise UnitNotFound(
                f"unit (run={run_id!r}, stage={stage!r}, unit_id={unit_id!r}) "
                "does not exist")
        unit_row_id, unit_state, _ = row

        if unit_state in _TERMINAL_UNIT_STATES:
            raise UnitTerminal(
                f"unit {unit_row_id!r} is {unit_state!r}, a terminal state; "
                "refusing to allocate another attempt")

        cur.execute(
            "SELECT max_attempts_per_unit FROM runs WHERE id = %s", (run_id,))
        (max_attempts,) = cur.fetchone()

        cur.execute(
            "SELECT count(*) FROM attempts WHERE unit = %s", (unit_row_id,))
        (existing_attempts,) = cur.fetchone()

        if existing_attempts >= max_attempts:
            raise AttemptAllowanceExhausted(
                f"unit {unit_row_id!r} has {existing_attempts} attempt(s), "
                f"at the run's max_attempts_per_unit ({max_attempts})")

        attempt_id = new_ulid()
        cur.execute(
            """
            INSERT INTO attempts (id, run, stage, unit, output_location)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (attempt_id, run_id, stage, unit_row_id,
             f"runs/{run_id}/{stage}/{unit_id}/{attempt_id}"),
        )
        cur.execute(
            "UPDATE units SET state = 'running', updated = now() WHERE id = %s",
            (unit_row_id,),
        )
    return attempt_id


# ======================================================================
# record_attempt_result
# ======================================================================

def record_attempt_result(
    conn: psycopg2.extensions.connection,
    attempt_id: str,
    exit_code: int | None,
    disposition: str,
    output_location: str,
    execution_record: dict[str, Any],
    scheduler_job_id: str | None,
) -> None:
    """Record an attempt's outcome and its execution record.

    ``exit_code`` is ``None`` for a ``killed`` attempt the launcher never
    saw a container exit code for (``attempts.exit_code`` is nullable for
    exactly this case; see ``rapidpipe.launch.batch.reconcile``, "no exit
    code" branch).

    ``disposition`` is one of the runs page's five values: 'succeeded',
    'failed', 'transient', 'killed', 'lost' -- "null while queued or
    running" (runs page, "Attempts"), so this function is the one place
    that ever sets a non-null disposition. A retryable failure ('transient'
    or 'lost') returns the unit to 'ready' while attempts remain,
    otherwise 'failed' terminates it (runs page, "Units"); success does
    NOT itself complete the unit -- completion happens only through
    ``select_attempt``, since "Selection of a successful completed
    attempt makes it complete."

    Idempotent per attempt: recording the same disposition and outputs
    twice (an uncertain-commit retry, per the stage contract's "Database
    writes are attempt-scoped and retry-safe") updates the row rather
    than erroring, since the caller may legitimately retry the same
    result write after a lost commit acknowledgement.
    """
    valid_dispositions = ("succeeded", "failed", "transient", "killed", "lost")
    if disposition not in valid_dispositions:
        raise ValueError(
            f"disposition must be one of {valid_dispositions}, got {disposition!r}")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT unit, run FROM attempts WHERE id = %s FOR UPDATE",
            (attempt_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise AttemptNotFound(f"attempt {attempt_id!r} does not exist")
        unit_row_id, attempt_run_id = row

        cur.execute(
            """
            UPDATE attempts
            SET exit_code = %s, disposition = %s, output_location = %s,
                scheduler_job_id = %s, ended = now(), reconcile_note = NULL
            WHERE id = %s
            """,
            (exit_code, disposition, output_location, scheduler_job_id, attempt_id),
        )

        cur.execute(
            """
            INSERT INTO execution_records (
                attempt, source_revision, working_copy_patch, image_digest,
                schema_version, resolved_settings, settings_hash,
                scheduler_metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (attempt) DO UPDATE SET
                source_revision = EXCLUDED.source_revision,
                working_copy_patch = EXCLUDED.working_copy_patch,
                image_digest = EXCLUDED.image_digest,
                schema_version = EXCLUDED.schema_version,
                resolved_settings = EXCLUDED.resolved_settings,
                settings_hash = EXCLUDED.settings_hash,
                scheduler_metadata = EXCLUDED.scheduler_metadata
            """,
            (
                attempt_id,
                execution_record.get("source_revision"),
                execution_record.get("working_copy_patch"),
                execution_record.get("image_digest"),
                execution_record.get("schema_version"),
                json.dumps(execution_record.get("resolved_settings", {})),
                execution_record.get("settings_hash"),
                json.dumps(execution_record.get("scheduler_metadata", {})),
            ),
        )

        if disposition in ("transient", "lost"):
            # A retryable failure returns the unit to ready while attempts
            # remain; otherwise it becomes failed (runs page, "Units").
            cur.execute(
                "SELECT max_attempts_per_unit FROM runs WHERE id = %s",
                (attempt_run_id,),
            )
            (max_attempts,) = cur.fetchone()
            cur.execute(
                "SELECT count(*) FROM attempts WHERE unit = %s", (unit_row_id,))
            (attempt_count,) = cur.fetchone()
            next_state = "ready" if attempt_count < max_attempts else "failed"
            cur.execute(
                """
                UPDATE units SET state = %s, updated = now()
                WHERE id = %s AND state NOT IN ('complete', 'cancelled')
                """,
                (next_state, unit_row_id),
            )
        elif disposition in ("failed", "killed"):
            cur.execute(
                """
                UPDATE units SET state = 'failed', updated = now()
                WHERE id = %s AND state NOT IN ('complete', 'cancelled')
                """,
                (unit_row_id,),
            )
        # disposition == 'succeeded': the unit stays 'running' until
        # select_attempt is called; success alone does not complete it.


# ======================================================================
# record_reconcile_note
# ======================================================================

def record_reconcile_note(
    conn: psycopg2.extensions.connection,
    attempt_id: str,
    note: str,
) -> None:
    """Record why :func:`~rapidpipe.launch.batch.reconcile` could not
    determine this attempt's outcome, without setting a disposition.

    Leaves ``disposition`` and ``scheduler_job_id`` untouched -- the
    attempt stays exactly as unresolved (and re-reconcilable) as it was
    before reconcile looked at it (runs page, "Attempts": disposition is
    null while queued or running). Used when a Batch job has SUCCEEDED
    but reconcile could not fetch its manifest or execution record from
    S3 for a reason other than the object being absent (e.g. an
    AccessDenied on the launcher's own role): that is not evidence the
    job failed, so it must not consume one of the unit's limited
    attempts. A later reconcile call for the same attempt overwrites
    this note, or clears it via :func:`record_attempt_result` once it
    can record a real disposition.

    Refuses (``AttemptAlreadyResolved``) if the attempt already has a
    disposition -- a completed attempt's outcome is not something a
    later reconcile call should annotate over.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT disposition FROM attempts WHERE id = %s FOR UPDATE",
            (attempt_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise AttemptNotFound(f"attempt {attempt_id!r} does not exist")
        (disposition,) = row
        if disposition is not None:
            raise AttemptAlreadyResolved(
                f"attempt {attempt_id!r} already has disposition "
                f"{disposition!r}; refusing to overwrite it with a "
                "reconcile note")

        cur.execute(
            "UPDATE attempts SET reconcile_note = %s WHERE id = %s",
            (note, attempt_id),
        )


# ======================================================================
# record_scheduler_job
# ======================================================================

def record_scheduler_job(
    conn: psycopg2.extensions.connection,
    attempt_id: str,
    scheduler_job_id: str,
    output_location: str | None = None,
) -> None:
    """Record the scheduler (Batch) job id an attempt was submitted as.

    Called by ``rapidpipe.launch.batch.submit_unit`` right after
    ``submit_job`` returns, so the attempt row carries its job id before
    ``reconcile`` ever needs to look it up. Refuses if the attempt
    already has a disposition: a completed attempt's scheduler job id is
    part of its recorded outcome, not something a later submission call
    should overwrite.

    ``output_location`` is optional and, when given, replaces
    ``allocate_attempt``'s own local-path-shaped placeholder (it has no
    way to know at allocation time whether the attempt's output root is
    local or an ``s3://`` location -- that is a submission-time choice)
    with the real location the caller resolved before submitting the
    job, in the same update.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT disposition FROM attempts WHERE id = %s FOR UPDATE",
            (attempt_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise AttemptNotFound(f"attempt {attempt_id!r} does not exist")
        (disposition,) = row
        if disposition is not None:
            raise AttemptAlreadyResolved(
                f"attempt {attempt_id!r} already has disposition "
                f"{disposition!r}; refusing to record a scheduler job id "
                "for a completed attempt")

        if output_location is not None:
            cur.execute(
                """
                UPDATE attempts
                SET scheduler_job_id = %s, output_location = %s
                WHERE id = %s
                """,
                (scheduler_job_id, output_location, attempt_id),
            )
        else:
            cur.execute(
                "UPDATE attempts SET scheduler_job_id = %s WHERE id = %s",
                (scheduler_job_id, attempt_id),
            )


# ======================================================================
# select_attempt
# ======================================================================

def select_attempt(conn: psycopg2.extensions.connection, attempt_id: str) -> None:
    """Select this attempt as its unit's result and mark the unit complete.

    Locks the unit row FOR UPDATE (runs page, "Attempts": "Selection
    locks the unit row and atomically sets its selected attempt and
    complete state"). Requires disposition == 'succeeded' --
    AttemptNotSucceeded otherwise. Refuses if the unit already has a
    selected attempt (runs page: "the selected attempt never changes")
    or is otherwise terminal -- UnitAlreadySelected / UnitTerminal.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT unit, disposition FROM attempts WHERE id = %s",
            (attempt_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise AttemptNotFound(f"attempt {attempt_id!r} does not exist")
        unit_row_id, disposition = row

        if disposition != "succeeded":
            raise AttemptNotSucceeded(
                f"attempt {attempt_id!r} has disposition {disposition!r}, "
                "not 'succeeded'; refusing to select it")

        cur.execute(
            "SELECT state, selected_attempt FROM units WHERE id = %s FOR UPDATE",
            (unit_row_id,),
        )
        unit_state, selected_attempt = cur.fetchone()

        if selected_attempt is not None:
            raise UnitAlreadySelected(
                f"unit {unit_row_id!r} already has selected attempt "
                f"{selected_attempt!r}; the selection never changes")
        if unit_state in _TERMINAL_UNIT_STATES:
            raise UnitTerminal(
                f"unit {unit_row_id!r} is {unit_state!r}, a terminal state; "
                "refusing to select an attempt for it")

        cur.execute(
            """
            UPDATE units
            SET selected_attempt = %s, state = 'complete', updated = now()
            WHERE id = %s
            """,
            (attempt_id, unit_row_id),
        )


# ======================================================================
# register_manifest
# ======================================================================

def _custody_for_run_kind(run_kind: str) -> str:
    # "a scratch run makes scratch, a production run makes candidates"
    # (runs page, "Runs").
    return "scratch" if run_kind == "scratch" else "candidate"


def register_manifest(
    conn: psycopg2.extensions.connection,
    manifest: dict[str, Any],
    registering_attempt_id: str,
) -> None:
    """Register every output entry in a completion manifest.

    One ``product_instances`` row per output entry, preserving the
    manifest's own instance id and the producing run/stage/attempt it
    names; the registering attempt (which may differ from the producing
    attempt -- "register" can run independently, stage contract, "The
    manifest") is recorded separately. Member rows are written for a
    bundle's files. Dependency edges are recorded from
    ``inputs.products`` and ``inputs.result_sets``, both mapped from
    logical/human product references to instance ids already resolved
    in the manifest -- the products page's identity rule ("downstream
    stages consume only the instance the launcher selected, and
    reference it by instance id, never by logical key") means every
    value under ``inputs.products``/``inputs.result_sets`` a manifest
    lists here IS already an instance id string.

    Replaying an identical manifest is a no-op (runs page, "Instances":
    "Replaying an identical manifest is a no-op"). A conflicting entry
    for an existing instance id raises :class:`ManifestConflict`
    ("conflicting content for an existing instance id is an error").

    Accepts the products page's complete manifest shape (run/unit/stage/
    attempt at the top, ``outputs`` a list of entries each with
    ``kind``, ``format_version``, ``instance``, ``key``, ``primary``,
    ``members``); tolerates the repository skeleton's older
    ``CompletionManifest.to_dict()`` shape (``outputs`` entries with
    ``identity``/``kind``/``format_version``/``location``/``byte_size``/
    ``sha256``, no ``key``/``members``/``primary``) where that mapping is
    unambiguous -- ``identity`` stands in for ``instance``, and a single
    file with no ``members`` list becomes that instance's one member
    (role "primary").
    """
    run_id = manifest["run_id"] if "run_id" in manifest else manifest["run"]
    stage = manifest["stage"]
    outputs = manifest.get("outputs", [])
    inputs = manifest.get("inputs", {})
    input_products = inputs.get("products", {}) if isinstance(inputs, dict) else {}
    input_result_sets = inputs.get("result_sets", []) if isinstance(inputs, dict) else []

    with conn.cursor() as cur:
        cur.execute("SELECT kind FROM runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
        if row is None:
            raise RunNotFound(f"run {run_id!r} does not exist")
        (run_kind,) = row
        custody = _custody_for_run_kind(run_kind)

        cur.execute(
            "SELECT unit FROM attempts WHERE id = %s", (registering_attempt_id,))
        row = cur.fetchone()
        if row is None:
            raise AttemptNotFound(
                f"registering attempt {registering_attempt_id!r} does not exist")

        # Producing attempt: the manifest's own top-level "attempt" (the
        # products page's shape) when present, else the registering
        # attempt (the skeleton's older shape has no separate producing
        # attempt at the manifest level -- registration and production
        # are the same attempt in that case).
        producing_attempt_id = manifest.get("attempt", registering_attempt_id)

        for entry in outputs:
            _register_one_output(
                cur,
                entry=entry,
                run_id=run_id,
                stage=stage,
                producing_attempt_id=producing_attempt_id,
                registering_attempt_id=registering_attempt_id,
                custody=custody,
                input_products=input_products,
                input_result_sets=input_result_sets,
            )


def _entry_instance_id(entry: dict[str, Any]) -> str:
    return entry.get("instance") or entry["identity"]


def _entry_members(entry: dict[str, Any]) -> list[dict[str, Any]]:
    if "members" in entry:
        return entry["members"]
    # The skeleton's OutputEntry shape: one file, no bundle. A database
    # result set (no byte_size/sha256) has no member row.
    if entry.get("byte_size") is not None or entry.get("sha256") is not None:
        return [{
            "role": "primary",
            "path": entry.get("location", ""),
            "bytes": entry.get("byte_size"),
            "sha256": entry.get("sha256"),
        }]
    return []


def _entry_primary_location(entry: dict[str, Any], members: list[dict[str, Any]]) -> str:
    if "primary" in entry:
        # A result set has no primary file ("primary": null); the column is
        # NOT NULL, so it records the empty string.
        return entry["primary"] or ""
    if members:
        return members[0]["path"]
    return entry.get("location", "")


def _entry_logical_key(entry: dict[str, Any]) -> dict[str, Any]:
    return entry.get("key", {})


def _entry_is_result_set(entry: dict[str, Any]) -> bool:
    # A database result set carries no byte-identified members; the
    # products page's "Database result sets" table and the skeleton's
    # OutputEntry.is_file() both use "has no byte_size/sha256" as the
    # file/result-set distinguisher.
    members = _entry_members(entry)
    if members:
        return False
    if "members" in entry:
        return len(entry["members"]) == 0 and entry.get("primary") is None
    return entry.get("byte_size") is None and entry.get("sha256") is None


def _register_one_output(
    cur,
    *,
    entry: dict[str, Any],
    run_id: str,
    stage: str,
    producing_attempt_id: str,
    registering_attempt_id: str,
    custody: str,
    input_products: dict[str, str],
    input_result_sets: list[str],
) -> None:
    instance_id = _entry_instance_id(entry)
    kind = entry["kind"]
    format_version = str(entry["format_version"])
    members = _entry_members(entry)
    primary_location = _entry_primary_location(entry, members)
    logical_key = _entry_logical_key(entry)
    manifest_ref = entry.get("manifest_ref", "")
    is_result_set = _entry_is_result_set(entry)

    cur.execute(
        """
        SELECT kind, logical_key, run, producing_stage, producing_attempt,
               format_version, primary_location
        FROM product_instances WHERE id = %s
        """,
        (instance_id,),
    )
    existing = cur.fetchone()

    if existing is not None:
        (existing_kind, existing_key, existing_run, existing_stage,
         existing_attempt, existing_format, existing_primary) = existing
        same = (
            existing_kind == kind
            and existing_key == logical_key
            and existing_run == run_id
            and existing_stage == stage
            and existing_attempt == producing_attempt_id
            and existing_format == format_version
            and existing_primary == primary_location
        )
        if same:
            # Replaying an identical manifest is a no-op (runs page,
            # "Instances").
            return
        raise ManifestConflict(
            f"instance {instance_id!r} already registered with different "
            "content; conflicting replay is refused")

    cur.execute(
        """
        INSERT INTO product_instances (
            id, kind, logical_key, run, producing_stage, producing_attempt,
            registering_attempt, custody, format_version, primary_location,
            manifest_ref
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            instance_id, kind, json.dumps(logical_key), run_id, stage,
            producing_attempt_id, registering_attempt_id, custody,
            format_version, primary_location, manifest_ref,
        ),
    )

    for member in members:
        cur.execute(
            """
            INSERT INTO product_members (id, instance, role, path, bytes, sha256)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                new_ulid(), instance_id, member.get("role", "primary"),
                member["path"], member.get("bytes", member.get("byte_size", 0)) or 0,
                member.get("sha256", ""),
            ),
        )

    if is_result_set:
        cur.execute(
            "INSERT INTO result_sets (instance, complete, row_count) VALUES (%s, %s, %s)",
            (instance_id, True, entry.get("row_count")),
        )

    dependency_producers: list[str] = list(input_products.values()) + list(input_result_sets)
    for producer_instance in dependency_producers:
        cur.execute(
            """
            INSERT INTO dependencies (id, consumer_instance, producer_instance)
            VALUES (%s, %s, %s)
            ON CONFLICT (consumer_instance, producer_instance) DO NOTHING
            """,
            (new_ulid(), instance_id, producer_instance),
        )


# ======================================================================
# promote
# ======================================================================

def promote(
    conn: psycopg2.extensions.connection,
    who: str,
    reason: str,
    changes: Sequence[tuple[str, dict[str, Any], str | None, str | None]],
    check_policy_version: str | None = None,
    check_result_ids: Sequence[str] = (),
    request_context: dict[str, Any] | None = None,
) -> str:
    """Apply a promotion under one advisory lock; return the promotion id.

    ``changes`` is a list of ``(kind, logical_key, expected_before_instance_or_None,
    after_instance_or_None)`` tuples (runs page, "Promotion"). Takes
    ``pg_advisory_xact_lock`` on one fixed key, then:

      1. Checks every expected before-instance (including expected
         absence, i.e. ``None``) against the actual current selection;
         refuses the WHOLE request on any mismatch
         (:class:`PromotionRefused`). Also refuses a change whose
         after-instance equals its before-instance (nothing to change,
         including ``None`` to ``None``) and a request naming the same
         (kind, logical_key) twice -- the runs page records exactly one
         before and after per affected key.
      2. Validates each non-``None`` after-instance is a candidate from a
         selected attempt, with every provenance dependency in project
         custody (runs page, "Promotion eligibility": "Every provenance
         dependency must identify a complete, retained instance in
         project custody."). A ``None`` after-instance is an unselect:
         there is nothing to validate.
      3. Sets the before rows to candidate and the after rows to
         current, maintains ``dev``'s ``vbest`` for kinds that have one
         (``vbest = 0`` on the before-instance's row, ``vbest = 1`` on the
         after-instance's row, found through the kind's table's
         ``instance`` column; see ``_VBEST_TABLES``), and records the
         promotion and its promotion_changes (an unselect records
         ``after_instance`` NULL). ``request_context`` is stored on the
         promotions row (``{}`` when ``None``); :func:`rollback_promotion`
         records ``{"rollback_of": <promotion id>}`` there.

    Reversal is :func:`rollback_promotion`, which calls this function with
    the inverse mapping: the previous after-instance as the new
    expected-before, and the previous before-instance (possibly ``None``)
    as the new after-instance for each key.
    """
    changes = list(changes)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(%s)", (_PROMOTION_ADVISORY_LOCK_KEY,))

        seen_keys: set[tuple[str, str]] = set()
        for kind, logical_key, expected_before, after_instance in changes:
            key_text = json.dumps(logical_key, sort_keys=True)
            if (kind, key_text) in seen_keys:
                raise PromotionRefused(
                    f"kind={kind!r} key={logical_key!r} appears more than once "
                    "in one promotion; refusing the whole promotion")
            seen_keys.add((kind, key_text))
            if after_instance == expected_before:
                raise PromotionRefused(
                    f"kind={kind!r} key={logical_key!r}: the after instance "
                    f"{after_instance!r} is the same as the before instance; "
                    "refusing the whole promotion")

        # Step 1: check every expected before against the actual current
        # selection. Refuse the whole request on any mismatch.
        for kind, logical_key, expected_before, _after in changes:
            cur.execute(
                """
                SELECT id FROM product_instances
                WHERE kind = %s AND logical_key = %s AND custody = 'current'
                """,
                (kind, json.dumps(logical_key)),
            )
            row = cur.fetchone()
            actual_before = row[0] if row else None
            if actual_before != expected_before:
                raise PromotionRefused(
                    f"expected current selection for kind={kind!r} "
                    f"key={logical_key!r} to be {expected_before!r}, but it "
                    f"is {actual_before!r}; refusing the whole promotion")

        # Step 2: validate every after-instance is eligible. An unselect
        # (after None) has nothing to validate.
        for kind, logical_key, _expected_before, after_instance in changes:
            if after_instance is not None:
                _validate_promotion_eligibility(cur, kind, logical_key, after_instance)

        # Step 3: apply. Before rows (if any) go back to candidate; after
        # rows become current; vbest follows. Record the promotion and
        # its changes.
        promotion_id = new_ulid()
        cur.execute(
            """
            INSERT INTO promotions (
                id, who, reason, check_policy_version, check_result_ids, request_context
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (promotion_id, who, reason, check_policy_version,
             list(check_result_ids), json.dumps(request_context or {})),
        )

        for kind, logical_key, expected_before, after_instance in changes:
            if expected_before is not None:
                cur.execute(
                    "UPDATE product_instances SET custody = 'candidate' WHERE id = %s",
                    (expected_before,),
                )
            if after_instance is not None:
                cur.execute(
                    "UPDATE product_instances SET custody = 'current' WHERE id = %s",
                    (after_instance,),
                )
            _maintain_vbest(cur, kind, expected_before, after_instance)
            cur.execute(
                """
                INSERT INTO promotion_changes (
                    id, promotion, kind, logical_key, before_instance, after_instance
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (new_ulid(), promotion_id, kind, json.dumps(logical_key),
                 expected_before, after_instance),
            )

    return promotion_id


def _maintain_vbest(
    cur, kind: str, before_instance: str | None, after_instance: str | None,
) -> None:
    """Flip ``dev``'s ``vbest`` for one promotion change, in the caller's
    transaction: 0 on the before-instance's row, 1 on the after-instance's.

    Only the kinds in ``_VBEST_TABLES`` have a ``dev`` row; every other kind
    (result sets, catalogs) is skipped. A missing ``dev`` row (an instance
    registered without one) updates nothing -- ``vbest`` is ``dev``'s
    legacy flag, kept in step where a row exists, not a promotion
    precondition. The baseline's CHECK allows 0, 1 and 2; this sets only
    0 and 1.
    """
    table = _VBEST_TABLES.get(kind)
    if table is None:
        return
    # table comes from the fixed mapping above, never from caller input.
    if before_instance is not None:
        cur.execute(f"UPDATE {table} SET vbest = 0 WHERE instance = %s", (before_instance,))
    if after_instance is not None:
        cur.execute(f"UPDATE {table} SET vbest = 1 WHERE instance = %s", (after_instance,))


def _validate_promotion_eligibility(
    cur, kind: str, logical_key: dict[str, Any], after_instance: str,
) -> None:
    cur.execute(
        """
        SELECT custody FROM product_instances WHERE id = %s
        """,
        (after_instance,),
    )
    row = cur.fetchone()
    if row is None:
        raise PromotionRefused(
            f"after instance {after_instance!r} for kind={kind!r} does not exist")
    (custody,) = row
    if custody not in ("candidate", "current"):
        raise PromotionRefused(
            f"after instance {after_instance!r} has custody {custody!r}, "
            "not 'candidate'; only a candidate from a selected attempt "
            "may be promoted")

    # "each from its unit's selected completed attempt" (runs page,
    # "Promotion"): the producing attempt must be the selected attempt of
    # its unit.
    cur.execute(
        """
        SELECT pi.producing_attempt, u.selected_attempt
        FROM product_instances pi
        JOIN attempts a ON a.id = pi.producing_attempt
        JOIN units u ON u.id = a.unit
        WHERE pi.id = %s
        """,
        (after_instance,),
    )
    row = cur.fetchone()
    if row is None or row[0] != row[1]:
        raise PromotionRefused(
            f"after instance {after_instance!r} was not produced by its "
            "unit's selected attempt; refusing")

    # Every provenance dependency must identify a complete, retained
    # instance in project custody (candidate or current; scratch is not
    # project custody).
    cur.execute(
        """
        SELECT d.producer_instance, pi.custody, pi.deletion_state
        FROM dependencies d
        JOIN product_instances pi ON pi.id = d.producer_instance
        WHERE d.consumer_instance = %s
        """,
        (after_instance,),
    )
    for producer_instance, producer_custody, deletion_state in cur.fetchall():
        if producer_custody not in ("candidate", "current"):
            raise PromotionRefused(
                f"after instance {after_instance!r} depends on "
                f"{producer_instance!r}, which is {producer_custody!r} "
                "(not project custody); refusing")
        if deletion_state != "retained":
            raise PromotionRefused(
                f"after instance {after_instance!r} depends on "
                f"{producer_instance!r}, which is {deletion_state!r}, "
                "not retained; refusing")


# ======================================================================
# promote_run / rollback_promotion
# ======================================================================

def promote_run(
    conn: psycopg2.extensions.connection,
    run_id: str,
    who: str,
    reason: str,
    *,
    kinds: Sequence[str] | None = None,
    check_policy_version: str | None = None,
) -> str:
    """Promote a production run's deliverables; return the promotion id.

    The run must be ``production`` -- a scratch run is refused ("scratch
    never leaves scratch", runs page, "Runs" and "Custody") -- and not
    deleting or deleted. Its deliverables are every ``product_instances``
    row of the run with custody ``candidate`` whose producing attempt is
    its unit's selected attempt, optionally filtered to ``kinds``. There
    must be exactly one such candidate per (kind, logical_key): the runs
    page's promotion replaces exactly one instance per key, so two
    candidates for one key is refused rather than guessed between. Each
    change's expected-before is the instance currently ``current`` for
    that (kind, logical_key), or ``None``; the changes are then applied by
    :func:`promote`, which takes the promotion lock -- taken here first
    too, so the expected-befores read below cannot go stale before
    ``promote`` re-checks them (the transaction-scoped lock is re-entrant).
    ``request_context`` on the promotions row is ``{"run": run_id}``.

    Refuses (:class:`PromotionRefused`) when there is nothing to promote.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(%s)", (_PROMOTION_ADVISORY_LOCK_KEY,))
        cur.execute(
            "SELECT kind, state FROM runs WHERE id = %s FOR SHARE", (run_id,))
        row = cur.fetchone()
        if row is None:
            raise RunNotFound(f"run {run_id!r} does not exist")
        run_kind, run_state = row
        if run_kind != "production":
            raise PromotionRefused(
                f"run {run_id!r} is a {run_kind!r} run; scratch never leaves "
                "scratch, only a production run's candidates may be promoted")
        if run_state in _TERMINAL_RUN_STATES:
            raise RunDeletingOrDeleted(f"run {run_id!r} is {run_state!r}; refusing")

        query = """
            SELECT pi.id, pi.kind, pi.logical_key
            FROM product_instances pi
            JOIN attempts a ON a.id = pi.producing_attempt
            JOIN units u ON u.id = a.unit
            WHERE pi.run = %s
              AND pi.custody = 'candidate'
              AND u.selected_attempt = pi.producing_attempt
        """
        params: list[Any] = [run_id]
        if kinds is not None:
            query += " AND pi.kind = ANY(%s)"
            params.append(list(kinds))
        query += " ORDER BY pi.kind, pi.id"
        cur.execute(query, params)
        deliverables = cur.fetchall()

        by_key: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        for instance_id, kind, logical_key in deliverables:
            key_text = json.dumps(logical_key, sort_keys=True)
            if (kind, key_text) in by_key:
                raise PromotionRefused(
                    f"run {run_id!r} has more than one candidate for "
                    f"kind={kind!r} key={logical_key!r} "
                    f"({by_key[(kind, key_text)][0]!r} and {instance_id!r}); "
                    "a promotion replaces exactly one instance per key")
            by_key[(kind, key_text)] = (instance_id, logical_key)

        if not by_key:
            raise PromotionRefused(
                f"run {run_id!r} has no candidate from a selected attempt"
                + (f" of kinds {list(kinds)!r}" if kinds is not None else "")
                + "; nothing to promote")

        changes: list[tuple[str, dict[str, Any], str | None, str | None]] = []
        for (kind, _key_text), (instance_id, logical_key) in by_key.items():
            cur.execute(
                """
                SELECT id FROM product_instances
                WHERE kind = %s AND logical_key = %s AND custody = 'current'
                """,
                (kind, json.dumps(logical_key)),
            )
            current = cur.fetchone()
            changes.append((kind, logical_key, current[0] if current else None, instance_id))

    return promote(
        conn, who, reason, changes,
        check_policy_version=check_policy_version,
        request_context={"run": run_id},
    )


def rollback_promotion(
    conn: psycopg2.extensions.connection,
    promotion_id: str,
    who: str,
    reason: str,
) -> str:
    """Reverse one promotion; return the new (reversing) promotion id.

    Reads the promotion's ``promotion_changes`` and applies the inverse
    mapping through :func:`promote`: for each key, the expected-before is
    the recorded after-instance and the new after-instance is the
    recorded before-instance, which may be ``None`` (the key goes back to
    having no current instance). ``promote`` refuses the whole reversal if
    any recorded after-selection is no longer current -- a later promotion
    changed that key, and the runs page reverses a promotion only against
    the selection it made. The new promotions row records
    ``request_context = {"rollback_of": promotion_id}``.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM promotions WHERE id = %s", (promotion_id,))
        if cur.fetchone() is None:
            raise PromotionRefused(f"promotion {promotion_id!r} does not exist")
        cur.execute(
            """
            SELECT kind, logical_key, before_instance, after_instance
            FROM promotion_changes WHERE promotion = %s ORDER BY id
            """,
            (promotion_id,),
        )
        recorded = cur.fetchall()
    if not recorded:
        raise PromotionRefused(
            f"promotion {promotion_id!r} recorded no changes; nothing to roll back")

    inverse = [
        (kind, logical_key, after_instance, before_instance)
        for kind, logical_key, before_instance, after_instance in recorded
    ]
    return promote(
        conn, who, reason, inverse,
        request_context={"rollback_of": promotion_id},
    )


# ======================================================================
# finish_run
# ======================================================================

def finish_run(conn: psycopg2.extensions.connection, run_id: str) -> None:
    """Mark an open run finished: state ``finished``, ``finished_at = now()``.

    Only an explicit call finishes a run -- ``rapidpipe.launch.batch.
    reconcile`` never does. Refuses (:class:`RunNotFinishable`, with the
    reason) unless the run is ``open``, has at least one unit, and every
    unit is terminal (complete, failed or cancelled). Locks the run row
    so a concurrent ``add_unit`` cannot slip a new unit in between the
    check and the update.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM runs WHERE id = %s FOR UPDATE", (run_id,))
        row = cur.fetchone()
        if row is None:
            raise RunNotFound(f"run {run_id!r} does not exist")
        (state,) = row
        if state != "open":
            raise RunNotFinishable(f"run {run_id!r} is {state!r}, not 'open'")

        cur.execute(
            """
            SELECT count(*),
                   count(*) FILTER (WHERE state NOT IN %s)
            FROM units WHERE run = %s
            """,
            (_TERMINAL_UNIT_STATES, run_id),
        )
        total, unfinished = cur.fetchone()
        if total == 0:
            raise RunNotFinishable(f"run {run_id!r} has no units")
        if unfinished:
            raise RunNotFinishable(
                f"run {run_id!r} has {unfinished} of {total} unit(s) not yet "
                "complete, failed or cancelled")

        cur.execute(
            "UPDATE runs SET state = 'finished', finished_at = now() WHERE id = %s",
            (run_id,),
        )


# ======================================================================
# mark_run_deleting / mark_run_deleted
# ======================================================================

def mark_run_deleting(
    conn: psycopg2.extensions.connection, run_id: str, requested_by: str,
) -> None:
    """Guard and begin deletion of a scratch run.

    Locks the run, verifies ``requested_by`` is the owner and the run's
    kind is 'scratch', refuses if any attempt is queued or running
    (disposition IS NULL) or unresolved ('lost'), or if any
    ``unit_inputs``/``dependencies`` row from OUTSIDE the run points at
    one of its instances, then sets state 'deleting' -- all in one
    transaction (runs page, "Deletion").
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, owner, state FROM runs WHERE id = %s FOR UPDATE",
            (run_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise RunNotFound(f"run {run_id!r} does not exist")
        kind, owner, state = row

        if kind != "scratch":
            raise DeletionRefused(
                f"run {run_id!r} has kind {kind!r}; only a scratch run may "
                "be deleted")
        if owner != requested_by:
            raise DeletionRefused(
                f"run {run_id!r} is owned by {owner!r}, not {requested_by!r}; "
                "refusing")
        if state in _TERMINAL_RUN_STATES:
            raise DeletionRefused(f"run {run_id!r} is already {state!r}")

        cur.execute(
            """
            SELECT count(*) FROM attempts
            WHERE run = %s AND (disposition IS NULL OR disposition = 'lost')
            """,
            (run_id,),
        )
        (unresolved,) = cur.fetchone()
        if unresolved:
            raise DeletionRefused(
                f"run {run_id!r} has {unresolved} queued, running or "
                "unresolved ('lost') attempt(s); refusing")

        # Any unit_inputs or dependencies row from OUTSIDE this run
        # pointing at one of its instances.
        cur.execute(
            """
            SELECT count(*)
            FROM unit_inputs ui
            JOIN product_instances pi ON pi.id = ui.producer_instance
            JOIN units u ON u.id = ui.unit
            WHERE pi.run = %s AND u.run != %s
            """,
            (run_id, run_id),
        )
        (outside_unit_inputs,) = cur.fetchone()
        if outside_unit_inputs:
            raise DeletionRefused(
                f"run {run_id!r} has {outside_unit_inputs} unit_inputs "
                "binding from another run against its outputs; refusing")

        cur.execute(
            """
            SELECT count(*)
            FROM dependencies d
            JOIN product_instances producer ON producer.id = d.producer_instance
            JOIN product_instances consumer ON consumer.id = d.consumer_instance
            WHERE producer.run = %s AND consumer.run != %s
            """,
            (run_id, run_id),
        )
        (outside_dependencies,) = cur.fetchone()
        if outside_dependencies:
            raise DeletionRefused(
                f"run {run_id!r} has {outside_dependencies} dependency "
                "edge(s) from a retained output outside the run; refusing")

        cur.execute(
            "UPDATE runs SET state = 'deleting' WHERE id = %s", (run_id,))


def mark_run_deleted(conn: psycopg2.extensions.connection, run_id: str) -> None:
    """Record that a deleting run's cleanup has completed.

    Physical cleanup of the run's S3 object versions and run-scoped
    science rows is not this function's job; it is
    :func:`rapidpipe.runs.cleanup.delete_run`'s (runs page, "Deletion":
    "After the deleting state commits, cleanup idempotently removes the
    run's object versions and run-scoped science rows, then records
    completion"). This function only performs that final "records
    completion" step, which ``delete_run`` calls once cleanup succeeded.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM runs WHERE id = %s FOR UPDATE", (run_id,))
        row = cur.fetchone()
        if row is None:
            raise RunNotFound(f"run {run_id!r} does not exist")
        (state,) = row
        if state != "deleting":
            raise DeletionRefused(
                f"run {run_id!r} is {state!r}, not 'deleting'; "
                "mark_run_deleted expects the deleting -> deleted transition")

        cur.execute(
            "UPDATE runs SET state = 'deleted', deleted_at = now() WHERE id = %s",
            (run_id,),
        )
