"""
File:    test_publisher_contract.py

The publisher's delivery contract, against a real database and stub brokers:
brief E's acceptance criteria 4 (wire boundary), 5 (crash windows and claims)
and 6 (delivery policy).

**A REAL DATABASE, BECAUSE EVERY PROPERTY HERE IS ONE.** The claim is an atomic
CAS whose correctness under two concurrent cycles is a property of PostgreSQL's
`UPDATE ... WHERE state = 'PENDING' ... FOR UPDATE SKIP LOCKED`, not of Python;
the ordering is an `ORDER BY` over a partial index; the lease is a `now() -
claimed_at` comparison the server evaluates; and the write-once envelope is a
trigger. None of it can be tested against a fake.

**STUB BROKERS THAT CAN REFUSE** (`pipeline/contract/stub_broker.py`). The
publisher's contract is almost entirely about what happens when a send goes
wrong, so the doubles model the three outcomes distinguishably — accepted,
ambiguous, definitely refused — and the tests assert the publisher's
CLASSIFICATION of each rather than assuming it.

**THE WIRE IS INSPECTED, NOT THE INTENT.** "Identical bytes on resend" is
asserted by comparing the bytes the stub actually received on the first send
with the bytes it received on the resend. A test that compared what the
publisher *meant* to send would pass against a publisher that reframed from a
moved registry, which is the exact defect the pinned schema version exists to
prevent.
"""

import unittest
import uuid

import pytest

from pipeline.contract import fixture
from pipeline.contract.stub_broker import (AmbiguousBroker, CrashingBroker,
                                           FlakyBroker, RecordingBroker,
                                           RefusingBroker)
from pipeline.publisher.cycle import PublisherCycle
from pipeline.publisher.outbox import OutboxRepository

#: A syntactically valid Glue schema-version UUID for fixture rows. Its VALUE
#: is never interpreted — the publisher copies it into the frame — but the
#: column is typed `uuid`, so it must parse.
SCHEMA_VERSION = "0a1b2c3d-4e5f-6a7b-8c9d-0e1f2a3b4c5d"

#: A SECOND version, for the registry-bump test. The point of pinning is that
#: this value never reaches the wire for a row pinned to the first one.
OTHER_SCHEMA_VERSION = "9f8e7d6c-5b4a-3928-1716-0f5e4d3c2b1a"

TOPIC = "rapid.internal.alerts.v1"


def _checksum(payload):
    import hashlib
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class PublisherFixture:
    """One test's outbox rows, its repository, and its cleanup.

    FIXTURE HONESTY (the tier's discipline): every row is tagged with this
    test's own run-unique release identity, and teardown deletes exactly those
    rows. Nothing truncates, so two runs may overlap on one database and a
    failure leaves its rows behind for inspection.
    """

    def __init__(self, conn, authorized=True):
        self.conn = conn
        self.execute = fixture.executor(conn)
        self.release = f"pub-{fixture.RUN_TAG}-{uuid.uuid4().hex[:8]}"
        # SCOPED TO THIS TEST'S RELEASE. See OutboxRepository.only_release:
        # the contract tier shares one database, and an unscoped publisher
        # correctly claims other tests' PENDING rows.
        self.repository = OutboxRepository(self.execute,
                                           only_release=self.release)
        self.alert_ids = []
        if authorized is not None:
            self.set_policy(authorized)

    def set_policy(self, authorized, reason="contract fixture"):
        self.execute(
            "INSERT INTO delivery_policies"
            "  (release_identity, authorized, reason, actor)"
            " VALUES (%s, %s, %s, 'contract-test')"
            " ON CONFLICT (release_identity) DO UPDATE"
            "    SET authorized = EXCLUDED.authorized,"
            "        reason = EXCLUDED.reason",
            [self.release, authorized, reason])
        self.conn.commit()

    def add_packet(self, payload=b"packet-bytes", schema_version=None,
                   created_offset_seconds=0, alert_id=None):
        """One PENDING outbox row. Returns its alert_id.

        `created_offset_seconds` places the row deliberately in time so the
        ORDER test can build rows whose `created_at` order differs from their
        `alert_id` order — otherwise a publisher that sorted by the wrong key
        would pass by coincidence.
        """
        alert_id = alert_id or ("sha256:" + uuid.uuid4().hex + uuid.uuid4().hex)[:71]
        self.execute(
            "INSERT INTO alert_outbox"
            "  (alert_id, identity_basis, payload, payload_checksum,"
            "   schema_version_id, topic, release_identity, exposure_id, sca,"
            "   created_at)"
            " VALUES (%s, 'product-key', %s, %s, %s, %s, %s, 1, 1,"
            "         now() + (%s * interval '1 second'))",
            [alert_id, __import__("psycopg2").Binary(payload),
             _checksum(payload), schema_version or SCHEMA_VERSION, TOPIC,
             self.release, created_offset_seconds])
        self.conn.commit()
        self.alert_ids.append(alert_id)
        return alert_id

    def state(self, alert_id):
        rows = self.execute(
            "SELECT state, resend_count, refusal_reason, sent_at,"
            "       claim_token"
            "  FROM alert_outbox WHERE alert_id = %s", [alert_id])
        return rows[0] if rows else None

    def expire_claims(self):
        """Age every claim past the lease, without waiting for wall time.

        The lease is a server-side `now() - claimed_at` comparison, so it is
        made to have elapsed by moving `claimed_at` into the past. Sleeping for
        five real minutes per crash-window test is not a test suite anyone
        runs.
        """
        self.execute(
            "UPDATE alert_outbox SET claimed_at = now() - interval '1 hour'"
            " WHERE release_identity = %s AND state = 'IN_FLIGHT'",
            [self.release])
        self.conn.commit()

    def cycle(self, broker, claim_token="pub-test", **kwargs):
        return PublisherCycle(self.repository, broker, claim_token, **kwargs)

    def cleanup(self):
        # SENT and REFUSED rows are undeletable BY TRIGGER (that is criterion
        # 7's backstop), so the fixture's own teardown has to move them back
        # to a deletable state first. Doing this as the connected superuser is
        # legitimate — the trigger guards the delivery record in production,
        # and this is a scratch database's test rows — but it is worth naming,
        # because a teardown that silently failed would leave every run's rows
        # behind and the next run would be reading them.
        try:
            self.execute(
                "UPDATE alert_outbox SET state = 'PENDING', sent_at = NULL,"
                "   refusal_reason = NULL, claim_token = NULL,"
                "   claimed_at = NULL"
                " WHERE release_identity = %s", [self.release])
            self.execute("DELETE FROM alert_outbox WHERE release_identity = %s",
                         [self.release])
            self.execute("DELETE FROM delivery_policies"
                         " WHERE release_identity = %s", [self.release])
            self.conn.commit()
        except Exception:                                   # noqa: BLE001
            self.conn.rollback()


@pytest.fixture
def outbox(conn):
    """A publisher fixture, skipped cleanly where DRAFT 050 is not applied.

    The skip is what keeps smdc CI green: CI builds its database from the
    authoritative stream, which does not contain `migrations-draft/`, so these
    tests skip there by design and run on rapid-admin where base + drafts are
    applied. Probing the catalog rather than catching a query failure keeps
    "the schema is not deployed" apart from "the query is wrong".
    """
    if not fixture.has_table(conn, "alert_outbox"):
        pytest.skip("DRAFT 050 is not applied (no alert_outbox); this suite "
                    "runs on rapid-admin where base + drafts are applied")
    state = PublisherFixture(conn)
    yield state
    state.cleanup()


# ---------------------------------------------------------------------------
# Criterion 4 — the publisher contract at the wire boundary
# ---------------------------------------------------------------------------

class WireBoundaryTests:
    """Marker-free container; pytest collects the module-level functions."""


def test_sends_in_created_at_then_alert_id_order(outbox):
    """The total order `(created_at, alert_id)`, tie-break included.

    The rows are built so `created_at` order and `alert_id` order DISAGREE: the
    oldest row has the alphabetically-last id. A publisher sorting by `alert_id`
    alone, or relying on RETURNING's order, gets a different sequence — which
    is the point, because a test whose rows agreed on both orders would pass
    against either implementation.
    """
    third = outbox.add_packet(payload=b"c", created_offset_seconds=2,
                              alert_id="sha256:" + "a" * 64)
    first = outbox.add_packet(payload=b"a", created_offset_seconds=0,
                              alert_id="sha256:" + "c" * 64)
    second = outbox.add_packet(payload=b"b", created_offset_seconds=1,
                               alert_id="sha256:" + "b" * 64)

    broker = RecordingBroker()
    counts = outbox.cycle(broker).run_once()

    assert counts["sent"] == 3
    assert broker.keys == [first.encode("utf-8"), second.encode("utf-8"),
                           third.encode("utf-8")]


def test_the_tie_break_orders_rows_sharing_one_created_at(outbox):
    """`created_at` alone is NOT a total order, and the schema knows it.

    Every packet of one chip is written by ONE confirmation transaction and so
    shares a `now()` to the microsecond. Without the `alert_id` tie-break the
    order within a chip would be whatever the plan produced, and two publishers
    could disagree about which packet is next.
    """
    ids = sorted("sha256:" + c * 64 for c in "def")
    for alert_id in reversed(ids):
        outbox.add_packet(payload=alert_id.encode(), alert_id=alert_id,
                          created_offset_seconds=0)

    # THE TIMESTAMPS ARE FORCED EQUAL, which the fixture alone cannot do:
    # each `add_packet` commits its own transaction, so each row gets its own
    # `now()` microsecond and they arrive already totally ordered by
    # `created_at`. The tie-break would then never be exercised and the test
    # would pass against a publisher that ignored it — which is exactly what
    # happened on this branch's second acceptance run, where the rows came back
    # in insertion order (f, e, d) and the assertion caught it.
    #
    # One UPDATE to a single literal reproduces what one confirmation
    # transaction actually writes: a whole chip's packets sharing one instant.
    outbox.execute(
        "UPDATE alert_outbox SET created_at = timestamptz '2026-01-01 00:00:00+00'"
        " WHERE release_identity = %s", [outbox.release])
    outbox.conn.commit()

    broker = RecordingBroker()
    outbox.cycle(broker).run_once()

    assert broker.keys == [i.encode("utf-8") for i in ids]


def test_the_message_key_is_the_alert_id(outbox):
    """Consumers deduplicate on the key, so the key must BE the identity.

    At-least-once delivery is only deduplicable if every copy of a packet
    arrives under the same key. `Transport.send` was value-only before brief E
    (`kafka_producer.py`), so this is a new parameter on the path and worth
    asserting directly rather than inferring from a successful send.
    """
    alert_id = outbox.add_packet()
    broker = RecordingBroker()

    outbox.cycle(broker).run_once()

    assert broker.keys == [alert_id.encode("utf-8")]


def test_the_wire_bytes_carry_the_pinned_schema_version(outbox):
    """The frame is built from the ROW's version, not the registry's latest."""
    from alerts.kafka_producer import unframe_alert

    outbox.add_packet(payload=b"payload-one", schema_version=SCHEMA_VERSION)
    broker = RecordingBroker()

    outbox.cycle(broker).run_once()

    version, payload = unframe_alert(broker.payloads[0])
    assert str(version) == SCHEMA_VERSION
    assert payload == b"payload-one"


def test_a_resend_is_byte_identical_after_a_registry_version_change(outbox):
    """THE CENTRAL PROPERTY of rule 14, tested at the wire.

    The first send is ambiguous, so the row returns to PENDING. Between the two
    sends the REGISTRY MOVES — simulated by pointing the module's registry at a
    different version, which is what a schema bump does to the production
    producer's `LatestVersion: True` lookup. The second send must still produce
    the first send's exact bytes, because the publisher frames from the row's
    pinned version and never asks the registry at all.

    Compared as BYTES, from what the stub actually received. A test that
    compared the publisher's intent would pass against the very implementation
    this guards against.
    """
    outbox.add_packet(payload=b"stable-bytes", schema_version=SCHEMA_VERSION)
    broker = FlakyBroker(failures=1)

    # First cycle: ambiguous, returned to PENDING with the counter bumped.
    first = outbox.cycle(broker).run_once()
    assert first["resend"] == 1

    # THE REGISTRY MOVES. Nothing on the publisher's send path reads it — that
    # is the property — so this is set up purely to prove the negative: if the
    # publisher DID consult a registry, the second frame would carry
    # OTHER_SCHEMA_VERSION and the byte comparison below would fail.
    import alerts.kafka_producer as kafka_producer
    original = kafka_producer.GlueSchemaRegistry.schema_version_id
    kafka_producer.GlueSchemaRegistry.schema_version_id = (
        lambda self, name: OTHER_SCHEMA_VERSION)
    try:
        second = outbox.cycle(broker).run_once()
    finally:
        kafka_producer.GlueSchemaRegistry.schema_version_id = original

    assert second["sent"] == 1
    assert len(broker.sent) == 2
    assert broker.payloads[0] == broker.payloads[1]
    assert broker.keys[0] == broker.keys[1]


def test_an_ambiguous_acknowledgement_resends_and_counts(outbox):
    """Ambiguous is not failure and not success: it is another send."""
    alert_id = outbox.add_packet()
    broker = AmbiguousBroker()

    counts = outbox.cycle(broker).run_once()

    assert counts["resend"] == 1
    state, resends, _reason, sent_at, claim_token = outbox.state(alert_id)
    assert state == "PENDING"
    assert resends == 1
    assert sent_at is None
    # The claim is RELEASED, not held: the row is available to the next cycle
    # immediately rather than waiting out the lease.
    assert claim_token is None


def test_a_definite_refusal_is_terminal_and_never_resent(outbox):
    """A refusal retried forever is a loop against a fixed answer."""
    alert_id = outbox.add_packet()
    broker = RefusingBroker(reason="message exceeds the topic maximum")

    first = outbox.cycle(broker).run_once()
    assert first["refused"] == 1

    state, _resends, reason, _sent_at, _token = outbox.state(alert_id)
    assert state == "REFUSED"
    assert "exceeds" in reason

    # A SECOND CYCLE MUST NOT PICK IT UP. REFUSED is terminal, and the claim
    # scans PENDING alone — asserted rather than assumed, because "terminal"
    # is a property of the query, not of the state name.
    second = outbox.cycle(RecordingBroker()).run_once()
    assert second["claimed"] == 0


def test_an_already_sent_row_is_never_resent(outbox):
    """SENT is terminal for the same reason REFUSED is."""
    outbox.add_packet()
    broker = RecordingBroker()

    outbox.cycle(broker).run_once()
    again = outbox.cycle(broker).run_once()

    assert again["claimed"] == 0
    assert len(broker.sent) == 1


# ---------------------------------------------------------------------------
# Criterion 5 — crash windows and claims
# ---------------------------------------------------------------------------

def _crash_then_recover(outbox, crash_after_send):
    """Kill a cycle at one window, then recover. Returns the recovery broker."""
    outbox.add_packet(payload=b"crash-window")
    crashing = CrashingBroker(crash_after_send=crash_after_send)

    # `KeyboardInterrupt` is a BaseException: it passes through the publisher's
    # `except Exception` ambiguity handler untouched and unwinds the cycle with
    # the row still IN_FLIGHT and no finalization — which is what a SIGKILL
    # looks like from the database's side.
    try:
        outbox.cycle(crashing, claim_token="dead-cycle").run_once()
    except KeyboardInterrupt:
        pass

    state, _resends, _reason, _sent_at, token = outbox.state(
        outbox.alert_ids[-1])
    assert state == "IN_FLIGHT"
    assert token == "dead-cycle"

    outbox.expire_claims()
    recovery = RecordingBroker()
    counts = outbox.cycle(recovery, claim_token="recovering").run_once()
    assert counts["reclaimed"] == 1
    return recovery, counts


def test_a_crash_before_the_send_recovers_with_one_send(outbox):
    """Window 1: nothing left the process."""
    recovery, counts = _crash_then_recover(outbox, crash_after_send=False)
    assert counts["sent"] == 1
    assert len(recovery.sent) == 1


def test_a_crash_after_the_send_recovers_with_one_resend(outbox):
    """Window 2/3: the bytes left, the acknowledgement did not land durably.

    THE SAME RECOVERY AS WINDOW 1, and that identity is the assertion. There is
    no durable ack evidence written between the claim and the finalization, so
    the database cannot distinguish "sent and acknowledged" from "never sent" —
    and the contract is that it does not try. The cost is a possible duplicate,
    which consumers deduplicate on the key; the alternative would be a durable
    write per message on the hot path to buy an exactly-once guarantee the
    target explicitly does not make.
    """
    recovery, counts = _crash_then_recover(outbox, crash_after_send=True)
    assert counts["sent"] == 1
    assert len(recovery.sent) == 1


def test_the_three_windows_are_indistinguishable_by_design(outbox):
    """Both windows leave the SAME database state, asserted side by side.

    Written as one test over both cases rather than trusting that the two tests
    above happen to agree: "the windows are indistinguishable" is a claim about
    their EQUALITY, and only a comparison can assert it.
    """
    outcomes = []
    for crash_after_send in (False, True):
        state = PublisherFixture(outbox.conn)
        try:
            state.add_packet(payload=b"window")
            crashing = CrashingBroker(crash_after_send=crash_after_send)
            try:
                state.cycle(crashing, claim_token="dead").run_once()
            except KeyboardInterrupt:
                pass
            row = state.state(state.alert_ids[-1])
            state.expire_claims()
            counts = state.cycle(RecordingBroker(),
                                 claim_token="recovering").run_once()
            outcomes.append((row[0], row[1], counts["reclaimed"],
                             counts["sent"]))
        finally:
            state.cleanup()

    assert outcomes[0] == outcomes[1], (
        "the pre-send and post-send crash windows produced different database "
        "states; they are indistinguishable by design and must recover "
        "identically")


def test_two_overlapping_cycles_cannot_both_claim_one_row(outbox, second_conn):
    """THE ATOMIC CLAIM, over two genuinely concurrent connections.

    Two `OutboxRepository` instances on two separate connections — not two
    calls on one, which would serialize trivially and prove nothing. The claim
    is `UPDATE ... WHERE state = 'PENDING'`, so the second one's predicate no
    longer matches whatever the first took.
    """
    outbox.add_packet(payload=b"contested")

    other = OutboxRepository(fixture.executor(second_conn),
                             only_release=outbox.release)

    first = outbox.repository.claim_batch("cycle-one")
    second = other.claim_batch("cycle-two")

    assert len(first) == 1
    assert second == []


def test_a_reclaimed_row_does_not_double_count_resends(outbox):
    """Recovery is not a resend, and the counter says so.

    `resend_count` counts SENDS BEYOND THE FIRST. A row reclaimed after a
    pre-send crash was never sent at all, so incrementing on reclaim would make
    the counter a claim counter — and an operator reading a high value would
    conclude the broker was flapping when the publisher had merely restarted.
    """
    outbox.add_packet(payload=b"reclaimed")
    crashing = CrashingBroker(crash_after_send=False)
    try:
        outbox.cycle(crashing, claim_token="dead").run_once()
    except KeyboardInterrupt:
        pass
    outbox.expire_claims()

    outbox.cycle(RecordingBroker(), claim_token="alive").run_once()

    _state, resends, _reason, _sent, _token = outbox.state(outbox.alert_ids[-1])
    assert resends == 0


# ---------------------------------------------------------------------------
# Criterion 6 — delivery policy
# ---------------------------------------------------------------------------

def test_an_unauthorized_release_is_held_not_sent(outbox):
    """Default-DENY, and held rather than refused: the decision is reversible."""
    outbox.set_policy(False, reason="not yet cleared for delivery")
    alert_id = outbox.add_packet()
    broker = RecordingBroker()

    counts = outbox.cycle(broker).run_once()

    assert counts["held"] == 1
    assert broker.sent == []
    state, _resends, _reason, _sent_at, token = outbox.state(alert_id)
    assert state == "PENDING"
    assert token is None


def test_a_release_with_no_policy_row_at_all_is_unauthorized(outbox):
    """ABSENCE IS REFUSAL. A new release must not deliver by default."""
    outbox.execute("DELETE FROM delivery_policies WHERE release_identity = %s",
                   [outbox.release])
    outbox.conn.commit()
    outbox.add_packet()
    broker = RecordingBroker()

    counts = outbox.cycle(broker).run_once()

    assert counts["held"] == 1
    assert broker.sent == []


def test_held_packets_are_visible_in_the_health_view(outbox):
    """Held is a STATE OF THE WORLD, not a stalled backlog to be diagnosed."""
    outbox.set_policy(False, reason="held for the test")
    outbox.add_packet()
    outbox.cycle(RecordingBroker()).run_once()

    rows = outbox.execute(
        "SELECT state, release_authorized, packets FROM alert_outbox_health"
        " WHERE release_identity = %s", [outbox.release])

    assert rows
    state, authorized, packets = rows[0]
    assert state == "PENDING"
    assert authorized is False
    assert packets == 1


def test_authorization_releases_the_backlog_on_the_next_cycle(outbox):
    """The outbox absorbed the hold; nothing was lost by it."""
    outbox.set_policy(False)
    outbox.add_packet()
    broker = RecordingBroker()
    assert outbox.cycle(broker).run_once()["held"] == 1

    outbox.set_policy(True, reason="cleared")
    counts = outbox.cycle(broker).run_once()

    assert counts["sent"] == 1
    assert len(broker.sent) == 1


def test_revocation_between_an_ambiguous_send_and_its_resend_prevents_it(outbox):
    """THE REASON THE CHECK IS ON THE SEND PATH, not at claim time.

    A first send goes out and its acknowledgement is ambiguous, so the packet
    is queued for a resend. Authorization is then REVOKED. The resend must not
    happen — which is only true if the policy is read immediately before every
    send rather than once per cycle or once per claim.
    """
    outbox.add_packet(payload=b"revoked-midway")
    broker = FlakyBroker(failures=1)

    first = outbox.cycle(broker).run_once()
    assert first["resend"] == 1
    assert len(broker.sent) == 1

    outbox.set_policy(False, reason="revoked between send and resend")
    second = outbox.cycle(broker).run_once()

    assert second["held"] == 1
    assert second["sent"] == 0
    # THE BROKER SAW NOTHING NEW. The assertion is on the stub's record rather
    # than on the row's state, because "the resend did not happen" is a fact
    # about the wire.
    assert len(broker.sent) == 1


if __name__ == "__main__":
    unittest.main()
