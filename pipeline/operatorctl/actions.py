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
              dry_run=True, policy_citation=None,
              lane=None, retry_attempts=None, retry_wallclock_s=None,
              attempt_timeout_s=None, reference_set_id=None):
    """Record a run and its provenance (migration 109: ``derived.create_run``).

    ``expected_state`` is ``{"already_present": false}`` for the ordinary
    case of declaring a run believed new — the same shape
    ``add_problem_category`` uses for the same reason. The idempotency key
    is FIRST, matching every DRAFT-047-shaped function; ``derived.create_run``
    has no unkeyed overload to fall back to (109's header: "these are new
    signatures with no pre-existing caller to stay compatible with").

    THE EXECUTION ENVELOPE (migration 122). ``lane``, ``retry_attempts``,
    ``retry_wallclock_s`` and ``attempt_timeout_s`` are the run's execution
    envelope — what lane its submissions take, how many times a unit may be
    retried for a transient failure, how long that unit may keep trying, and
    how long one attempt may run. They go LAST so every existing positional
    call site keeps working.

    **Each defaults to None, not to the value.** The defaults belong to the
    database function, which derives the wall-clock from the lane and is also
    the boundary a caller reaching psql directly must cross. Restating them
    here would give the same fact two homes that could drift; passing None
    means "use the function's default", and the CLI prints what that will be
    rather than deciding it.

    THE REFERENCE SET (migration 126). ``reference_set_id`` is which set of
    references this run differences against. Passed as an id rather than a
    name because the CLI resolves the name — or creates the set — before
    calling, so what reaches the database is unambiguous. None means the
    default set, resolved by ``derived.create_run`` and STORED: the stored
    value is never "whatever is default later", which is the behaviour
    migration 126 exists to remove.
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
        #
        # The four envelope parameters carry explicit casts for a sharper
        # version of the same reason: all four are optional and a caller
        # taking the defaults binds four untyped NULLs at once, which is
        # exactly the shape that produced "function create_run(unknown,
        # unknown, ...) does not exist" for `resolve_attempt`
        # (`observability/attempts.py`'s own comment records that incident).
        #
        # THE FOUR ENVELOPE ARGUMENTS ARE PASSED BY NAME, and that is not a
        # style choice. `derived.create_run` carries `p_dispatcher` at
        # position 14, between `p_policy_citation` and the envelope 122
        # appended at 15-18. A positional call listing thirteen arguments and
        # then four more would put `lane` into `p_dispatcher` — a text
        # parameter, so PostgreSQL would accept it silently, write "bulk" as
        # the audit row's dispatcher, and leave the envelope at its defaults
        # with no error anywhere. Naming them makes that unrepresentable.
        "SELECT derived.create_run(%s, %s, %s, %s, %s, %s, %s, %s, "
        "                          %s::text[], %s, %s::jsonb, %s, %s, "
        "                          p_lane => %s::text, "
        "                          p_retry_attempts => %s::integer, "
        "                          p_retry_wallclock_s => %s::integer, "
        "                          p_attempt_timeout_s => %s::integer, "
        "                          p_reference_set_id => %s::bigint)",
        (idempotency_key, name, owner, kind, purpose, branch, image_digest,
         config_hash, input_generations, reason, _json(expected_state),
         dry_run, policy_citation,
         lane, retry_attempts, retry_wallclock_s, attempt_timeout_s,
         reference_set_id))


def create_reference_set(conn, idempotency_key, name, owner, reason,
                         purpose=None, coadder=None, coadder_version=None,
                         frame_rule=None, min_frames=None,
                         epoch_start=None, epoch_end=None, psf_set=None,
                         image_digest=None, built_by_run=None,
                         expected_state=None, dry_run=True,
                         policy_citation=None):
    """Create a named reference set (migration 127:
    ``derived.create_reference_set``).

    ``psf_set`` and ``built_by_run`` are NAMES, resolved inside the function
    — an operator names a set and a run, never an id, and resolving in SQL
    keeps the check and the insert in one transaction rather than leaving a
    window between them.

    A set is NEVER created as the default. Moving the default is
    ``set_default_reference_set``, a separate and separately audited act,
    because a new set silently becoming default at creation is the
    supersede-on-arrival behaviour migration 126 exists to remove.

    Every argument after ``reason`` is passed BY NAME for the reason
    ``create_run``'s envelope arguments are: this function takes eighteen
    parameters, eleven of them optional and most of them text, so a
    positional call that dropped one would slide the rest along silently and
    PostgreSQL would accept it.
    """
    return call_function(
        conn,
        "SELECT derived.create_reference_set("
        "    %s, %s, %s, %s, %s, "
        "    p_coadder => %s::text, "
        "    p_coadder_version => %s::text, "
        "    p_frame_rule => %s::text, "
        "    p_min_frames => %s::integer, "
        "    p_epoch_start => %s::timestamptz, "
        "    p_epoch_end => %s::timestamptz, "
        "    p_psf_set => %s::text, "
        "    p_image_digest => %s::text, "
        "    p_built_by_run => %s::text, "
        "    p_expected_state => %s::jsonb, "
        "    p_dry_run => %s, "
        "    p_policy_citation => %s::text)",
        (idempotency_key, name, owner, purpose, reason,
         coadder, coadder_version, frame_rule, min_frames,
         epoch_start, epoch_end, psf_set, image_digest, built_by_run,
         _json(expected_state), dry_run, policy_citation))


def set_default_reference_set(conn, idempotency_key, name, reason,
                              expected_state=None, dry_run=True,
                              policy_citation=None):
    """Move production's default reference set (migration 127:
    ``derived.set_default_reference_set``).

    ``expected_state`` is ``{"current_default": "..."}`` — which set the dry
    run showed as default; the apply refuses if it moved since (RA001). That
    matters more here than for most: this is the one command that changes
    what every LATER run with no declaration of its own will read, so an
    operator acting on a stale reading would redirect work they never saw.

    Runs ALREADY created keep the set they stored. That is the point of
    storing it at creation, and is why this command touches no run.
    """
    return call_function(
        conn,
        "SELECT derived.set_default_reference_set(%s, %s, %s, %s::jsonb, "
        "                                         %s, %s)",
        (idempotency_key, name, reason, _json(expected_state), dry_run,
         policy_citation))


def archive_reference_set(conn, idempotency_key, name, reason,
                          expected_state=None, dry_run=True,
                          policy_citation=None):
    """Archive a reference set (migration 127:
    ``derived.archive_reference_set``).

    DELIBERATELY NOT ``archive_run``'S SEMANTIC. Archiving a run demotes its
    scratch products; archiving a SET demotes nothing at all, because a run
    already declared on it must keep reading exactly the references it has
    been reading. Archiving says only "do not choose this set for new work".

    The function refuses to archive the default set and reports how many runs
    still declare the one being archived — reported, not refused, since a set
    runs still declare is an ordinary thing to archive.
    """
    return call_function(
        conn,
        "SELECT derived.archive_reference_set(%s, %s, %s, %s::jsonb, %s, %s)",
        (idempotency_key, name, reason, _json(expected_state), dry_run,
         policy_citation))


def archive_run(conn, idempotency_key, name, reason, expected_state=None,
                dry_run=True, policy_citation=None):
    """Archive a run and demote its scratch products (``derived.archive_run``).

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


def delete_run(conn, idempotency_key, name, reason, objects, max_items=None,
              expected_state=None, dry_run=True, policy_citation=None):
    """Delete a scratch run's own scratch-prefix objects
    (``derived.delete_run``, migration 131).

    ``objects`` is the candidate object-version list the CLI enumerated
    from S3 (S3 is not visible to Postgres): a list of
    ``{"bucket", "key", "version_id", "size", "modified"}`` dicts, passed
    through as the ``p_objects`` jsonb array. ``max_items`` bounds how many
    PENDING items this call advances to a terminal outcome, leaving the
    rest ``pending`` on the open plan for a resumed call against the SAME
    ``name`` (found by the run's own key, not by idempotency key, since a
    resumed call necessarily carries a fresh key each time).

    Refuses a non-scratch run, refuses a caller who is neither the run's
    owner nor a ``rapid_admin`` member, and refuses -- naming the
    dependent run(s) -- while the run's own objects are still bound as
    another run's current product version, still depended on by another
    run's superseding work units, or while the run's own attempts are
    still in flight.
    """
    return call_function(
        conn,
        "SELECT derived.delete_run(%s, %s, %s, %s::jsonb, %s::jsonb, %s, "
        "                          %s, %s)",
        (idempotency_key, name, reason, _json(objects),
         _json(expected_state), max_items, dry_run, policy_citation))


def start_run(conn, idempotency_key, name, reason, expected_state=None,
              dry_run=True, policy_citation=None):
    """Move a declared run to ``running`` (migration 121:
    ``derived.start_run``).

    Idempotent from ``running``, which is what makes a ramp step legal: a
    second ``run start`` on a run already under way is the SAME run
    continuing, and stamps no new ``started_at`` — the first start is the
    one the row records. Refuses ``complete``/``archived`` (RA012) and an
    undeclared name (RA010).

    ``expected_state`` is ``{"state": "..."}``, the same "the world hasn't
    moved" shape ``archive_run`` beside it uses.
    """
    return call_function(
        conn,
        "SELECT derived.start_run(%s, %s, %s, %s::jsonb, %s, %s)",
        (idempotency_key, name, reason, _json(expected_state), dry_run,
         policy_citation))


def complete_run(conn, idempotency_key, name, reason, expected_state=None,
                 dry_run=True, policy_citation=None):
    """Move a ``running`` run to ``complete`` (``derived.complete_run``).

    Refuses (RA013) while any attempt of the run is in an open lifecycle
    state — counted by ``run_key`` where the run has keyed attempts and by
    the name prefix otherwise, so rows predating 121 and rows the deployed
    reconciler creates until the next repin are still seen by the gate.
    Refuses any other prior state (RA012).
    """
    return call_function(
        conn,
        "SELECT derived.complete_run(%s, %s, %s, %s::jsonb, %s, %s)",
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
#
# The `terminal%` LIKE pattern is a literal percent, doubled to `terminal%%`
# below. This constant is only ever spliced into `_RUN_ATTEMPT_TALLY`, which
# IS executed with a parameter (`run_id LIKE %s`) -- psycopg2 scans the
# whole query string for `%`-placeholders whenever ANY parameters are
# supplied, so an unescaped literal `%` is parsed as the start of one. A
# single positional parameter then meets a query psycopg2 thinks needs two,
# raising `IndexError: tuple index out of range` from inside `cur.execute`
# -- not a SQL error, so it looks nothing like a query bug at the call site.
# `test_run.py` unescapes this back to a single `%` before handing it to
# SQLite, which has no `%`-placeholder convention at all and would
# otherwise treat a doubled `%%` as two literal percent characters.
_RUN_FAILURE_PREDICATE = (
    "((lifecycle_state LIKE 'terminal%%' AND rapid_outcome IS DISTINCT FROM "
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

# The SAME tally over the SAME predicate, read by the registry key instead
# of the name prefix (migration 121). Two constants rather than one with a
# swapped WHERE clause, so the failure predicate is written once and the
# two readings can never drift into asking different questions of the two
# populations. The key is resolved from the name by subquery rather than
# taken as a bigint parameter, so every reader in this module keeps the
# same one-argument (name) call shape.
_RUN_ATTEMPT_TALLY_BY_KEY = (
    "SELECT count(*) AS total,"
    "       count(*) FILTER (WHERE " + _RUN_FAILURE_PREDICATE + ") AS failures"
    "  FROM attempts"
    " WHERE run_key = (SELECT run_id FROM runs WHERE name = %s)"
)

#: How many attempts carry this run's key at all — what decides which of
#: the two readings above `run status` uses.
_RUN_KEYED_ATTEMPT_COUNT = (
    "SELECT count(*) AS n FROM attempts"
    " WHERE run_key = (SELECT run_id FROM runs WHERE name = %s)"
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

# `run compare`'s BUILD PROVENANCE half — what actually ran, as opposed to
# `_RUN_ROW`'s `branch`/`image_digest`/`config_hash`, which are the
# registry's DECLARED intent for the run rather than a record of what any
# attempt actually executed. The two can disagree (a job definition
# repinned mid-run, a retry on a rebuilt image) and that disagreement is
# exactly the thing an operator comparing two runs needs surfaced, not
# hidden behind one declared-intent value.
#
# GROUPed on all four columns together, not counted per `container_digest`
# alone: two attempts can share an image but disagree on `config_digest`
# (a `--set` retry) or `source_sha` (a hotfix rebuild under the same image
# tag), and collapsing those into one row would erase exactly the split a
# reader needs to see. `count(*)` on each combination is what makes "every
# attempt of this run shares one container_digest" a single, countable row
# rather than something the operator has to tally by eye across many
# attempt lines. NULLs (an attempt that predates these columns, or one
# that never reached `mark_started`) group together under NULL like any
# other value here — `count(*) FILTER` is not needed, since a row with
# all-NULL provenance is itself the fact worth showing, not one to hide.
#
# Ordered by `n` descending so the dominant combination — the one the
# comparison is actually about — sorts first; a tied secondary order on
# the columns themselves keeps repeated runs of this query byte-identical
# for the same data, which is what lets a test assert against the output.
_RUN_BUILD_PROVENANCE = """
SELECT source_sha, container_digest, config_digest, config_snapshot_key,
       count(*) AS n
  FROM attempts
 WHERE run_id LIKE %s
 GROUP BY source_sha, container_digest, config_digest, config_snapshot_key
 ORDER BY n DESC, container_digest, config_digest, source_sha
"""

# The DISTINCT `container_digest` set alone, separate from the full
# four-column breakdown above, because the single fact migration 121's
# acceptance check reads — `count(distinct container_digest) = 1` across a
# run — deserves to be answerable by eye from one line rather than summed
# across however many `_RUN_BUILD_PROVENANCE` rows happen to share a
# digest. `NULL` counts as one distinct value here, same as Postgres's own
# `count(distinct ...)`, deliberately: a run whose attempts never recorded
# a digest is a fact about that run, not a reason to hide the count.
_RUN_CONTAINER_DIGEST_COUNT = """
SELECT count(DISTINCT container_digest) AS n_distinct, count(*) AS n_attempts
  FROM attempts
 WHERE run_id LIKE %s
"""

# The run's declared scratch overlay (migration 112's `runs.config_overlay`,
# jsonb) — read here rather than folded into `_RUN_ROW`, for the same
# reason `run.py`'s own `config_overlay` read stays a separate one-column
# query (see that file's comment on this exact column): `_RUN_ROW` is read
# by every lightweight run subcommand (`status`, `compare`), and a
# production run's row should not pay for selecting a column that is
# always NULL for it. `run compare` is exactly the caller who wants it, so
# it asks directly instead of asking `_RUN_ROW` to carry it for everyone.
_RUN_CONFIG_OVERLAY = "SELECT config_overlay FROM runs WHERE run_id = %s"

# The reference product key each of a run's difference images cites, and
# the input identity — `(exposure_id, sca)` — of the attempt that produced
# each one. Joins `diffimages` (this run's product rows, by the same
# `run_id LIKE` prefix every other product reader uses) to `refimages` on
# the legacy `rfid` FK the two tables have always shared (`pipeline.
# repositories.diffimages._OVERLAP_SQL` joins the identical pair the same
# way), then to `products` through `refimages.product_id` for the key
# itself — the same path `pipeline.repositories.products._REFERENCE_KEY_
# SQL` reads a single reference's key by, generalized here to every
# reference a run's difference images cite rather than one `rfid` at a
# time. `attempts` joins on `diffimages.run_id`/`attempt_id`... but
# `diffimages` denormalizes `run_id` rather than carrying `attempt_id`
# (108's product tables are attempt-agnostic by design), so the input
# identity comes from `attempts` by the SAME `run_id LIKE` prefix, not by
# joining the two tables to each other; a run's distinct `(exposure_id,
# sca)` pairs are what the input side of a comparison means; which
# attempt produced which diffimage is not a claim this query makes.
#
# `r.product_id` may be NULL (a reference registered before its product
# row was linked — see `link_reference_image`'s own "binds without being"
# comment), so the join to `products` is LEFT: a diffimage whose reference
# has no product row yet still appears, with `product_key` NULL, rather
# than being silently dropped from the count.
_RUN_REFERENCE_PRODUCT_KEYS = """
SELECT DISTINCT p.product_key
  FROM diffimages d
  JOIN refimages r ON r.rfid = d.rfid
  LEFT JOIN products p ON p.product_id = r.product_id
 WHERE d.run_id LIKE %s
 ORDER BY p.product_key
"""

_RUN_INPUT_IDENTITIES = """
SELECT DISTINCT exposure_id, sca
  FROM attempts
 WHERE run_id LIKE %s
 ORDER BY exposure_id, sca
"""

_RUN_ROW = """
SELECT r.run_id, r.name, r.owner, r.kind, r.purpose, r.branch,
       r.image_digest, r.config_hash, r.input_generations, r.state,
       r.created_at, r.started_at, r.completed_at, r.archived_at,
       r.lane, r.retry_attempts, r.retry_wallclock_s, r.attempt_timeout_s,
       r.reference_set_id, s.name AS reference_set, s.psf_set_id
  FROM runs r
  LEFT JOIN reference_sets s ON s.reference_set_id = r.reference_set_id
 WHERE r.name = %s
"""
# THE JOIN IS A LEFT JOIN, and every set column is read with `.get`
# downstream. An INNER JOIN would return NO ROW for a run whose
# `reference_set_id` is NULL, turning "this run has no set recorded" into
# "this run does not exist" — and `_bind_registry_row` refuses an absent run
# by telling the operator to declare it, which would be exactly the wrong
# advice. A NULL id is possible on any database where 126 ran but 127's
# `SET NOT NULL` has not (the applier commits per file), and on a run row
# written between the two.
#
# `reference_sets` itself is assumed to EXIST, unlike the column, because
# `pipeline/intent/schema_contract.py` carries 126 under REQUIRED_MIGRATIONS:
# operatorctl runs from the checkout against a database at the checkout's own
# floor, so a database without the table is one this command already refuses
# to act on for reasons that have nothing to do with sets.


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


_REFERENCE_SET_ROWS = """
SELECT s.reference_set_id, s.name, s.owner, s.purpose, s.coadder,
       s.coadder_version, s.frame_rule, s.min_frames,
       s.epoch_start, s.epoch_end, s.psf_set_id, s.image_digest,
       s.state, s.is_default, s.created_at, s.archived_at,
       b.name AS built_by_run_name,
       (SELECT count(*) FROM runs r
         WHERE r.reference_set_id = s.reference_set_id) AS runs_declaring_it,
       (SELECT count(*) FROM refimages f
         WHERE f.reference_set_id = s.reference_set_id) AS refimages_rows
  FROM reference_sets s
  LEFT JOIN runs b ON b.run_id = s.built_by_run
 WHERE (%s OR s.state = 'current')
 ORDER BY s.is_default DESC, s.name
"""


def reference_set_rows(conn, include_archived=False):
    """Every reference set, with what depends on it (migration 126).

    The two counts are subqueries rather than joins because a set with no
    runs and no references must still appear — that is exactly the state a
    freshly created set is in, and a join would hide the set an operator
    just made. `built_by_run` is LEFT JOINed for the same reason: most sets
    have none.

    Archived sets are hidden by default. An archived set is one nobody
    should CHOOSE, and a list whose common use is "which set do I declare"
    reads better without them; `--all` shows the history.
    """
    return _rows(conn, _REFERENCE_SET_ROWS, (bool(include_archived),))


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


def run_keyed_attempt_count(conn, name):
    """How many attempts carry this run's REGISTRY KEY (migration 121).

    Zero means one of two things, and the caller treats them alike: the
    run predates 121 entirely, or it is a 121-era run none of whose
    attempts have been written yet. Either way there is no keyed
    population to count and the prefix is the only reading available.
    """
    rows = _rows(conn, _RUN_KEYED_ATTEMPT_COUNT, (name,))
    return rows[0]["n"] if rows else 0


def run_attempt_tally(conn, name, by_key=None):
    """`{"total": n, "failures": n}` for a run's attempts, plus `counted_by`.

    **TWO READINGS, AND THE ANSWER SAYS WHICH IT USED (migration 121).**
    Before 121 a run's attempts could only be found by matching `name` as
    a LIKE PREFIX against `attempts.run_id`; since 121 the submitter also
    writes `attempts.run_key`, the registry row's own id. The two agree for
    a run whose rows were all written by the submitter since 121, and
    disagree for a run that has any of: rows written before 121, or rows
    the DEPLOYED image's reconciler created on retry (that image predates
    this column and this brief does not repin it, so those rows carry no
    key until the next repin).

    The key is preferred when the run has ANY keyed attempt, because a key
    is a reference and a prefix is a string convention — but the prefix
    reading is still computed and returned beside it, so `run status` can
    print both when they differ rather than silently showing the smaller
    number. See `_RUN_FAILURE_PREDICATE` for what counts as a failure.

    `by_key` forces a reading rather than detecting one; `None` (the
    default, and what every caller uses) detects.
    """
    prefix = _rows(conn, _RUN_ATTEMPT_TALLY, (_run_prefix_pattern(name),))
    prefix_tally = prefix[0] if prefix else {"total": 0, "failures": 0}

    if by_key is None:
        by_key = run_keyed_attempt_count(conn, name) > 0
    if not by_key:
        return {"total": prefix_tally["total"],
                "failures": prefix_tally["failures"],
                "counted_by": "name_prefix",
                "prefix_total": prefix_tally["total"],
                "prefix_failures": prefix_tally["failures"]}

    keyed = _rows(conn, _RUN_ATTEMPT_TALLY_BY_KEY, (name,))
    keyed_tally = keyed[0] if keyed else {"total": 0, "failures": 0}
    return {"total": keyed_tally["total"],
            "failures": keyed_tally["failures"],
            "counted_by": "run_key",
            "prefix_total": prefix_tally["total"],
            "prefix_failures": prefix_tally["failures"]}


def run_state_breakdown(conn, name):
    """`lifecycle_state -> count` for every attempt matching `name` by prefix."""
    return _rows(conn, _RUN_STATE_BREAKDOWN, (_run_prefix_pattern(name),))


def run_stage_walltime(conn, name):
    """Per-stage walltime distribution (min/p50/p90/max, ms) for a run."""
    return _rows(conn, _RUN_STAGE_WALLTIME, (_run_prefix_pattern(name),))


# D7: per-job resource usage (peak RSS, CPU seconds), joining the walltime
# panel rather than growing a second one — same one-query-per-run-reader
# shape as `_RUN_STAGE_WALLTIME`, and the same min/p50/p90/max/n columns, so
# `run status` prints it as one more row beside the per-stage timings rather
# than a differently-shaped block. Not joined to `attempt_stages` (unlike
# walltime, which is a per-stage measurement): peak_rss_kb/cpu_seconds are
# per-ATTEMPT — the whole job process tree's rusage at terminal — so this
# reads `attempts` alone. `WHERE ... IS NOT NULL` on each column
# independently rather than on the row, since a rusage read failure
# (`capture_resource_usage`, best-effort) can leave one column populated
# and the other NULL on the same row (unlikely in practice — both come from
# the same rusage calls — but the two never gate each other in the writer,
# so the read here does not assume they always arrive together).
_RUN_RESOURCE_USAGE = """
SELECT 'peak_rss_kb' AS metric,
       count(*) FILTER (WHERE peak_rss_kb IS NOT NULL) AS n,
       min(peak_rss_kb) AS min_v,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY peak_rss_kb) AS p50_v,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY peak_rss_kb) AS p90_v,
       max(peak_rss_kb) AS max_v
  FROM attempts
 WHERE run_id LIKE %s
UNION ALL
SELECT 'cpu_seconds',
       count(*) FILTER (WHERE cpu_seconds IS NOT NULL),
       min(cpu_seconds),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY cpu_seconds),
       percentile_cont(0.9) WITHIN GROUP (ORDER BY cpu_seconds),
       max(cpu_seconds)
  FROM attempts
 WHERE run_id LIKE %s
"""


def run_resource_usage(conn, name):
    """Peak-RSS (KB) and CPU-seconds distribution (min/p50/p90/max, n) for
    every attempt matching `name` by prefix — the D7 measurement, read
    beside `run_stage_walltime` in the same panel.
    """
    pattern = _run_prefix_pattern(name)
    return _rows(conn, _RUN_RESOURCE_USAGE, (pattern, pattern))


def run_product_counts(conn, name):
    """`{"refimages": n, "diffimages": n, "psfs": n}` for a run."""
    pattern = _run_prefix_pattern(name)
    rows = _rows(conn, _RUN_PRODUCT_COUNTS, (pattern, pattern, pattern))
    return {row["product"]: row["n"] for row in rows}


def run_build_provenance(conn, name):
    """The distinct `(source_sha, container_digest, config_digest,
    config_snapshot_key)` combinations this run's attempts actually ran
    under, each with how many attempts carried it — `run compare`'s
    "what produced this" half, read by the same name-prefix convention
    every other run reader here uses.

    Ordered by count descending (see `_RUN_BUILD_PROVENANCE`'s own
    comment): the first row is the dominant build, which for a healthy
    run is usually the only one.
    """
    pattern = _run_prefix_pattern(name)
    return _rows(conn, _RUN_BUILD_PROVENANCE, (pattern,))


def run_container_digest_count(conn, name):
    """`{"n_distinct": n, "n_attempts": n}` — the single fact behind
    "every attempt of this run shares one image" (migration 121's
    acceptance check), without making a reader sum it themselves out of
    `run_build_provenance`'s full breakdown.
    """
    pattern = _run_prefix_pattern(name)
    rows = _rows(conn, _RUN_CONTAINER_DIGEST_COUNT, (pattern,))
    return rows[0] if rows else {"n_distinct": 0, "n_attempts": 0}


def run_config_overlay(conn, run_id):
    """This run's `config_overlay` (migration 112), or None.

    Takes `run_id` (the registry's surrogate key), not `name` — the same
    split `run.py`'s own read of this column uses, because the caller
    already has a bound `run_row` result in hand (`_cmd_run_compare` does)
    and re-resolving the name to an id a second time would be a wasted
    round trip. `psycopg2` decodes the `jsonb` column straight to a dict
    (or `None`), so nothing here calls `json.loads` on the result — see
    `run.py`'s own comment on this column for why a second decode would
    be wrong.
    """
    rows = _rows(conn, _RUN_CONFIG_OVERLAY, (int(run_id),))
    return rows[0]["config_overlay"] if rows else None


def run_reference_product_keys(conn, name):
    """The distinct reference product keys this run's difference images
    cite, oldest-`rfid`-independent — i.e. the SET of references the run's
    science actually used, not one row per difference image.

    A `None` entry in the returned list means at least one difference
    image cites a reference that has no `products` row yet (see
    `_RUN_REFERENCE_PRODUCT_KEYS`'s LEFT JOIN comment) — printed as `n/a`
    by the caller, not filtered out, because a reference with no product
    key yet is a fact about this run's provenance, not noise to hide.
    """
    pattern = _run_prefix_pattern(name)
    rows = _rows(conn, _RUN_REFERENCE_PRODUCT_KEYS, (pattern,))
    return [row["product_key"] for row in rows]


def run_input_identities(conn, name):
    """The distinct `(exposure_id, sca)` pairs this run's attempts were
    submitted against — the input side of a comparison, as opposed to
    `run_reference_product_keys`'s output side.
    """
    pattern = _run_prefix_pattern(name)
    rows = _rows(conn, _RUN_INPUT_IDENTITIES, (pattern,))
    return [(row["exposure_id"], row["sca"]) for row in rows]


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
