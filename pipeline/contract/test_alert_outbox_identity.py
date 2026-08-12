"""
File:    test_alert_outbox_identity.py

Packet identity where the database enforces it: brief E's acceptance criterion
1 (the DB half) and criterion 9 (the pre-D legacy basis).

The pure-digest properties — determinism, sensitivity, namespace separation,
forbidden inputs — are asserted in `alerts/test/test_identity.py`, where they
need no database. What needs one is everything the SCHEMA promises about those
digests: the UNIQUE constraint, the same-id-different-envelope guard, and the
`diffimages.product_id -> products.product_key` join that decides which basis a
real difference image takes.

**THE JOIN IS TESTED AGAINST REAL ROWS, NOT A MOCK.** `_diffimage_parents` in
the shared fixture builds a difference image with all five of its foreign-key
parents satisfied, which is the only way to find out what the basis selection
does with an image that genuinely has — or genuinely lacks — a product binding.
A mocked lookup would assert what the test author believed the join returns.
"""

import unittest
import uuid

import psycopg2
import pytest

from alerts.identity import (BASIS_LEGACY_PID, BASIS_PRODUCT_KEY,
                             alert_identity, payload_checksum)
from pipeline.contract import fixture

SCHEMA_VERSION = "0a1b2c3d-4e5f-6a7b-8c9d-0e1f2a3b4c5d"
OTHER_SCHEMA_VERSION = "9f8e7d6c-5b4a-3928-1716-0f5e4d3c2b1a"
TOPIC = "rapid.internal.alerts.v1"


@pytest.fixture
def outbox_db(conn):
    """A connection with DRAFT 050 applied, or a clean skip."""
    if not fixture.has_table(conn, "alert_outbox"):
        pytest.skip("DRAFT 050 is not applied (no alert_outbox); this suite "
                    "runs on rapid-admin where base + drafts are applied")
    release = f"ident-{fixture.RUN_TAG}-{uuid.uuid4().hex[:8]}"
    yield conn, fixture.executor(conn), release
    try:
        execute = fixture.executor(conn)
        execute("UPDATE alert_outbox SET state = 'PENDING', sent_at = NULL,"
                "   refusal_reason = NULL, claim_token = NULL,"
                "   claimed_at = NULL WHERE release_identity = %s", [release])
        execute("DELETE FROM alert_outbox WHERE release_identity = %s",
                [release])
        conn.commit()
    except Exception:                                       # noqa: BLE001
        conn.rollback()


def _insert(execute, alert_id, release, payload=b"bytes",
            checksum=None, schema_version=SCHEMA_VERSION,
            basis=BASIS_PRODUCT_KEY, topic=TOPIC):
    """Insert through the migration's FUNCTION, never a bare INSERT.

    The function is the production insert path: it carries the collision guard,
    and a test that inserted around it would be testing a path nothing uses.
    """
    return execute(
        "SELECT insert_alert_outbox_packet(%s, %s, %s, %s, %s, %s, %s, %s,"
        "                                  %s, %s, %s)",
        [alert_id, basis, psycopg2.Binary(payload),
         checksum or payload_checksum(payload), schema_version, topic,
         release, 1, 1, None, None])[0][0]


class DatabaseIdentityTests:
    """Marker container; pytest collects the module-level functions."""


def test_the_database_enforces_alert_id_uniqueness(outbox_db):
    """UNIQUE is the idempotency fence, and it is the DATABASE's job.

    The application computes deterministic digests, so a re-run recomputes the
    same ids — but "the application will not write a duplicate" is a promise,
    and the fence has to be a constraint. Asserted with a bare INSERT rather
    than through the function, because the function's job is to ABSORB an
    identical re-insert; what is under test here is the constraint underneath
    it.
    """
    conn, execute, release = outbox_db
    alert_id = "sha256:" + "1" * 64
    _insert(execute, alert_id, release)
    conn.commit()

    with pytest.raises(psycopg2.errors.UniqueViolation):
        execute(
            "INSERT INTO alert_outbox"
            "  (alert_id, identity_basis, payload, payload_checksum,"
            "   schema_version_id, topic, release_identity, exposure_id, sca)"
            " VALUES (%s, 'product-key', %s, %s, %s, %s, %s, 1, 1)",
            [alert_id, psycopg2.Binary(b"other"), payload_checksum(b"other"),
             SCHEMA_VERSION, TOPIC, release])
    conn.rollback()


def test_an_identical_reinsert_is_absorbed_as_idempotent(outbox_db):
    """THE RE-RUN AFTER A LOST RESPONSE, which must not be an error.

    An attempt whose confirmation transaction's response was lost re-runs,
    recomputes the same digests over the same data, and offers the same
    packets. That is the ordinary case, not a collision, and the function
    reports it as such.
    """
    conn, execute, release = outbox_db
    alert_id = "sha256:" + "2" * 64

    assert _insert(execute, alert_id, release, payload=b"same") == "inserted"
    assert _insert(execute, alert_id, release, payload=b"same") == "idempotent"
    conn.commit()

    rows = execute("SELECT count(*) FROM alert_outbox WHERE alert_id = %s",
                   [alert_id])
    assert rows[0][0] == 1


def test_a_same_id_insert_with_a_different_checksum_fails_loudly(outbox_db):
    """One identity, two different packets — a hard invariant violation.

    Never `ON CONFLICT DO NOTHING`: silently keeping the first packet would
    hide either an incomplete digest input set or two genuinely different
    packets minted under one identity, and consumers would eventually receive
    contradictory bytes under one key.
    """
    conn, execute, release = outbox_db
    alert_id = "sha256:" + "3" * 64
    _insert(execute, alert_id, release, payload=b"first")
    conn.commit()

    with pytest.raises(psycopg2.errors.RaiseException) as caught:
        _insert(execute, alert_id, release, payload=b"second")
    assert "checksum" in str(caught.value)
    conn.rollback()


def test_a_same_id_insert_with_a_different_schema_version_fails_loudly(
        outbox_db):
    """The PINNED VERSION is envelope, not metadata.

    Two packets agreeing on identity and bytes but pinned to different registry
    versions would produce different WIRE bytes — so accepting the second
    silently would break "identical bytes on resend" for the row that already
    existed. The brief asks for this as its own test beside the checksum one,
    because they fail for different reasons.
    """
    conn, execute, release = outbox_db
    alert_id = "sha256:" + "4" * 64
    _insert(execute, alert_id, release, payload=b"fixed",
            schema_version=SCHEMA_VERSION)
    conn.commit()

    with pytest.raises(psycopg2.errors.RaiseException) as caught:
        _insert(execute, alert_id, release, payload=b"fixed",
                schema_version=OTHER_SCHEMA_VERSION)
    assert "schema version" in str(caught.value)
    conn.rollback()


def test_the_two_bases_coexist_without_colliding(outbox_db):
    """Namespace separation, at the table rather than in the digest function.

    The pure-Python proof is in `alerts/test/test_identity.py`; this is the
    consequence that matters operationally — packets under both bases live in
    one table with one UNIQUE constraint, and inserting both must not conflict.
    """
    conn, execute, release = outbox_db
    product_id, _payload = alert_identity(
        product_key="sha256:" + "a" * 64, catalog_id=7, isdiffpos=True,
        release_identity=release)
    legacy_id, _payload = alert_identity(
        legacy_pid=4242, catalog_id=7, isdiffpos=True,
        release_identity=release)

    assert product_id != legacy_id
    assert _insert(execute, product_id, release,
                   basis=BASIS_PRODUCT_KEY) == "inserted"
    assert _insert(execute, legacy_id, release,
                   basis=BASIS_LEGACY_PID) == "inserted"
    conn.commit()


def test_a_later_product_binding_does_not_remint_an_outboxed_identity(
        outbox_db):
    """IDENTITY IS FROZEN AT OUTBOX-WRITE TIME (the brief's fixed reading).

    A difference image registered before D has no product binding, so its
    packets take the legacy basis. If that image is later registered under the
    identity model and GAINS a `product_id`, the packets already in the outbox
    keep the identity they were written with — the row is write-once, and
    re-minting would change the key consumers deduplicate on for bytes they may
    already have received.

    Tested by doing exactly that: write a legacy-basis packet, then bind the
    image, then read the row back and assert nothing moved. The binding is real
    (a `products` row and a `diffimages.product_id` update), so this exercises
    the actual join rather than a hypothetical.
    """
    conn, execute, release = outbox_db
    if not fixture.has_table(conn, "products"):
        pytest.skip("DRAFT 048 is not applied (no products table), so there "
                    "is no binding to add")

    attempt_id = fixture.make_attempt(conn)
    pid = fixture.make_diffimage(conn, attempt_id, field=7001, ppid=15)
    conn.commit()

    legacy_id, _payload = alert_identity(
        legacy_pid=pid, catalog_id=11, isdiffpos=False,
        release_identity=release)
    _insert(execute, legacy_id, release, basis=BASIS_LEGACY_PID)
    conn.commit()

    # THE IMAGE GAINS A BINDING, after the packet was written.
    product_key = "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex[:32]
    execute(
        "INSERT INTO products"
        "  (product_key, product_class, role, identity_payload,"
        "   serialization_version, process_family)"
        " VALUES (%s, 'difference_image', 'difference_image', '{}'::jsonb,"
        "         1, 15) RETURNING product_id", [product_key])
    execute("UPDATE diffimages SET product_id ="
            " (SELECT product_id FROM products WHERE product_key = %s)"
            " WHERE pid = %s", [product_key, pid])
    conn.commit()

    rows = execute("SELECT alert_id, identity_basis FROM alert_outbox"
                   "  WHERE alert_id = %s", [legacy_id])
    assert rows[0][0] == legacy_id
    assert rows[0][1] == BASIS_LEGACY_PID


def test_the_basis_selection_reads_the_real_product_join(outbox_db):
    """Which basis a REAL difference image takes, through the real query.

    `get_difference_image_product_key` is what the alert-production stage asks
    before minting identities, and its answer decides the basis permanently for
    every packet on that chip. An image with no binding must answer None — not
    raise, not return an empty string — and an image with one must answer its
    key.
    """
    conn, execute, _release = outbox_db
    if not fixture.has_table(conn, "products"):
        pytest.skip("DRAFT 048 is not applied (no products table)")

    from database.modules.utils.rapid_db import RAPIDDB

    attempt_id = fixture.make_attempt(conn)
    unbound = fixture.make_diffimage(conn, attempt_id, field=7002, ppid=15)
    bound = fixture.make_diffimage(conn, attempt_id, field=7003, ppid=15)
    product_key = "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex[:32]
    execute(
        "INSERT INTO products"
        "  (product_key, product_class, role, identity_payload,"
        "   serialization_version, process_family)"
        " VALUES (%s, 'difference_image', 'difference_image', '{}'::jsonb,"
        "         1, 15)", [product_key])
    execute("UPDATE diffimages SET product_id ="
            " (SELECT product_id FROM products WHERE product_key = %s)"
            " WHERE pid = %s", [product_key, bound])
    conn.commit()

    handle = RAPIDDB.borrowing(conn)
    assert handle.get_difference_image_product_key(unbound) is None
    assert handle.get_difference_image_product_key(bound) == product_key


def test_a_legacy_basis_packet_records_its_basis_in_the_row(outbox_db):
    """Criterion 9: the degradation is MARKED, not silent.

    The whole justification for admitting a database surrogate into an identity
    is that it is visible as a degradation — so an operator, or a later
    migration, can find exactly which packets took it.
    """
    conn, execute, release = outbox_db
    legacy_id, payload = alert_identity(
        legacy_pid=999001, catalog_id=3, isdiffpos=True,
        release_identity=release)

    _insert(execute, legacy_id, release, basis=BASIS_LEGACY_PID)
    conn.commit()

    rows = execute("SELECT identity_basis FROM alert_outbox"
                   " WHERE alert_id = %s", [legacy_id])
    assert rows[0][0] == BASIS_LEGACY_PID
    # And the basis is inside what was HASHED, not only in the column: the
    # column is queryable, the payload entry is what makes collision
    # impossible.
    assert payload["identity_basis"] == BASIS_LEGACY_PID
    assert payload["image"]["legacy_processing_id"] == 999001


def test_the_basis_column_refuses_an_unknown_value(outbox_db):
    """The CHECK constraint is the schema's own statement of the vocabulary."""
    conn, execute, release = outbox_db

    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert(execute, "sha256:" + "5" * 64, release, basis="invented-basis")
    conn.rollback()


if __name__ == "__main__":
    unittest.main()
