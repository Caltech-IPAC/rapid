"""Contract tests: the grant posture DRAFT 047 must preserve (brief G, G2).

    "Preserve 030's invariants exactly: append-only trigger untouched,
     `write_mutation_audit` stays ungranted, one-path rule intact, role
     grants unchanged in spirit (`rapid_operator` human tier, enumerated
     service grants)."

**THESE SKIP WHERE DRAFT MIGRATION 047 IS ABSENT.**

WHY THE GRANT MAP IS ASSERTED RATHER THAN READ. 031's own header records a
grant that was wrong for two weeks while a comment two lines above it
claimed otherwise: EXECUTE granted to `rapid_read` silently reached
`rapid_orchestrator` through the membership graph, so the service role held
break-glass. That was caught by a rehearsal QUERYING the catalog, not by
reading the migration. A grant is a fact about `pg_proc.proacl`, and the only
way to know it is to ask.

These are the questions a reviewer of the change request will ask, encoded so
that the answer arrives with the diff rather than after it.
"""

import pytest

from pipeline.operatorctl import actions

pytestmark = pytest.mark.usefixtures("_requires_draft_047")


@pytest.fixture
def _requires_draft_047(conn):
    """Skip unless DRAFT 047 is applied. Probed, never assumed."""
    if not actions.draft_schema_present(conn):
        pytest.skip("DRAFT migration 047 is not applied; the grant-posture "
                    "tests need it and skip cleanly without it")


def _has_execute(conn, role, function, nargs):
    """Does `role` hold EXECUTE on this function, by any path?

    `has_function_privilege` follows membership, which is exactly the
    question: the 031 defect was a privilege reaching a role INDIRECTLY,
    and a query that only inspected direct ACL entries would have missed
    it just as the comment did.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.oid::regprocedure::text FROM pg_proc p "
            "  JOIN pg_namespace n ON n.oid = p.pronamespace "
            " WHERE n.nspname = 'derived' AND p.proname = %s "
            "   AND p.pronargs = %s", [function, nargs])
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                    [role, row[0]])
        return cur.fetchone()[0]


def _role_exists(conn, role):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
        return cur.fetchone() is not None


def test_the_operate_tier_holds_the_keyed_actions(conn):
    """`rapid_operator` can call what it could call before, keyed."""
    assert _role_exists(conn, "rapid_operator")
    assert _has_execute(conn, "rapid_operator", "retry_parked_attempts", 8)
    assert _has_execute(conn, "rapid_operator", "add_problem_category", 8)
    assert _has_execute(conn, "rapid_operator", "record_external_action", 10)


def test_the_orchestrator_gains_no_new_action_class(conn):
    """Enumerated, not tiered — and the enumeration is unchanged.

    The one class 031 grants it (scoped retry) it still holds in the
    keyed spelling; the classes it was deliberately denied it still does
    not hold. This is the assertion that would have caught 031's own
    rehearsal defect.
    """
    if not _role_exists(conn, "rapid_orchestrator"):
        pytest.skip("rapid_orchestrator is not present in this database")

    assert _has_execute(conn, "rapid_orchestrator",
                        "retry_parked_attempts", 8) is True
    assert _has_execute(conn, "rapid_orchestrator",
                        "add_problem_category", 8) is False, (
        "vocabulary extension is a human judgment and is not the "
        "orchestrator's to make")
    assert _has_execute(conn, "rapid_orchestrator",
                        "record_external_action", 10) is False


def test_write_mutation_audit_is_still_granted_to_nobody(conn):
    """The one-path rule's load-bearing revocation, untouched by 047.

    Granting this would let a caller forge an audit row without
    performing a mutation — the single change that would make the whole
    history untrustworthy.
    """
    for role in ("rapid_operator", "rapid_orchestrator", "rapid_read",
                 "rapid_dashboard"):
        if not _role_exists(conn, role):
            continue
        assert _has_execute(conn, role, "write_mutation_audit", 9) is False, (
            "%s holds EXECUTE on write_mutation_audit" % role)


def test_public_holds_nothing_on_the_new_functions(conn):
    """PostgreSQL grants EXECUTE to PUBLIC on creation; 047 revokes it.

    Without the REVOKE, every role in the cluster — `rapid_dashboard`
    included — would hold the mutation API, and the grant map would be a
    fiction.
    """
    for function, nargs in (("retry_parked_attempts", 8),
                            ("add_problem_category", 8),
                            ("record_external_action", 10),
                            ("mutation_replay", 3)):
        assert _has_execute(conn, "public", function, nargs) is False, (
            "PUBLIC holds EXECUTE on derived.%s" % function)


def test_the_audit_table_is_still_append_only(conn):
    """030's trigger refuses UPDATE and DELETE regardless of who asks.

    047 adds two columns to this table. Adding a column does not disturb
    a row-level trigger — but the invariant is the reason the ledger can
    be trusted, and "the migration probably didn't break it" is not the
    standard for an audit history.
    """
    import psycopg2

    with conn.cursor() as cur:
        cur.execute("SELECT audit_id FROM derived.mutation_audit "
                    " ORDER BY audit_id DESC LIMIT 1")
        row = cur.fetchone()
    if row is None:
        pytest.skip("no audit rows to attempt a rewrite against")

    with pytest.raises(psycopg2.Error):
        with conn.cursor() as cur:
            cur.execute("UPDATE derived.mutation_audit SET reason = %s"
                        " WHERE audit_id = %s", ["rewritten", row[0]])
    conn.rollback()

    with pytest.raises(psycopg2.Error):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM derived.mutation_audit"
                        " WHERE audit_id = %s", [row[0]])
    conn.rollback()


def test_no_role_holds_direct_write_on_the_audit_table(conn):
    """The table half of the one-path rule.

    Writes exist only through the SECURITY DEFINER functions, which is
    what makes "every mutation is audited" a checkable grant fact rather
    than a convention.
    """
    for role in ("rapid_operator", "rapid_orchestrator", "rapid_read",
                 "rapid_dashboard"):
        if not _role_exists(conn, role):
            continue
        with conn.cursor() as cur:
            cur.execute(
                "SELECT has_table_privilege(%s, 'derived.mutation_audit', "
                "       'INSERT'), "
                "       has_table_privilege(%s, 'derived.mutation_audit', "
                "       'UPDATE'), "
                "       has_table_privilege(%s, 'derived.mutation_audit', "
                "       'DELETE')", [role, role, role])
            insert, update, delete = cur.fetchone()
        assert not (insert or update or delete), (
            "%s holds a direct write grant on derived.mutation_audit"
            % role)
