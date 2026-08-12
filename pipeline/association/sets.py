"""Association set identity and set-scoped table naming (rule 19, brief F1).

Two facts live here, and only here on the Python side:

1. WHICH SET IS LIVE. Read from `association_sets`, never written as a
   constant. The brief's constraint is that no code path hard-codes the live
   set "outside a single well-known-row lookup" — `live_association_set` is
   that lookup, mirroring SQL's `derived.live_association_set()`.

2. WHICH TABLES A SET WRITES. `table_name` mirrors
   `derived.association_table_name`: the live set keeps today's unprefixed
   clone names, a non-live set gets its own prefix. Reprocessing isolation is
   a consequence of that naming rather than a rule anything enforces — a
   reprocessing set cannot mutate live tables because it never names them.

The two implementations exist in both languages on purpose. SQL needs it
because the migration's own grants and any operator query key on the set;
Python needs it because the stages compute their target table names before
they open a cursor. The contract tier asserts the two agree
(`test_association_sets.py`) so the duplication cannot drift silently.
"""

import logging

logger = logging.getLogger("rapid.association.sets")

#: The kinds `association_sets.kind` admits, mirroring the CHECK constraint in
#: DRAFT 049. Duplicated deliberately and asserted equal in the contract tier:
#: a Python path that admitted a kind the database refuses would fail at the
#: INSERT rather than at the call, which is the wrong place to find out.
KIND_LIVE_PROMPT = "live_prompt"
KIND_REPROCESSING = "reprocessing"

#: The lane every set starts with. §2.5: "initially one lane per association
#: set". Named rather than spelled `0` at call sites so the day lanes multiply
#: there is one place that stops being a constant.
DEFAULT_LANE = 0

_LIVE_SET_SQL = (
    "SELECT association_set FROM association_sets WHERE kind = %s"
)

_SET_KIND_SQL = (
    "SELECT kind FROM association_sets WHERE association_set = %s"
)


class UnknownAssociationSet(Exception):
    """A set identity that is not in the registry.

    Raised rather than defaulted to the live set. Defaulting would mean a
    reprocessing run with a typo in its set silently writing the live tables,
    which is the exact failure the set identity exists to prevent.
    """


def live_association_set(conn):
    """Return the live prompt set's identity.

    THE ONE WELL-KNOWN-ROW LOOKUP (brief F1). Every caller that needs "the
    live set" comes through here; nothing else may spell the identity.

    Raises `UnknownAssociationSet` when no live set is registered, which on a
    database carrying DRAFT 049 cannot happen — the migration inserts it and
    `association_sets_one_live` keeps it singular. The check is here for the
    database that does NOT carry the draft: a caller gets a named error rather
    than `None` propagating into a table name.
    """
    with conn.cursor() as cur:
        cur.execute(_LIVE_SET_SQL, (KIND_LIVE_PROMPT,))
        row = cur.fetchone()
    if row is None:
        raise UnknownAssociationSet(
            "no live prompt association set is registered; DRAFT migration "
            "049 inserts it as a well-known row")
    return int(row[0])


def set_kind(conn, association_set):
    """Return a set's kind, or raise `UnknownAssociationSet`."""
    with conn.cursor() as cur:
        cur.execute(_SET_KIND_SQL, (int(association_set),))
        row = cur.fetchone()
    if row is None:
        raise UnknownAssociationSet(
            f"association set {association_set} is not registered")
    return str(row[0])


def table_name(prototype, association_set, field, kind):
    """The clone-family table name this set writes for `field`.

    Mirrors `derived.association_table_name`. `kind` is passed in rather than
    looked up so this stays a pure function — the stages already hold the kind
    from one read and would otherwise re-query per table.

    THE LIVE SET KEEPS TODAY'S NAMES. `astroobjects_4641773`, not
    `astroobjects_s1_4641773`. That is what makes adopting 049 a no-op for
    existing data: no rename, no motion, no backfill, and every existing
    reader keeps working. A non-live set is prefixed, and is therefore
    incapable of naming a live table.
    """
    if kind == KIND_LIVE_PROMPT:
        return f"{prototype}_{int(field)}"
    return f"{prototype}_s{int(association_set)}_{int(field)}"
