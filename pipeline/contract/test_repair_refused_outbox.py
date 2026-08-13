"""Contract tests: `derived.repair_refused_outbox_rows` (migration 059).

Adopted 2026-08-13 from the `rapid` repository's own
`migrations-draft/DRAFT-repair-refused-outbox.sql`
(`migrations-draft/README.md`, "Adopted separately, 2026-08-13") — an
audited operate-tier repair moving REFUSED `alert_outbox` rows for one
`release_identity` back to PENDING once an operator has fixed the
external condition (broker ACL, client/broker version) that
terminalized them. Requires 050 (`alert_outbox`, the state machine and
its shape CHECKs) and 047 (`derived.mutation_audit`'s
`idempotency_key`/`expected_state` columns, `derived.mutation_replay`)
— both already exercised by `test_operator_mutations.py` and
`test_alert_outbox_grants.py`, which this file borrows its fixture-row
conventions from directly.

**GATED ON THE FUNCTION ITSELF, NOT ON 047's PROBE.** `test_operator_
mutations.py`'s `_requires_draft_047` asks whether `retry_parked_
attempts` exists at 8 arguments — true wherever 047 landed, which is not
the same fact as "059 landed too". A database behind 047+050 but short
of 059 exists (any environment mid-rollout onto the bumped
`RAPID_SYSTEMS_REF`), and this suite must skip cleanly there rather than
fail on an `UndefinedFunction` that looks like a broken test. So this
file probes `derived.repair_refused_outbox_rows` directly, the same
"ask the catalog, don't assume" discipline `fixture.has_function`
already documents for 046's entry point.

WHY THE ROW BUILDER GOES THROUGH `insert_alert_outbox_packet` PLUS A
STATE WALK, NOT A BARE INSERT INTO A REFUSED ROW. `alert_outbox_refusal_
shape_ck` (050) requires `refusal_reason IS NOT NULL` exactly when
`state = 'REFUSED'`, and `alert_outbox_claim_shape_ck` ties the claim
columns to `IN_FLIGHT`. A REFUSED row is therefore only reachable by the
same PENDING -> IN_FLIGHT -> REFUSED walk the publisher's own state
machine takes (`test_alert_outbox_grants.py`'s `test_a_refused_row_
cannot_be_deleted_even_by_the_owner` builds its one REFUSED fixture row
the same way) — a fixture that skipped the walk and wrote REFUSED
directly would either violate the CHECK or leave the claim columns in a
shape 059's own UPDATE does not expect to find them in.
"""

import hashlib
import uuid

import pytest

from pipeline.contract import fixture
from pipeline.operatorctl import actions
from pipeline.operatorctl.contract import ExpectedStateMismatch

SCHEMA_VERSION_ID = "00000000-0000-0000-0000-b00000000001"


def _require_059(conn):
    if not fixture.has_function(conn, "repair_refused_outbox_rows"):
        pytest.skip(
            "derived.repair_refused_outbox_rows (migration 059) is not "
            "applied on this database; this suite needs it and skips "
            "cleanly without it")


def _key(name):
    """A key unique to this test and this run, matching `test_operator_
    mutations.py`'s `_key` convention exactly.
    """
    return "repair-outbox-%s-%s-%s" % (name, fixture.RUN_TAG,
                                       uuid.uuid4().hex[:8])


def _release(name):
    return "repair-outbox-%s-%s" % (name, fixture.RUN_TAG)


def _outbox_alert_id(name):
    """A run-unique AND call-unique alert id, `alert_outbox_alert_id_shape_ck`
    satisfied (`^sha256:[0-9a-f]{64}$`).

    Mirrors `test_alert_outbox_grants.py`'s `_outbox_alert_id`: `RUN_TAG`
    alone collides across two tests in one process asking for the same
    `name`, which is exactly the failure that suite's own docstring
    records hitting.
    """
    digest = hashlib.sha256(
        ("%s-%s-%s" % (name, fixture.RUN_TAG, uuid.uuid4().hex)).encode()
    ).hexdigest()
    return "sha256:" + digest


def _insert_pending(conn, alert_id, release, payload=b"repair-outbox-bytes"):
    """One PENDING `alert_outbox` row, through the real insert function.

    Through `insert_alert_outbox_packet`, not a bare INSERT — the same
    discipline `test_alert_outbox_identity.py` and `test_alert_outbox_
    grants.py` both hold to: a hand-written INSERT would build a row the
    production writer never builds.
    """
    import psycopg2

    checksum = "sha256:" + hashlib.sha256(payload).hexdigest()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT insert_alert_outbox_packet("
            "  %s, 'product-key', %s, %s, %s::uuid, 'alerts.live', %s,"
            "  1, 1, NULL)",
            [alert_id, psycopg2.Binary(payload), checksum,
             SCHEMA_VERSION_ID, release])
        return cur.fetchone()[0]


def _claim(conn, alert_id, token="repair-outbox-claim"):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE alert_outbox SET state = 'IN_FLIGHT',"
            "   claim_token = %s, claimed_at = now()"
            " WHERE alert_id = %s", [token, alert_id])


def _refuse(conn, alert_id, reason="topic authorization denied"):
    """Walk a claimed row to REFUSED — the publisher's own transition."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE alert_outbox SET state = 'REFUSED',"
            "   refusal_reason = %s, claim_token = NULL, claimed_at = NULL"
            " WHERE alert_id = %s", [reason, alert_id])


def _send(conn, alert_id):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE alert_outbox SET state = 'SENT', sent_at = now(),"
            "   claim_token = NULL, claimed_at = NULL"
            " WHERE alert_id = %s", [alert_id])


def make_refused_row(conn, release, name="refused", resend_count=None):
    """One REFUSED row for `release`, through the real state walk.

    Returns its `alert_id`. `resend_count` is set afterward, directly,
    when a test needs a nonzero value to prove the repair leaves it
    alone — `insert_alert_outbox_packet` always starts a row at 0 and
    nothing in the ordinary claim/refuse walk advances it, so there is no
    production path this fixture could take instead.
    """
    alert_id = _outbox_alert_id(name)
    _insert_pending(conn, alert_id, release)
    _claim(conn, alert_id, token=alert_id[-16:])
    _refuse(conn, alert_id)
    if resend_count is not None:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE alert_outbox SET resend_count = %s"
                " WHERE alert_id = %s", [resend_count, alert_id])
    return alert_id


def _row(conn, alert_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state, claim_token, claimed_at, refusal_reason,"
            "       resend_count FROM alert_outbox WHERE alert_id = %s",
            [alert_id])
        columns = [d[0] for d in cur.description]
        row = cur.fetchone()
        return dict(zip(columns, row)) if row else None


def _cleanup(conn, release):
    """Undo the delivered-row protection, then remove every row this
    release's tests wrote — mirrors `test_alert_outbox_identity.py`'s
    `outbox_db` and `test_alert_outbox_confirmation.py`'s `emission`
    teardown exactly: `PENDING` is the one state the DELETE trigger does
    not guard, so every row is walked back to it first.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE alert_outbox SET state = 'PENDING', sent_at = NULL,"
                "   claim_token = NULL, claimed_at = NULL,"
                "   refusal_reason = NULL WHERE release_identity = %s",
                [release])
            cur.execute("DELETE FROM alert_outbox WHERE release_identity = %s",
                        [release])
        conn.commit()
    except Exception:                                           # noqa: BLE001
        conn.rollback()


def _audit_rows(conn, key):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT audit_id, action_class, action_tier, target_scope,"
            "       reason, dry_run, rows_affected, idempotency_key,"
            "       expected_state, detail"
            "  FROM derived.mutation_audit"
            " WHERE idempotency_key = %s ORDER BY audit_id", [key])
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Dry run: counts, changes nothing, audits the rehearsal.
# ---------------------------------------------------------------------------
def test_dry_run_counts_without_mutating(conn):
    """The dry run reports the real REFUSED population and touches nothing."""
    _require_059(conn)
    release = _release("dry")
    try:
        alert_id = make_refused_row(conn, release)
        conn.commit()

        key = _key("dry")
        result = actions.repair_refused_outbox_rows(
            conn, key, release, "contract test: dry-run rehearsal",
            dry_run=True)

        assert result["dry_run"] is True
        assert result["replayed"] is False
        assert result["candidates"] == 1
        assert result["rows_affected"] == 0

        row = _row(conn, alert_id)
        assert row["state"] == "REFUSED", "the dry run mutated the row"
        assert row["refusal_reason"] is not None

        rows = _audit_rows(conn, key)
        assert len(rows) == 1, "a dry run is audited too"
        assert rows[0]["dry_run"] is True
        assert rows[0]["rows_affected"] == 0
        assert rows[0]["action_class"] == "repair_refused_outbox"
        assert rows[0]["action_tier"] == "operate"
        assert rows[0]["target_scope"] == (
            "alert_outbox:release_identity=%s:limit=200" % release)
    finally:
        _cleanup(conn, release)


# ---------------------------------------------------------------------------
# Apply: moves exactly the named release's REFUSED rows, clears the right
# columns, leaves resend_count and other rows alone.
# ---------------------------------------------------------------------------
def test_apply_moves_refused_rows_to_pending_and_clears_claim_and_reason(conn):
    """The repair itself: REFUSED -> PENDING, claim columns and reason cleared.

    `resend_count` is asserted UNCHANGED — 059's header is explicit that a
    REFUSED row's prior sends did happen and the repair does not erase
    that history — and the row is re-read against `alert_outbox_refusal_
    shape_ck` and `alert_outbox_claim_shape_ck` (050) implicitly: a
    PENDING row with a non-NULL `refusal_reason` or claim column would
    have been refused by the UPDATE itself before this test ever got to
    assert on it.
    """
    _require_059(conn)
    release = _release("apply")
    try:
        alert_id = make_refused_row(conn, release, resend_count=3)
        conn.commit()

        key = _key("apply")
        result = actions.repair_refused_outbox_rows(
            conn, key, release, "contract test: apply the repair",
            dry_run=False)

        assert result["dry_run"] is False
        assert result["replayed"] is False
        assert result["candidates"] == 1
        assert result["rows_affected"] == 1

        row = _row(conn, alert_id)
        assert row["state"] == "PENDING"
        assert row["claim_token"] is None
        assert row["claimed_at"] is None
        assert row["refusal_reason"] is None
        assert row["resend_count"] == 3, (
            "resend_count counts sends that actually happened and must "
            "survive the repair untouched")

        rows = _audit_rows(conn, key)
        assert len(rows) == 1
        assert rows[0]["dry_run"] is False
        assert rows[0]["rows_affected"] == 1
        assert rows[0]["detail"]["release_identity"] == release
        assert rows[0]["detail"]["candidates"] == 1
        assert rows[0]["detail"]["alert_ids"] == [alert_id]
    finally:
        _cleanup(conn, release)


def test_apply_moves_exactly_the_named_releases_refused_rows(conn):
    """Scoping is by `release_identity`, and the bound is exact both ways.

    A second REFUSED row under a DIFFERENT release, and a PENDING and a
    SENT row under the SAME release, are all planted alongside the one
    row the repair should touch — the population it must leave alone on
    both axes (wrong release; right release, wrong state) in one test.
    """
    _require_059(conn)
    release = _release("scope")
    other_release = _release("scope-other")
    try:
        target = make_refused_row(conn, release, name="scope-target")
        other_refused = make_refused_row(conn, other_release,
                                         name="scope-other-refused")

        pending_id = _outbox_alert_id("scope-pending")
        _insert_pending(conn, pending_id, release)

        sent_id = _outbox_alert_id("scope-sent")
        _insert_pending(conn, sent_id, release)
        _claim(conn, sent_id, token=sent_id[-16:])
        _send(conn, sent_id)
        conn.commit()

        key = _key("scope")
        result = actions.repair_refused_outbox_rows(
            conn, key, release, "contract test: scope by release_identity",
            dry_run=False)

        assert result["candidates"] == 1
        assert result["rows_affected"] == 1

        assert _row(conn, target)["state"] == "PENDING"
        assert _row(conn, other_refused)["state"] == "REFUSED", (
            "a row under a different release_identity was repaired")
        assert _row(conn, pending_id)["state"] == "PENDING", (
            "an already-PENDING row's untouched state should stay a no-op, "
            "not evidence of anything the repair did"
        )
        assert _row(conn, sent_id)["state"] == "SENT", (
            "a SENT row under the SAME release was moved off SENT")
    finally:
        _cleanup(conn, release)
        _cleanup(conn, other_release)


# ---------------------------------------------------------------------------
# Expected-state discipline: a stale {"candidates": n} refuses with RA001
# and writes nothing.
# ---------------------------------------------------------------------------
def test_expected_state_mismatch_refuses_and_writes_no_mutation(conn):
    """The compare-and-swap: a stale candidate count is refused, not honored.

    Mirrors `test_operator_mutations.py`'s equivalent test for `retry_
    parked_attempts` exactly, against this function's own RA001 path.
    """
    _require_059(conn)
    release = _release("mismatch")
    try:
        alert_id = make_refused_row(conn, release)
        conn.commit()

        key = _key("mismatch")
        with pytest.raises(ExpectedStateMismatch):
            actions.repair_refused_outbox_rows(
                conn, key, release, "contract test: stale expectation",
                expected_state={"candidates": 0}, dry_run=False)

        assert _row(conn, alert_id)["state"] == "REFUSED", (
            "the refused call mutated the row anyway")
        assert _audit_rows(conn, key) == [], (
            "the refused call wrote an audit row; a refusal is not an "
            "action and must not be recorded as one")
    finally:
        _cleanup(conn, release)


def test_expected_state_agreement_proceeds(conn):
    """The other half: a correct expected count does not block the apply."""
    _require_059(conn)
    release = _release("agree")
    try:
        alert_id = make_refused_row(conn, release)
        conn.commit()

        key = _key("agree")
        result = actions.repair_refused_outbox_rows(
            conn, key, release, "contract test: correct expectation",
            expected_state={"candidates": 1}, dry_run=False)

        assert result["rows_affected"] == 1
        assert _row(conn, alert_id)["state"] == "PENDING"
        rows = _audit_rows(conn, key)
        assert rows[0]["expected_state"] == {"candidates": 1}
    finally:
        _cleanup(conn, release)


# ---------------------------------------------------------------------------
# Idempotency: a repeat call under one key replays, and does not re-update.
# ---------------------------------------------------------------------------
def test_repeat_apply_under_one_key_replays_without_re_updating(conn):
    """The idempotency key's whole purpose, asserted against the ledger AND
    against the row: a naive re-apply that recomputed the candidate
    population on the SECOND call would find zero REFUSED rows left (the
    first call already moved the one row to PENDING) and report `rows_
    affected: 0` — indistinguishable, from the caller's side, of "already
    done" and "there was never anything to do". The replay path exists
    precisely so the second call returns the FIRST call's recorded outcome
    instead of re-deriving a different-looking answer for the same request.
    """
    _require_059(conn)
    release = _release("repeat")
    try:
        alert_id = make_refused_row(conn, release)
        conn.commit()

        key = _key("repeat")
        first = actions.repair_refused_outbox_rows(
            conn, key, release, "contract test: first call", dry_run=False)
        second = actions.repair_refused_outbox_rows(
            conn, key, release, "contract test: second call, same key",
            dry_run=False)

        assert first["replayed"] is False
        assert first["rows_affected"] == 1
        assert second["replayed"] is True, "the repeat must be a replay"
        assert second["audit_id"] == first["audit_id"]
        assert second["rows_affected"] == 1, (
            "a replay must return the FIRST call's rows_affected, not "
            "recompute against the now-empty candidate population")

        assert _row(conn, alert_id)["state"] == "PENDING"
        rows = _audit_rows(conn, key)
        assert len(rows) == 1, (
            "a second real audit row means the mutation happened twice; "
            "found %d" % len(rows))
    finally:
        _cleanup(conn, release)


def test_dry_run_does_not_consume_the_key(conn):
    """Rehearse-then-apply under one key is the intended workflow."""
    _require_059(conn)
    release = _release("rehearse")
    try:
        alert_id = make_refused_row(conn, release)
        conn.commit()

        key = _key("rehearse")
        preview = actions.repair_refused_outbox_rows(
            conn, key, release, "contract test: preview", dry_run=True)
        applied = actions.repair_refused_outbox_rows(
            conn, key, release, "contract test: now for real", dry_run=False)

        assert preview["dry_run"] is True
        assert applied["replayed"] is False, (
            "the apply replayed its own dry run — the key was consumed by "
            "the rehearsal")
        assert applied["rows_affected"] == 1
        assert _row(conn, alert_id)["state"] == "PENDING"

        rows = _audit_rows(conn, key)
        assert len(rows) == 2
        assert [r["dry_run"] for r in rows] == [True, False]
    finally:
        _cleanup(conn, release)


# ---------------------------------------------------------------------------
# max_rows caps the population.
# ---------------------------------------------------------------------------
def test_max_rows_bounds_the_candidate_population(conn):
    """`p_max_rows` caps both the dry-run count and what apply touches.

    Three REFUSED rows planted, capped at 2: the dry run must report 2
    candidates (not 3), and the apply must move exactly 2, leaving one
    REFUSED row behind for a subsequent call — the same "visible in the
    dry run's count before anything is written" property 059's header
    states as the reason the cap exists at all.
    """
    _require_059(conn)
    release = _release("maxrows")
    try:
        ids = [make_refused_row(conn, release, name="maxrows-%d" % i)
               for i in range(3)]
        conn.commit()

        preview = actions.repair_refused_outbox_rows(
            conn, _key("maxrows-preview"), release, "contract test: capped "
            "dry run", max_rows=2, dry_run=True)
        assert preview["candidates"] == 2

        result = actions.repair_refused_outbox_rows(
            conn, _key("maxrows-apply"), release, "contract test: capped "
            "apply", max_rows=2, dry_run=False)
        assert result["candidates"] == 2
        assert result["rows_affected"] == 2

        states = [_row(conn, alert_id)["state"] for alert_id in ids]
        assert states.count("PENDING") == 2
        assert states.count("REFUSED") == 1, (
            "max_rows did not leave the excess candidate untouched")
    finally:
        _cleanup(conn, release)


# ---------------------------------------------------------------------------
# Grants: rapid_operator can execute; a role without the grant cannot.
# ---------------------------------------------------------------------------
def _role_exists(conn, role):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
        return cur.fetchone() is not None


def _has_execute(conn, role, function, nargs):
    """Mirrors `test_operator_grants.py`'s `_has_execute` exactly: asked
    through `has_function_privilege` so a role reaching the grant by
    membership (031's own two-week defect) is not missed.
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


def test_rapid_operator_holds_execute(conn):
    """059's own grant block: `rapid_operator`, and only after PUBLIC is
    revoked (PostgreSQL grants EXECUTE to PUBLIC on creation)."""
    _require_059(conn)
    assert _role_exists(conn, "rapid_operator")
    assert _has_execute(conn, "rapid_operator", "repair_refused_outbox_rows",
                        8) is True


def test_public_holds_no_execute(conn):
    """The REVOKE half of 059's grant block, asserted against the catalog —
    `test_operator_grants.py`'s `test_public_holds_nothing_on_the_new_
    functions` establishes exactly this shape for 047's functions."""
    _require_059(conn)
    assert _has_execute(conn, "public", "repair_refused_outbox_rows",
                        8) is False, (
        "PUBLIC holds EXECUTE on derived.repair_refused_outbox_rows")


def test_a_role_without_the_grant_cannot_execute(conn):
    """The negative case named in the task: some role the function is NOT
    granted to genuinely cannot call it — provoked as a real call, not
    only read from the catalog, the same "ask the database, not just the
    grant map" discipline `test_alert_outbox_grants.py`'s section 1b/2b
    establishes for `rapid_pipeline_write`/`rapid_publisher`.

    `rapid_publisher` is the right negative: 059's own header states by
    name that it is deliberately NOT granted this function ("rapid_
    publisher in particular holds no grant on alert_outbox.state at all
    outside its own claim/finalize columns... this function's SECURITY
    DEFINER body is what makes the repair possible without widening that
    grant"), so a passing refusal here is asserting the documented
    boundary rather than an arbitrary unrelated role.
    """
    _require_059(conn)
    if not _role_exists(conn, "rapid_publisher"):
        pytest.skip("rapid_publisher is not present in this database")

    import psycopg2
    from psycopg2 import sql

    release = _release("norole")
    try:
        alert_id = make_refused_row(conn, release)
        conn.commit()

        # `SET LOCAL ROLE`, inside a SAVEPOINT, rolled back to before the
        # next statement — the exact mechanism `test_alert_outbox_grants.
        # py`'s `_as_role_expect_insufficient_privilege` documents: the
        # contract connection is a superuser, which PostgreSQL exempts from
        # `SET ROLE`'s normal membership check, so this is usable even
        # though 059 grants `rapid_publisher` no membership toward it at
        # all.
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT norole_attempt")
            cur.execute(sql.SQL("SET LOCAL ROLE {}").format(
                sql.Identifier("rapid_publisher")))
        try:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT derived.repair_refused_outbox_rows("
                        "  %s, %s, %s, NULL::jsonb, 200, true, NULL)",
                        [_key("norole"), release, "contract test: refused "
                         "role"])
        finally:
            with conn.cursor() as cur:
                cur.execute("ROLLBACK TO SAVEPOINT norole_attempt")

        assert _row(conn, alert_id)["state"] == "REFUSED", (
            "the refused call mutated the row anyway")
    finally:
        _cleanup(conn, release)
