"""Contract tests: immutability and grants on `alert_outbox` and
`delivery_policies` (brief E, acceptance criterion 7).

    "pipeline writer is INSERT-only — cannot UPDATE any column (state/claim/
     ack included) and cannot DELETE any row, PENDING included; publisher can
     UPDATE state columns, cannot touch payload/identity/schema-UUID, cannot
     INSERT/DELETE outbox rows, cannot write policies; SENT/REFUSED rows
     undeletable by either (trigger, not grant, is the backstop)."

**THESE SKIP WHERE DRAFT MIGRATION 050 IS ABSENT.** `migrations-draft/
050-alert-outbox-and-publisher.sql` is a proposed change request against
`rapid_systems`, not part of the authoritative stream — CI builds from that
stream alone and skips these; the rapid-admin acceptance run applies base +
drafts and runs them for real, exactly the "probe the schema, don't assume"
discipline `fixture.has_table` documents.

**WHY THE GRANTS ARE ASSERTED AGAINST THE CATALOG, NOT READ FROM 050's SQL.**
`test_operator_grants.py` records the reason this whole tier exists: 031's
own header carried a grant that was wrong for two weeks while a comment two
lines above it claimed otherwise, caught only by a rehearsal QUERYING
`pg_proc`/`pg_class`, never by reading the migration. A grant is a fact
about the catalog (`has_table_privilege`, `has_column_privilege`), and the
only way to know it is to ask the catalog the same question the database
itself asks when it authorizes a statement.

**WHY THE TRIGGER TESTS ARE BEHAVIOURAL, RUN AS THE CONNECTED SUPERUSER.**
050's own comment on the envelope-immutability trigger says why: "the table
owner and any SECURITY DEFINER function would bypass [column grants]", so a
grant-only test would prove nothing about the guarantee the acceptance
criterion actually needs — that immutability holds for EVERY role, owner
included. The contract connection (`fixture.connect()`) authenticates as the
scratch database's owning superuser, which is exactly the role every grant
in 050 is deliberately written to still refuse. Provoking the trigger from
here is the strongest test this tier can write for it.
"""

import hashlib

import psycopg2
import pytest

from pipeline.contract import fixture

#: The DISPATCH ENVELOPE columns 050's trigger freezes against every role,
#: including the owner. Mirrors the trigger function's own IS DISTINCT FROM
#: list line for line, so a column added to one and not the other is exactly
#: the drift this suite exists to catch.
ENVELOPE_COLUMNS = (
    "alert_id", "identity_basis", "payload", "payload_checksum",
    "schema_version_id", "topic", "release_identity", "corrects_alert_id",
    "created_at",
)

#: The state/claim/ack columns the publisher's column-level UPDATE grant
#: names — the only columns it may write. Copied from the GRANT UPDATE (...)
#: list in 050 itself, for the same "assert against the fact, not the
#: source" reason as ENVELOPE_COLUMNS above: if this list and the grant ever
#: disagree, this test is what notices.
PUBLISHER_WRITABLE_COLUMNS = (
    "state", "claim_token", "claimed_at", "sent_at", "broker_metadata",
    "refusal_reason", "resend_count",
)


def _require_schema(conn):
    if not fixture.has_table(conn, "alert_outbox"):
        pytest.skip("DRAFT migration 050 is not applied on this database; "
                     "the alert_outbox grant-and-immutability tests need it "
                     "and skip cleanly without it")


def _role_exists(conn, role):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
        return cur.fetchone() is not None


def _table_priv(conn, role, table, privilege):
    """Does `role` hold `privilege` on `table`, by any path (membership too)?

    `has_table_privilege` is the same function PostgreSQL's own executor
    consults to authorize a statement, so a role that were owner-by-
    membership or reached the privilege through a group grant would still
    show up here — the same reason `test_operator_grants.py` uses
    `has_function_privilege` rather than reading `pg_class.relacl` by hand.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT has_table_privilege(%s, %s, %s)",
                    [role, table, privilege])
        return cur.fetchone()[0]


def _column_priv(conn, role, table, column, privilege):
    """Does `role` hold `privilege` on one COLUMN of `table`?

    `has_column_privilege` is what the publisher's narrow grant is actually
    tested against: 050 grants `UPDATE (state, claim_token, ...)`, a
    column-level ACL entry that `has_table_privilege(role, table, 'UPDATE')`
    cannot see at all (it answers only for table-wide grants), which is why
    criterion 7 has to be asserted per column rather than per table.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT has_column_privilege(%s, %s, %s, %s)",
                    [role, table, column, privilege])
        return cur.fetchone()[0]


# ============================================================================
# 1. rapid_pipeline_write: INSERT-only on alert_outbox
# ============================================================================

def test_pipeline_writer_holds_insert_but_not_update_or_delete(conn):
    """The writer that produces packets has no further business with them.

    050's own comment states the risk this closes: a pipeline bug that could
    UPDATE or DELETE its own outbox rows would look, from a consumer's
    vantage point, exactly like alerts that were never produced — silently,
    because the row's absence or mutation leaves no trace of what happened.
    """
    _require_schema(conn)
    if not _role_exists(conn, "rapid_pipeline_write"):
        pytest.skip("rapid_pipeline_write is not present in this database")

    assert _table_priv(conn, "rapid_pipeline_write", "alert_outbox",
                       "INSERT") is True
    assert _table_priv(conn, "rapid_pipeline_write", "alert_outbox",
                       "UPDATE") is False, (
        "rapid_pipeline_write holds UPDATE on alert_outbox; the writer must "
        "not be able to move a row's state (that is the publisher's "
        "protocol) or touch ack columns (it has no acknowledgements)")
    assert _table_priv(conn, "rapid_pipeline_write", "alert_outbox",
                       "DELETE") is False, (
        "rapid_pipeline_write holds DELETE on alert_outbox; deleting a "
        "PENDING row it wrote would look exactly like an alert that was "
        "never produced")


def test_pipeline_writer_holds_no_update_on_any_individual_column(conn):
    """The table-level revoke, reconfirmed column by column.

    `has_table_privilege(..., 'UPDATE')` being false does not by itself rule
    out a stray column-level UPDATE grant sitting alongside it — the two ACL
    kinds are independent — so criterion 7's "cannot UPDATE any column" is
    checked one column at a time, state/claim/ack included, exactly as the
    criterion names them.
    """
    _require_schema(conn)
    if not _role_exists(conn, "rapid_pipeline_write"):
        pytest.skip("rapid_pipeline_write is not present in this database")

    for column in ENVELOPE_COLUMNS + PUBLISHER_WRITABLE_COLUMNS:
        assert _column_priv(conn, "rapid_pipeline_write", "alert_outbox",
                            column, "UPDATE") is False, (
            "rapid_pipeline_write holds UPDATE on alert_outbox.%s" % column)


def test_pipeline_writer_can_call_the_insert_function(conn):
    """The one write path the writer needs, granted explicitly.

    INSERT-only on the table plus EXECUTE on `insert_alert_outbox_packet` is
    the whole of the writer's outbox posture; a writer holding table INSERT
    but denied the function would be unable to write through the collision
    guard at all, which would be a different (and self-defeating) defect
    from the one this file's other tests check for.
    """
    _require_schema(conn)
    if not _role_exists(conn, "rapid_pipeline_write"):
        pytest.skip("rapid_pipeline_write is not present in this database")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT has_function_privilege(%s, "
            "  'insert_alert_outbox_packet(text, text, bytea, text, uuid, "
            "   text, text, bigint, smallint, bigint, text)', 'EXECUTE')",
            ["rapid_pipeline_write"])
        assert cur.fetchone()[0] is True


def test_pipeline_writer_reads_policies_but_cannot_write_them(conn):
    """The pipeline records whether a release is authorized; it never decides.

    Read access lets it stamp that fact alongside the packet at outbox-write
    time; any write access would let a production code path change WHICH
    releases may deliver, a decision 050 reserves for an operator with a
    recorded reason and actor.
    """
    _require_schema(conn)
    if not _role_exists(conn, "rapid_pipeline_write"):
        pytest.skip("rapid_pipeline_write is not present in this database")

    assert _table_priv(conn, "rapid_pipeline_write", "delivery_policies",
                       "SELECT") is True
    for privilege in ("INSERT", "UPDATE", "DELETE"):
        assert _table_priv(conn, "rapid_pipeline_write", "delivery_policies",
                           privilege) is False, (
            "rapid_pipeline_write holds %s on delivery_policies" % privilege)


# ============================================================================
# 2. rapid_publisher: SELECT both tables, UPDATE state/claim/ack ONLY
# ============================================================================

def test_publisher_holds_select_on_both_tables(conn):
    """The publisher reads packets to send and policies to gate the send."""
    _require_schema(conn)
    if not _role_exists(conn, "rapid_publisher"):
        pytest.skip("rapid_publisher is not present in this database")

    assert _table_priv(conn, "rapid_publisher", "alert_outbox",
                       "SELECT") is True
    assert _table_priv(conn, "rapid_publisher", "delivery_policies",
                       "SELECT") is True


def test_publisher_holds_update_on_exactly_the_state_claim_ack_columns(conn):
    """The column boundary IS the contract: state and ack, never the wire.

    050's comment names this explicitly — "the publisher's whole contract is
    that it moves state and never changes what goes on the wire" — so this
    test asserts BOTH halves of that sentence: every writable column holds
    the grant (the state machine and finalization would otherwise be unable
    to run at all) and every envelope column does not (a widened grant here
    would be the exact defect the trigger exists to survive, but the grant
    should still say what it means).
    """
    _require_schema(conn)
    if not _role_exists(conn, "rapid_publisher"):
        pytest.skip("rapid_publisher is not present in this database")

    for column in PUBLISHER_WRITABLE_COLUMNS:
        assert _column_priv(conn, "rapid_publisher", "alert_outbox",
                            column, "UPDATE") is True, (
            "rapid_publisher lacks UPDATE on alert_outbox.%s, one of the "
            "state/claim/ack columns its protocol needs to write" % column)

    for column in ENVELOPE_COLUMNS:
        assert _column_priv(conn, "rapid_publisher", "alert_outbox",
                            column, "UPDATE") is False, (
            "rapid_publisher holds UPDATE on alert_outbox.%s, a dispatch-"
            "envelope column; the publisher must never be able to change "
            "what goes on the wire, only whether and when" % column)


def test_publisher_holds_neither_insert_nor_delete_on_alert_outbox(conn):
    """The publisher drains the outbox; it does not populate or empty it.

    INSERT belongs to the pipeline writer alone (a second inserter would be
    a second, ungoverned way for packets to appear); DELETE would let the
    publisher erase a row's evidence of its own accord, which is exactly
    what the undeletable-when-delivered trigger below exists to prevent
    regardless of any grant.
    """
    _require_schema(conn)
    if not _role_exists(conn, "rapid_publisher"):
        pytest.skip("rapid_publisher is not present in this database")

    assert _table_priv(conn, "rapid_publisher", "alert_outbox",
                       "INSERT") is False
    assert _table_priv(conn, "rapid_publisher", "alert_outbox",
                       "DELETE") is False


def test_publisher_holds_no_write_of_any_kind_on_delivery_policies(conn):
    """Authorization is an operator decision; the publisher only consults it.

    A publisher that could write its own policy row could authorize its own
    deliveries, which would make delivery_policies a record of what the
    publisher decided rather than what an operator decided — the opposite of
    what a default-DENY authorization table is for.
    """
    _require_schema(conn)
    if not _role_exists(conn, "rapid_publisher"):
        pytest.skip("rapid_publisher is not present in this database")

    for privilege in ("INSERT", "UPDATE", "DELETE"):
        assert _table_priv(conn, "rapid_publisher", "delivery_policies",
                           privilege) is False, (
            "rapid_publisher holds %s on delivery_policies" % privilege)


# ============================================================================
# 3. THE TRIGGER IS THE BACKSTOP — behavioural, run as the connected
#    superuser, which bypasses every grant above by construction
# ============================================================================

# THE FIXTURE UUIDs CARRY A HEX LETTER IN THEIR LAST SEGMENT, deliberately. A
# UUID's final group is twelve hex characters, and an all-numeric one is
# indistinguishable from an AWS account id to `.githooks/pre-push`, which hard
# blocks any twelve-digit run in this PUBLIC repository with no allowlist. The
# honest fix is the one `fixture.py` already reached for its synthetic ARNs:
# stop writing account-shaped strings, rather than weaken a guard that is
# protecting a public repo. (This comment cannot spell the example either.)
def _insert_packet(conn, alert_id, payload=b"packet-bytes",
                   schema_version_id="00000000-0000-0000-0000-a00000000001"):
    """One alert_outbox row through the real insert function.

    Through `insert_alert_outbox_packet`, not a bare INSERT: that function
    is the collision guard under test elsewhere in this file, and a
    hand-written INSERT here would build rows the production insert path
    never builds. `RUN_TAG`-scoped `alert_id` so concurrent or repeated
    suite runs never collide on the table's own UNIQUE constraint.
    """
    checksum = "sha256:" + hashlib.sha256(payload).hexdigest()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT insert_alert_outbox_packet("
            "  %s, 'product-key', %s, %s, %s::uuid, 'alerts.live', 'live',"
            "  1, 1, NULL)",
            [alert_id, psycopg2.Binary(payload), checksum, schema_version_id])
        return cur.fetchone()[0]


def _outbox_alert_id(name):
    return f"outbox-{fixture.RUN_TAG}-{name}"


def test_envelope_columns_cannot_be_rewritten_even_by_the_owner(conn):
    """The dispatch envelope is write-once against EVERY role, owner included.

    This is the test brief E's acceptance criterion 7 names as the critical
    one: column grants alone cannot make this guarantee, because the table
    owner and any SECURITY DEFINER function bypass them outright. The
    contract connection authenticates as the scratch database's owner, so a
    trigger that raised here is a trigger that would raise for anyone.
    Each column is attempted, and each attempt gets its own SAVEPOINT: a
    RAISE aborts the enclosing transaction in PostgreSQL, so without a
    savepoint the second attempt would fail on "current transaction is
    aborted" rather than on the property under test.
    """
    _require_schema(conn)
    alert_id = _outbox_alert_id("envelope")
    _insert_packet(conn, alert_id)
    conn.commit()

    attempts = (
        ("payload", "UPDATE alert_outbox SET payload = %s"
                    " WHERE alert_id = %s", [psycopg2.Binary(b"different")]),
        ("alert_id", "UPDATE alert_outbox SET alert_id = %s"
                     " WHERE alert_id = %s", [_outbox_alert_id("rewritten")]),
        ("schema_version_id",
         "UPDATE alert_outbox SET schema_version_id = %s WHERE alert_id = %s",
         ["00000000-0000-0000-0000-a00000000002"]),
    )
    try:
        for label, statement, params in attempts:
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT envelope_attempt")
            with pytest.raises(psycopg2.Error) as raised:
                with conn.cursor() as cur:
                    cur.execute(statement, params + [alert_id])
            assert "write-once" in str(raised.value), (
                "the %s rewrite failed for a reason other than the "
                "write-once trigger: %s" % (label, raised.value))
            with conn.cursor() as cur:
                cur.execute("ROLLBACK TO SAVEPOINT envelope_attempt")
    finally:
        conn.rollback()

    with conn.cursor() as cur:
        cur.execute("DELETE FROM alert_outbox WHERE alert_id = %s",
                    [alert_id])
    conn.commit()


def test_a_sent_row_cannot_be_deleted_even_by_the_owner(conn):
    """Delivery evidence is undeletable, and the trigger says why in its text.

    A SENT row is the only record that a packet reached the broker; erasing
    it would erase the evidence itself, not merely a copy of it.
    """
    _require_schema(conn)
    alert_id = _outbox_alert_id("sent")
    _insert_packet(conn, alert_id)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE alert_outbox SET state = 'IN_FLIGHT',"
            "   claim_token = 'tok', claimed_at = now()"
            " WHERE alert_id = %s", [alert_id])
        cur.execute(
            "UPDATE alert_outbox SET state = 'SENT', sent_at = now(),"
            "   claim_token = NULL, claimed_at = NULL"
            " WHERE alert_id = %s", [alert_id])
    conn.commit()

    with pytest.raises(psycopg2.Error) as raised:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM alert_outbox WHERE alert_id = %s",
                        [alert_id])
    assert "cannot be deleted" in str(raised.value)
    conn.rollback()

    with conn.cursor() as cur:
        cur.execute("SELECT state FROM alert_outbox WHERE alert_id = %s",
                    [alert_id])
        assert cur.fetchone()[0] == "SENT", (
            "the row is gone after the DELETE was supposed to fail")
        cur.execute("DELETE FROM alert_outbox WHERE alert_id = %s",
                    [alert_id])
    conn.commit()


def test_a_refused_row_cannot_be_deleted_even_by_the_owner(conn):
    """The other delivery-decision state the trigger protects.

    REFUSED is the evidence a packet was DELIBERATELY not delivered; losing
    it would be indistinguishable from losing the SENT case above — both are
    "the delivery decision was made and now nobody can see what it was".
    """
    _require_schema(conn)
    alert_id = _outbox_alert_id("refused")
    _insert_packet(conn, alert_id)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE alert_outbox SET state = 'IN_FLIGHT',"
            "   claim_token = 'tok', claimed_at = now()"
            " WHERE alert_id = %s", [alert_id])
        cur.execute(
            "UPDATE alert_outbox SET state = 'REFUSED',"
            "   refusal_reason = 'topic authorization denied',"
            "   claim_token = NULL, claimed_at = NULL"
            " WHERE alert_id = %s", [alert_id])
    conn.commit()

    with pytest.raises(psycopg2.Error) as raised:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM alert_outbox WHERE alert_id = %s",
                        [alert_id])
    assert "cannot be deleted" in str(raised.value)
    conn.rollback()

    with conn.cursor() as cur:
        cur.execute("DELETE FROM alert_outbox WHERE alert_id = %s",
                    [alert_id])
    conn.commit()


def test_a_pending_row_can_still_be_deleted_by_the_owner(conn):
    """The trigger is deliberately narrow, and this is the case it exempts.

    050's own comment: "an operator draining a mis-built batch before it
    ships has a legitimate reason, and no consumer has seen those bytes."
    A trigger that blocked every DELETE regardless of state would remove
    that legitimate path along with the one it means to close; this test
    is what would catch the `WHEN (OLD.state IN ('SENT', 'REFUSED'))`
    clause being accidentally dropped in favour of an unconditional guard.
    """
    _require_schema(conn)
    alert_id = _outbox_alert_id("pending")
    _insert_packet(conn, alert_id)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("DELETE FROM alert_outbox WHERE alert_id = %s",
                    [alert_id])
        assert cur.rowcount == 1
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM alert_outbox WHERE alert_id = %s",
                    [alert_id])
        assert cur.fetchone() is None


# ============================================================================
# 4. insert_alert_outbox_packet's collision guard, behaviourally
# ============================================================================

def test_reinserting_an_identical_packet_is_absorbed_as_idempotent(conn):
    """The ordinary case: a lost-response re-run recomputes the same packet.

    Rule 14's "identical bytes on resend" starts here, before the publisher
    is even involved — an application-level retry of the SAME confirmation
    transaction must not fail on the outbox write just because the first
    attempt's response never arrived.
    """
    _require_schema(conn)
    alert_id = _outbox_alert_id("idempotent")
    payload = b"idempotent-packet-bytes"
    schema_version_id = "00000000-0000-0000-0000-a00000000003"

    first = _insert_packet(conn, alert_id, payload, schema_version_id)
    assert first == "inserted"

    second = _insert_packet(conn, alert_id, payload, schema_version_id)
    assert second == "idempotent", (
        "an identical re-insert did not return 'idempotent'; either it "
        "raised or the guard's comparison is not comparing the right "
        "fields")
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM alert_outbox WHERE alert_id = %s",
                    [alert_id])
        assert cur.fetchone()[0] == 1, (
            "the idempotent re-insert produced a second row")
        cur.execute("DELETE FROM alert_outbox WHERE alert_id = %s",
                    [alert_id])
    conn.commit()


def test_same_alert_id_with_a_different_checksum_raises(conn):
    """A same-identity collision with different bytes is a hard invariant
    violation, not a silent overwrite.

    050's comment names the two readings of this state: either the digest
    inputs are incomplete (two genuinely different packets hashed to the
    same `alert_id`) or two different packets were minted under one
    identity. Both are defects a silent `ON CONFLICT DO NOTHING` would hide
    until consumers received contradictory bytes under one Kafka key — which
    is why the function raises instead.
    """
    _require_schema(conn)
    alert_id = _outbox_alert_id("checksum-collision")
    schema_version_id = "00000000-0000-0000-0000-a00000000004"

    first = _insert_packet(conn, alert_id, b"original-bytes",
                           schema_version_id)
    assert first == "inserted"
    conn.commit()

    with pytest.raises(psycopg2.Error) as raised:
        _insert_packet(conn, alert_id, b"different-bytes", schema_version_id)
    assert "one identity, two different packets" in str(raised.value)
    conn.rollback()

    with conn.cursor() as cur:
        cur.execute("DELETE FROM alert_outbox WHERE alert_id = %s",
                    [alert_id])
    conn.commit()


def test_same_alert_id_with_a_different_schema_version_raises(conn):
    """The pinned schema version is part of the envelope, checked separately.

    A SEPARATE test from the checksum collision above, as the brief requires
    ("both tests"): the function checks `payload_checksum` and
    `schema_version_id` as two distinct conditions with two distinct
    messages, and a collision that changed only the pinned version (bytes
    identical, registry version different) must be caught even though the
    checksum comparison alone would not catch it.
    """
    _require_schema(conn)
    alert_id = _outbox_alert_id("schema-collision")
    payload = b"same-bytes-different-schema-version"

    first = _insert_packet(conn, alert_id, payload,
                           "00000000-0000-0000-0000-a00000000005")
    assert first == "inserted"
    conn.commit()

    with pytest.raises(psycopg2.Error) as raised:
        _insert_packet(conn, alert_id, payload,
                       "00000000-0000-0000-0000-a00000000006")
    assert "pinned version" in str(raised.value)
    conn.rollback()

    with conn.cursor() as cur:
        cur.execute("DELETE FROM alert_outbox WHERE alert_id = %s",
                    [alert_id])
    conn.commit()


# ============================================================================
# 5. alert_id UNIQUE, enforced directly by the database
# ============================================================================

def test_alert_id_unique_is_enforced_by_a_bare_duplicate_insert(conn):
    """The idempotency fence itself, provoked with a raw INSERT.

    Distinct from the collision-guard tests above, which go through the
    function and exercise its comparison logic: this test bypasses the
    function entirely to confirm the guarantee does not depend on callers
    going through it. `alert_outbox_alert_id_uq` is what makes that true
    for any writer, present or future.
    """
    _require_schema(conn)
    alert_id = _outbox_alert_id("bare-duplicate")
    payload = b"bare-duplicate-bytes"
    checksum = "sha256:" + hashlib.sha256(payload).hexdigest()

    _insert_packet(conn, alert_id, payload,
                   "00000000-0000-0000-0000-a00000000007")
    conn.commit()

    with pytest.raises(psycopg2.errors.UniqueViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO alert_outbox (alert_id, identity_basis,"
                "   payload, payload_checksum, schema_version_id, topic,"
                "   release_identity, exposure_id, sca)"
                " VALUES (%s, 'product-key', %s, %s,"
                "   '00000000-0000-0000-0000-a00000000007'::uuid,"
                "   'alerts.live', 'live', 1, 1)",
                [alert_id, psycopg2.Binary(payload), checksum])
    conn.rollback()

    with conn.cursor() as cur:
        cur.execute("DELETE FROM alert_outbox WHERE alert_id = %s",
                    [alert_id])
    conn.commit()
