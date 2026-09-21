"""Contract-tier tests for `derived.create_child_table` (migrations
072/141/145) — the sources-family per-field/per-date-sca child-table
creator, called directly against the real applied migration stream.

Covers case-map-2.txt's child-table battery: ct-00 (precondition) through
ct-10-owner, all PASS in the recorded rehearsal transcript.

Read `145-child-table-ownership-and-relocation.sql` (read-only reference,
rapid_systems) for the signature and behaviour under test:
`derived.create_child_table(p_tablename text, p_prototype text,
p_inherit boolean DEFAULT false) RETURNS boolean` — `SECURITY DEFINER`,
`SET search_path = derived, public, pg_temp`. Validates the prototype
against a fixed allowlist (`sources`, `merges`, `astroobjects`,
`astroobjectsmeta`), validates the child name shape and that it belongs to
the claimed prototype, clones `LIKE public.<prototype> INCLUDING DEFAULTS
INCLUDING CONSTRAINTS INCLUDING INDEXES` into `public` explicitly (never
`derived`, the first schema of its own `search_path`), optionally
`INHERIT`s the prototype, and — as of 145 — assigns the new child's owner
to the prototype's own owner and copies the prototype's table grants onto
it. Returns `true` when it created the table, `false` on an idempotent
repeat.
"""

import uuid

import pytest

from pipeline.contract import fixture

pytestmark = pytest.mark.contract


def require_create_child_table(conn):
    if not fixture.has_function(conn, "create_child_table"):
        pytest.skip("migration 072/141/145 (derived.create_child_table) is "
                   "not applied")


def _tag():
    return uuid.uuid4().hex[:8]


def _drop_if_exists(conn, table_name):
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS public.%s" % table_name)
    conn.commit()


def _relation_schema(conn, table_name):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT n.nspname FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE c.relname = %s", [table_name])
        row = cur.fetchone()
    return row[0] if row else None


def _relation_owner(conn, table_name):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_get_userbyid(c.relowner) FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE c.relname = %s AND n.nspname = 'public'", [table_name])
        row = cur.fetchone()
    return row[0] if row else None


def _create(conn, table_name, prototype, inherit=False):
    with conn.cursor() as cur:
        cur.execute("SELECT derived.create_child_table(%s, %s, %s)",
                    [table_name, prototype, inherit])
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# ct-00 — precondition.
# ---------------------------------------------------------------------------
def test_ct_00_precondition_function_exists_and_qualifies_ddl_to_public(
        conn):
    """`derived.create_child_table` exists, and its own source text
    qualifies every composed `CREATE TABLE`/`ALTER TABLE` statement to
    `public.` explicitly (141's own fix, preserved by 145) — checked
    against the function's real source in `pg_proc`, not merely its
    presence.
    """
    require_create_child_table(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_get_functiondef(p.oid) FROM pg_proc p"
            " JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'derived' AND p.proname = 'create_child_table'")
        row = cur.fetchone()
    assert row is not None
    source = row[0]
    assert "CREATE TABLE IF NOT EXISTS public.%I" in source, (
        "the composed CREATE TABLE is not qualified to public — a child "
        "could land in derived, the first schema of this function's own "
        "search_path")
    assert "LIKE public.%I" in source, (
        "the LIKE source is not qualified to public — the prototype could "
        "be shadowed by a same-named relation in derived")


# ---------------------------------------------------------------------------
# ct-01 — the child lands in public, never derived.
# ---------------------------------------------------------------------------
def test_ct_01_child_lands_in_public_not_derived(conn):
    """Called under a derived-first search_path (`derived, public,
    pg_temp`) — the SAME search_path the function's own `SET search_path`
    clause fixes it to internally, exercised here by setting it on THIS
    session too, so the assertion is not merely "the function's own SET
    worked" but "a caller whose own session already prefers derived still
    gets a child in public".
    """
    require_create_child_table(conn)
    table_name = "sources_%s" % _tag()
    _drop_if_exists(conn, table_name)
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path = derived, public, pg_temp")
        created = _create(conn, table_name, "sources")
        assert created is True
        assert _relation_schema(conn, table_name) == "public"
        assert _relation_schema(conn, table_name) != "derived"
    finally:
        with conn.cursor() as cur:
            cur.execute("RESET search_path")
        conn.commit()
        _drop_if_exists(conn, table_name)


# ---------------------------------------------------------------------------
# ct-02 — inherits the public prototype when asked.
# ---------------------------------------------------------------------------
def test_ct_02_inherits_public_prototype_when_asked(conn):
    require_create_child_table(conn)
    table_name = "sources_%s" % _tag()
    _drop_if_exists(conn, table_name)
    try:
        created = _create(conn, table_name, "sources", inherit=True)
        assert created is True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT p.relname FROM pg_inherits i"
                " JOIN pg_class c ON c.oid = i.inhrelid"
                " JOIN pg_class p ON p.oid = i.inhparent"
                " WHERE c.relname = %s", [table_name])
            parents = [r[0] for r in cur.fetchall()]
        assert parents == ["sources"]
    finally:
        _drop_if_exists(conn, table_name)


# ---------------------------------------------------------------------------
# ct-03 — structure matches the prototype BY DEFINITION.
# ---------------------------------------------------------------------------
def test_ct_03_structure_like_copied_columns_and_indexes_match(conn):
    """Columns and index shapes match the prototype BY THE `LIKE ...
    INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES` CLAUSE
    ITSELF, not a generated name this test would have to keep in sync —
    asserted by comparing the catalog's own column set and index count
    between the child and the prototype.
    """
    require_create_child_table(conn)
    table_name = "sources_%s" % _tag()
    _drop_if_exists(conn, table_name)
    try:
        created = _create(conn, table_name, "sources")
        assert created is True

        def _columns(name):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name, data_type FROM information_schema"
                    ".columns WHERE table_schema = 'public'"
                    "   AND table_name = %s ORDER BY ordinal_position",
                    [name])
                return cur.fetchall()

        def _index_count(name):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM pg_indexes"
                    " WHERE schemaname = 'public' AND tablename = %s",
                    [name])
                return cur.fetchone()[0]

        assert _columns(table_name) == _columns("sources")
        # Index COUNT matches; index NAMES differ (PostgreSQL renames a
        # cloned index to avoid a catalog collision with the prototype's
        # own), which is exactly why this compares counts, not names.
        assert _index_count(table_name) == _index_count("sources")
    finally:
        _drop_if_exists(conn, table_name)


# ---------------------------------------------------------------------------
# ct-04 — constraints copied.
# ---------------------------------------------------------------------------
def test_ct_04_constraints_copied_check_and_not_null_counts_match(conn):
    require_create_child_table(conn)
    table_name = "sources_%s" % _tag()
    _drop_if_exists(conn, table_name)
    try:
        created = _create(conn, table_name, "sources")
        assert created is True

        def _not_null_count(name):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND table_name = %s"
                    "   AND is_nullable = 'NO'", [name])
                return cur.fetchone()[0]

        def _check_defs(name):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c"
                    " JOIN pg_class t ON t.oid = c.conrelid"
                    " WHERE t.relname = %s AND c.contype = 'c'", [name])
                return sorted(r[0] for r in cur.fetchall())

        assert _not_null_count(table_name) == _not_null_count("sources")
        assert _check_defs(table_name) == _check_defs("sources")
    finally:
        _drop_if_exists(conn, table_name)


# ---------------------------------------------------------------------------
# ct-05 — grants present.
# ---------------------------------------------------------------------------
def test_ct_05_every_prototype_grant_is_present_on_the_child(conn):
    """Every prototype grantee/privilege pair (145's own grant-copy loop,
    over `information_schema.role_table_grants` for SELECT/INSERT/UPDATE/
    DELETE/TRUNCATE/REFERENCES/TRIGGER) is present on the child.
    """
    require_create_child_table(conn)
    table_name = "sources_%s" % _tag()
    _drop_if_exists(conn, table_name)
    try:
        created = _create(conn, table_name, "sources")
        assert created is True

        def _grants(name):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT grantee, privilege_type"
                    "  FROM information_schema.role_table_grants"
                    " WHERE table_schema = 'public' AND table_name = %s"
                    "   AND privilege_type IN"
                    "       ('SELECT', 'INSERT', 'UPDATE', 'DELETE',"
                    "        'TRUNCATE', 'REFERENCES', 'TRIGGER')",
                    [name])
                return set(cur.fetchall())

        prototype_grants = _grants("sources")
        child_grants = _grants(table_name)
        missing = prototype_grants - child_grants
        assert not missing, (
            "grantee/privilege pairs present on the prototype but missing "
            "on the child: %r" % (missing,))
    finally:
        _drop_if_exists(conn, table_name)


# ---------------------------------------------------------------------------
# ct-06 — repeat call is a no-op.
# ---------------------------------------------------------------------------
def test_ct_06_repeat_call_reports_false_and_is_a_no_op(conn):
    require_create_child_table(conn)
    table_name = "sources_%s" % _tag()
    _drop_if_exists(conn, table_name)
    try:
        first = _create(conn, table_name, "sources")
        assert first is True
        second = _create(conn, table_name, "sources")
        assert second is False

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_class c"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE c.relname = %s", [table_name])
            relation_count = cur.fetchone()[0]
        assert relation_count == 1
    finally:
        _drop_if_exists(conn, table_name)


# ---------------------------------------------------------------------------
# ct-07 / ct-08 / ct-09 — the three refusals (RA001).
# ---------------------------------------------------------------------------
def test_ct_07_refuse_bad_prototype(conn):
    """A prototype outside the fixed allowlist (sources, merges,
    astroobjects, astroobjectsmeta) is refused (RA001).
    """
    require_create_child_table(conn)
    import psycopg2

    with pytest.raises(psycopg2.Error) as caught:
        with conn.cursor() as cur:
            cur.execute("SELECT derived.create_child_table(%s, %s, %s)",
                       ["sources_bad_%s" % _tag(), "not_a_real_prototype",
                        False])
    assert getattr(caught.value, "pgcode", None) == "RA001"
    conn.rollback()


def test_ct_08_refuse_bad_name_shape(conn):
    """A malformed child name — neither `<prototype>_<field>` nor
    `<prototype>_<yyyymmdd>_<sca>` — is refused (RA001).
    """
    require_create_child_table(conn)
    import psycopg2

    with pytest.raises(psycopg2.Error) as caught:
        with conn.cursor() as cur:
            cur.execute("SELECT derived.create_child_table(%s, %s, %s)",
                       ["Sources-Not-A-Valid-Shape!", "sources", False])
    assert getattr(caught.value, "pgcode", None) == "RA001"
    conn.rollback()


def test_ct_09_refuse_name_prototype_mismatch(conn):
    """A syntactically valid child name that does not belong to the
    claimed prototype (`merges_<field>` claimed as a `sources` child) is
    refused (RA001).
    """
    require_create_child_table(conn)
    import psycopg2

    with pytest.raises(psycopg2.Error) as caught:
        with conn.cursor() as cur:
            cur.execute("SELECT derived.create_child_table(%s, %s, %s)",
                       ["merges_991109", "sources", False])
    assert getattr(caught.value, "pgcode", None) == "RA001"
    conn.rollback()


# ---------------------------------------------------------------------------
# ct-10-owner — the child takes the prototype's owner.
# ---------------------------------------------------------------------------
def test_ct_10_owner_child_takes_prototype_owner(conn):
    """The new child and its prototype (`sources`) are both owned by the
    SAME role — `rapid_pipeline_write` for `sources` (007's own `ALTER
    TABLE sources OWNER TO rapid_pipeline_write`) — read from
    `pg_class.relowner` via `pg_get_userbyid`, never hardcoded to a role
    name in this test, so the assertion tracks whichever role the
    prototype is actually owned by rather than asserting an assumption
    that could drift from 007 independently.
    """
    require_create_child_table(conn)
    table_name = "sources_%s" % _tag()
    _drop_if_exists(conn, table_name)
    try:
        created = _create(conn, table_name, "sources")
        assert created is True

        prototype_owner = _relation_owner(conn, "sources")
        child_owner = _relation_owner(conn, table_name)
        assert prototype_owner is not None
        assert child_owner == prototype_owner, (
            "the child %r is owned by %r, not the prototype's owner %r"
            % (table_name, child_owner, prototype_owner))
    finally:
        _drop_if_exists(conn, table_name)
