"""Connection and role assumption for ``rapidctl``.

THE MODERN PATH ONLY. This module reaches the database through
``rapid_db_connect.connect()`` with an explicit ``Credentials``, never
through ``RAPIDDB`` and never by setting environment variables for
something downstream to pick up. The distinction is not stylistic:
``RAPIDDB.__init__`` calls ``exit()`` on missing configuration (rule 17's
defect, addressed in ``rapid_db.py``'s freeze), and an operator tool that
terminates the interpreter instead of raising cannot print a diagnostic,
cannot clean up, and cannot be called from a test.

HOW A HUMAN REACHES THE OPERATE TIER. ``rapid_operator`` is NOLOGIN
(031:354): nobody connects as it. A person connects as their own login
role — the one ``db-users.tsv`` grants — and reaches the operate tier by
membership, with ``SET ROLE rapid_operator`` making the assumption
explicit in the session. Break-glass is one step further out:
``rapid_break_glass`` is granted to ``rapid_operator`` WITH INHERIT FALSE
(032:75) precisely so its privileges are never ambient, so reaching it
means a second ``SET ROLE`` from an operator session and never a
connection parameter.

That two-step is modelled here as ``operator_session()`` and
``break_glass_session()`` rather than as a role name a caller passes in,
because the sequence is the control: an operator session that silently
started as break-glass would defeat the "assumed, never resident"
property the migration went to some length to establish.

HOW AN AGENT REACHES THE OPERATE TIER. An unattended agent run is a
third category, neither of the two this module started with. It is not
a human at a terminal — nobody is present to read a break-glass prompt
or absorb a permission failure. It is also not a service role: a
shared service credential run against ``derived.write_mutation_audit``
would put a fleet-wide identity in the actor column, and every run
would be indistinguishable from every other in the ledger. What an
agent run has instead is a per-run LOGIN — something like
``agent_sims_fix_2026_08`` — created for that run and granted
membership in ``rapid_agent_operator`` (NOLOGIN, holding the same
``rapid_read`` grant and the same EXECUTE privileges on ``derived.*``
that ``rapid_operator`` holds). ``SET ROLE rapid_agent_operator`` makes
that assumption explicit in the session exactly as the human path does,
and because ``session_user`` is what ends up in
``derived.write_mutation_audit``, the per-run login is what names the
run in the audit trail — not a shared identity, not a human's name
borrowed for the occasion.

The agent tier is deliberately NOT granted ``rapid_break_glass``
membership. Break-glass exists for a human to invoke, under whatever
out-of-band scrutiny accompanies an emergency elevation; an unattended
run reaching for it on its own would turn a safety valve into
something a bug could pull. ``break_glass_role()`` below refuses
outright when the session it is handed assumed the agent tier, rather
than attempting the ``SET ROLE`` and letting Postgres deny it — the
refusal is a property of this module's own logic, not merely of what
the database happens to grant today.

Which tier ``operator_session()`` assumes is selected by the
``RAPID_OPERATOR_TIER`` environment variable: unset or ``"human"``
assumes ``rapid_operator`` (today's only behaviour, preserved
byte-for-byte), ``"agent"`` assumes ``rapid_agent_operator``. Any other
value is refused rather than silently treated as one of the two —
a typo in this variable picks the operate tier a run acts under, so
guessing which one was meant is worse than stopping.
"""

import contextlib
import os

from database.modules.utils.rapid_db_connect import (Credentials,
                                                     DBCredentialError,
                                                     LANE_SESSION,
                                                     connect)

# `SET ROLE` needs a session, not a transaction-pooled connection: the role
# must persist across the statements of one operator action, and PgBouncer's
# transaction lane hands the underlying server connection to whoever needs it
# next between statements — which would drop the role somewhere in the middle
# of a multi-statement action and, worse, could leave it set on a connection
# handed to a stranger.
OPERATOR_LANE = LANE_SESSION

OPERATOR_ROLE = "rapid_operator"
AGENT_OPERATOR_ROLE = "rapid_agent_operator"
BREAK_GLASS_ROLE = "rapid_break_glass"

# Tried in this order by `submission_role()` — see that function's docstring
# for why a list rather than a single hard-coded role. `rapid_admin` comes
# first because it is the tier the survey found the human login already
# holds; `rapid_orchestrator` is the narrower, submission-only role a
# login might hold instead without also being an admin. Neither name is
# the human login's own — both are roles reached the same way the operate
# tier itself is reached, by membership plus `SET ROLE`.
SUBMISSION_ROLES = ("rapid_admin", "rapid_orchestrator")

APPLICATION_NAME = "rapidctl"

# The env var that selects which operate tier `operator_session()` assumes.
# Values map 1:1 onto the two roles above; anything else is a config error
# (see `_resolve_operator_role`), never a silent default.
OPERATOR_TIER_ENV = "RAPID_OPERATOR_TIER"
HUMAN_TIER = "human"
AGENT_TIER = "agent"
_TIER_ROLES = {HUMAN_TIER: OPERATOR_ROLE, AGENT_TIER: AGENT_OPERATOR_ROLE}

#: Which operate tier each open operator session actually assumed, keyed by
#: ``id(conn)``.
#:
#: A SIDE TABLE RATHER THAN AN ATTRIBUTE ON THE CONNECTION, because
#: `psycopg2.extensions.connection` is a C type with no ``__dict__`` and
#: refuses new attributes outright — `operator_session()` used to set one and
#: died with `AttributeError` the first time either tier was driven against a
#: real database. Entries are written by `operator_session()` on entry and
#: removed in its `finally`, so the table never holds an id whose connection
#: has been closed, and a later object reusing that id cannot inherit a tier
#: it never assumed.
_ASSUMED_ROLES = {}


class OperatorSessionError(Exception):
    """Raised when the operate tier cannot be assumed.

    Distinct from ``DBUnavailable`` (the database is unreachable) and
    ``DBCredentialError`` (no credential could be resolved): here the
    connection succeeded and the login is real, but this login is not a
    member of the role it tried to assume. That is an authorization fact
    with an unambiguous remedy — ``GRANT rapid_operator TO <login>`` for
    the human tier, the same membership grant 031:379-390 makes for the
    team, or ``GRANT rapid_agent_operator TO <login>`` for a per-run
    agent login on the agent tier — so it is worth its own type rather
    than surfacing as a raw psycopg2 error whose message the operator
    would have to interpret. The message always names the role that was
    actually attempted, never a role hard-coded to the human tier,
    because the two tiers fail the same way for different logins and a
    fixed message would send an agent run's operator toward the wrong
    grant.
    """

    error_category = "not_authorized"


def _resolve_operator_role():
    """Return ``(tier, role)`` selected by ``RAPID_OPERATOR_TIER``.

    Unset resolves to the human tier — today's only behaviour — so every
    existing deployment that has never heard of this variable keeps
    assuming ``rapid_operator`` exactly as before. A value present but
    not one of the two known tiers is refused here, before any
    connection is opened: this selects the privilege tier a run acts
    under, and guessing a default for a typo would silently run a
    command under the wrong identity rather than failing loudly.
    """
    tier = os.environ.get(OPERATOR_TIER_ENV) or HUMAN_TIER
    try:
        return tier, _TIER_ROLES[tier]
    except KeyError:
        raise OperatorSessionError(
            "invalid %s=%r: accepted values are %r (default) and %r"
            % (OPERATOR_TIER_ENV, tier, HUMAN_TIER, AGENT_TIER)) from None


def operator_credentials():
    """Resolve the personal login credential ``rapidctl`` connects with.

    A human running this tool authenticates as themselves. The credential
    comes from the ordinary libpq environment (``PGUSER``/``PGPASSWORD``,
    or ``~/.pgpass`` via libpq when the password is unset) rather than
    from Secrets Manager, because the operate tier is reached by
    MEMBERSHIP, not by a service credential: there is no
    ``rapid_operator`` password to fetch, and a tool that fetched a
    service secret in order to act as a human would put the wrong actor
    in every audit row.

    ``session_user`` is what ``derived.write_mutation_audit`` records as
    the actor (031:99), so the login this returns is the name that ends
    up in the ledger. That is the intended behaviour and the reason this
    never falls back to a shared identity.

    **THE ``~/.pgpass`` PATH IS REAL, AND USED NOT TO BE.** This docstring
    described it and the code refused it: the call passed ``""`` into
    ``Credentials``, whose check rejects a falsy password, so an operator
    with a working ``~/.pgpass`` and no ``PGPASSWORD`` got a
    ``DBCredentialError`` instead of a connection. The two halves now
    agree — an unset or empty ``PGPASSWORD`` builds
    ``Credentials.for_pgpass(user)``, which carries ``password = None``
    and lets libpq do the resolution this function has always claimed it
    would. The distinction is not cosmetic: libpq consults ``~/.pgpass``
    only when NO password is supplied, so the empty string this used to
    pass would not have worked even had it been let through.
    """
    user = os.environ.get("PGUSER") or os.environ.get("USER")
    if not user:
        raise DBCredentialError(
            "no login role: set PGUSER to the personal login role granted "
            "rapid_operator")
    # NO PASSWORD IS LEGITIMATE HERE, and it is now actually reachable.
    # This comment used to claim that libpq would consult `~/.pgpass` when
    # `PGPASSWORD` was unset, while the call below passed `""` into
    # `Credentials`, whose check refuses a falsy password — so the
    # documented path raised `DBCredentialError` before libpq ever saw it.
    # `for_pgpass` states the intent the comment always described, and
    # carries `password = None` rather than `""` because libpq consults
    # `~/.pgpass` only when NO password is supplied; an empty string is a
    # supplied one, sent as-is and rejected by the server.
    password = os.environ.get("PGPASSWORD")
    if not password:
        return Credentials.for_pgpass(user)
    # `Credentials` validates the user and redacts the password in repr.
    return Credentials(user, password)


@contextlib.contextmanager
def operator_session(credentials=None, connect_fn=connect):
    """Yield a connection with the selected operate tier assumed.

    The tier is chosen by ``RAPID_OPERATOR_TIER`` (see
    ``_resolve_operator_role``) BEFORE anything connects — an invalid
    value is refused without opening a connection or issuing a `SET
    ROLE`, so a bad env var never spends a login attempt it cannot use.
    Unset resolves to the human tier, which is today's only behaviour,
    unchanged: same role, same connect call, same error message shape.

    The role actually assumed is recorded AGAINST the connection object,
    in this module's ``_ASSUMED_ROLES`` side table, so
    ``break_glass_role()`` — handed only the connection at each of its
    call sites — can tell which tier it is being asked to elevate from,
    without every caller having to carry a second value alongside `conn`
    for that one purpose.

    **IT IS A SIDE TABLE BECAUSE THE OBVIOUS VERSION DOES NOT WORK.**
    This used to do ``conn.rapid_operator_role = role``, which raises
    ``AttributeError: 'psycopg2.extensions.connection' object has no
    attribute 'rapid_operator_role' and no __dict__ for setting new
    attributes`` against a REAL connection — the C extension type
    defines no ``__dict__``, so it takes no new attributes. Every test
    covering this passed because their fake connections are ordinary
    Python objects, which accept any attribute silently: the doubles
    could not refuse what the real type forbids. The failure was
    therefore invisible until the tier was first driven end-to-end
    (2026-08-15). Keyed by ``id(conn)`` and removed on exit, so a
    connection object cannot outlive its entry and a recycled id cannot
    inherit a stale tier.

    The role is set once, on entry, and the session is closed on exit.
    Nothing resets the role on the way out because the connection does
    not outlive the block — this is a one-shot CLI, and a `RESET ROLE`
    would only matter to a pooled connection this deliberately does not
    use.
    """
    _tier, role = _resolve_operator_role()
    conn = connect_fn(APPLICATION_NAME, lane=OPERATOR_LANE,
                      credentials=credentials or operator_credentials())
    try:
        with conn.cursor() as cur:
            try:
                # Not parameterized because SET ROLE takes an identifier,
                # not a value, and a placeholder is a syntax error there.
                # The name comes from this module's own tier table, never
                # caller input — so there is nothing here to inject.
                cur.execute("SET ROLE " + role)
            except Exception as exc:              # noqa: BLE001 — re-typed
                conn.rollback()
                raise OperatorSessionError(
                    "cannot assume %s: this login is not a member of the "
                    "operate tier (%s)" % (role, exc)) from exc
        _ASSUMED_ROLES[id(conn)] = role
        yield conn
    finally:
        _ASSUMED_ROLES.pop(id(conn), None)
        conn.close()


@contextlib.contextmanager
def break_glass_role(conn):
    """Assume ``rapid_break_glass`` for the duration of the block.

    Takes an already-open operator session rather than opening its own,
    which is the whole point: break-glass is reached FROM the operate
    tier, and the migration's NOINHERIT grant means it arrives only after
    this explicit second assumption.

    HUMAN-ONLY. Break-glass exists for a human to invoke under whatever
    out-of-band scrutiny accompanies an emergency elevation; an agent
    session reaching for it unattended would turn a safety valve into
    something a bug could pull, so the migration never grants
    ``rapid_agent_operator`` membership in ``rapid_break_glass`` at all.
    This function enforces the same property one layer up, refusing
    before it ever issues a `SET ROLE`: it looks up this session's tier
    in ``_ASSUMED_ROLES`` (recorded by `operator_session()`) and raises
    if the session is not on the human tier, so the refusal is a
    property of this module's own logic and not merely of what the
    database happens to grant today.

    On exit the role returns to whichever operate tier the session
    actually assumed — read back from ``_ASSUMED_ROLES``, never
    hard-coded to the human role — so a break-glass elevation cannot
    leak into the rest of a command's work and a restore can never
    itself widen privilege.
    """
    assumed_role = _ASSUMED_ROLES.get(id(conn), OPERATOR_ROLE)
    if assumed_role != OPERATOR_ROLE:
        raise OperatorSessionError(
            "break-glass is human-only: this session assumed %s, not %s "
            "— an agent-tier session cannot open break-glass"
            % (assumed_role, OPERATOR_ROLE))
    with conn.cursor() as cur:
        try:
            cur.execute("SET ROLE " + BREAK_GLASS_ROLE)
        except Exception as exc:                  # noqa: BLE001 — re-typed
            conn.rollback()
            raise OperatorSessionError(
                "cannot assume %s from this session (%s)"
                % (BREAK_GLASS_ROLE, exc)) from exc
    try:
        yield conn
    finally:
        with conn.cursor() as cur:
            cur.execute("SET ROLE " + assumed_role)


@contextlib.contextmanager
def submission_role(conn):
    """Widen an open operator session to a submission-capable role for the
    duration of the block, then restore whichever operate tier the session
    actually assumed.

    THE DEFECT THIS CLOSES. ``rapid_operator`` (and ``rapid_agent_operator``)
    hold only ``rapid_read`` — SELECT, no writes — because the operate tier
    exists to run read-mostly operator commands (``run status``, ``run
    archive`` dry-runs, and so on) under an identity that cannot mutate
    anything by accident. ``run start --apply`` broke that assumption: it
    performs the pipeline's OWN submission work (inserting the row that
    claims a work unit, per the docstring on ``pipeline.operatorctl.run.
    submit_run``) from inside an ``operator_session()`` block, and hit
    ``InsufficientPrivilege: permission denied for table work_units`` the
    first time it ran against the real database (2026-09-11). The fix is
    NOT to widen ``rapid_operator`` — that would make every operator
    command able to write, including the read-only ones this tier exists
    to keep read-only. The fix is this function: submission is the
    pipeline's work, not the operator's, so it runs under a
    submission-capable role for exactly the one call that needs it, and
    the session narrows back to the operate tier immediately after.

    WHY A ROLE SWITCH AND NOT A SEPARATE CONNECTION. The audit function
    (``derived.write_mutation_audit``, migration 031:92-108) records the
    actor as ``session_user`` — the LOGIN that opened the connection —
    never ``current_user``, which is what ``SET ROLE`` changes. So the
    same property `break_glass_role()` relies on holds here: switching
    roles inside one session cannot disturb which login lands in the
    audit ledger, and a person's own name keeps writing every audited row
    a person caused, even though the submission INSERT in between ran
    under a different current_user. A second connection would have had to
    reconnect as something else entirely and lose that property outright.

    WHY A LIST OF ROLES, TRIED IN ORDER, RATHER THAN ONE HARD-CODED NAME.
    ``rapid_orchestrator`` is the narrower role built for exactly this
    (submission, nothing wider) but the human login ``rusholme`` is not a
    member of it — only of ``rapid_admin``, which happens to carry the
    same ``rapid_pipeline_write`` grant. A future login might be the
    other way around: a member of ``rapid_orchestrator`` and not
    ``rapid_admin``. Hard-coding either name would work for one login and
    fail the other with a misleading "not a member" error, so
    ``SUBMISSION_ROLES`` is tried in order and the first the session can
    actually assume wins — this asks Postgres what the login can do
    rather than this module guessing from which operate tier it assumed.

    WHY A SAVEPOINT AROUND EACH ATTEMPT. A failed `SET ROLE` aborts the
    current transaction in Postgres — every statement after it raises
    `InFailedSqlTransaction` until a rollback, so trying a second role
    name after the first fails would itself fail, not because the second
    role is unavailable but because the connection is already poisoned.
    Each attempt therefore runs inside its own `SAVEPOINT`, released on
    success or rolled back to (not `conn.rollback()`, which would also
    discard whatever the caller's transaction had already done before
    entering this block) on failure, so a rejected first guess never
    contaminates the second attempt or anything the caller did earlier in
    the same transaction.

    Raises `OperatorSessionError` — never a raw psycopg2 error — naming
    every role that was tried, if none can be assumed: the remedy is a
    `GRANT <role> TO <login>` for one of `SUBMISSION_ROLES`, and the
    message says so rather than surfacing a bare "permission denied"
    the caller would have to interpret.

    On exit, restores whichever operate tier `operator_session()` recorded
    for this connection in `_ASSUMED_ROLES` — read back exactly as
    `break_glass_role()` does, never hard-coded to a single tier — so a
    submission elevation can never leak past the block it was requested
    for, and a restore can never itself widen privilege.
    """
    assumed_role = _ASSUMED_ROLES.get(id(conn), OPERATOR_ROLE)
    tried = []
    for candidate in SUBMISSION_ROLES:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT submission_role_attempt")
            try:
                cur.execute("SET ROLE " + candidate)
            except Exception as exc:               # noqa: BLE001 — re-typed
                cur.execute("ROLLBACK TO SAVEPOINT submission_role_attempt")
                tried.append("%s (%s)" % (candidate, exc))
                continue
            cur.execute("RELEASE SAVEPOINT submission_role_attempt")
        break
    else:
        raise OperatorSessionError(
            "cannot assume a submission-capable role: tried %s — grant "
            "this login membership in one of %r to fix"
            % ("; ".join(tried), SUBMISSION_ROLES))
    # EVERY TRANSACTION BOUNDARY RE-APPLIES THE WIDENING, because `SET ROLE`
    # does not survive one: `COMMIT` and `ROLLBACK` both revert it, and to
    # the LOGIN role rather than to whatever was current before. A body that
    # widens once and then commits or rolls back per item silently loses the
    # widening on its first boundary. Registered here and cleared below, so
    # the widening lasts exactly as long as this block claims it does.
    try:
        from database.modules.utils.rapid_db_connect import (
            forget_widened_role, remember_widened_role)
    except ImportError:  # pragma: no cover - driver-less stub tier
        forget_widened_role = remember_widened_role = None

    if remember_widened_role is not None:
        remember_widened_role(conn, candidate)

    try:
        yield conn
    finally:
        if forget_widened_role is not None:
            forget_widened_role(conn)
        # THE RESTORE MUST NOT MASK THE BODY'S OWN FAILURE, and a bare
        # `SET ROLE` here does exactly that. If the body raised something
        # that aborted the transaction — any database error, the ordinary
        # case — PostgreSQL refuses every subsequent statement on that
        # connection with `InFailedSqlTransaction: current transaction is
        # aborted, commands ignored until end of transaction block`. That
        # exception is raised from inside `finally`, so it REPLACES the
        # real error the caller needs to see. Measured against the live
        # database rather than reasoned: a divide-by-zero in the body
        # followed by this restore produced exactly that substitution
        # (2026-09-11, found by a diff-scoped defect review).
        #
        # Swallowing it is right here, and only here. This block's whole
        # job is to leave the session no wider than it found it, and an
        # aborted transaction already guarantees that: the abort discards
        # the `SET ROLE` that widened us, so `current_user` falls back to
        # the bare login — narrower than either the submission role or the
        # operate tier. Verified the same way: `current_user` reads as the
        # login itself after the rollback. So the failure mode is
        # fail-closed, the restore is redundant in precisely the case it
        # cannot run, and the caller's own exception survives.
        try:
            with conn.cursor() as cur:
                cur.execute("SET ROLE " + assumed_role)
        except Exception:                          # noqa: BLE001
            # Deliberately not re-raised: on this path the caller is
            # already carrying the real exception, and privilege has
            # already narrowed.
            pass
