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


def unreconciled_break_glass(conn):
    """Region 7's panel: open sessions lacking a close and a passing sweep."""
    return _rows(conn, _UNRECONCILED, ())


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
