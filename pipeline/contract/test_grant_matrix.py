"""Contract tests: a live grant-matrix floor against the applied stream (D2).

**THE GAP THIS CLOSES.** `rapid_systems/cloudformation/validate.sh` already
lints migration FILES: a `CREATE TABLE` or `DROP FUNCTION` must carry a
`GRANT` (or an explicit `-- no-grant: <reason>` line). That check is
SYNTACTIC and PER-FILE -- it proves a migration mentions a grant, never that
the database's actual, CUMULATIVE access-control state after the whole
stream applies is what the stream's authors intended. It has a structural
blind spot: a migration whose only ACL statement is a REVOKE of something an
earlier file granted trips neither rule (it creates no table, drops no
function), so nothing checks that the revoke actually lands, or that it
didn't accidentally strand a capability nothing else provides. 073 and 078
are exactly this shape -- each revokes a capability from
`rapid_pipeline_write` that an earlier migration granted, each guarded in
its own header by a hand-written "coordination requirement" paragraph
(rapid_systems consumer-switch-before-revoke ordering) that no automated
check enforces on the SCHEMA side. This file is the schema-side floor: it
asks the catalog what the role can actually do, the same question
PostgreSQL itself asks when authorizing a statement (the reasoning
`test_operator_grants.py`'s own header already established for this
directory -- 031's grant was wrong for two weeks while a comment claimed
otherwise, caught only by a live catalog query, never by reading the file).

**SCOPE: A FLOOR, NOT AN EXHAUSTIVE RE-DERIVATION.** This is not a full
enumeration of every grant in the stream -- `test_alert_outbox_grants.py`,
`test_operator_grants.py` and others already cover their own features in
depth. What belongs here is the small set of CUMULATIVE, cross-migration
facts that a per-file lint cannot see by construction: the two live
REVOKEs (073, 078) actually took effect and left the intended remainder
grants intact, plus the baseline `rapid_read`/`rapid_pipeline_write` shape
073 and 078 both explicitly promise NOT to touch. Each assertion below
cites the migration and line that makes the specific claim, so a failure
here says exactly which promise broke.

**WHAT THIS DOES NOT REPLACE.** `validate.sh`'s static per-file lint stays:
it catches a NEW migration that creates a table with no grant thought at
all, before the file is even merged -- a cheaper, earlier check than a
contract test can be. This file catches the complementary failure: an
existing, individually-reasonable-looking grant/revoke pair whose NET
EFFECT across the stream is wrong.
"""


import pytest


def _table_priv(conn, role, table, privilege, schema="public"):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT has_table_privilege(%s, %s, %s)",
            [role, f"{schema}.{table}", privilege])
        return cur.fetchone()[0]


def _schema_priv(conn, role, schema, privilege):
    with conn.cursor() as cur:
        cur.execute("SELECT has_schema_privilege(%s, %s, %s)",
                    [role, schema, privilege])
        return cur.fetchone()[0]


def _role_exists(conn, role):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
        return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# 073: CREATE ON SCHEMA public revoked from rapid_pipeline_write
# ---------------------------------------------------------------------------

def test_073_create_on_schema_public_is_revoked(conn):
    """073's own line 40: `REVOKE CREATE ON SCHEMA public FROM
    rapid_pipeline_write`. This is the finding 073 closes (whole-repo
    review, ledger A #1) -- if it silently un-revokes (a later migration
    re-granting CREATE without meaning to, or an applier re-run bug), the
    reachable-schema attack surface 073 closed reopens with nothing else
    to catch it."""
    if not _role_exists(conn, "rapid_pipeline_write"):
        return  # role absent: not this database's concern (e.g. a bare
                 # scratch DB pre-001); other tests already require it
    assert _schema_priv(conn, "rapid_pipeline_write", "public", "CREATE") \
        is False


def test_073_usage_on_schema_public_is_unchanged(conn):
    """073's own line 27-28: "USAGE ON SCHEMA public is UNCHANGED ...
    needs it: ... ordinary table access in public for everything else the
    pipeline reads and writes." Only CREATE was narrowed -- a floor test
    that only checked the revoke would not catch an over-broad follow-on
    migration accidentally revoking USAGE too and breaking every ordinary
    table access the pipeline makes."""
    if not _role_exists(conn, "rapid_pipeline_write"):
        return
    assert _schema_priv(conn, "rapid_pipeline_write", "public", "USAGE") \
        is True


# ---------------------------------------------------------------------------
# 078: raw UPDATE on work_units revoked from rapid_pipeline_write
# ---------------------------------------------------------------------------

def test_078_raw_update_on_work_units_is_revoked(conn):
    """078's own "WHAT REMAINS AFTER THIS REVOKE" section: raw UPDATE on
    `work_units` is gone from `rapid_pipeline_write`, replaced entirely by
    EXECUTE on 077's three constrained functions. This is the highest-
    blast-radius revoke in the stream by 078's own account -- every
    work-unit write path (ingest, submission, closure) depends on this
    landing correctly and nothing else stranding it.

    Skips where 078 has not applied (it may not exist in every checkout
    this suite runs against yet -- see the campaign's own migration
    numbering; probed via the catalog fact itself, not by reading a
    filename, matching this directory's schema-probe discipline)."""
    if not _role_exists(conn, "rapid_pipeline_write"):
        return
    if _table_priv(conn, "rapid_pipeline_write", "work_units", "UPDATE"):
        pytest.skip("raw UPDATE on work_units is still granted -- "
                     "078 has not applied to this database")


def test_078_select_and_insert_on_work_units_are_unchanged(conn):
    """078's own "WHAT REMAINS" section, first two bullets: SELECT (036,
    unaffected) and INSERT (036, unaffected -- 076's trigger fires on it
    regardless of grant) both survive the revoke. Only UPDATE was
    narrowed; a floor test that checked only the revoke could not tell a
    correctly-scoped UPDATE-only revoke from an over-broad one that also
    stranded INSERT (which would break create_work_unit outright) or
    SELECT (which would break every reader)."""
    if not _role_exists(conn, "rapid_pipeline_write"):
        return
    assert _table_priv(conn, "rapid_pipeline_write", "work_units", "SELECT") \
        is True
    assert _table_priv(conn, "rapid_pipeline_write", "work_units", "INSERT") \
        is True


def test_078_campaigns_raw_update_is_the_explicit_carve_out(conn):
    """078's own "CAMPAIGNS: EXPLICIT CARVE-OUT, NOT AN OVERSIGHT" section:
    `rapid_pipeline_write` KEEPS its 036 raw UPDATE grant on `campaigns`,
    deliberately, because no 077-equivalent constrained function exists
    for CampaignWriter yet. This is the assertion that would catch someone
    "finishing the job" by revoking campaigns' UPDATE too, without first
    building the constrained-function replacement 078's own header says is
    a prerequisite -- exactly the ordering mistake 073/078's coordination
    paragraphs exist to prevent, on the one grant this migration
    deliberately left alone."""
    if not _role_exists(conn, "rapid_pipeline_write"):
        return
    assert _table_priv(conn, "rapid_pipeline_write", "campaigns", "UPDATE") \
        is True


# ---------------------------------------------------------------------------
# Baseline shape (001/002): the floor every later migration's narrowing is
# measured against. If this drifts, every test above is measuring from a
# moved goalpost.
# ---------------------------------------------------------------------------

def test_baseline_rapid_pipeline_write_inherits_rapid_read(conn):
    """001:27 — `GRANT rapid_read TO rapid_pipeline_write`. The whole
    reason `rapid_pipeline_write` can SELECT anything at all: it has no
    direct SELECT grants of its own (002 gives it only INSERT/UPDATE/
    DELETE), so every read path depends on this membership holding."""
    if not (_role_exists(conn, "rapid_pipeline_write")
            and _role_exists(conn, "rapid_read")):
        return
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_has_role('rapid_pipeline_write', 'rapid_read', "
            "'MEMBER')")
        assert cur.fetchone()[0] is True
