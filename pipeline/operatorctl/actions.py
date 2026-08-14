"""The operate-tier actions, the break-glass protocol, and the read views.

Each function here is a thin call into one database function from
migrations 031, 032, or DRAFT 047. They take an open connection and
return the function's own jsonb result: transaction ownership is the
caller's (``main`` opens one session per invocation), and nothing here
decides for itself whether to commit.

WHY THE KEYED OVERLOADS. Every mutating call below binds DRAFT 047's
signature, which takes the idempotency key first. When the drafts are
absent the call fails with an undefined-function error rather than
silently falling back to the unkeyed 031 signature — deliberate, because
a silent fallback would drop the idempotency and expected-state contract
without telling anyone, which is the exact failure mode the contract
exists to prevent. ``draft_schema_present`` lets a caller ask first.
"""

from pipeline.operatorctl.contract import call_function

# ---------------------------------------------------------------------------
# Availability probe.
# ---------------------------------------------------------------------------
# The same shape `pipeline.intent.cancellation.is_available` uses for DRAFT
# 046: ask the catalog whether the function exists rather than calling it and
# interpreting the failure. A probe that asks pg_proc is unambiguous; a probe
# that catches an exception cannot tell "not deployed" from "deployed and
# broken".
_KEYED_RETRY_PROBE = """
SELECT EXISTS (
  SELECT 1 FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
   WHERE n.nspname = 'derived'
     AND p.proname = 'retry_parked_attempts'
     AND p.pronargs = 8
)
"""


def draft_schema_present(conn):
    """True when DRAFT 047's keyed overloads are applied.

    Distinguished from 031's unkeyed function by argument count: 031's
    takes six, 047's keyed overload takes eight. Both may legitimately
    exist at once — that is the point of an additive overload — so the
    probe must be specific about which one it is asking for.
    """
    with conn.cursor() as cur:
        cur.execute(_KEYED_RETRY_PROBE)
        return bool(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# Operate tier.
# ---------------------------------------------------------------------------
def retry_parked_attempts(conn, idempotency_key, run_id, reason,
                          expected_state=None, max_attempts=50,
                          dry_run=True, policy_citation=None):
    """Release parked attempts within a mandatory run_id scope.

    ``expected_state`` is the candidate count the operator saw, as
    ``{"candidates": n}``: the apply refuses if the population moved
    between the rehearsal and the decision.
    """
    return call_function(
        conn,
        "SELECT derived.retry_parked_attempts(%s, %s, %s, %s::jsonb, %s, %s, %s)",
        (idempotency_key, run_id, reason, _json(expected_state),
         max_attempts, dry_run, policy_citation))


def add_problem_category(conn, idempotency_key, category, description, reason,
                         expected_state=None, dry_run=True,
                         policy_citation=None):
    """Extend the problems-taxonomy vocabulary.

    ``expected_state`` is ``{"already_present": false}`` for the ordinary
    case of adding something believed new.
    """
    return call_function(
        conn,
        "SELECT derived.add_problem_category(%s, %s, %s, %s, %s::jsonb, %s, %s)",
        (idempotency_key, category, description, reason,
         _json(expected_state), dry_run, policy_citation))


def repair_refused_outbox_rows(conn, idempotency_key, release_identity, reason,
                               expected_state=None, max_rows=200, dry_run=True,
                               policy_citation=None):
    """Move REFUSED alert_outbox rows for one release back to PENDING.

    ``expected_state`` is the REFUSED count the operator saw, as
    ``{"candidates": n}``: the apply refuses if the population moved
    between the rehearsal and the decision — the same shape
    ``retry_parked_attempts`` uses, for the same reason (draft 053's
    header: this targets specific state, not a fire-and-forget action).
    """
    return call_function(
        conn,
        "SELECT derived.repair_refused_outbox_rows(%s, %s, %s, %s::jsonb, "
        "                                          %s, %s, %s)",
        (idempotency_key, release_identity, reason, _json(expected_state),
         max_rows, dry_run, policy_citation))


def record_external_action(conn, idempotency_key, action_class, target_scope,
                           reason, expected_state=None, dry_run=True,
                           rows_affected=0, detail=None,
                           policy_citation=None):
    """Record an operator action whose target is outside this database.

    The ledger records operator actions, not only database mutations
    (brief G, G3): an AWS Batch termination has no row here to point at,
    and would otherwise leave the audited history claiming a quiet night.
    """
    return call_function(
        conn,
        "SELECT derived.record_external_action("
        "%s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s)",
        (idempotency_key, action_class, target_scope, reason,
         _json(expected_state), dry_run, rows_affected, _json(detail),
         policy_citation))


# ---------------------------------------------------------------------------
# Break-glass. Three events, none dry-runnable — the event IS the mutation.
# ---------------------------------------------------------------------------
def break_glass_open(conn, reason, target_scope):
    """Open a break-glass session loudly. Returns the open audit id."""
    return _scalar(conn, "SELECT derived.break_glass_open(%s, %s)",
                   (reason, target_scope))


def break_glass_close(conn, open_audit_id, reason, tables_touched, changes):
    """Close explicitly: reason, tables touched, and changes all mandatory."""
    return _scalar(
        conn, "SELECT derived.break_glass_close(%s, %s, %s, %s)",
        (open_audit_id, reason, tables_touched, changes))


def break_glass_reconcile(conn, open_audit_id, reason, passed):
    """Record the reconciliation outcome. Only a PASS clears region 7."""
    return _scalar(conn, "SELECT derived.break_glass_reconcile(%s, %s, %s)",
                   (open_audit_id, reason, passed))


# ---------------------------------------------------------------------------
# Read-only views.
# ---------------------------------------------------------------------------
_UNRECONCILED = """
SELECT open_audit_id, opened_at, actor, open_reason, target_scope,
       close_audit_id, has_passing_reconciliation,
       round(age_hours::numeric, 2) AS age_hours, state
  FROM derived.region7_unreconciled_break_glass
 ORDER BY opened_at
"""

_RECENT_AUDIT = """
SELECT audit_id, performed_at, actor, action_class, action_tier,
       target_scope, reason, dry_run, rows_affected, idempotency_key
  FROM derived.mutation_audit
 ORDER BY audit_id DESC
 LIMIT %s
"""

# The same query without the DRAFT columns, for a database where 047 has not
# landed. A read view that simply failed there would make `rapidctl audit` —
# the one subcommand an operator reaches for when something has gone wrong —
# unavailable on exactly the deployed schema it is most needed against.
_RECENT_AUDIT_BASE = """
SELECT audit_id, performed_at, actor, action_class, action_tier,
       target_scope, reason, dry_run, rows_affected
  FROM derived.mutation_audit
 ORDER BY audit_id DESC
 LIMIT %s
"""


# The `attempts --state --older-than` filter. Reads the attempt row alone —
# no join — because a state/age filter over the population is the coarse
# panel an operator scans BEFORE reaching for `show-attempt` on one row; the
# joined detail belongs to that narrower command, not to every row of a
# possibly-large listing.
#
# `rapid_outcome = 'success' AND product_disposition = 'none'` is flagged
# in its own column rather than left for the caller to notice: the campaign
# names this exact shape "success+none on a product route" — an attempt the
# application reported successful while recording no product at all, which
# is either a job type that legitimately produces none (registration,
# reprocessing — see `pipeline.seams._operational_class_for`'s docstring on
# both being route-vocabulary types with no product path) or a silent gap
# in what the record captured. The CLI cannot tell those apart from this row
# alone, so it surfaces the anomaly and leaves the judgment to the operator
# reading it, rather than guessing either way.
_ATTEMPTS_BY_STATE = """
SELECT attempt_id, run_id, logical_job_id, lifecycle_state, scheduler_job_id,
       rapid_outcome, product_disposition, error_category,
       submitted_at, started_at, ended_at,
       (rapid_outcome = 'success' AND product_disposition = 'none')
         AS success_with_no_product
  FROM attempts
 WHERE lifecycle_state = %s
   AND COALESCE(ended_at, started_at, submitted_at) < now() - %s::interval
 ORDER BY COALESCE(ended_at, started_at, submitted_at)
"""

# `show-attempt`'s joined detail. One attempt row, LEFT JOINed to its work
# unit and its submission — LEFT, because neither FK is guaranteed populated
# (see `consumer.py`'s `_COLUMNS` comment: `work_unit_id` is NULL on every
# pre-intent-layer row, and a submission row exists only where DRAFT 044 was
# applied at submission time) and a detail view that inner-joined either one
# away would silently hide the exact rows an operator is most likely to be
# chasing down. `attempt_stages` and `registration_outcome` are NOT joined
# here — stages are one-to-many and the outcome is already a jsonb column on
# the attempts row — so they are read separately by `attempt_detail` below,
# the same split `pipeline.reconciler.closure.read_attempt_stages` already
# makes for the identical reason (a many-rows join would duplicate every
# scalar column once per stage).
#
# `submissions` joins on `run_id` alone — `submissions.run_id` is written as
# `str(batch.manifest.batch_id)` (`pipeline.seams._open_submission`) and
# `batch.manifest.batch_id` IS `run_id` (`submit_units`: "Manifest(...,
# batch_id=run_id, ...)"), so a submission's run_id and an attempt's run_id
# are the same string by construction; there is no job_name column on
# `attempts` to join through instead. One `run_id` can in principle carry
# more than one `submissions` row (multiple job types gathered under one
# run), so this takes the most recently created — the submission an
# operator asking about a specific attempt almost always means — rather
# than silently duplicating the attempt row per submission.
_ATTEMPT_CORE = """
SELECT a.attempt_id, a.run_id, a.logical_job_id, a.lifecycle_state,
       a.scheduler_job_id, a.exposure_id, a.sca, a.sky_tile,
       a.submitted_at, a.started_at, a.ended_at,
       a.rapid_outcome, a.product_disposition,
       a.application_intended_exit, a.scheduler_state, a.error_category,
       a.terminal_record_key, a.terminal_record_sequence,
       a.terminal_record_checksum,
       a.registered_at, a.registered_record_sequence,
       a.registration_outcome,
       a.work_unit_id, a.binding_job_definition_arn,
       a.binding_job_definition_rev, a.binding_image_digest,
       a.binding_release_identity, a.binding_manifest_checksum,
       (a.rapid_outcome = 'success' AND a.product_disposition = 'none')
         AS success_with_no_product,
       w.job_type AS work_unit_job_type, w.input_scope AS work_unit_input_scope,
       w.state AS work_unit_state, w.blocked_reason AS work_unit_blocked_reason,
       w.operational_class AS work_unit_operational_class,
       s.submission_id, s.job_name AS submission_job_name,
       s.job_queue AS submission_job_queue, s.state AS submission_state,
       s.array_size AS submission_array_size,
       s.manifest_uri AS submission_manifest_uri
  FROM attempts a
  LEFT JOIN work_units w ON w.work_unit_id = a.work_unit_id
  LEFT JOIN LATERAL (
         SELECT * FROM submissions
          WHERE submissions.run_id = a.run_id
          ORDER BY submissions.created_at DESC
          LIMIT 1
       ) s ON true
 WHERE a.attempt_id = %s
"""

_ATTEMPT_STAGES = """
SELECT stage_name, outcome, started_at, duration_ms
  FROM attempt_stages WHERE attempt_id = %s
 ORDER BY started_at, stage_name
"""

# `rapidctl work-units`'s population — the panel this package never had.
# Before this, the ONLY `work_units` reference anywhere in `operatorctl` was
# the LEFT JOIN inside `_ATTEMPT_CORE` above, reachable only per-attempt via
# `show-attempt`; there was no way to list work units by state at all, no way
# to see a stuck unit's `blocked_reason` without already knowing which
# attempt to ask about (and a blocked unit may have no attempt yet — that is
# exactly the case an operator most needs this for), and nothing anywhere
# read `unit_events`. This is "what is stuck and why" in one query: state,
# the job identity, why it is blocked if it is, how long it has sat there,
# and which campaign owns it.
#
# AGE IS MEASURED FROM THE UNIT'S OWN `updated_at`, not from a joined
# `unit_events` row — `work_units.updated_at` is stamped on every write that
# creates or transitions the row (`WorkUnitWriter.create_work_unit`/
# `transition_unit`, both in `pipeline/intent/writer.py`), so it is already
# the exact "how long has this row looked like this" fact without a second
# query or a LATERAL join per unit. `unit-events` (below) is the place to
# read the transition history itself.
#
# LEFT JOIN campaigns: `campaign_id` is nullable (a work unit created outside
# a campaign has none), and an INNER join would silently drop every
# non-campaign unit from a listing whose whole purpose is showing what is
# stuck — the same reason `_ATTEMPT_CORE` LEFT JOINs `work_units`/
# `submissions` rather than requiring them.
_WORK_UNITS_BASE = """
SELECT w.work_unit_id, w.job_type, w.input_scope, w.operational_class,
       w.state, w.blocked_reason, w.campaign_id, c.campaign_name,
       w.created_at, w.updated_at,
       extract(epoch FROM (now() - w.updated_at)) AS age_in_state_seconds
  FROM work_units w
  LEFT JOIN campaigns c ON c.campaign_id = w.campaign_id
"""

# Two shapes of the same query rather than one with an optional clause
# spliced in as a string: `--state` (repeatable) filters to an explicit,
# operator-named set via `= ANY(%s)`; `--non-terminal` filters to the
# three states that mean "still moving" (`blocked`, `ready`, `submitted`)
# via `NOT IN (...)` naming the four terminal ones explicitly — the same
# discipline `pipeline.gc.references.ELIGIBLE_OWNER_STATES`'s docstring
# uses ("the literal predicate, because `failed` and `quarantined` are
# called terminal elsewhere in this codebase") rather than a vaguer
# "not complete" that would silently admit a state nobody meant to include
# if the vocabulary grows. Composing the two as one parameterized WHERE
# would need to handle "neither given" (list everything) and "both given"
# (an operator asking two different questions at once) as extra cases;
# kept as two literal statements instead, chosen once in Python before any
# SQL runs, so each one's WHERE clause is exactly what it says and nothing
# is threaded together at the string level.
_WORK_UNITS_BY_STATE = _WORK_UNITS_BASE + \
    " WHERE w.state = ANY(%s)" \
    " ORDER BY w.updated_at ASC" \
    " LIMIT %s"

_WORK_UNITS_NON_TERMINAL = _WORK_UNITS_BASE + \
    " WHERE w.state NOT IN ('complete', 'failed', 'quarantined',"  \
    "                       'cancelled')" \
    " ORDER BY w.updated_at ASC" \
    " LIMIT %s"

# `rapidctl unit-events <work_unit_id>`'s population — the unit's own
# transition history, oldest first (the creation event has `from_state IS
# NULL`, migration 036: "from_state NULL on the unit's first event
# (creation)"), so reading top-to-bottom is reading the unit's life in
# order.
_UNIT_EVENTS = """
SELECT unit_event_id, from_state, to_state, writer, occurred_at, reason,
       detail
  FROM unit_events
 WHERE work_unit_id = %s
 ORDER BY unit_event_id
"""


def attempts_by_state(conn, state, older_than_seconds):
    """`rapidctl attempts --state ... --older-than ...`'s population.

    ``older_than_seconds`` is measured from whichever of ended_at,
    started_at or submitted_at is the latest the row has — the same
    "furthest fact the row actually carries" COALESCE `closure.py` uses
    for its own age reasoning, so an attempt that never started is aged
    from its submission and one that finished is aged from its end.
    """
    return _rows(conn, _ATTEMPTS_BY_STATE,
                (state, "%s seconds" % older_than_seconds))


def attempt_detail(conn, attempt_id):
    """`rapidctl show-attempt`'s joined view. Returns None if no such attempt.

    Three queries, not one: the core row (attempt + work unit + submission,
    all scalar), the stage list (one-to-many), and — read straight off the
    core row rather than a fourth query — `registration_outcome`, already a
    jsonb column. Matches the split `read_attempt_stages` already documents
    the reasoning for: a stages join would duplicate every scalar column
    once per stage row.
    """
    rows = _rows(conn, _ATTEMPT_CORE, (attempt_id,))
    if not rows:
        return None
    detail = rows[0]
    detail["stages"] = _rows(conn, _ATTEMPT_STAGES, (attempt_id,))
    return detail


def unreconciled_break_glass(conn):
    """Region 7's panel: open sessions lacking a close and a passing sweep."""
    return _rows(conn, _UNRECONCILED, ())


def work_units_by_state(conn, states, limit=200):
    """`rapidctl work-units --state ...`'s population, one or more states.

    ``states`` is a non-empty sequence of exact `work_units.state` values —
    the CLI's ``--state`` is repeatable and passes every named value here in
    one call, matched with ``= ANY(%s)`` rather than one query per state.
    """
    if not states:
        raise ValueError("work_units_by_state needs at least one state; "
                         "an empty list would be `= ANY('{}')`, which "
                         "matches nothing and is never what an operator "
                         "typing --state meant")
    return _rows(conn, _WORK_UNITS_BY_STATE, (list(states), limit))


def work_units_non_terminal(conn, limit=200):
    """`rapidctl work-units --non-terminal`'s population: blocked/ready/
    submitted — everything still actively moving through the machine.
    """
    return _rows(conn, _WORK_UNITS_NON_TERMINAL, (limit,))


def unit_events_for_work_unit(conn, work_unit_id):
    """`rapidctl unit-events <work_unit_id>`'s population: the unit's own
    transition history, oldest first. Returns `[]` for an unknown
    `work_unit_id` — there is no separate existence check, matching
    `attempts_by_state`'s own convention of returning what the query finds
    rather than probing for the row first.
    """
    return _rows(conn, _UNIT_EVENTS, (work_unit_id,))


def recent_mutations(conn, limit=20, with_draft_columns=None):
    """The tail of the audit history, newest first."""
    if with_draft_columns is None:
        with_draft_columns = _has_idempotency_column(conn)
    sql = _RECENT_AUDIT if with_draft_columns else _RECENT_AUDIT_BASE
    return _rows(conn, sql, (limit,))


def _has_idempotency_column(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema='derived' AND table_name='mutation_audit' "
            "AND column_name='idempotency_key')")
        return bool(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
def _json(value):
    """Adapt a dict to the jsonb parameter, preserving a genuine NULL.

    ``None`` must reach the function as SQL NULL — meaning "the caller
    made no expected-state claim" — and not as the JSON string "null",
    which would be a claim about nothing.
    """
    if value is None:
        return None
    import json                                   # noqa: PLC0415
    return json.dumps(value)


def _scalar(conn, sql, params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    conn.commit()
    return row[0] if row else None


def _rows(conn, sql, params):
    """Return rows as dicts, so callers name columns rather than index them."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, r)) for r in cur.fetchall()]
