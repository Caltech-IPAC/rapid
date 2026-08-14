"""Contract tests: `gc_fences` exercised AS the real runtime roles, not the
scratch superuser (brief H3/H4, closing the gap 085 fixes).

**THE GAP THIS FILE CLOSES.** `test_gc_fence_registration.py` proves
`pipeline.gc.fence`'s acquire/release/held_by semantics are correct SQL — but
every test there runs as the contract connection's owning superuser
(`fixture.connect()`), which bypasses every GRANT by construction, exactly as
`test_alert_outbox_grants.py`'s own header explains for `alert_outbox`. That
is precisely how migration 052's missing `UPDATE` grant on `gc_fences` (for
`rapid_pipeline_write`/`rapid_orchestrator`) and missing write grants
entirely (for `rapid_operator`, reached only through `rapid_read`'s
SELECT-only membership per 082) went undetected: GC execute was structurally
non-functional under the real production roles, and no contract test ever
ran as those roles to notice. 085 (`rapid_systems/cloudformation/
db-migrations/085-gc-fences-update-grant.sql`) is the fix — this file is the
regression coverage that would have caught the original defect and now
guards against it recurring.

**MECHANISM: `SET LOCAL ROLE`, matching `test_alert_outbox_grants.py`
exactly.** The contract connection is a superuser, which PostgreSQL exempts
unconditionally from `SET ROLE`'s membership check (docs: "database
superusers can set to any role"), so `SET LOCAL ROLE x` inside a transaction
makes every subsequent statement authorize as `x` would, then unwinds
automatically at ROLLBACK. Every test here follows that file's shape: BEGIN
(implicit), `SET LOCAL ROLE`, attempt the operation, assert, ROLLBACK — using
the SAME `_as_role_expect_success`/`_as_role_expect_insufficient_privilege`
helpers, copied rather than imported (this module has no dependency on
`test_alert_outbox_grants`, and duplicating two small helpers is cheaper than
inventing a shared import path across two independently-skippable files).

**WHY THESE DO NOT DUPLICATE `test_gc_fence_registration.py`.** That file's
tests differ from these by CONNECTION IDENTITY, not by SQL shape: same
`acquire_fence`/`release_fence` calls, same fresh/contested/expired-reclaim
scenarios, but run as the scratch superuser there and as `rapid_operator` /
`rapid_pipeline_write` / a grant-less role here. The property under test is
different — "is the SQL correct" versus "does the GRANT actually let the SQL
run" — and 085's own fix note is explicit that the two can silently disagree
(the reclaim arm's SQL was always correct; only the grant was missing).

**THE ASSERTION THAT WOULD HAVE CAUGHT THE ORIGINAL DEFECT.**
`test_role_with_no_gc_fences_grant_raises_rather_than_reporting_not_acquired`
below is that assertion: `rapid_read` holds SELECT only on `gc_fences` (052:
638, `GRANT SELECT ON gc_plans, gc_plan_items, gc_fences TO rapid_read` —
verified by reading 052's grant block directly, no INSERT/UPDATE/DELETE
anywhere in it for that role), so `acquire_fence` under `SET LOCAL ROLE
rapid_read` must hit `InsufficientPrivilege` on the INSERT and, per
`pipeline/gc/fence.py`'s post-fix contract (the module docstring: "a database
error here propagates rather than folding into the boolean"), that error
must RAISE out of `acquire_fence`, not be swallowed into a `False` that reads
identically to ordinary contention. The module docstring names exactly this
as the failure mode of the OLD code: a swallowed `InsufficientPrivilege`
would render as "the fence is always held", silently disabling GC execution
under the affected role while every run reports a clean "nothing to do".
Before 085, `rapid_operator` was in precisely this position for EVERY
acquire, not only the reclaim arm — this test's shape is what that defect
needed and never had.

**SKIP GUARD FOR SCHEMA PINS OLDER THAN 085.** CI builds from the pinned
`rapid_systems` stream; until that pin reaches 085, `gc_fences` exists (052)
but the roles hold the pre-085 grant set, under which several assertions
below (the reclaim-succeeds and negative-control-raises cases) would fail for
a reason that is "the schema pin is old", not "085 regressed" — the same
"probe the schema, don't assume" discipline `fixture.has_table` documents
elsewhere in this tier, applied to a GRANT fact instead of a table's
existence. `_require_085` probes `has_table_privilege(rapid_operator,
gc_fences, 'UPDATE')` directly (085's own new grant, absent from every prior
migration touching this table) rather than checking a schema_migrations row,
matching how draft-migration tests in this tier probe the catalog fact they
actually depend on rather than a version number.

**UNRUN UNTIL CI/rapid-admin — NOT EXECUTED THIS SESSION.** Auto-marked
`contract` by `pipeline/contract/conftest.py`'s directory-scoped hook;
excluded from the default `-m 'not contract and not live'` selection. Per
the project's execution rule (nothing executes locally for RAPID beyond
stub-tier no-I/O pytest), this file is verified by import and
`pytest --collect-only -m contract` only, in this worktree; the earliest
real run is CI or rapid-admin against a schema pin at or past 085.
"""

import pytest

from pipeline.contract import fixture
from pipeline.gc import fence as gc_fence

pytestmark = pytest.mark.contract

BUCKET = "roman-rapid-products"


def require_gc_fences_table(conn):
    if not fixture.has_table(conn, "gc_fences"):
        pytest.skip("DRAFT (gc_fences) is not applied")


def _role_exists(conn, role):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
        return cur.fetchone() is not None


def _require_role(conn, role):
    if not _role_exists(conn, role):
        pytest.skip("%s is not present in this database" % role)


def _require_085(conn):
    """Skip unless the 085 grant is actually present on `rapid_operator`.

    `has_table_privilege(rapid_operator, gc_fences, 'UPDATE')` is the one
    fact only 085 grants — no earlier migration touching `gc_fences` (052,
    082) gives `rapid_operator` UPDATE — so a True reading here is a direct
    catalog proof the pin includes 085, not an inference from a migration
    count or a schema_migrations row this tier does not otherwise consult.
    """
    if not _role_exists(conn, "rapid_operator"):
        pytest.skip("rapid_operator is not present in this database")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT has_table_privilege('rapid_operator', 'gc_fences',"
            " 'UPDATE')")
        has_085 = cur.fetchone()[0]
    if not has_085:
        pytest.skip(
            "the schema pin present predates migration 085 "
            "(rapid_operator lacks UPDATE on gc_fences); these role-scoped "
            "fence tests need 085's grant to exercise the post-fix "
            "contract and skip cleanly without it")


def _as_role_expect_success(conn, role, statement, params=None):
    """Attempt one statement AS `role` and assert PostgreSQL PERMITS it.

    Copied from `test_alert_outbox_grants.py`'s helper of the same name and
    docstring intent: `SET LOCAL ROLE`, scoped to the transaction, then the
    caller's own ROLLBACK unwinds it. Not wrapped in its own SAVEPOINT — an
    unexpected failure here should abort the test loudly, like any other
    unexpected exception.
    """
    from psycopg2 import sql

    with conn.cursor() as cur:
        cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
    cur = conn.cursor()
    cur.execute(statement, params)
    return cur


def _set_local_role(conn, role):
    """`SET LOCAL ROLE role`, for tests that then drive `acquire_fence`
    through the shared `pipeline.gc.fence` module rather than a bare
    statement — the role applies to every subsequent statement in the
    transaction, `acquire_fence`'s own INSERT included.
    """
    from psycopg2 import sql

    with conn.cursor() as cur:
        cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))


def _role_executor(conn):
    """The `execute(sql, params)` shape `pipeline.gc.fence` takes, WITHOUT a
    commit after every call.

    Deliberately different from `test_gc_fence_registration._conn_executor`,
    which commits after each statement: `SET LOCAL ROLE` is transaction-
    scoped, and a `conn.commit()` mid-test would end the transaction and
    silently drop back to the superuser identity for the next statement —
    exactly the mistake this tier's role-scoped tests must not make. Callers
    here manage their own transaction boundary (ROLLBACK at the end of every
    test, matching `test_alert_outbox_grants.py`'s discipline) instead.
    """
    def execute(statement, params=None):
        with conn.cursor() as cur:
            cur.execute(statement, params)
            if cur.description is not None:
                return cur.fetchall()
            return cur.rowcount
    return execute


def _age_fence_into_the_past(conn, bucket, object_key):
    """Move an already-acquired fence's lease into the past, AS THE
    SUPERUSER, so a role-scoped reclaim test has an expired row to reclaim
    without waiting out a real lease.

    Reused verbatim from `test_gc_fence_registration`'s own helper and its
    documented reasoning (`gc_fences_lease_ck` rules out `lease_seconds=0`
    or a negative lease as a shortcut). Run with `conn`'s ambient role at
    call time — every caller below invokes this BEFORE its own `SET LOCAL
    ROLE`, so it executes as the superuser regardless, matching the
    registration file's fixture-setup use of the identical helper.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE gc_fences"
            "   SET acquired_at = now() - interval '2 hours',"
            "       expires_at  = now() - interval '1 hour'"
            " WHERE bucket = %s AND object_key = %s", (bucket, object_key))
    conn.commit()


def _superuser_acquire(conn, bucket, object_key, holder, holder_kind,
                       lease_seconds=gc_fence.DEFAULT_LEASE_SECONDS):
    """Seed a fence row AS THE SUPERUSER, committed, before a role-scoped
    test begins — so the row exists independent of (and before) any `SET
    LOCAL ROLE` the test under it establishes.
    """
    def execute(statement, params=None):
        with conn.cursor() as cur:
            cur.execute(statement, params)
            if cur.description is not None:
                result = cur.fetchall()
            else:
                result = cur.rowcount
        conn.commit()
        return result
    return gc_fence.acquire_fence(
        execute, bucket=bucket, object_key=object_key, holder=holder,
        holder_kind=holder_kind, lease_seconds=lease_seconds)


def _superuser_release(conn, bucket, object_key, holder):
    def execute(statement, params=None):
        with conn.cursor() as cur:
            cur.execute(statement, params)
            if cur.description is not None:
                result = cur.fetchall()
            else:
                result = cur.rowcount
        conn.commit()
        return result
    gc_fence.release_fence(execute, bucket=bucket, object_key=object_key,
                           holder=holder)


# ============================================================================
# 1. rapid_operator: the role every `rapidctl gc-*` subcommand actually runs
#    as (session.py:114, SET ROLE rapid_operator) — 085's second gap.
# ============================================================================

def test_operator_fresh_acquire_succeeds(conn):
    """A fresh key (no conflicting row): the INSERT arm, as `rapid_operator`.

    Before 085, `rapid_operator` held SELECT only on `gc_fences` (via
    `rapid_read`, 082), so even this simplest case — no reclaim, no
    contention, a bare INSERT into an empty key — raised
    `InsufficientPrivilege`. 085 grants `SELECT, INSERT, UPDATE, DELETE`
    directly to `rapid_operator`; this is the direct proof the INSERT half
    of that grant landed.
    """
    require_gc_fences_table(conn)
    _require_role(conn, "rapid_operator")
    _require_085(conn)
    key = "science/r/u/role-operator-fresh/x.fits"
    try:
        _set_local_role(conn, "rapid_operator")
        execute = _role_executor(conn)
        assert gc_fence.acquire_fence(
            execute, bucket=BUCKET, object_key=key, holder="operator-1",
            holder_kind=gc_fence.HOLDER_GC), (
            "rapid_operator could not acquire a fresh fence despite 085's "
            "INSERT grant")
    finally:
        conn.rollback()


def test_operator_contested_acquire_returns_false(conn):
    """A LIVE conflicting fence: `rapid_operator` observes contention as an
    ordinary `False`, not an error — the genuine "not acquired" outcome
    `pipeline/gc/fence.py`'s docstring reserves for exactly this case.
    """
    require_gc_fences_table(conn)
    _require_role(conn, "rapid_operator")
    _require_085(conn)
    key = "science/r/u/role-operator-contested/x.fits"
    assert _superuser_acquire(conn, BUCKET, key, "other-holder",
                              gc_fence.HOLDER_GC, lease_seconds=300)
    try:
        _set_local_role(conn, "rapid_operator")
        execute = _role_executor(conn)
        assert not gc_fence.acquire_fence(
            execute, bucket=BUCKET, object_key=key, holder="operator-2",
            holder_kind=gc_fence.HOLDER_GC), (
            "rapid_operator's acquire succeeded against a live conflicting "
            "fence")
    finally:
        conn.rollback()
    _superuser_release(conn, BUCKET, key, "other-holder")


def test_operator_reclaims_an_expired_fence(conn):
    """THE RECLAIM ARM — the `ON CONFLICT (...) DO UPDATE` branch 052 made
    impossible for every role, `rapid_operator` included (085's header,
    gap 1: the DO UPDATE arm needs UPDATE privilege, which no earlier
    migration granted). This is the direct regression proof for that half
    of the fix, run as the role that actually executes `rapidctl gc-*`.
    """
    require_gc_fences_table(conn)
    _require_role(conn, "rapid_operator")
    _require_085(conn)
    key = "science/r/u/role-operator-reclaim/x.fits"
    assert _superuser_acquire(conn, BUCKET, key, "crashed-operator",
                              gc_fence.HOLDER_GC)
    _age_fence_into_the_past(conn, BUCKET, key)
    try:
        _set_local_role(conn, "rapid_operator")
        execute = _role_executor(conn)
        assert gc_fence.acquire_fence(
            execute, bucket=BUCKET, object_key=key, holder="operator-3",
            holder_kind=gc_fence.HOLDER_GC), (
            "rapid_operator could not reclaim an expired fence — the DO "
            "UPDATE arm 085 exists to unblock")
        held = gc_fence.held_by(execute, bucket=BUCKET, object_key=key)
        assert held is not None and held[0] == "operator-3"
    finally:
        conn.rollback()


def test_operator_release_works(conn):
    """`release_fence`'s DELETE, as `rapid_operator` — 085 grants DELETE on
    `gc_fences` to this role alongside SELECT/INSERT/UPDATE, so the full
    acquire-then-release cycle a real `rapidctl` invocation performs must
    complete under the role alone, with no superuser step in between.
    """
    require_gc_fences_table(conn)
    _require_role(conn, "rapid_operator")
    _require_085(conn)
    key = "science/r/u/role-operator-release/x.fits"
    try:
        _set_local_role(conn, "rapid_operator")
        execute = _role_executor(conn)
        assert gc_fence.acquire_fence(
            execute, bucket=BUCKET, object_key=key, holder="operator-4",
            holder_kind=gc_fence.HOLDER_GC)
        gc_fence.release_fence(execute, bucket=BUCKET, object_key=key,
                               holder="operator-4")
        assert gc_fence.held_by(execute, bucket=BUCKET, object_key=key) \
            is None, "rapid_operator's release did not remove its own fence"
    finally:
        conn.rollback()


# ============================================================================
# 2. rapid_pipeline_write: registration's own role
#    (pipeline.registration.consumer._bind_fence runs under this role in
#    production) — 085's first gap, the UPDATE-for-the-reclaim-arm half.
# ============================================================================

def test_pipeline_write_fresh_acquire_succeeds(conn):
    """052 already granted `rapid_pipeline_write` SELECT/INSERT/DELETE on
    `gc_fences` (052:632) — so the plain INSERT arm was never broken for
    this role. Kept here as the baseline the reclaim test below contrasts
    against, under the identical role.
    """
    require_gc_fences_table(conn)
    _require_role(conn, "rapid_pipeline_write")
    key = "science/r/u/role-pipeline-write-fresh/x.fits"
    try:
        _set_local_role(conn, "rapid_pipeline_write")
        execute = _role_executor(conn)
        assert gc_fence.acquire_fence(
            execute, bucket=BUCKET, object_key=key, holder="writer-1",
            holder_kind=gc_fence.HOLDER_REGISTRATION)
    finally:
        conn.rollback()


def test_pipeline_write_reclaims_an_expired_fence(conn):
    """THE RECLAIM ARM for `rapid_pipeline_write` — 085's header, gap 1,
    naming this role by name: 052 granted SELECT/INSERT/DELETE only, no
    UPDATE, so `_bind_fence`'s crash-recovery path (a registrar that
    crashed mid-bind, its expired fence reclaimed by the NEXT registration
    attempt) was structurally unreachable under this exact role before 085.
    """
    require_gc_fences_table(conn)
    _require_role(conn, "rapid_pipeline_write")
    _require_085(conn)
    key = "science/r/u/role-pipeline-write-reclaim/x.fits"
    assert _superuser_acquire(conn, BUCKET, key, "crashed-registrar",
                              gc_fence.HOLDER_REGISTRATION)
    _age_fence_into_the_past(conn, BUCKET, key)
    try:
        _set_local_role(conn, "rapid_pipeline_write")
        execute = _role_executor(conn)
        assert gc_fence.acquire_fence(
            execute, bucket=BUCKET, object_key=key, holder="writer-2",
            holder_kind=gc_fence.HOLDER_REGISTRATION), (
            "rapid_pipeline_write could not reclaim an expired fence — "
            "085's UPDATE grant is what makes the DO UPDATE arm reachable")
        held = gc_fence.held_by(execute, bucket=BUCKET, object_key=key)
        assert held is not None and held[0] == "writer-2"
    finally:
        conn.rollback()


# ============================================================================
# 3. THE NEGATIVE CONTROL — the assertion that would have caught the
#    original defect. A role with NO write grant on gc_fences must RAISE,
#    never quietly return False (the post-fix contract this whole gap was
#    about).
# ============================================================================

def test_role_with_no_gc_fences_grant_raises_rather_than_reporting_not_acquired(
        conn):
    """`rapid_read` holds SELECT ONLY on `gc_fences` (052:638 — `GRANT
    SELECT ON gc_plans, gc_plan_items, gc_fences TO rapid_read`; verified by
    reading 052's grant block directly, no INSERT/UPDATE/DELETE anywhere in
    it for this role, and 085 does not touch `rapid_read` at all). Under
    `SET LOCAL ROLE rapid_read`, `acquire_fence`'s INSERT has no grant to
    run on at all, so PostgreSQL raises `InsufficientPrivilege` before the
    `ON CONFLICT` clause is ever reached.

    THIS IS THE ASSERTION THAT WOULD HAVE CAUGHT THE ORIGINAL DEFECT. Before
    `pipeline/gc/fence.py`'s fix (see the module's own docstring, "THE
    STATIC-GRANT CASE THIS MUST NOT MASK"), `acquire_fence` swallowed every
    database error into the same `False` a live conflicting row produces —
    which means a grant gap and ordinary contention were INDISTINGUISHABLE
    from the caller's side. `rapid_operator` sat in exactly this position
    for every acquire before 085 (082 gave it SELECT only, via `rapid_read`
    membership): every call would have returned `False`, every GC item would
    have been skipped as "fenced", and the run would have reported a clean
    "nothing to do" with no error anywhere — silently disabling GC execution
    under that role. A contract test suite that only ever ran as the
    superuser (bypassing every grant by construction, `test_gc_fence_
    registration.py`'s whole tier) could never have observed this regardless
    of which behaviour `acquire_fence` implemented, because the superuser
    was never subject to the missing grant in the first place. This test
    closes that blind spot: it runs as a role that GENUINELY lacks the
    grant, and asserts the post-fix contract — raise, don't mask — directly.
    """
    require_gc_fences_table(conn)
    _require_role(conn, "rapid_read")
    key = "science/r/u/role-no-grant-negative-control/x.fits"
    import psycopg2
    try:
        _set_local_role(conn, "rapid_read")
        execute = _role_executor(conn)
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            gc_fence.acquire_fence(
                execute, bucket=BUCKET, object_key=key, holder="reader-1",
                holder_kind=gc_fence.HOLDER_GC)
    finally:
        conn.rollback()


# ============================================================================
# 4. Cross-role contention: the fence is exclusive per KEY, not per
#    (key, holder_kind) or per calling role — a live fence acquired under
#    one role's SET ROLE blocks acquisition under the other, both
#    directions.
# ============================================================================

def test_fence_held_by_operator_blocks_pipeline_write_acquire(conn):
    """`rapid_operator` (GC's operate-tier role) holds the key live;
    `rapid_pipeline_write` (registration's role) must be refused — the
    SAME cross-holder-kind exclusivity `test_gc_fence_registration.
    test_acquire_fence_is_exclusive_across_holder_kinds` proves as the
    superuser, reproduced here across the two REAL runtime roles.
    """
    require_gc_fences_table(conn)
    _require_role(conn, "rapid_operator")
    _require_role(conn, "rapid_pipeline_write")
    _require_085(conn)
    key = "science/r/u/role-cross-operator-blocks-writer/x.fits"
    try:
        _set_local_role(conn, "rapid_operator")
        execute = _role_executor(conn)
        assert gc_fence.acquire_fence(
            execute, bucket=BUCKET, object_key=key, holder="operator-cross",
            holder_kind=gc_fence.HOLDER_GC, lease_seconds=300)

        _set_local_role(conn, "rapid_pipeline_write")
        execute = _role_executor(conn)
        assert not gc_fence.acquire_fence(
            execute, bucket=BUCKET, object_key=key, holder="writer-cross",
            holder_kind=gc_fence.HOLDER_REGISTRATION), (
            "rapid_pipeline_write acquired a key rapid_operator's live "
            "fence held")
    finally:
        conn.rollback()


def test_fence_held_by_pipeline_write_blocks_operator_acquire(conn):
    """The other direction: `rapid_pipeline_write` (registration) holds the
    key live; `rapid_operator` (GC's operate tier) must be refused. Both
    directions matter separately — the fence's exclusivity must not depend
    on which role happened to ask first.
    """
    require_gc_fences_table(conn)
    _require_role(conn, "rapid_operator")
    _require_role(conn, "rapid_pipeline_write")
    _require_085(conn)
    key = "science/r/u/role-cross-writer-blocks-operator/x.fits"
    try:
        _set_local_role(conn, "rapid_pipeline_write")
        execute = _role_executor(conn)
        assert gc_fence.acquire_fence(
            execute, bucket=BUCKET, object_key=key, holder="writer-cross-2",
            holder_kind=gc_fence.HOLDER_REGISTRATION, lease_seconds=300)

        _set_local_role(conn, "rapid_operator")
        execute = _role_executor(conn)
        assert not gc_fence.acquire_fence(
            execute, bucket=BUCKET, object_key=key,
            holder="operator-cross-2", holder_kind=gc_fence.HOLDER_GC), (
            "rapid_operator acquired a key rapid_pipeline_write's live "
            "fence held")
    finally:
        conn.rollback()
