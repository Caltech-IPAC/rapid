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


def create_run(conn, idempotency_key, name, owner, kind, purpose=None,
              branch=None, image_digest=None, config_hash=None,
              input_generations=None, reason=None, expected_state=None,
              dry_run=True, policy_citation=None):
    """Record a run and its provenance (migration 109: ``derived.create_run``).

    ``expected_state`` is ``{"already_present": false}`` for the ordinary
    case of declaring a run believed new — the same shape
    ``add_problem_category`` uses for the same reason. The idempotency key
    is FIRST, matching every DRAFT-047-shaped function; ``derived.create_run``
    has no unkeyed overload to fall back to (109's header: "these are new
    signatures with no pre-existing caller to stay compatible with").
    """
    return call_function(
        conn,
        # `input_generations` carries an explicit cast for the same reason
        # `expected_state` does: it is the one parameter whose type is not
        # inferable from a bare Python value, and psycopg2 binds None as an
        # untyped NULL. Resolution happens to succeed without it today —
        # measured, not assumed — but this is the only array parameter in the
        # package and stating its type costs nothing, where discovering that
        # it matters would cost a failed operator command.
        "SELECT derived.create_run(%s, %s, %s, %s, %s, %s, %s, %s, "
        "                          %s::text[], %s, %s::jsonb, %s, %s)",
        (idempotency_key, name, owner, kind, purpose, branch, image_digest,
         config_hash, input_generations, reason, _json(expected_state),
         dry_run, policy_citation))


def archive_run(conn, idempotency_key, name, reason, expected_state=None,
                dry_run=True, policy_citation=None):
    """Archive a run and demote its campaign products (``derived.archive_run``).

    ``expected_state`` is ``{"state": "..."}`` — the run's state as the dry
    run showed it; the apply refuses if the run moved states since (RA001),
    the same "the world hasn't moved" contract every other expected-state
    check in this module enforces.
    """
    return call_function(
        conn,
        "SELECT derived.archive_run(%s, %s, %s, %s::jsonb, %s, %s)",
        (idempotency_key, name, reason, _json(expected_state), dry_run,
         policy_citation))


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


# ---------------------------------------------------------------------------
# `run status` / `run compare` (108/109): every reader here matches a run's
# attempts by PREFIX, `run_id LIKE %s` with the wildcard appended to the
# PARAMETER (never spliced into the SQL text as a literal `%`, which would
# collide with psycopg2's own `%s`-substitution) — NEVER by equality. The
# ninth defect of the 8/21 rerun (108's own COMMENT ON TABLE) was matching
# by equality, which misses every split-pass batch's `<name>-<n>` suffix
# (`pipeline.seams.submit_gathered`: "each batch gets its own run-scoped
# identity `<run_id>-<n>` where there is more than one"). `runs.name` is
# CHECKed free of LIKE metacharacters (108: `runs_name_shape_ck`), so
# `_run_prefix_pattern` below needs no escaping the way `consumer.
# _escape_like` needs it for an arbitrary caller-supplied prefix.
# ---------------------------------------------------------------------------

# THE FAILURE PREDICATE, matching `d9-post.sh`'s corrected gate exactly (the
# gate the ramp chain settled on after an EARLIER version used
# `rapid_outcome <> 'success'` alone and reported a clean pass over 218
# dead letters that were parked, not attempted at all — see
# `d8_release_blocked.py`'s header). Two disjuncts, not one:
#
#   * a TERMINAL attempt whose outcome is not success — `IS DISTINCT FROM`,
#     not `<>`, because `<>` against a NULL `rapid_outcome` evaluates to
#     NULL (neither true nor false) and silently excludes the row from
#     both sides of the comparison. `IS DISTINCT FROM` treats NULL as its
#     own comparable value, which is exactly the sci-c shape: 218 attempts
#     dead-lettered with `started_at IS NULL` and `rapid_outcome` never set
#     at all.
#   * a `missing_or_contradictory` row — the reconciler's own dead-letter
#     verdict, which is not itself a `lifecycle_state LIKE 'terminal%'` value
#     (see `_dead_lettered_pairs` in `pipeline.test.live_w9_ramp`) and so is
#     not caught by the first disjunct at all; it needs naming explicitly or
#     it is invisible to a status tally built only from the first clause.
_RUN_FAILURE_PREDICATE = (
    "((lifecycle_state LIKE 'terminal%' AND rapid_outcome IS DISTINCT FROM "
    "'success') OR lifecycle_state = 'missing_or_contradictory')")

# `run status`'s tally: total attempts, failures (the predicate above), and
# a lifecycle_state breakdown, all in one query over the prefix-matched
# population — one round trip rather than one query per number, matching
# `_ATTEMPTS_BY_STATE`'s own "coarse panel, one query" style.
_RUN_ATTEMPT_TALLY = (
    "SELECT count(*) AS total,"
    "       count(*) FILTER (WHERE " + _RUN_FAILURE_PREDICATE + ") AS failures"
    "  FROM attempts"
    " WHERE run_id LIKE %s"
)

_RUN_STATE_BREAKDOWN = """
SELECT lifecycle_state, count(*)
  FROM attempts
 WHERE run_id LIKE %s
 GROUP BY lifecycle_state
 ORDER BY lifecycle_state
"""

# Walltime distribution per stage — min/median/p90/max over `duration_ms`,
# using PostgreSQL's own `percentile_cont` (a standard aggregate, not
# anything this schema defines) rather than hand-rolled percentile
# arithmetic in Python, which is the kind of thing `catalog.md` principle 4
# already argues against doing in application code when the database can
# state it as a structural fact about the rows. Joins to `attempts` (not a
# bare `attempt_stages` scan) so the prefix match is the same one every
# other run reader uses.
_RUN_STAGE_WALLTIME = """
SELECT s.stage_name,
       count(*) AS n,
       min(s.duration_ms) AS min_ms,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY s.duration_ms) AS p50_ms,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY s.duration_ms) AS p90_ms,
       max(s.duration_ms) AS max_ms
  FROM attempt_stages s
  JOIN attempts a ON a.attempt_id = s.attempt_id
 WHERE a.run_id LIKE %s
   AND s.duration_ms IS NOT NULL
 GROUP BY s.stage_name
 ORDER BY s.stage_name
"""

# `run compare`'s product-count half — one row per product table, matching
# 108's own denormalized `run_id` columns (`refimages`, `diffimages`,
# `psfs`; `l2files` is deliberately absent, per 108's header: admission is
# shared by every run and stays out of run-scoped accounting entirely).
_RUN_PRODUCT_COUNTS = """
SELECT 'refimages' AS product, count(*) AS n
  FROM refimages WHERE run_id LIKE %s
UNION ALL
SELECT 'diffimages', count(*) FROM diffimages WHERE run_id LIKE %s
UNION ALL
SELECT 'psfs', count(*) FROM psfs WHERE run_id LIKE %s
"""

_RUN_ROW = """
SELECT run_id, name, owner, kind, purpose, branch, image_digest,
       config_hash, input_generations, state, created_at, started_at,
       completed_at, archived_at
  FROM runs WHERE name = %s
"""


def run_row(conn, name):
    """The `runs` row for `name`, or None if no run has been declared by it.

    Note this is an exact match on `runs.name` (the registry key), NOT a
    prefix match — the prefix match is only ever against `attempts.run_id`
    and the other product tables' denormalized `run_id` columns, never
    against the registry itself, which has exactly one row per declared
    name (108: `runs_name_uq`).
    """
    rows = _rows(conn, _RUN_ROW, (name,))
    return rows[0] if rows else None


def _run_prefix_pattern(name):
    """`name` turned into the LIKE pattern every run reader matches
    `attempts.run_id` (and the product tables' own `run_id`) against.

    The wildcard is appended to the PARAMETER, not spliced into the SQL
    text — the same convention `pipeline.registration.consumer.candidates`
    already uses for `run_id_prefix` — so there is exactly one place a `%`
    character enters a query, and it is never adjacent to a `%s`
    placeholder in the SQL string itself. `runs.name` is CHECK-guaranteed
    free of `%`/`_`/`\\` (108: `runs_name_shape_ck`), so no escaping is
    needed here the way `consumer._escape_like` needs it for an arbitrary
    caller-supplied `run_id_prefix`.
    """
    return name + "%"


def run_attempt_tally(conn, name):
    """`{"total": n, "failures": n}` for every attempt whose run_id matches
    `name` by prefix. See `_RUN_FAILURE_PREDICATE` for exactly what counts
    as a failure and why it is two disjuncts, not one.
    """
    rows = _rows(conn, _RUN_ATTEMPT_TALLY, (_run_prefix_pattern(name),))
    return rows[0] if rows else {"total": 0, "failures": 0}


def run_state_breakdown(conn, name):
    """`lifecycle_state -> count` for every attempt matching `name` by prefix."""
    return _rows(conn, _RUN_STATE_BREAKDOWN, (_run_prefix_pattern(name),))


def run_stage_walltime(conn, name):
    """Per-stage walltime distribution (min/p50/p90/max, ms) for a run."""
    return _rows(conn, _RUN_STAGE_WALLTIME, (_run_prefix_pattern(name),))


def run_product_counts(conn, name):
    """`{"refimages": n, "diffimages": n, "psfs": n}` for a run."""
    pattern = _run_prefix_pattern(name)
    rows = _rows(conn, _RUN_PRODUCT_COUNTS, (pattern, pattern, pattern))
    return {row["product"]: row["n"] for row in rows}


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
