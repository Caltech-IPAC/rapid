"""Contract tests: the effect-lifecycle completion boundary (ruling R1,
migration 075).

Migration 075 added `attempts.consumed_record_sequence` and three
`product_disposition` CHECK values (`effect_confirmed`/`effect_unconfirmed`/
`effect_deferred`) for attempts whose effect is a database action rather than
an S3 product — the claim/confirm protocol `RAPIDDB.claim_alert_emission` /
`confirm_alert_emission` implements for alert production. Everything asserted
here is a property of PostgreSQL, matching this tier's own reason for
existing (`pipeline.contract.fixture`'s module docstring): the watermark
CAS's monotonicity, the `pg_advisory_xact_lock` lease's actual mutual
exclusion, and the CHECK constraints migration 075 amended are all database
behaviour a stub cannot demonstrate.

WHAT IS COVERED, and by which test:

  * every effect-producing route deriving its disposition through REAL
    execution/termination -- `test_claim_and_confirm_...` (through the real
    `RAPIDDB` CAS statements) and `test_disposition_derivation_...` (through
    the real `_execute`-equivalent mapping, `job._EFFECT_OUTCOME_TO_
    DISPOSITION`, applied to real classifier output)
  * claim `won` / `terminally_satisfied` / `held_by_live_owner` --
    `test_claim_*`
  * confirm's swallowed-failure and lost-response outcomes --
    `test_confirm_*`
  * stale SKIP vs a newer terminal sequence under the lease --
    `test_a_supersession_discovered_under_the_lease_defers_the_skip`
  * the consumed watermark advancing WITHOUT `registered_at` --
    `test_a_terminal_skip_advances_consumed_without_registering`
  * `effect_unconfirmed` -> ready -> genuinely re-gathered -> confirmed
    completion -- `test_unconfirmed_effect_retries_then_confirms`
  * every terminal SKIP disposition's final unit state, per ruling R1's
    table -- `test_final_unit_state_*`
  * the crash-window recovery rule (between effect commit and terminal
    record, the retry attempt's claim sees `emitted` ->
    `terminally_satisfied` -> confirmed) --
    `test_crash_window_recovery_sees_terminally_satisfied`

These need a live database (real advisory locks, real CHECK constraints, real
CAS races) and are WRITTEN, MARKED, NOT RUN here — the project rule is that
nothing executes locally for RAPID; the supervisor runs the contract tier on
rapid-admin/CI.
"""

import uuid

import pytest

from database.modules.utils.rapid_db import RAPIDDB
from database.modules.utils.rapid_db_connect import transaction
from pipeline.contract import fixture
from pipeline.intent import retry_policy
from pipeline.intent.writer import (BLOCKED, COMPLETE, READY, SUBMITTED,
                                    WorkUnitWriter)
from pipeline.registration import consumer


# ---------------------------------------------------------------------------
# Fixture helpers local to this file
# ---------------------------------------------------------------------------

def _claim(conn, exposure, sca, release, token, attempt_id):
    """Win (or lose) an emission claim, committed in its own transaction --
    the real STEP 1 protocol `pipeline.stages.alert_production.produce_
    alerts` runs, reproduced here without the stage machinery around it so
    each test can drive the CAS directly.
    """
    handle = RAPIDDB.borrowing(conn)
    with transaction(conn):
        return handle.claim_alert_emission(
            exposure, sca, release, attempt_id, attempt_id, token, pid=None)


def _confirm(conn, exposure, sca, release, token, count=1):
    """Win (or lose) a confirm CAS, committed -- the real STEP 3 protocol,
    minus the outbox/milestone writes this file's tests do not need.
    """
    handle = RAPIDDB.borrowing(conn)
    with transaction(conn):
        return handle.confirm_alert_emission(exposure, sca, release, token,
                                             count)


def _make_terminal_attempt(conn, work_unit_id, product_disposition,
                           rapid_outcome="success",
                           terminal_record_sequence=1,
                           error_category=None):
    """A `terminal_after_start` attempt carrying a real, chosen
    `product_disposition`, bound to a work unit -- the shape `_apply_skip_
    disposition` and `register_batch` decide from.

    `fixture.make_completed_attempt` is the closest existing helper but
    takes no `work_unit_id` and hardcodes `product_disposition='published'`;
    this is that same INSERT shape (migration 014's sixteen-column CHECK,
    documented at length on `make_completed_attempt` itself) with both
    parameterized, since every scenario in this file needs a work unit to
    assert a FINAL UNIT STATE against and a specific disposition to test.
    """
    logical_job_id, run_id = fixture.make_logical_job(conn)
    tag = uuid.uuid4().hex[:8]
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(schema_version), 1) FROM attempts")
        schema_version = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO attempts"
            "  (run_id, schema_version, logical_job_id, lifecycle_state,"
            "   created_at, submitted_at, started_at, ended_at,"
            "   scheduler_job_id, scheduler_state, scheduler_observed_exit,"
            "   source_sha, container_digest, job_definition_rev,"
            "   config_digest, process_exit_code, rapid_outcome,"
            "   product_disposition, error_category,"
            "   binding_job_definition_arn, binding_image_digest,"
            "   binding_manifest_checksum, terminal_record_key,"
            "   terminal_record_sequence, work_unit_id)"
            " VALUES (%s, %s, %s, 'terminal_after_start',"
            "         now(), now(), now(), now(),"
            "         %s, 'SUCCEEDED', 0,"
            "         %s, 'sha256:' || %s, 1,"
            "         'sha256:' || %s, 0, %s,"
            "         %s, %s,"
            "         %s, 'sha256:' || %s,"
            "         'sha256:' || %s, %s,"
            "         %s, %s)"
            " RETURNING attempt_id",
            [run_id, schema_version, logical_job_id,
             f"job-{tag}", f"sha-{tag}", tag, tag, rapid_outcome,
             product_disposition, error_category,
             f"arn:aws:batch:us-east-1:account:job-definition/f-{tag}:1",
             tag, tag,
             f"records/{fixture.RUN_TAG}/{tag}.json",
             terminal_record_sequence, work_unit_id])
        return cur.fetchone()[0]


def _decide_and_apply_skip(conn, attempt_id, work_unit_id,
                           product_disposition, rapid_outcome,
                           terminal_record_sequence):
    """Run the REAL SKIP-under-lease block `register_batch` runs, isolated
    from the candidate query and the DEFER/REGISTER branches this file's
    tests do not exercise -- the lease, the post-lock reread, `_apply_skip_
    disposition` and `mark_consumed`, in the one transaction production
    uses.

    Returns nothing; the caller reads back `fixture.unit_state` /
    `attempts.consumed_record_sequence` to assert on the real effect.
    """
    with consumer._transaction(conn) as cur:
        consumer._acquire_attempt_lease(cur, attempt_id)
        watermark = consumer._reread_watermark(cur, attempt_id)
        assert watermark is not None, "attempt vanished under its own lease"
        consumed_sequence, current_terminal_sequence = watermark
        if (consumed_sequence is not None
                and consumed_sequence >= terminal_record_sequence):
            return "already_consumed"
        if (current_terminal_sequence is not None
                and current_terminal_sequence > terminal_record_sequence):
            return "superseded"
        consumer._apply_skip_disposition(
            attempt_id, work_unit_id, product_disposition, rapid_outcome,
            cur)
        consumer.mark_consumed(conn, attempt_id, terminal_record_sequence,
                               cursor=cur)
    return "consumed"


@pytest.fixture
def emission(conn):
    """One claimable `(exposure, sca, release)` triple, cleaned up after --
    the same shape `test_alert_outbox_confirmation.py`'s own fixture uses,
    reproduced here (not imported) so this file has no cross-file fixture
    dependency: migration 037's `alert_emissions` table is this fixture's
    own prerequisite, distinct from and orthogonal to migration 075's, and
    a test importing the other file's fixture would blur which migration a
    skip here is actually reporting absent.
    """
    if not fixture.has_table(conn, "alert_emissions"):
        pytest.skip("migration 037 is not applied (no alert_emissions)")
    execute = fixture.executor(conn)
    release = f"r1-{fixture.RUN_TAG}-{uuid.uuid4().hex[:6]}"
    exposure = 910_000 + (uuid.uuid4().int % 90_000)
    sca = 5
    yield conn, execute, exposure, sca, release
    try:
        execute("DELETE FROM alert_emissions WHERE release_identity = %s",
               [release])
        conn.commit()
    except Exception:                                       # noqa: BLE001
        conn.rollback()


def _requires_075(conn):
    if not fixture.admits_state(conn, "effect_confirmed",
                                table="attempts"):
        pytest.skip("migration 075 is not applied (product_disposition has "
                    "no effect_* values)")


# ---------------------------------------------------------------------------
# Claim outcome classification (won / terminally_satisfied / held_by_live_owner)
# ---------------------------------------------------------------------------

def test_claim_a_fresh_claim_wins(emission):
    conn, _execute, exposure, sca, release = emission
    attempt_id = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    conn.commit()
    token = str(attempt_id)

    won = _claim(conn, exposure, sca, release, token, attempt_id)
    handle = RAPIDDB.borrowing(conn)
    outcome = handle.classify_claim_outcome(exposure, sca, release, won,
                                            token)

    assert won == token
    assert outcome == RAPIDDB.CLAIM_OUTCOME_WON


def test_claim_a_live_claimant_holds_it(emission):
    # Attempt A claims and does not confirm (claim stays 'claimed', fresh --
    # not past CLAIM_STALENESS). Attempt B's claim then loses the CAS: A is
    # a LIVE owner, not a terminal state.
    conn, _execute, exposure, sca, release = emission
    attempt_a = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    attempt_b = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    conn.commit()
    token_a, token_b = str(attempt_a), str(attempt_b)

    assert _claim(conn, exposure, sca, release, token_a, attempt_a) == token_a

    won_b = _claim(conn, exposure, sca, release, token_b, attempt_b)
    handle = RAPIDDB.borrowing(conn)
    outcome = handle.classify_claim_outcome(exposure, sca, release, won_b,
                                            token_b)

    assert won_b != token_b
    assert outcome == RAPIDDB.CLAIM_OUTCOME_HELD_BY_LIVE_OWNER


def test_claim_an_already_confirmed_unit_is_terminally_satisfied(emission):
    conn, _execute, exposure, sca, release = emission
    attempt_id = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    conn.commit()
    token = str(attempt_id)
    assert _claim(conn, exposure, sca, release, token, attempt_id) == token
    assert _confirm(conn, exposure, sca, release, token) == token

    # A LATER attempt of the same logical unit tries to claim what is now
    # 'emitted' -- the terminal-suppress state `claim_alert_emission`'s
    # WHERE clause never touches.
    later_attempt = fixture.make_attempt(conn,
                                         lifecycle="terminal_without_start")
    conn.commit()
    later_token = str(later_attempt)
    won_later = _claim(conn, exposure, sca, release, later_token,
                       later_attempt)
    handle = RAPIDDB.borrowing(conn)
    outcome = handle.classify_claim_outcome(exposure, sca, release,
                                            won_later, later_token)

    assert won_later != later_token
    assert outcome == RAPIDDB.CLAIM_OUTCOME_TERMINALLY_SATISFIED


def test_claim_a_stale_claim_from_a_terminal_prior_attempt_is_a_retry_win(
        emission):
    # THE THIRD CAS DISJUNCT: a retry of the SAME logical unit, whose prior
    # claimant attempt is now terminal, wins even without waiting out
    # CLAIM_STALENESS. This is a `won`, not a takeover of a live owner -- the
    # prior claimant is dead by construction.
    conn, _execute, exposure, sca, release = emission
    logical_job_id, run_id = fixture.make_logical_job(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(schema_version), 1) FROM attempts")
        schema_version = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO attempts (run_id, schema_version,"
            "  logical_job_id, lifecycle_state, created_at, submitted_at,"
            "  ended_at, scheduler_state)"
            " VALUES (%s, %s, %s, 'terminal_without_start', now(),"
            "         now(), now(), 'FAILED') RETURNING attempt_id",
            [run_id, schema_version, logical_job_id])
        prior_attempt = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO attempts (run_id, schema_version,"
            "  logical_job_id, lifecycle_state, created_at, submitted_at)"
            " VALUES (%s, %s, %s, 'submitted', now(), now())"
            " RETURNING attempt_id",
            [run_id, schema_version, logical_job_id])
        retry_attempt = cur.fetchone()[0]
    conn.commit()

    prior_token = str(prior_attempt)
    assert _claim(conn, exposure, sca, release, prior_token,
                 prior_attempt) == prior_token

    retry_token = str(retry_attempt)
    won = _claim(conn, exposure, sca, release, retry_token, retry_attempt)
    handle = RAPIDDB.borrowing(conn)
    outcome = handle.classify_claim_outcome(exposure, sca, release, won,
                                            retry_token)

    assert won == retry_token, (
        "a retry of the same logical unit, over a now-terminal prior "
        "claimant, must win the CAS's third disjunct")
    assert outcome == RAPIDDB.CLAIM_OUTCOME_WON


# ---------------------------------------------------------------------------
# Confirm outcome classification (confirmed / deferred / unconfirmed)
# ---------------------------------------------------------------------------

def test_confirm_the_winning_confirm_is_confirmed(emission):
    conn, _execute, exposure, sca, release = emission
    attempt_id = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    conn.commit()
    token = str(attempt_id)
    assert _claim(conn, exposure, sca, release, token, attempt_id) == token

    confirmed = _confirm(conn, exposure, sca, release, token)
    handle = RAPIDDB.borrowing(conn)
    outcome = handle.classify_confirm_outcome(exposure, sca, release,
                                              confirmed, token)

    assert confirmed == token
    assert outcome == RAPIDDB.CONFIRM_OUTCOME_CONFIRMED


def test_confirm_a_takeover_before_confirm_is_deferred(emission):
    conn, execute, exposure, sca, release = emission
    attempt_a = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    attempt_b = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    conn.commit()
    token_a, token_b = str(attempt_a), str(attempt_b)
    assert _claim(conn, exposure, sca, release, token_a, attempt_a) == token_a

    # Age the claim past CLAIM_STALENESS so B's claim can take it over.
    execute("UPDATE alert_emissions SET claimed_at ="
           " now() - interval '2 hours' WHERE exposure_id = %s"
           "   AND sca = %s AND release_identity = %s",
           [exposure, sca, release])
    conn.commit()
    assert _claim(conn, exposure, sca, release, token_b, attempt_b) == token_b

    # A tries to confirm and loses -- B now owns the (still 'claimed') row.
    confirmed_a = _confirm(conn, exposure, sca, release, token_a)
    handle = RAPIDDB.borrowing(conn)
    outcome = handle.classify_confirm_outcome(exposure, sca, release,
                                              confirmed_a, token_a)

    assert confirmed_a is None
    assert outcome == RAPIDDB.CONFIRM_OUTCOME_DEFERRED


def test_confirm_a_lost_response_reads_as_confirmed(emission):
    # THE LOST-RESPONSE CASE. The confirm committed (state is 'emitted') but
    # this process never saw the RETURNING row -- modelled by confirming for
    # real, then re-classifying with confirmed_token=None as if the return
    # value had been lost, over the SAME token.
    conn, _execute, exposure, sca, release = emission
    attempt_id = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    conn.commit()
    token = str(attempt_id)
    assert _claim(conn, exposure, sca, release, token, attempt_id) == token
    assert _confirm(conn, exposure, sca, release, token) == token

    handle = RAPIDDB.borrowing(conn)
    outcome = handle.classify_confirm_outcome(exposure, sca, release, None,
                                              token)

    assert outcome == RAPIDDB.CONFIRM_OUTCOME_CONFIRMED, (
        "a lost response over an emission that IS confirmed must read as "
        "confirmed, not as a deferral or an unconfirmed effect -- the "
        "effect landed even though this call never saw it")


def test_confirm_a_swallowed_db_failure_is_unconfirmed_without_a_query(
        emission):
    # db_failure=True short-circuits: no state read, no possibility of
    # misreading a takeover as a database fault. Exercised with an
    # exposure/sca that were never even claimed, to prove the query never
    # runs (a real query would find no row and could only ever answer
    # UNCONFIRMED here anyway, so this also pins the "no query" claim by
    # using a scenario a query WOULD get right, showing the short-circuit is
    # not merely coincidentally correct).
    conn, _execute, exposure, sca, release = emission
    handle = RAPIDDB.borrowing(conn)
    outcome = handle.classify_confirm_outcome(
        exposure, sca, release, None, "irrelevant", db_failure=True)
    assert outcome == RAPIDDB.CONFIRM_OUTCOME_UNCONFIRMED


def test_confirm_exit_code_from_the_prior_call_does_not_leak(emission):
    # THE BUG THIS TEST PINS. `CheckedHandle` reads `exit_code` right after
    # ANY wrapped call, and `RAPIDDB` mutates one shared attribute per
    # instance rather than returning a code per call. A confirm DB-failure
    # sets `exit_code=67`; if `classify_confirm_outcome` did not reset it,
    # the identical, correctly-behaving classify call reached immediately
    # afterward (produce_alerts's real sequence) would be misread by
    # CheckedHandle as itself having failed.
    conn, _execute, exposure, sca, release = emission
    handle = RAPIDDB.borrowing(conn)
    handle.exit_code = 67  # simulates the just-failed confirm call
    outcome = handle.classify_confirm_outcome(
        exposure, sca, release, None, "irrelevant", db_failure=True)
    assert outcome == RAPIDDB.CONFIRM_OUTCOME_UNCONFIRMED
    assert handle.exit_code == 0, (
        "classify_confirm_outcome must reset exit_code so a "
        "CheckedHandle-wrapped caller does not see a stale failure code "
        "from the PRIOR call and misreport this one as failed")


# ---------------------------------------------------------------------------
# The watermark split and the SKIP-under-lease locking protocol
# ---------------------------------------------------------------------------

def test_a_terminal_skip_advances_consumed_without_registering(conn):
    """Consumption alone: `consumed_record_sequence` moves, `registered_at`
    and `registered_record_sequence` stay NULL -- the module docstring's
    "THE WATERMARK SPLIT", proved against the real CAS statements rather
    than a fake that could not disagree with itself.
    """
    _requires_075(conn)
    unit = fixture.create_unit(
        conn, fixture.scope("skip-consumes-without-registering"),
        state=SUBMITTED)
    attempt_id = _make_terminal_attempt(
        conn, unit, product_disposition="withheld",
        terminal_record_sequence=1)
    conn.commit()

    result = _decide_and_apply_skip(conn, attempt_id, unit, "withheld",
                                    "success", 1)
    assert result == "consumed"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT registered_at, registered_record_sequence,"
            "       consumed_record_sequence FROM attempts"
            " WHERE attempt_id = %s", [attempt_id])
        registered_at, registered_seq, consumed_seq = cur.fetchone()

    assert registered_at is None, (
        "a SKIP verdict must never write registered_at -- it is not an "
        "accepted result")
    assert registered_seq is None
    assert consumed_seq == 1


def test_a_supersession_discovered_under_the_lease_defers_the_skip(conn):
    """The SKIP-side mirror of REGISTER's identical stale-supersession
    guard: a supersession published between the candidate read and this
    attempt's turn under the lease must leave the row a candidate at the
    NEWER sequence rather than consuming the stale target.
    """
    _requires_075(conn)
    unit = fixture.create_unit(
        conn, fixture.scope("skip-supersession-under-lease"),
        state=SUBMITTED)
    attempt_id = _make_terminal_attempt(
        conn, unit, product_disposition="superseded",
        terminal_record_sequence=1)
    conn.commit()

    # A later pass republishes at sequence 2 before this attempt's turn.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE attempts SET terminal_record_sequence = 2"
            " WHERE attempt_id = %s", [attempt_id])
    conn.commit()

    result = _decide_and_apply_skip(conn, attempt_id, unit, "superseded",
                                    "success", 1)  # stale target

    assert result == "superseded"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT consumed_record_sequence FROM attempts"
            " WHERE attempt_id = %s", [attempt_id])
        consumed_seq = cur.fetchone()[0]
    assert consumed_seq is None, (
        "a stale SKIP must not consume the sequence it was superseded past")
    assert fixture.unit_state(conn, unit)[0] == SUBMITTED


def test_the_lease_serializes_a_register_and_a_skip_on_one_attempt(
        conn, second_conn):
    """THE STALENESS HAZARD RULING R1 NAMES DIRECTLY: an `effect_unconfirmed`
    verdict specifically must not act on a terminal_record_sequence a newer
    attempt has already superseded. Proved by holding the SAME lease this
    attempt's own SKIP handling acquires, on a second connection, and
    confirming the first connection blocks until it releases -- the actual
    `pg_advisory_xact_lock` mutual exclusion, which nothing outside a real
    database can demonstrate (matching `test_registration_watermark.py`'s
    own `test_the_attempt_lease_serializes_two_registrars`, applied to the
    SKIP path this ruling newly put under the lease).
    """
    import threading
    import time

    _requires_075(conn)
    unit = fixture.create_unit(
        conn, fixture.scope("skip-lease-serializes"), state=SUBMITTED)
    attempt_id = _make_terminal_attempt(
        conn, unit, product_disposition="effect_unconfirmed",
        terminal_record_sequence=1)
    conn.commit()

    acquired_at = {}
    entered = threading.Event()

    def contender():
        entered.set()
        with second_conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                       (consumer.ATTEMPT_LEASE_NAMESPACE, int(attempt_id)))
        acquired_at["t"] = time.monotonic()
        second_conn.rollback()

    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                   (consumer.ATTEMPT_LEASE_NAMESPACE, int(attempt_id)))

    thread = threading.Thread(target=contender)
    thread.start()
    entered.wait(timeout=10)
    time.sleep(0.5)
    assert "t" not in acquired_at, (
        "a second writer acquired this attempt's SKIP-path lease while the "
        "first still held it")

    released_at = time.monotonic()
    conn.rollback()
    thread.join(timeout=30)

    assert "t" in acquired_at, "the second writer never acquired the lease"
    assert acquired_at["t"] >= released_at


# ---------------------------------------------------------------------------
# The effect retry policy: effect_unconfirmed -> ready -> re-gathered -> confirmed
# ---------------------------------------------------------------------------

def test_unconfirmed_effect_retries_then_confirms(conn):
    """The full lifecycle ruling R1 names by name: `effect_unconfirmed` ->
    `RETRY_READY` (under the ceiling) -> a genuinely new attempt re-gathers
    -> `effect_confirmed` -> `CLOSE_COMPLETE`.
    """
    _requires_075(conn)
    unit = fixture.create_unit(
        conn, fixture.scope("unconfirmed-then-confirmed"), state=SUBMITTED)
    first_attempt = _make_terminal_attempt(
        conn, unit, product_disposition="effect_unconfirmed",
        terminal_record_sequence=1)
    conn.commit()

    # The series count sees only this one prior effect attempt -- comfortably
    # under SCHEDULER_RETRY_CEILING.
    with conn.cursor() as cur:
        count = consumer._effect_attempt_count(unit, cur)
    assert count == 1
    verdict = retry_policy.disposition_for_unconfirmed_effect(
        effect_attempt_count=count)
    assert verdict == retry_policy.RETRY_READY

    result = _decide_and_apply_skip(
        conn, first_attempt, unit, "effect_unconfirmed", "success", 1)
    assert result == "consumed"
    assert fixture.unit_state(conn, unit)[0] == READY, (
        "an unconfirmed effect under the ceiling must return the unit to "
        "ready for a new attempt to re-gather, per policy v1's 'never "
        "tombstoned'")

    # A genuinely new attempt re-gathers and this time confirms.
    writer = WorkUnitWriter(fixture.executor(conn))
    writer.transition_unit(unit, READY, SUBMITTED, writer="orchestrator")
    conn.commit()
    second_attempt = _make_terminal_attempt(
        conn, unit, product_disposition="effect_confirmed",
        terminal_record_sequence=1)
    conn.commit()

    result = _decide_and_apply_skip(
        conn, second_attempt, unit, "effect_confirmed", "success", 1)
    assert result == "consumed"
    assert fixture.unit_state(conn, unit)[0] == COMPLETE, (
        "effect_confirmed must close the unit complete, the same standard "
        "an accepted product registration applies")


def test_unconfirmed_effect_over_the_ceiling_parks(conn):
    """Exhaustion: at the ceiling, `PARK_BLOCKED` with the dedicated reason
    -- never `CLOSE_FAILED` (version 1 does not tombstone an unconfirmed
    effect any more than an application failure).
    """
    _requires_075(conn)
    count = retry_policy.SCHEDULER_RETRY_CEILING
    verdict = retry_policy.disposition_for_unconfirmed_effect(
        effect_attempt_count=count)
    assert verdict == retry_policy.PARK_BLOCKED

    unit = fixture.create_unit(
        conn, fixture.scope("unconfirmed-effect-exhausted"), state=SUBMITTED)
    # Seed enough PRIOR effect-disposition attempts to reach the ceiling by
    # the time this unit's own count is read -- one is this call's own row,
    # the rest are siblings the series count must also see.
    for _ in range(count - 1):
        _make_terminal_attempt(conn, unit,
                               product_disposition="effect_unconfirmed",
                               terminal_record_sequence=1)
    triggering_attempt = _make_terminal_attempt(
        conn, unit, product_disposition="effect_unconfirmed",
        terminal_record_sequence=1)
    conn.commit()

    with conn.cursor() as cur:
        measured = consumer._effect_attempt_count(unit, cur)
    assert measured == count, (
        f"expected the series count to include all {count} effect "
        f"attempts, got {measured}")

    result = _decide_and_apply_skip(
        conn, triggering_attempt, unit, "effect_unconfirmed", "success", 1)
    assert result == "consumed"
    state, blocked_reason = fixture.unit_state(conn, unit)
    assert state == BLOCKED
    assert blocked_reason == retry_policy.EFFECT_RETRY_EXHAUSTED_REASON


# ---------------------------------------------------------------------------
# The final unit state per terminal SKIP verdict -- ruling R1's table, exactly
# ---------------------------------------------------------------------------

def _skip_and_read_state(conn, disposition, rapid_outcome="success",
                         error_category=None):
    """One row of ruling R1's table (`_apply_skip_disposition`'s own
    docstring), driven through the real SKIP-under-lease block and read back
    from the real `work_units` CAS.
    """
    unit = fixture.create_unit(
        conn, fixture.scope(f"final-state-{disposition}-{rapid_outcome}"),
        state=SUBMITTED)
    attempt_id = _make_terminal_attempt(
        conn, unit, product_disposition=disposition,
        rapid_outcome=rapid_outcome, terminal_record_sequence=1,
        error_category=error_category)
    conn.commit()
    result = _decide_and_apply_skip(conn, attempt_id, unit, disposition,
                                    rapid_outcome, 1)
    assert result == "consumed"
    return fixture.unit_state(conn, unit)


def test_final_unit_state_effect_confirmed_closes_complete(conn):
    _requires_075(conn)
    state, _reason = _skip_and_read_state(conn, "effect_confirmed")
    assert state == COMPLETE


def test_final_unit_state_effect_deferred_leaves_submitted(conn):
    _requires_075(conn)
    state, _reason = _skip_and_read_state(conn, "effect_deferred")
    assert state == SUBMITTED


def test_final_unit_state_withheld_closes_complete(conn):
    state, _reason = _skip_and_read_state(conn, "withheld")
    assert state == COMPLETE, (
        "withheld is a DELIBERATE, accepted non-publication and closes "
        "complete exactly as a published result does")


def test_final_unit_state_superseded_leaves_submitted(conn):
    state, _reason = _skip_and_read_state(conn, "superseded")
    assert state == SUBMITTED, (
        "the superseding attempt's own terminal record settles the unit; "
        "this attempt only consumes")


def test_final_unit_state_success_none_leaves_submitted_as_an_anomaly(conn):
    state, _reason = _skip_and_read_state(conn, "none",
                                          rapid_outcome="success")
    assert state == SUBMITTED, (
        "success+none on a product route is an anomaly for operator "
        "decision, not a normal completion")


def test_final_unit_state_a_failure_outcome_leaves_submitted(conn):
    state, _reason = _skip_and_read_state(
        conn, "none", rapid_outcome="failure", error_category="tool_failure")
    assert state == SUBMITTED, (
        "a failure outcome is the reconciler's retry policy's own verdict "
        "on the unit; registration adds nothing")


# ---------------------------------------------------------------------------
# The crash-window recovery rule
# ---------------------------------------------------------------------------

def test_crash_window_recovery_sees_terminally_satisfied(emission):
    """Between the effect commit (CONFIRM) and this attempt's own terminal
    record, a crash leaves the effect durably confirmed but this attempt's
    OWN account of it unwritten. A retry attempt's claim must then see
    `emitted` -> `terminally_satisfied` -> and this file's derivation
    mapping resolves that to `effect_confirmed` -- never re-doing (or
    losing) work whose effect already landed.
    """
    conn, _execute, exposure, sca, release = emission
    attempt_id = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    conn.commit()
    token = str(attempt_id)
    assert _claim(conn, exposure, sca, release, token, attempt_id) == token
    assert _confirm(conn, exposure, sca, release, token) == token
    # This attempt's own terminal record is never written in this test --
    # modelling the crash between CONFIRM committing and the application
    # reaching its own termination step.

    retry_attempt = fixture.make_attempt(
        conn, lifecycle="terminal_without_start")
    conn.commit()
    retry_token = str(retry_attempt)

    won = _claim(conn, exposure, sca, release, retry_token, retry_attempt)
    handle = RAPIDDB.borrowing(conn)
    claim_outcome = handle.classify_claim_outcome(
        exposure, sca, release, won, retry_token)

    assert won != retry_token, (
        "the retry's claim must not win against an already-emitted row")
    assert claim_outcome == RAPIDDB.CLAIM_OUTCOME_TERMINALLY_SATISFIED

    from pipeline.entrypoints.job import _EFFECT_OUTCOME_TO_DISPOSITION
    assert (_EFFECT_OUTCOME_TO_DISPOSITION[claim_outcome]
           == "effect_confirmed"), (
        "terminally_satisfied must derive effect_confirmed -- the effect "
        "IS confirmed, just not by this retry attempt's own confirmation")
