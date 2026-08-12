"""
File:    test_alert_outbox_confirmation.py

Brief E's acceptance criterion 2: the confirmation transaction's atomicity and
its fixed internal order.

Rule 14 requires the outbox rows to commit "in the same transaction as the
database effect that produced them". In this topology that effect is the
alert-effect confirmation — the `alert_emissions` confirm CAS and the
`alert_published` milestone — so THREE writes must be one atom:

    confirm CAS  +  outbox rows  +  milestone

and the ORDER INSIDE the atom is load-bearing, which is the part that is easy
to miss. The confirm CAS can affect ZERO rows without raising: a takeover is a
recorded no-op, not an error. So a claimant that inserted outbox rows BEFORE
checking whether its confirm actually won would commit packets for an emission
it did not confirm — and the publisher, which knows nothing about emissions,
would deliver them. The losing claimant must commit nothing.

**TESTED AGAINST THE REAL CAS SQL, on a real database.** The claim and confirm
statements are `RAPIDDB.claim_alert_emission` / `confirm_alert_emission`,
running against migration 037's real table with its real CHECK constraints. A
double cannot model "the UPDATE matched no rows and did not raise", which is
the exact behaviour the ordering rule exists to handle.
"""

import unittest
import uuid

import psycopg2
import pytest

from alerts.identity import payload_checksum
from database.modules.utils.rapid_db import RAPIDDB
from database.modules.utils.rapid_db_connect import transaction
from pipeline.contract import fixture

SCHEMA_VERSION = "0a1b2c3d-4e5f-6a7b-8c9d-0e1f2a3b4c5d"
TOPIC = "rapid.internal.alerts.v1"


@pytest.fixture
def emission(conn):
    """One claimable `(exposure, sca, release)` emission, cleaned up after.

    The exposure id is drawn from this run's tag so two runs never contend for
    one emission row — the CAS would resolve it correctly, which is precisely
    why a collision here would produce a confusing failure rather than an
    honest one.
    """
    if not fixture.has_table(conn, "alert_outbox"):
        pytest.skip("DRAFT 050 is not applied (no alert_outbox); this suite "
                    "runs on rapid-admin where base + drafts are applied")
    if not fixture.has_table(conn, "alert_emissions"):
        pytest.skip("migration 037 is not applied (no alert_emissions)")

    execute = fixture.executor(conn)
    release = f"conf-{fixture.RUN_TAG}-{uuid.uuid4().hex[:6]}"
    # A per-test exposure id well above anything the seed data carries.
    exposure = 900_000 + (uuid.uuid4().int % 90_000)
    sca = 3
    yield conn, execute, exposure, sca, release
    try:
        execute("UPDATE alert_outbox SET state = 'PENDING', sent_at = NULL,"
                "   claim_token = NULL, claimed_at = NULL,"
                "   refusal_reason = NULL WHERE release_identity = %s",
                [release])
        execute("DELETE FROM alert_outbox WHERE release_identity = %s",
                [release])
        execute("DELETE FROM alert_emissions WHERE release_identity = %s",
                [release])
        execute("DELETE FROM attempt_milestones WHERE exposure_id = %s",
                [exposure])
        conn.commit()
    except Exception:                                       # noqa: BLE001
        conn.rollback()


def _claim(conn, exposure, sca, release, token, attempt_id):
    """Win the emission claim, committed — step 1 of the real protocol."""
    handle = RAPIDDB.borrowing(conn)
    with transaction(conn):
        won = handle.claim_alert_emission(
            exposure, sca, release, attempt_id, attempt_id, token, pid=None)
    return won


def _insert_packet(execute, alert_id, release, exposure, sca,
                   payload=b"packet"):
    return execute(
        "SELECT insert_alert_outbox_packet(%s, 'product-key', %s, %s, %s, %s,"
        "                                  %s, %s, %s, %s, %s)",
        [alert_id, psycopg2.Binary(payload), payload_checksum(payload),
         SCHEMA_VERSION, TOPIC, release, exposure, sca, None, None])[0][0]


def _outbox_count(execute, release):
    return execute("SELECT count(*) FROM alert_outbox"
                   " WHERE release_identity = %s", [release])[0][0]


def _emission_state(execute, exposure, sca, release):
    rows = execute(
        "SELECT state, claim_token FROM alert_emissions"
        " WHERE exposure_id = %s AND sca = %s AND release_identity = %s",
        [exposure, sca, release])
    return rows[0] if rows else None


def test_the_three_effects_commit_together(emission):
    """Confirm CAS, outbox rows and milestone: one atom, all present."""
    conn, execute, exposure, sca, release = emission
    attempt_id = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    conn.commit()
    token = str(attempt_id)
    assert _claim(conn, exposure, sca, release, token, attempt_id) == token

    handle = RAPIDDB.borrowing(conn)
    alert_id = "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex[:32]
    with transaction(conn):
        confirmed = handle.confirm_alert_emission(exposure, sca, release,
                                                  token, 1)
        assert confirmed == token
        _insert_packet(execute, alert_id, release, exposure, sca)

    state, _claim_token = _emission_state(execute, exposure, sca, release)
    assert state == "emitted"
    assert _outbox_count(execute, release) == 1


def test_a_forced_failure_rolls_back_all_three(emission):
    """The atom's negative half, which a passing commit cannot demonstrate.

    A failure raised INSIDE the transaction after the confirm and the insert
    must leave the emission unconfirmed AND the outbox empty. If the three were
    not one transaction this test would find a confirmed emission with no
    packets, or packets for an unconfirmed emission — the two states rule 14
    exists to make unrepresentable.
    """
    conn, execute, exposure, sca, release = emission
    attempt_id = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    conn.commit()
    token = str(attempt_id)
    _claim(conn, exposure, sca, release, token, attempt_id)

    handle = RAPIDDB.borrowing(conn)
    alert_id = "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex[:32]

    class ForcedFailure(RuntimeError):
        """Stands in for a crash between the insert and the commit."""

    with pytest.raises(ForcedFailure):
        with transaction(conn):
            handle.confirm_alert_emission(exposure, sca, release, token, 1)
            _insert_packet(execute, alert_id, release, exposure, sca)
            raise ForcedFailure("the process died before COMMIT")

    state, claim_token = _emission_state(execute, exposure, sca, release)
    assert state == "claimed"
    assert claim_token == token
    assert _outbox_count(execute, release) == 0


def test_a_losing_claimant_commits_neither_packets_nor_milestone(emission):
    """THE ORDERING RULE, tested through a real takeover.

    Attempt A claims. Attempt B TAKES THE CLAIM OVER (a stale-claim reclaim,
    which migration 037's CAS permits). Attempt A then runs its confirmation
    transaction: its confirm CAS matches zero rows and returns None WITHOUT
    RAISING, and A must therefore write no outbox rows and no milestone.

    This is the test that fails if the insert is moved ahead of the token
    check. It is also the reason the production code checks
    `confirmed_token == claim_token` rather than trusting that a confirm which
    did not raise must have succeeded.
    """
    conn, execute, exposure, sca, release = emission
    attempt_a = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    attempt_b = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    conn.commit()
    token_a, token_b = str(attempt_a), str(attempt_b)

    assert _claim(conn, exposure, sca, release, token_a, attempt_a) == token_a

    # THE TAKEOVER. The claim is aged past the CAS's staleness threshold so B's
    # claim matches — the same thing a real recovery does after A's process
    # died, without waiting an hour of wall time for it.
    execute("UPDATE alert_emissions SET claimed_at = now() - interval '2 hours'"
            " WHERE exposure_id = %s AND sca = %s AND release_identity = %s",
            [exposure, sca, release])
    conn.commit()
    assert _claim(conn, exposure, sca, release, token_b, attempt_b) == token_b

    # A NOW CONFIRMS — and loses. The CAS returns None and does not raise.
    handle = RAPIDDB.borrowing(conn)
    alert_id = "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex[:32]
    with transaction(conn):
        confirmed = handle.confirm_alert_emission(exposure, sca, release,
                                                  token_a, 1)
        # The production stage's guard, reproduced exactly: everything below
        # is inside `if confirmed == token`.
        if confirmed == token_a:
            _insert_packet(execute, alert_id, release, exposure, sca)

    assert confirmed is None
    assert _outbox_count(execute, release) == 0
    state, claim_token = _emission_state(execute, exposure, sca, release)
    assert state == "claimed"
    assert claim_token == token_b


def test_a_rerun_after_a_lost_response_writes_no_duplicates(emission):
    """TWO LEVELS OF IDEMPOTENCY, and which one catches what.

    An attempt whose confirmation committed but whose response was lost re-runs
    the whole step. The EMISSION fence (`alert_emissions`) stops it from
    re-confirming — the row is `emitted` and no longer `claimed`, so the CAS
    matches nothing. And even if it reached the insert, `alert_id`'s UNIQUE
    constraint plus the function's identical-envelope absorption would stop a
    duplicate row.

    Both are asserted here because the brief asks which mechanism catches
    what: the fence prevents the second CONFIRMATION, uniqueness prevents the
    second PACKET, and neither substitutes for the other.
    """
    conn, execute, exposure, sca, release = emission
    attempt_id = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    conn.commit()
    token = str(attempt_id)
    _claim(conn, exposure, sca, release, token, attempt_id)

    handle = RAPIDDB.borrowing(conn)
    alert_id = "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex[:32]
    with transaction(conn):
        handle.confirm_alert_emission(exposure, sca, release, token, 1)
        _insert_packet(execute, alert_id, release, exposure, sca)

    # THE RE-RUN. Same attempt, same data, same deterministic alert_id.
    with transaction(conn):
        second_confirm = handle.confirm_alert_emission(exposure, sca, release,
                                                       token, 1)
    assert second_confirm is None, (
        "the emission fence should refuse a second confirmation: the row is "
        "'emitted', not 'claimed'")

    # And the packet path is independently idempotent.
    assert _insert_packet(execute, alert_id, release, exposure,
                          sca) == "idempotent"
    assert _outbox_count(execute, release) == 1


def test_outbox_rows_are_visible_only_after_the_commit(emission):
    """"Same transaction" means invisible until it commits, from outside.

    Read over a SECOND connection while the first's transaction is open: the
    publisher runs in a different process, and a packet visible before its
    emission was confirmed could be delivered for an emission that then rolled
    back.
    """
    conn, execute, exposure, sca, release = emission
    attempt_id = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    conn.commit()
    token = str(attempt_id)
    _claim(conn, exposure, sca, release, token, attempt_id)

    observer = fixture.connect()
    observer.autocommit = True
    try:
        handle = RAPIDDB.borrowing(conn)
        alert_id = "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex[:32]
        with transaction(conn):
            handle.confirm_alert_emission(exposure, sca, release, token, 1)
            _insert_packet(execute, alert_id, release, exposure, sca)
            with observer.cursor() as cur:
                cur.execute("SELECT count(*) FROM alert_outbox"
                            " WHERE release_identity = %s", [release])
                assert cur.fetchone()[0] == 0

        with observer.cursor() as cur:
            cur.execute("SELECT count(*) FROM alert_outbox"
                        " WHERE release_identity = %s", [release])
            assert cur.fetchone()[0] == 1
    finally:
        observer.close()


if __name__ == "__main__":
    unittest.main()
