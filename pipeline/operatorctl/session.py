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

APPLICATION_NAME = "rapidctl"

# The env var that selects which operate tier `operator_session()` assumes.
# Values map 1:1 onto the two roles above; anything else is a config error
# (see `_resolve_operator_role`), never a silent default.
OPERATOR_TIER_ENV = "RAPID_OPERATOR_TIER"
HUMAN_TIER = "human"
AGENT_TIER = "agent"
_TIER_ROLES = {HUMAN_TIER: OPERATOR_ROLE, AGENT_TIER: AGENT_OPERATOR_ROLE}


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

    The role actually assumed is recorded on the connection object
    (``conn.rapid_operator_role``) so ``break_glass_role()`` — handed
    only the connection at each of its call sites — can tell which tier
    it is being asked to elevate from, without every caller having to
    carry a second value alongside `conn` for that one purpose.

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
        conn.rapid_operator_role = role
        yield conn
    finally:
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
    before it ever issues a `SET ROLE`: it checks
    ``conn.rapid_operator_role`` (set by `operator_session()`) and
    raises if the session is not on the human tier, so the refusal is a
    property of this module's own logic and not merely of what the
    database happens to grant today.

    On exit the role returns to whichever operate tier the session
    actually assumed — read back from ``conn.rapid_operator_role``,
    never hard-coded to the human role — so a break-glass elevation
    cannot leak into the rest of a command's work and a restore can
    never itself widen privilege.
    """
    assumed_role = getattr(conn, "rapid_operator_role", OPERATOR_ROLE)
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
