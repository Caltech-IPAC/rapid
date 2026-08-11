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
BREAK_GLASS_ROLE = "rapid_break_glass"

APPLICATION_NAME = "rapidctl"


class OperatorSessionError(Exception):
    """Raised when the operate tier cannot be assumed.

    Distinct from ``DBUnavailable`` (the database is unreachable) and
    ``DBCredentialError`` (no credential could be resolved): here the
    connection succeeded and the login is real, but this person is not a
    member of ``rapid_operator``. That is an authorization fact with an
    unambiguous remedy — ``GRANT rapid_operator TO <login>``, the same
    membership grant 031:379-390 makes for the team — so it is worth its
    own type rather than surfacing as a raw psycopg2 error whose message
    the operator would have to interpret.
    """

    error_category = "not_authorized"


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
    """
    user = os.environ.get("PGUSER") or os.environ.get("USER")
    if not user:
        raise DBCredentialError(
            "no login role: set PGUSER to the personal login role granted "
            "rapid_operator")
    # An empty password is legitimate — libpq then consults ~/.pgpass or a
    # local trust/peer method, both ordinary ways for a person to connect.
    # `Credentials` validates the user and redacts the password in repr.
    return Credentials(user, os.environ.get("PGPASSWORD", ""))


@contextlib.contextmanager
def operator_session(credentials=None, connect_fn=connect):
    """Yield a connection with ``rapid_operator`` assumed.

    The role is set once, on entry, and the session is closed on exit.
    Nothing resets the role on the way out because the connection does
    not outlive the block — this is a one-shot CLI, and a `RESET ROLE`
    would only matter to a pooled connection this deliberately does not
    use.
    """
    conn = connect_fn(APPLICATION_NAME, lane=OPERATOR_LANE,
                      credentials=credentials or operator_credentials())
    try:
        with conn.cursor() as cur:
            try:
                # Not parameterized because SET ROLE takes an identifier,
                # not a value, and a placeholder is a syntax error there.
                # The name is this module's own constant — never caller
                # input — so there is nothing here to inject.
                cur.execute("SET ROLE " + OPERATOR_ROLE)
            except Exception as exc:              # noqa: BLE001 — re-typed
                conn.rollback()
                raise OperatorSessionError(
                    "cannot assume %s: this login is not a member of the "
                    "operate tier (%s)" % (OPERATOR_ROLE, exc)) from exc
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

    On exit the role returns to ``rapid_operator``, so a break-glass
    elevation cannot leak into the rest of a command's work.
    """
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
            cur.execute("SET ROLE " + OPERATOR_ROLE)
