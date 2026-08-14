"""Contract tests: the applier's ledger/checksum properties (D1).

**THE GAP THIS CLOSES.** Until now, CI's contract job applied the migration
stream through `scripts/run-contract-tests.sh`'s own loop -- `psql -f "$f"`
then a bare `INSERT INTO schema_migrations (filename) VALUES (...) ON
CONFLICT DO NOTHING` -- which reproduces the SCHEMA the real applier
(`rapid_systems/cloudformation/apply-db-migrations.sh`) produces but exercises
NONE of the applier's own ledger logic: no checksum recorded, no checksum
compared, no refusal on a changed already-applied file. Those properties have
never run in CI.

**WHY A TEST HERE, NOT A RUN OF THE REAL SCRIPT.** The applier is bash,
lives in `rapid_systems` (a different, private repo), and its pending-files
sequence is built for SSM delivery to a podman container on rapid-admin --
it stages files through a `STAGE_DIR` a remote host populates, shells out to
`podman exec`, and expects AWS credentials this workflow deliberately holds
none of (see `.github/workflows/contract-tests.yml`'s own header). Vendoring
or invoking it verbatim is out of reach for a public-repo, credential-free CI
job. What CAN run here is the SQL SHAPE of its ledger logic -- the exact
statements `apply-db-migrations.sh` builds into its combined script (read
there in full before touching this file): an `INSERT ... ON CONFLICT
(filename) DO NOTHING`, a `SELECT sha256 FROM schema_migrations WHERE
filename = ...` compared against the staged file's own hash, and a hard
refusal when they disagree. `_apply_via_ledger` below is that SQL, restated
as the three statements it is (not a re-implementation of the applier's
staging/locking/heredoc-escaping machinery, which is what would make this a
test of a re-implementation instead of a test of the property).

**WHY THIS TESTS THE PROPERTY RATHER THAN THE EASIEST THING TO WIRE.** The
easy option was a test that CI's own loop's current behavior is unchanged --
but that loop is exactly what does NOT exercise checksum verification, so
such a test would prove nothing about the gap. The chosen shape drives the
ledger through the same statements the real applier issues, against a real
`schema_migrations` table with its real `sha256 ~ '^[0-9a-f]{64}$'` CHECK
(070) and real `filename` PRIMARY KEY (000) -- so a change to either
constraint, or to the applier's SQL shape drifting from what is asserted
here, is caught by PostgreSQL enforcing the same constraints production
does, not by Python re-deriving what "should" happen.

**SCOPED TO THE LEDGER, NOT THE ADVISORY LOCK.** The applier also wraps its
whole pending-files sequence in a `pg_try_advisory_lock` so two concurrent
runs cannot race the apply/record gap. That property needs two genuinely
concurrent applier invocations to demonstrate (the existing concurrency
tests in this directory -- `test_association_claim_order.py`,
`test_work_unit_cas.py` -- use `second_conn` for exactly that shape) and is
a distinct property from the one this file tests. Left for a follow-on; not
silently assumed to be covered here.

**TEMP-TABLE SHADOWING**, the same technique `test_schema_preflight.py`
uses: the real `schema_migrations` carries the rows CI's own stream-apply
step wrote (every other contract test that reads it depends on those rows
being present and unmodified), so these tests shadow it with a session-local
temp table rather than mutate or truncate the shared one.
"""

import hashlib

import pytest


class AlreadyAppliedWithChangedHash(Exception):
    """Mirrors apply-db-migrations.sh's hard refusal (fix #13, migration 070):
    an already-applied migration whose recorded sha256 disagrees with the
    staged file's own hash must never be silently re-applied."""


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _apply_via_ledger(conn, filename, content):
    """The three statements apply-db-migrations.sh's combined script issues
    for one pending file, restated directly (not through the bash/SSM/podman
    machinery around them -- see the module docstring):

      1. is `filename` already recorded, and with what sha256?
      2. if recorded with a non-NULL sha256 that disagrees with the staged
         file's hash: refuse, exactly as the applier's
         `!! ... an already-applied migration must never be edited` does.
      3. otherwise: apply `content` (this test's stand-in for the file's own
         SQL body) and record filename+sha256 in the SAME statement sequence
         the real applier uses -- `INSERT ... ON CONFLICT (filename) DO
         NOTHING`, then a NULL-only sha256 backfill -- never a separate,
         un-atomic step.

    Returns "applied" or "skipped" (already applied, unchanged).
    """
    fsha = _sha256(content)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sha256 FROM schema_migrations WHERE filename = %s",
            [filename])
        row = cur.fetchone()

    if row is not None:
        recorded_sha = row[0]
        if recorded_sha is not None and recorded_sha != fsha:
            raise AlreadyAppliedWithChangedHash(
                f"{filename} was already applied with sha256 {recorded_sha} "
                f"but now hashes to {fsha} -- an already-applied migration "
                f"must never be edited; refusing to proceed")
        return "skipped"

    with conn.cursor() as cur:
        # `content` stands in for the migration file's own SQL body; the
        # ledger tests below pass an inert statement so the assertion stays
        # on the LEDGER's behavior, not on any particular migration's DDL.
        cur.execute(content)
        cur.execute(
            "INSERT INTO schema_migrations (filename, sha256) VALUES (%s, %s)"
            " ON CONFLICT (filename) DO NOTHING",
            [filename, fsha])
        cur.execute(
            "UPDATE schema_migrations SET sha256 = %s"
            " WHERE filename = %s AND sha256 IS NULL",
            [fsha, filename])
    return "applied"


@pytest.fixture
def shadow_ledger(conn):
    """A session-local temp table standing in for `schema_migrations`, so
    these tests can drive real INSERT/UPDATE/CHECK-constraint behavior
    without touching the rows the rest of the contract suite depends on."""
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE schema_migrations"
                    " (LIKE public.schema_migrations INCLUDING ALL)")
    try:
        yield conn
    finally:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE schema_migrations")
        conn.commit()


def test_first_apply_records_filename_and_sha256(shadow_ledger):
    conn = shadow_ledger
    outcome = _apply_via_ledger(conn, "999-probe.sql", "SELECT 1")
    assert outcome == "applied"

    with conn.cursor() as cur:
        cur.execute("SELECT sha256 FROM schema_migrations"
                    " WHERE filename = %s", ["999-probe.sql"])
        (recorded,) = cur.fetchone()
    assert recorded == _sha256("SELECT 1")


def test_unchanged_reapply_is_skipped_not_reexecuted(shadow_ledger):
    """The idempotent path apply-db-migrations.sh's own header names: a
    re-run only applies what is new. A second call with IDENTICAL content
    must not re-execute the file's SQL -- proven here by a statement that
    would fail on a second execution (CREATE TABLE with no IF NOT EXISTS),
    which only a genuine skip survives."""
    conn = shadow_ledger
    content = "CREATE TEMP TABLE probe_999 (x int)"
    first = _apply_via_ledger(conn, "999-probe.sql", content)
    assert first == "applied"

    second = _apply_via_ledger(conn, "999-probe.sql", content)
    assert second == "skipped"          # would raise DuplicateTable otherwise


def test_changed_hash_on_an_already_applied_file_is_refused(shadow_ledger):
    """The hard refusal (070's whole reason for existing): editing a file
    that already ran must be caught, not silently re-applied identically to
    a new one."""
    conn = shadow_ledger
    _apply_via_ledger(conn, "999-probe.sql", "SELECT 1")

    with pytest.raises(AlreadyAppliedWithChangedHash) as caught:
        _apply_via_ledger(conn, "999-probe.sql", "SELECT 2")   # edited body
    message = str(caught.value)
    assert "999-probe.sql" in message
    assert _sha256("SELECT 1") in message


def test_a_null_recorded_sha256_is_nothing_to_check_against(shadow_ledger):
    """070's own column comment: rows applied before the checksum column
    existed (001-069) carry a NULL sha256, and the applier must treat that
    as 'nothing to check against', never as a mismatch -- a pre-070
    deployment must not fail closed the moment 070 lands."""
    conn = shadow_ledger
    with conn.cursor() as cur:
        cur.execute("INSERT INTO schema_migrations (filename, sha256)"
                    " VALUES (%s, NULL)", ["061-schema-qualify-definer-bodies.sql"])

    # A "re-apply" of a pre-070 file with a NULL recorded hash must be
    # treated as already-applied (skipped), not as a mismatch to refuse.
    outcome = _apply_via_ledger(
        conn, "061-schema-qualify-definer-bodies.sql", "SELECT 1")
    assert outcome == "skipped"


def test_sha256_must_match_070s_shape_constraint(shadow_ledger):
    """The ledger logic is asserted against the REAL constraint (070), not a
    Python re-derivation of it: a malformed hash must be rejected by
    PostgreSQL itself, proving 070's CHECK is armed on whatever schema this
    suite built the stream against."""
    conn = shadow_ledger
    with conn.cursor() as cur:
        with pytest.raises(Exception) as caught:
            cur.execute(
                "INSERT INTO schema_migrations (filename, sha256)"
                " VALUES (%s, %s)", ["999-probe.sql", "not-a-valid-hash"])
    assert "schema_migrations_sha256_shape_ck" in str(caught.value)
    conn.rollback()
