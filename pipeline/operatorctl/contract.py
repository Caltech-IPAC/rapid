"""The mutation contract as the CLI enforces it.

Every mutating subcommand takes the same four things — a reason, an
idempotency key, an explicit ``--apply``, and a named target scope — and
prints the same shape of plan before it does anything. This module owns
that shape so the subcommands cannot drift from each other, and so
"which fields does an operator action carry?" has exactly one answer in
the code as it does in ``derived.mutation_audit``.

WHY THE PLAN IS PRINTED FROM THE DRY RUN'S OWN RESULT, NOT COMPOSED
LOCALLY. A plan the CLI writes from its own arguments is a plan about
what the CLI intends, and the operator would be reviewing the tool's
understanding rather than the database's. The dry-run call is the real
function, against the real candidate population, computing exactly what
the apply would compute — so the plan shown IS the answer the apply will
act on, minus the writing. This is the difference between a preview and
a promise.
"""

import json
import uuid

# SQLSTATEs draft 047 raises. Classified by code, never by message text —
# the same discipline `pipeline/intent/errors.py` establishes for 23505 and
# 23503, and for the same reason: message text is a presentation detail that
# changes with a locale or a wording fix, and a tool that greps it breaks
# silently when it does.
SQLSTATE_EXPECTED_STATE_MISMATCH = "RA001"
SQLSTATE_IDEMPOTENCY_CONFLICT = "RA002"


class OperatorError(Exception):
    """Base for the operator surface's own refusals."""

    error_category = "operator_error"
    exit_code = 1


class ExpectedStateMismatch(OperatorError):
    """The world is not in the state the operator said it was.

    Not a failure of the action — a refusal to perform it. The operator
    decided against a picture of the system, that picture is stale, and
    the correct response is to look again rather than to force the call
    through. Nothing was written (draft 047 raises before any INSERT), so
    there is no partial effect to unwind.
    """

    error_category = "expected_state_mismatch"
    exit_code = 65


class IdempotencyConflict(OperatorError):
    """This idempotency key was already used for a different action.

    A caller bug rather than a replay: returning the other action's
    recorded outcome would report success for something this call never
    did.
    """

    error_category = "idempotency_conflict"
    exit_code = 66


def new_idempotency_key(prefix="rapidctl"):
    """Mint a key for a caller that did not supply one.

    A minted key is single-use by construction, which makes the FIRST
    call safe to retry only if the operator reuses the printed key. That
    is why every result prints its key: an operator whose apply died
    mid-flight without a visible outcome re-runs with ``--idempotency-key
    <the printed one>`` and gets the recorded result instead of a second
    mutation.
    """
    return "%s-%s" % (prefix, uuid.uuid4().hex[:16])


def classify(exc):
    """Re-raise a psycopg2 error as this module's typed refusal, or None.

    Returns the typed exception to raise, or ``None`` when the error is
    not one of draft 047's contract refusals and should propagate as
    itself. Reads ``pgcode``/``sqlstate`` in both spellings, matching
    ``pipeline.intent.errors.sqlstate_of`` — the two modules classify
    different code families but agree on how a SQLSTATE is read.
    """
    code = getattr(exc, "pgcode", None) or getattr(exc, "sqlstate", None)
    if code == SQLSTATE_EXPECTED_STATE_MISMATCH:
        return ExpectedStateMismatch(_message_of(exc))
    if code == SQLSTATE_IDEMPOTENCY_CONFLICT:
        return IdempotencyConflict(_message_of(exc))
    return None


def _message_of(exc):
    """The server's own message, without the traceback noise around it.

    psycopg2 puts the primary message plus context into ``str(exc)``; the
    diagnostics object has the primary message alone, which is what an
    operator wants to read.
    """
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None) if diag else None
    return primary or str(exc).strip()


def call_function(conn, sql, params):
    """Call one mutation function and return its jsonb result as a dict.

    Every mutating call in this package goes through here, so the typed
    classification above cannot be forgotten at a call site, and so there
    is exactly one place composing statements — all of them parameterized,
    all of them a bare ``SELECT <function>(...)``. No subcommand ever
    composes state-changing SQL of its own; the function IS the mutation.
    """
    import psycopg2                               # noqa: PLC0415 — see below
    # Imported here rather than at module import time: the stub tier puts a
    # fake psycopg2 into sys.modules, and a module-level import would bind
    # whichever object existed when this module was first imported.
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        conn.commit()
    except psycopg2.Error as exc:
        conn.rollback()
        typed = classify(exc)
        if typed is not None:
            raise typed from exc
        raise
    result = row[0] if row else None
    # psycopg2 already adapts jsonb to a dict; a text-returning driver or a
    # stub that hands back the raw string still parses here.
    if isinstance(result, str):
        result = json.loads(result)
    return result


def render_plan(action, target_scope, reason, idempotency_key, result,
                apply_requested):
    """The operator-facing account of what a call did or would do.

    One renderer for the plan and for the outcome, because they must be
    comparable: an operator reviewing a dry run and then reading the
    apply's output should be able to see at a glance that the same action
    against the same scope produced the rows it said it would.
    """
    lines = []
    mode = "APPLY" if apply_requested else "DRY RUN"
    if result.get("replayed"):
        mode = "REPLAY"
    lines.append("%s: %s" % (mode, action))
    lines.append("  target scope    : %s" % target_scope)
    lines.append("  reason          : %s" % reason)
    lines.append("  idempotency key : %s" % idempotency_key)
    if result.get("replayed"):
        lines.append("  ALREADY PERFORMED — recorded outcome returned, "
                     "nothing mutated")
        lines.append("  performed at    : %s" % result.get("performed_at"))
    if "candidates" in result:
        lines.append("  candidates      : %s" % result["candidates"])
    if "already_present" in result:
        lines.append("  already present : %s" % result["already_present"])
    lines.append("  rows affected   : %s" % result.get("rows_affected", 0))
    lines.append("  audit id        : %s" % result.get("audit_id"))
    if result.get("scale_advisory"):
        lines.append("  ADVISORY        : %s" % result["scale_advisory"])
    if not apply_requested and not result.get("replayed"):
        lines.append("")
        lines.append("  Nothing was changed. Re-run with --apply and the "
                     "SAME --idempotency-key")
        lines.append("  to perform this action exactly once.")
    return "\n".join(lines)
