"""Contract tests: the registration watermark CAS and the advisory-lock lease
(rule 9, integration ruling 4).

**THIS IS THE FAMILY THE BRIEF NAMES AS THE DEMONSTRATION** ("fails on a
real-semantics defect that the stub tier cannot express — demonstrate with the
watermark CAS predicate or advisory-lock scoping"). Both are here, and both
are properties of PostgreSQL:

  * the CAS predicate `registered_record_sequence IS NULL OR
    registered_record_sequence < %s` is evaluated by the database against
    whatever another transaction committed while this one was waiting. A fake
    executor evaluates it against a value the test itself set, which makes
    the test a tautology: the predicate always agrees with the fake because
    the fake IS the predicate's input.
  * `pg_advisory_xact_lock(namespace, id)` serializes two writers on one
    attempt id and — the scoping half — does NOT serialize writers on
    different ids, or on the same id under a different namespace. That last
    property is why the registrar (0x5234) and the reconciler (0x5732)
    deliberately do not collide, and nothing outside a real database can
    demonstrate it at all.
"""

import threading
import time

from pipeline.contract import fixture
from pipeline.registration.consumer import (ATTEMPT_LEASE_NAMESPACE,
                                            _MARK_REGISTERED_SQL,
                                            candidates, mark_registered)


def _watermark(conn, attempt_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT registered_record_sequence, terminal_record_sequence"
            "  FROM attempts WHERE attempt_id = %s", [attempt_id])
        return cur.fetchone()


def test_the_watermark_cas_refuses_to_move_backwards(conn):
    """A lower sequence must not overwrite a higher one.

    THE PREDICATE IS THE TEST. `_MARK_REGISTERED_SQL` guards on
    `registered_record_sequence IS NULL OR registered_record_sequence < %s`,
    so registering sequence 1 after sequence 2 has landed must match zero
    rows. Against a fake, "zero rows" is whatever the fake was told to
    return; here it is the database's own evaluation of the row it holds.

    This matters beyond tidiness: the watermark is what makes re-registration
    on a later supersession work. A watermark that could regress would make
    an attempt eligible for registration forever, which is the unbounded
    re-registration loop the watermark was added to stop.
    """
    attempt_id = fixture.make_attempt(
        conn, lifecycle="terminal_without_start", terminal_record_sequence=2)
    conn.commit()

    mark_registered(conn, attempt_id, 2)
    conn.commit()
    assert _watermark(conn, attempt_id)[0] == 2

    with conn.cursor() as cur:
        # Five placeholders since ruling R1 (migration 075): moment,
        # registered_record_sequence, consumed_record_sequence, attempt_id,
        # the CAS bound — see `_MARK_REGISTERED_SQL`'s own docstring.
        cur.execute(_MARK_REGISTERED_SQL,
                    ("2026-01-01T00:00:00+00:00", 1, 1, attempt_id, 1))
        refused = cur.rowcount
    conn.commit()

    assert refused == 0, (
        "the watermark CAS matched a row for a LOWER sequence; the predicate "
        "is not guarding monotonicity")
    assert _watermark(conn, attempt_id)[0] == 2, "the watermark regressed"


def test_a_later_record_sequence_makes_the_attempt_a_candidate_again(conn):
    """Registered at 1, reconciler publishes 2 -> candidate again.

    The candidate predicate and the watermark are one mechanism, and this is
    the behaviour they exist for. Asserted through the production
    `candidates()` query rather than a hand-written SELECT, so a change to
    that SQL is caught here.
    """
    attempt_id = fixture.make_attempt(
        conn, lifecycle="terminal_without_start", terminal_record_sequence=1)
    conn.commit()

    def is_candidate():
        return any(row["attempt_id"] == attempt_id
                   for row in candidates(conn))

    assert is_candidate(), "a reconciled, unregistered attempt is not a candidate"

    mark_registered(conn, attempt_id, 1)
    conn.commit()
    assert not is_candidate(), (
        "an attempt registered at its current record sequence is still a "
        "candidate — the watermark predicate is not filtering it")

    # The reconciler supersedes the record: sequence 2.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE attempts SET terminal_record_sequence = 2"
            " WHERE attempt_id = %s", [attempt_id])
    conn.commit()

    assert is_candidate(), (
        "a superseded record (sequence 2 > watermark 1) did not make the "
        "attempt a candidate again")


def test_the_attempt_lease_serializes_two_registrars(conn, second_conn):
    """Two transactions holding the same attempt's lease do not overlap.

    The lease is `pg_advisory_xact_lock(0x5234, attempt_id)`, blocking, taken
    as the first statement of the attempt's transaction. This test holds it
    on one connection, has a second connection try to take it, and asserts
    the second was still waiting while the first held it — the actual mutual
    exclusion, not the fact that a function was called.

    NO SLEEP-BASED PASS. The second thread records the moment it acquires;
    the main thread records the moment it commits and releases. The assertion
    is on the ORDER of those two events, so a slow machine makes the test
    slower, never falser.
    """
    attempt_id = fixture.make_attempt(conn, lifecycle="terminal_without_start",
                                      terminal_record_sequence=1)
    conn.commit()

    acquired_at = {}
    entered = threading.Event()

    def contender():
        entered.set()
        with second_conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                        (ATTEMPT_LEASE_NAMESPACE, int(attempt_id)))
        acquired_at["t"] = time.monotonic()
        second_conn.rollback()

    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                    (ATTEMPT_LEASE_NAMESPACE, int(attempt_id)))

    thread = threading.Thread(target=contender)
    thread.start()
    entered.wait(timeout=10)
    # The contender is now blocked in the database, not merely un-started:
    # give it a moment to reach the lock, then confirm it has NOT acquired.
    time.sleep(0.5)
    assert "t" not in acquired_at, (
        "the second registrar acquired the lease while the first still held "
        "it — the attempts are not serialized")

    released_at = time.monotonic()
    conn.rollback()                      # releases the xact lock
    thread.join(timeout=30)

    assert "t" in acquired_at, "the second registrar never acquired the lease"
    assert acquired_at["t"] >= released_at, (
        "the second lease predates the first's release")


def test_the_lease_namespace_scopes_the_lock(conn, second_conn):
    """The SAME attempt id under the reconciler's namespace does NOT block.

    ADVISORY-LOCK SCOPING, the brief's other named demonstration. The
    registrar's 0x5234 and the reconciler's 0x5732 lease the same integer id
    for different concerns, and the design depends on them not colliding.
    That is a two-argument-form property of PostgreSQL: nothing in Python
    observes it, and a fake that recorded "lock taken on attempt 7" would
    report a collision that does not exist — the very false alarm this
    separation was designed to avoid.
    """
    from pipeline.reconciler.lease import LEASE_NAMESPACE

    assert LEASE_NAMESPACE != ATTEMPT_LEASE_NAMESPACE, (
        "the two lease namespaces have converged; the scoping this test "
        "asserts is no longer meaningful")

    attempt_id = fixture.make_attempt(conn, lifecycle="terminal_without_start",
                                      terminal_record_sequence=1)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                    (ATTEMPT_LEASE_NAMESPACE, int(attempt_id)))

    # A DIFFERENT namespace, same id: must be free. `try_` so the test fails
    # fast with a verdict rather than deadlocking if the scoping is broken.
    with second_conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_xact_lock(%s, %s)",
                    (LEASE_NAMESPACE, int(attempt_id)))
        got_other_namespace = cur.fetchone()[0]

    # And the SAME namespace, same id: must be taken. Without this half the
    # test above would pass against a database taking no locks at all.
    with second_conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_xact_lock(%s, %s)",
                    (ATTEMPT_LEASE_NAMESPACE, int(attempt_id)))
        got_same_namespace = cur.fetchone()[0]

    second_conn.rollback()
    conn.rollback()

    assert got_other_namespace is True, (
        "the reconciler's namespace collided with the registrar's on one "
        "attempt id; the two leases are not independent")
    assert got_same_namespace is False, (
        "a second lease on the SAME namespace and id succeeded; the lock is "
        "not exclusive")
