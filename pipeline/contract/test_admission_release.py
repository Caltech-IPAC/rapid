"""Contract tests: the admission release pointer (brief H, acceptance
criterion 5).

    "Switching the admission release pointer changes only SUBSEQUENT
     admissions; an existing admission's stamp is never rewritten. The
     pointer is single-valued, superseded rows are retained, an
     unregistered release is refused at mutation time, and the mutation
     travels the full contract — reason, idempotency key, expected state,
     dry-run default, one audit row per real run."

Rule 18's rollback clause, verbatim, is what this file is testing:

    "...rollback changes only the release used for future admissions."

The word doing the work is FUTURE. A pointer switch that rewrote existing
admissions' stamps would not be a rollback — it would be a retroactive
reclassification of work that already ran, and the results of that work would
stop being attributable to the release they were actually produced under. The
whole reason `admission_release_pointer` is a separate table from the
`release_identity` column on `admission_exposures` is that the two answer
different questions: the pointer says what the NEXT admission gets, the column
says what THIS admission got. `test_switching_the_pointer_leaves_existing_
admissions_untouched` below is the assertion that keeps them different.

**THESE SKIP WHERE DRAFT MIGRATION 051 IS ABSENT**, which is everywhere the
authoritative stream is applied without this branch's drafts — CI included.
051 is a change request against `rapid_systems`, not part of the deployed
schema, so the tier probes for the table AND for the function and skips
cleanly rather than failing. The rapid-admin acceptance run applies base +
drafts and therefore executes them.

WHY THIS TIER AND NOT THE STUB TIER. Every property here is a property of
PostgreSQL and of nothing else:

  * "at most one current pointer" is a PARTIAL UNIQUE INDEX raising 23505 —
    a fake would be asserting its own bookkeeping;
  * "an unregistered release is refused" is a FOREIGN KEY plus an explicit
    RA001 raise inside a plpgsql body, and the ORDER of that raise against
    the INSERT is what makes the refusal leave nothing behind;
  * "the superseded row keeps its original set_at and reason" is a statement
    about what an UPDATE did NOT touch, which is only observable against
    rows a real database wrote;
  * "a replayed key does not double-apply" is a lookup against an
    append-only audit table under an advisory lock.

A stub answering any of these would be asserting its fixture's arithmetic —
`pipeline/contract/test_double_agreement.py` is the standing proof that the
doubles in this repository cannot refuse what the live system refuses.

ROLE ASSUMPTION IS NOT EXERCISED HERE. These run as the fixture's superuser
connection, which holds EXECUTE by ownership. The grant posture 051 declares
(the pipeline write role may READ the pointer and may never write it) is a
membership question and belongs in a grants file, not here; a test that both
assumed a role and asserted an effect would fail for two unrelated reasons
with one message.
"""

import uuid

import pytest

from pipeline.contract import fixture

pytestmark = pytest.mark.usefixtures("_requires_draft_051")


@pytest.fixture
def _requires_draft_051(conn):
    """Skip unless DRAFT 051 is applied. Probed, never assumed.

    BOTH halves are probed. The tables can exist without the function (a
    partially-applied file, or a hand-built scratch schema), and a test that
    checked only the table would fail with `undefined_function` — reported as
    a broken test rather than as an unapplied draft, which is exactly the
    conflation `fixture.has_table`'s docstring warns against.
    """
    if not fixture.has_table(conn, "admission_release_pointer"):
        pytest.skip(
            "DRAFT migration 051 (admission identity and release pointer) is "
            "not applied; the release-pointer contract tests need it and skip "
            "cleanly without it")
    if not fixture.has_function(conn, "set_admission_release"):
        pytest.skip(
            "DRAFT 051's tables are present but derived.set_admission_release "
            "is not; the mutation contract cannot be exercised")


# ---------------------------------------------------------------------------
# Fixture helpers. Every row this module writes carries RUN_TAG, so a re-run
# never collides with its own leftovers and a failure leaves its rows behind
# for inspection (the tier's fixture-honesty discipline).
# ---------------------------------------------------------------------------
def _key(name):
    """An idempotency key unique to this test and this run.

    Keys are globally unique by construction in production; here they must
    also not collide across re-runs of the same test, because a collision
    would be indistinguishable from the replay behaviour under test.
    """
    return "brief-h-%s-%s-%s" % (name, fixture.RUN_TAG, uuid.uuid4().hex[:8])


def _release(name):
    """A release identity unique to this run."""
    return "h5-%s-%s" % (name, fixture.RUN_TAG)


def _register_release(conn, release_identity):
    """Record a release as known, so the pointer's FK can name it.

    Inserted directly rather than through `AdmissionRepository.
    register_release`: the repository's own idempotency is asserted in the
    repository tests, and reaching through it here would make a pointer test
    fail for a repository reason.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO admission_releases (release_identity, manifest_uri)"
            " VALUES (%s, %s) ON CONFLICT (release_identity) DO NOTHING",
            [release_identity, "s3://contract-tier/%s" % release_identity])
    conn.commit()
    return release_identity


def _set_release(conn, key, release_identity, reason, expected_state=None,
                 dry_run=True, policy_citation=None):
    """Call `derived.set_admission_release` the way `rapidctl` calls it.

    Through the same statement `pipeline/operatorctl/main.py:_cmd_set_release`
    composes — a bare parameterized `SELECT derived.set_admission_release(...)`
    — so the test exercises the shipped call shape rather than a hand-written
    variant that happens to agree with it. `call_function` is not used because
    it commits and rolls back on the caller's behalf, and several tests here
    need to inspect the transaction's state between statements.

    Returns the function's jsonb result as a dict; psycopg2 adapts jsonb
    already, and a string is parsed for a driver that does not.
    """
    import json

    expected = (json.dumps(expected_state)
                if expected_state is not None else None)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT derived.set_admission_release(%s, %s, %s, %s, %s, %s)",
            [key, release_identity, reason, expected, dry_run,
             policy_citation])
        row = cur.fetchone()
    conn.commit()
    result = row[0] if row else None
    if isinstance(result, str):
        result = json.loads(result)
    return result


def _current_release(conn):
    """The release the pointer currently names, or None."""
    with conn.cursor() as cur:
        cur.execute("SELECT release_identity FROM admission_release_pointer"
                    " WHERE is_current")
        row = cur.fetchone()
    return row[0] if row else None


def _pointer_rows(conn, release_identities):
    """Every pointer row naming one of these releases, oldest first.

    Scoped to this test's own releases rather than reading the whole table:
    the pointer is a singleton across the database, and another run's rows
    are legitimately present.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pointer_id, release_identity, is_current, set_at, set_by,"
            "       reason, audit_id"
            "  FROM admission_release_pointer"
            " WHERE release_identity = ANY(%s) ORDER BY pointer_id",
            [list(release_identities)])
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, r)) for r in cur.fetchall()]


def _audit_rows(conn, key):
    """Every audit row carrying this idempotency key, oldest first."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT audit_id, action_class, action_tier, target_scope, reason,"
            "       dry_run, rows_affected, idempotency_key, expected_state,"
            "       actor"
            "  FROM derived.mutation_audit"
            " WHERE idempotency_key = %s ORDER BY audit_id", [key])
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, r)) for r in cur.fetchall()]


def _make_exposure(conn, dateobs_offset):
    """One `exposures` row, returning its expid.

    `admission_exposures.expid` carries an FK to this table, so an admission
    fixture needs a real exposure — an admission stamped against an expid
    nobody wrote is an admission of nothing.

    `exposurespk` is UNIQUE on `dateobs` ALONE, so the timestamp is made
    unique per row and `mjdobs` moves with it: an exposure whose MJD
    disagrees with its dateobs is a row no pipeline would ever write. `fid`
    is READ from `filters` rather than invented — 009-seed-data.sql seeds
    that catalogue and the pipeline reads it as reference data, so a fixture
    adding to it would be putting test rows in production reference data.
    (This is `fixture._diffimage_parents`'s exposure logic, narrowed: that
    helper also builds l2files and refimages parents this file has no use
    for.)
    """
    with conn.cursor() as cur:
        cur.execute("SELECT fid FROM filters ORDER BY fid LIMIT 1")
        row = cur.fetchone()
        if row is None:
            raise AssertionError(
                "no rows in `filters`; 009-seed-data.sql seeds them, so an "
                "empty table means the stream was not fully applied")
        fid = row[0]
        cur.execute(
            "INSERT INTO exposures (dateobs, field, fid, exptime, mjdobs,"
            "                       hp6, hp9)"
            " SELECT timestamptz '2020-01-01 00:00:00+00'"
            "          + (coalesce(max(expid), 0) + 1 + %s)"
            "            * interval '1 second',"
            "        %s, %s, 100.0,"
            "        58849.0 + coalesce(max(expid), 0) + 1 + %s, 1, 1"
            "   FROM exposures"
            " RETURNING expid",
            [dateobs_offset, 900000 + dateobs_offset, fid, dateobs_offset])
        return cur.fetchone()[0]


def _admit_exposure(conn, expid, release_identity):
    """One `admission_exposures` row stamped with `release_identity`.

    **THE STAMP IS TAKEN FROM THE POINTER BY THE CALLER, ONCE**, which is the
    linearization `AdmissionRepository.current_release` documents: a pointer
    switch mid-run must not split one manifest across two releases, so the
    ingest reads the pointer at the start of a manifest's admission and
    passes the same value to every admission from it. This helper takes the
    already-read value for the same reason, so a test can control exactly
    which release an admission is stamped with and when it was read.

    `manifest_id` is left NULL: the sealed-manifest trigger only fires for a
    non-NULL manifest, and manifest sealing is rule 20's property, asserted
    elsewhere. What this file needs is the stamp.
    """
    identity_ = "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO admission_exposures"
            "  (admission_identity, expid, release_identity, admitted_facts)"
            " VALUES (%s, %s, %s, %s::jsonb)"
            " RETURNING admission_id, release_identity, admitted_at",
            [identity_, expid, release_identity,
             '{"run_tag": "%s"}' % fixture.RUN_TAG])
        admission_id, stamped, admitted_at = cur.fetchone()
    conn.commit()
    return admission_id, stamped, admitted_at


def _stamp_of(conn, admission_id):
    """The release an existing admission carries, read fresh."""
    with conn.cursor() as cur:
        cur.execute("SELECT release_identity, admitted_at"
                    "  FROM admission_exposures WHERE admission_id = %s",
                    [admission_id])
        return cur.fetchone()


def _cleanup(conn, admission_ids=(), expids=(), releases=()):
    """Remove only what this test created, child rows first.

    The audit rows are NOT removed: `derived.mutation_audit`'s trigger
    refuses DELETE regardless of who asks, and that append-only property is
    one of 030's invariants. Fixture honesty here means unique keys and
    RUN_TAG-scoped identities, not truncation.

    Pointer rows naming this test's releases MUST go before the releases
    themselves — `admission_release_pointer_release_fk` would otherwise
    refuse the release delete, and the refusal would abort the teardown
    transaction and mask whatever the test was actually asserting.
    """
    try:
        with conn.cursor() as cur:
            for admission_id in admission_ids:
                cur.execute("DELETE FROM admission_exposures"
                            " WHERE admission_id = %s", [admission_id])
            for expid in expids:
                cur.execute("DELETE FROM exposures WHERE expid = %s", [expid])
            if releases:
                cur.execute("DELETE FROM admission_release_pointer"
                            " WHERE release_identity = ANY(%s)",
                            [list(releases)])
                cur.execute("DELETE FROM admission_releases"
                            " WHERE release_identity = ANY(%s)",
                            [list(releases)])
        conn.commit()
    except Exception:                                    # noqa: BLE001
        # Teardown must never mask a test failure: a cleanup that raises
        # replaces the real assertion error with a database error about
        # rows nobody was asserting on.
        conn.rollback()


# ---------------------------------------------------------------------------
# 1 — the rule-18 clause itself: the switch reaches only FUTURE admissions
# ---------------------------------------------------------------------------
def test_switching_the_pointer_leaves_existing_admissions_untouched(conn):
    """**THE RULE-18 ROLLBACK CLAUSE, ASSERTED DIRECTLY.**

    Rule 18: "...rollback changes only the release used for FUTURE
    admissions." So the switch must be observable in exactly two ways and no
    others — the next admission gets the new release, and the previous one
    still reads the old one.

    THE FAILURE THIS CATCHES is the obvious implementation nobody should
    write and somebody eventually does: making the switch an UPDATE that also
    re-stamps admissions, or storing the release only in the pointer and
    resolving it by join at read time. Either would make an admission's
    recorded release a function of WHEN IT IS READ rather than of when it was
    made, and the results of work already run would silently reattribute
    themselves to whatever release is current. That is why the stamp is a
    COLUMN on the admission row and the pointer is a separate table: they
    answer different questions, and this test is what keeps them different.

    Both halves are asserted. Asserting only that the old admission is
    unchanged would pass against a pointer that never moved at all; asserting
    only that the new one is stamped R2 would pass against an implementation
    that rewrote everything.
    """
    r1 = _register_release(conn, _release("r1-switch"))
    r2 = _register_release(conn, _release("r2-switch"))
    admissions, expids = [], []
    try:
        _set_release(conn, _key("switch-to-r1"), r1,
                     "brief H criterion 5: point at R1", dry_run=False)
        assert _current_release(conn) == r1

        # An admission made while the pointer names R1, stamped from the
        # pointer exactly as an ingest would.
        expid_one = _make_exposure(conn, 1)
        expids.append(expid_one)
        first_id, first_stamp, first_admitted_at = _admit_exposure(
            conn, expid_one, _current_release(conn))
        admissions.append(first_id)
        assert first_stamp == r1

        # THE ROLLBACK.
        _set_release(conn, _key("switch-to-r2"), r2,
                     "brief H criterion 5: roll forward to R2", dry_run=False)
        assert _current_release(conn) == r2

        # HALF ONE: the existing admission is untouched — same release, and
        # the same admitted_at, because a switch is not an event in the life
        # of an admission that already happened.
        stamp_now, admitted_at_now = _stamp_of(conn, first_id)
        assert stamp_now == r1, (
            "the pointer switch rewrote an existing admission's stamp; rule "
            "18 says a rollback changes only the release used for FUTURE "
            "admissions, so work already admitted must keep the release it "
            "was admitted under")
        assert admitted_at_now == first_admitted_at, (
            "the switch moved an existing admission's admitted_at")

        # HALF TWO: the next admission gets the new release. Without this the
        # pointer would be a value nothing reads — the "isolated pointer"
        # non-fix the brief names explicitly.
        expid_two = _make_exposure(conn, 2)
        expids.append(expid_two)
        second_id, second_stamp, _at = _admit_exposure(
            conn, expid_two, _current_release(conn))
        admissions.append(second_id)
        assert second_stamp == r2, (
            "an admission made after the switch was not stamped with the new "
            "release; the pointer moved but nothing reads it")

        # And the first is STILL R1 after the second was written, which rules
        # out a re-stamp triggered by the later insert rather than by the
        # switch.
        assert _stamp_of(conn, first_id)[0] == r1
    finally:
        _cleanup(conn, admissions, expids, (r1, r2))


# ---------------------------------------------------------------------------
# 2 — at most one current pointer, by index and not by convention
# ---------------------------------------------------------------------------
def test_a_second_current_pointer_row_is_refused(conn):
    """`admission_release_pointer_current_uq` holds against a direct INSERT.

    ASSERTED AGAINST THE INDEX, NOT AGAINST THE FUNCTION. The mutation
    function clears the old flag before inserting the new row, so calling it
    twice could never produce two current rows even if the index did not
    exist — a test that only called the function would pass on a table with
    no uniqueness at all. The property that matters is that ANY writer,
    including a hand-written INSERT during an incident, cannot create a
    second current pointer. That is a database guarantee or it is nothing.

    This is DRAFT 049's "at most one live prompt set" discipline: single-
    valued by a partial unique index, so superseded rows may still
    accumulate freely.
    """
    import psycopg2

    r1 = _register_release(conn, _release("r1-single"))
    r2 = _register_release(conn, _release("r2-single"))
    try:
        _set_release(conn, _key("single"), r1,
                     "brief H criterion 5: establish a current pointer",
                     dry_run=False)
        assert _current_release(conn) == r1

        with pytest.raises(psycopg2.errors.UniqueViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO admission_release_pointer"
                    "  (release_identity, is_current, set_by, reason)"
                    " VALUES (%s, true, %s, %s)",
                    [r2, "contract-test",
                     "a second current pointer, which must be refused"])
        conn.rollback()

        # The refusal left the existing pointer alone — a partial index that
        # refused by clearing something would be worse than no index.
        assert _current_release(conn) == r1
    finally:
        _cleanup(conn, releases=(r1, r2))


def test_superseded_rows_do_not_collide_with_each_other(conn):
    """The index is PARTIAL, so history accumulates.

    The other half of the constraint's shape. A plain `UNIQUE (is_current)`
    would admit exactly one false row as well as one true one, and the second
    switch would fail — turning the audit trail into a table that can hold two
    entries. Asserted because "the index is partial" is a claim about the
    `WHERE is_current` clause that only shows up on the third switch.
    """
    r1 = _register_release(conn, _release("r1-accum"))
    r2 = _register_release(conn, _release("r2-accum"))
    r3 = _register_release(conn, _release("r3-accum"))
    try:
        for index, release in enumerate((r1, r2, r3)):
            _set_release(conn, _key("accum-%d" % index), release,
                         "brief H criterion 5: switch %d" % index,
                         dry_run=False)

        rows = _pointer_rows(conn, (r1, r2, r3))
        assert len(rows) == 3, (
            "three switches did not leave three rows; the uniqueness is not "
            "partial and superseded history cannot accumulate")
        assert [r["is_current"] for r in rows] == [False, False, True]
    finally:
        _cleanup(conn, releases=(r1, r2, r3))


# ---------------------------------------------------------------------------
# 3 — superseded rows are RETAINED, not updated
# ---------------------------------------------------------------------------
def test_superseded_rows_are_retained_with_their_original_facts(conn):
    """A rollback is visible in the table as exactly what it was.

    051's header: "Superseded rows are RETAINED rather than updated, so the
    pointer's history is its own audit trail." That is a claim about what the
    supersede UPDATE does NOT touch — it clears `is_current` and nothing else.

    THE FAILURE THIS CATCHES is an implementation that supersedes by
    rewriting the existing row in place (`UPDATE ... SET release_identity =
    new`), which leaves one row that has always claimed to name the current
    release. An operator asking "what were we pointing at last Tuesday, and
    who moved it, and why?" would get no answer at all, and the pointer's own
    history — the thing that makes a rollback reviewable — would not exist.

    So the superseded row is checked field by field against what it said when
    it was written: same release, same set_at, same set_by, same reason. Only
    the flag moved.
    """
    r1 = _register_release(conn, _release("r1-retain"))
    r2 = _register_release(conn, _release("r2-retain"))
    first_reason = "brief H criterion 5: the original pointing, for R1"
    try:
        _set_release(conn, _key("retain-1"), r1, first_reason, dry_run=False)

        before = _pointer_rows(conn, (r1,))
        assert len(before) == 1
        original = before[0]
        assert original["is_current"] is True

        _set_release(conn, _key("retain-2"), r2,
                     "brief H criterion 5: superseding pointing, for R2",
                     dry_run=False)

        rows = _pointer_rows(conn, (r1, r2))
        assert len(rows) >= 2, (
            "a switch left fewer than two rows; the old pointing was "
            "rewritten rather than superseded")
        current = [r for r in rows if r["is_current"]]
        assert len(current) == 1, (
            "exactly one row may be current; found %d" % len(current))
        assert current[0]["release_identity"] == r2

        superseded = [r for r in rows
                      if r["pointer_id"] == original["pointer_id"]][0]
        assert superseded["is_current"] is False
        # THE ORIGINAL FACTS SURVIVED. Each of these is a separate way the
        # supersede could have destroyed the history, and an operator
        # reviewing a rollback needs all four.
        assert superseded["release_identity"] == r1
        assert superseded["set_at"] == original["set_at"], (
            "the superseded row's set_at moved; its timestamp is when THAT "
            "pointing was made, not when it stopped being current")
        assert superseded["set_by"] == original["set_by"]
        assert superseded["reason"] == first_reason, (
            "the superseded row's reason was overwritten by the switch that "
            "replaced it; the reason belongs to the pointing it explains")
    finally:
        _cleanup(conn, releases=(r1, r2))


# ---------------------------------------------------------------------------
# 4 — an unregistered release is refused AT MUTATION TIME
# ---------------------------------------------------------------------------
def test_an_unregistered_release_is_refused_at_mutation_time(conn):
    """The refusal happens when the pointer is SET, not when work is submitted.

    051's header: "A pointer naming an unresolvable release is REFUSED AT
    MUTATION TIME rather than discovered at submission time." The distinction
    is the whole value of `admission_releases` existing: discovering it at
    submission time means the bad pointer has already stamped an unknown
    number of admissions, and every one of them names a release nothing can
    resolve. Refusing at mutation time means the operator finds out while
    they are still holding the decision.

    CLASSIFIED BY SQLSTATE, NEVER BY MESSAGE TEXT — `pipeline/operatorctl/
    contract.py`'s established discipline (RA001/RA002), for the reason it
    records: message text is a presentation detail that changes with a
    wording fix, and a caller that greps it breaks silently when it does. The
    message IS additionally checked to name the release, because an operator
    needs to know WHICH release was rejected, but that assertion is about
    legibility and never about classification.
    """
    import psycopg2

    from pipeline.operatorctl.contract import SQLSTATE_EXPECTED_STATE_MISMATCH

    unknown = _release("never-registered")
    key = _key("unregistered")
    try:
        with pytest.raises(psycopg2.Error) as caught:
            _set_release(conn, key, unknown,
                         "brief H criterion 5: point at an unknown release",
                         dry_run=False)
        conn.rollback()

        code = (getattr(caught.value, "pgcode", None)
                or getattr(caught.value, "sqlstate", None))
        assert code == SQLSTATE_EXPECTED_STATE_MISMATCH, (
            "an unregistered release must be refused with SQLSTATE RA001 so "
            "the caller classifies it by code; got %r" % (code,))
        assert unknown in str(caught.value), (
            "the refusal does not name the release it rejected")

        # NOTHING WAS WRITTEN. A refusal is not an action, and a refused
        # mutation that left an audit row would put a history entry in the
        # ledger for something nobody did.
        assert _audit_rows(conn, key) == []
        assert _pointer_rows(conn, (unknown,)) == []
    finally:
        _cleanup(conn, releases=(unknown,))


# ---------------------------------------------------------------------------
# 5 — the mutation contract: reason, key, dry-run default, replay
# ---------------------------------------------------------------------------
def test_an_empty_reason_is_refused(conn):
    """A reason is mandatory on every operator mutation (7a ruling 5).

    Whitespace is checked as well as empty, because `length(btrim(...)) = 0`
    is the guard the function actually carries and a bare `IS NOT NULL` or
    `<> ''` would admit a single space — a reason field an operator can
    satisfy with a keystroke is not a reason field.
    """
    import psycopg2

    r1 = _register_release(conn, _release("r1-reason"))
    try:
        for reason in ("", "   ", "\t\n "):
            with pytest.raises(psycopg2.Error):
                _set_release(conn, _key("reason"), r1, reason, dry_run=False)
            conn.rollback()
        assert _pointer_rows(conn, (r1,)) == [], (
            "a call refused for a missing reason moved the pointer anyway")
    finally:
        _cleanup(conn, releases=(r1,))


def test_an_empty_idempotency_key_is_refused(conn):
    """A key is mandatory: without one there is nothing for a replay to find.

    Refused with RA002 rather than RA001 — the key family, not the
    expected-state family — so a caller distinguishes "you gave me a bad
    premise" from "you gave me a malformed call".
    """
    import psycopg2

    from pipeline.operatorctl.contract import SQLSTATE_IDEMPOTENCY_CONFLICT

    r1 = _register_release(conn, _release("r1-key"))
    try:
        for key in ("", "   "):
            with pytest.raises(psycopg2.Error) as caught:
                _set_release(conn, key, r1,
                             "brief H criterion 5: no key supplied",
                             dry_run=False)
            conn.rollback()
            code = (getattr(caught.value, "pgcode", None)
                    or getattr(caught.value, "sqlstate", None))
            assert code == SQLSTATE_IDEMPOTENCY_CONFLICT, (
                "a missing idempotency key must be refused with RA002; got %r"
                % (code,))
        assert _pointer_rows(conn, (r1,)) == []
    finally:
        _cleanup(conn, releases=(r1,))


def test_dry_run_is_the_default_and_changes_nothing(conn):
    """**DRY RUN IS THE DEFAULT**, and a dry run is not a no-op.

    Two assertions with different targets. The first is that the DEFAULT is
    the safe one: the function is called WITHOUT `p_dry_run` at all, relying
    on `DEFAULT true` in the signature, because a mutation whose default is
    "apply" turns every forgotten flag into an unintended action. The second
    is that the rehearsal computed a real plan (`changed` is true, the
    previous release is reported) while affecting zero rows — a dry run is a
    preview of the real answer, not a stub.
    """
    import json

    r1 = _register_release(conn, _release("r1-default"))
    r2 = _register_release(conn, _release("r2-default"))
    try:
        _set_release(conn, _key("default-setup"), r1,
                     "brief H criterion 5: establish a pointer", dry_run=False)
        assert _current_release(conn) == r1

        # THE DEFAULT, exercised by omission. Only the three mandatory
        # arguments are passed; `p_dry_run` falls to its declared default.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT derived.set_admission_release(%s, %s, %s)",
                [_key("default"), r2,
                 "brief H criterion 5: relying on the dry-run default"])
            result = cur.fetchone()[0]
        conn.commit()
        if isinstance(result, str):
            result = json.loads(result)

        assert result["dry_run"] is True, (
            "the function applied when no p_dry_run was given; the default "
            "must be the rehearsal, because a forgotten flag must not mutate")
        assert result["rows_affected"] == 0
        # The plan is REAL: it names what is current now and what would
        # change, which is what makes the printed plan a preview of the
        # apply's own answer rather than the CLI's understanding of it.
        assert result["previous_release"] == r1
        assert result["requested_release"] == r2
        assert result["changed"] is True

        # AND THE POINTER DID NOT MOVE.
        assert _current_release(conn) == r1, (
            "a dry run moved the pointer")
        assert _pointer_rows(conn, (r2,)) == [], (
            "a dry run wrote a pointer row")
    finally:
        _cleanup(conn, releases=(r1, r2))


def test_an_apply_moves_the_pointer_and_returns_an_audit_id(conn):
    """The other half: `p_dry_run=false` performs the action.

    Without this, a function that refused everything would pass the dry-run
    test above and be indistinguishable from a working one.
    """
    r1 = _register_release(conn, _release("r1-apply"))
    key = _key("apply")
    try:
        result = _set_release(conn, key, r1,
                              "brief H criterion 5: the real thing",
                              dry_run=False)

        assert result["dry_run"] is False
        assert result["rows_affected"] == 1
        assert result["audit_id"] is not None, (
            "an apply returned no audit id; the operator has no handle on "
            "the action they just performed")
        assert _current_release(conn) == r1

        # The pointer row records the audit row that authorised it, which is
        # what ties the table's own history to the ledger.
        rows = _pointer_rows(conn, (r1,))
        assert len(rows) == 1
        assert rows[0]["audit_id"] == result["audit_id"]
        # The actor is the database's `session_user`, never caller-supplied —
        # the one field an operator cannot lie about.
        assert rows[0]["set_by"]
    finally:
        _cleanup(conn, releases=(r1,))


def test_a_replayed_key_does_not_double_apply(conn):
    """**A REPLAYED KEY RETURNS THE RECORDED OUTCOME AND MUTATES NOTHING.**

    THE FAILURE THIS CATCHES is the one an operator actually hits: an apply
    whose connection dropped between the commit and the response, leaving
    them unable to tell whether the pointer moved. Without a key the only
    options are to re-run (and maybe act twice) or not to (and maybe never
    act). With one, re-running is safe — and "safe" has to mean the pointer
    moved ONCE and the ledger shows ONE action, not two identical ones.

    The second call is asserted to carry `replayed: true` (what
    `set_admission_release` adds to the recorded outcome) AND to have left
    the table alone. Both are needed: a function that returned the flag while
    still inserting a second pointer row would pass the first assertion and
    have done exactly the damage the key exists to prevent.
    """
    r1 = _register_release(conn, _release("r1-replay"))
    r2 = _register_release(conn, _release("r2-replay"))
    key = _key("replay")
    try:
        _set_release(conn, _key("replay-setup"), r1,
                     "brief H criterion 5: establish a pointer", dry_run=False)

        first = _set_release(conn, key, r2,
                             "brief H criterion 5: the apply", dry_run=False)
        assert first.get("replayed") is not True
        assert first["rows_affected"] == 1
        assert _current_release(conn) == r2

        second = _set_release(conn, key, r2,
                              "brief H criterion 5: the operator re-ran it",
                              dry_run=False)
        assert second.get("replayed") is True, (
            "a repeat call under the same key was not reported as a replay")
        # The replay returns the PRIOR outcome, including its audit id, so a
        # caller comparing the two sees one action rather than two.
        assert second.get("audit_id") == first["audit_id"]

        # THE POINTER MOVED ONCE. Two rows naming r2 would mean the mutation
        # ran twice regardless of what the second call reported.
        r2_rows = [r for r in _pointer_rows(conn, (r2,))]
        assert len(r2_rows) == 1, (
            "the replayed call inserted a second pointer row; the key did "
            "not prevent the double-apply, found %d rows" % len(r2_rows))
        assert _current_release(conn) == r2

        # AND ONE AUDIT ROW. The ledger is where an auditor counts
        # operate-tier actions, so a second row here would be a second action
        # in the permanent record for something that happened once.
        rows = _audit_rows(conn, key)
        assert len(rows) == 1, (
            "the replayed call wrote a second audit row; found %d"
            % len(rows))
    finally:
        _cleanup(conn, releases=(r1, r2))


# ---------------------------------------------------------------------------
# 6 — expected_state: the compare-and-swap at the API boundary
# ---------------------------------------------------------------------------
def test_a_wrong_expected_current_release_refuses_and_writes_nothing(conn):
    """The operator's picture of the pointer is stale, so the call refuses.

    THE WORKFLOW THIS PROTECTS: an operator runs a dry run, sees the pointer
    at R1, decides to roll back to R2, and applies. If someone else moved the
    pointer in between, the decision was made against a system that no longer
    exists — and applying it would silently overwrite whatever the other
    operator just did. `--expect-current` is how the decision carries its own
    premise into the apply.

    TWO assertions, and the second has the teeth. Refusing is easy; refusing
    without leaving a trace of a mutation that did not happen is what keeps
    the ledger honest.
    """
    import psycopg2

    from pipeline.operatorctl.contract import SQLSTATE_EXPECTED_STATE_MISMATCH

    r1 = _register_release(conn, _release("r1-expect"))
    r2 = _register_release(conn, _release("r2-expect"))
    key = _key("expect-wrong")
    try:
        _set_release(conn, _key("expect-setup"), r1,
                     "brief H criterion 5: the pointer is genuinely at R1",
                     dry_run=False)
        assert _current_release(conn) == r1

        stale = {"current_release": "a-release-the-pointer-left-behind"}
        with pytest.raises(psycopg2.Error) as caught:
            _set_release(conn, key, r2,
                         "brief H criterion 5: acting on a stale picture",
                         expected_state=stale, dry_run=False)
        conn.rollback()

        code = (getattr(caught.value, "pgcode", None)
                or getattr(caught.value, "sqlstate", None))
        assert code == SQLSTATE_EXPECTED_STATE_MISMATCH, (
            "an expected-state mismatch must refuse with RA001; got %r"
            % (code,))

        assert _audit_rows(conn, key) == [], (
            "the refused call wrote an audit row; a refusal is not an action "
            "and must not be recorded as one")
        assert _current_release(conn) == r1, (
            "the refused call moved the pointer anyway")
        assert _pointer_rows(conn, (r2,)) == []
    finally:
        _cleanup(conn, releases=(r1, r2))


def test_a_correct_expected_current_release_proceeds(conn):
    """A correct expectation does not block the action.

    Without this, an expected-state check that refused EVERYTHING would pass
    the mismatch test above and be indistinguishable from a working one — the
    same pairing `test_operator_mutations.py` uses for the same reason.

    The expectation is also asserted to be RECORDED in the audit row: it is
    evidence of what the operator believed at the moment they acted, not
    merely a precondition checked and discarded.
    """
    r1 = _register_release(conn, _release("r1-agree"))
    r2 = _register_release(conn, _release("r2-agree"))
    key = _key("expect-right")
    try:
        _set_release(conn, _key("agree-setup"), r1,
                     "brief H criterion 5: establish R1", dry_run=False)
        assert _current_release(conn) == r1

        result = _set_release(
            conn, key, r2,
            "brief H criterion 5: the picture is current",
            expected_state={"current_release": r1}, dry_run=False)

        assert result["rows_affected"] == 1
        assert _current_release(conn) == r2

        rows = _audit_rows(conn, key)
        assert len(rows) == 1
        assert rows[0]["expected_state"] == {"current_release": r1}, (
            "the expectation was checked but not recorded; the ledger should "
            "show what the operator believed when they acted")
    finally:
        _cleanup(conn, releases=(r1, r2))


# ---------------------------------------------------------------------------
# 7 — the audit ledger receives the row
# ---------------------------------------------------------------------------
def test_the_apply_writes_one_audit_row_with_the_right_action_class(conn):
    """Every real switch lands in `derived.mutation_audit`, correctly typed.

    The release pointer is the single value deciding what all future work is
    stamped with, so "who moved it, when, why, and under what key" must be
    answerable from the ledger alone — the same append-only history every
    other operate-tier action writes to, with the same fields, so an auditor
    reads one vocabulary rather than a special case for this one action.

    `action_class` is asserted exactly because it is what an auditor filters
    on: a switch recorded under a generic or misspelled class is a switch
    that does not appear in the query anyone runs.
    """
    r1 = _register_release(conn, _release("r1-ledger"))
    key = _key("ledger")
    reason = "brief H criterion 5: the ledger must show this switch"
    try:
        result = _set_release(conn, key, r1, reason, dry_run=False)

        rows = _audit_rows(conn, key)
        assert len(rows) == 1, (
            "one apply must write exactly one audit row; found %d"
            % len(rows))
        row = rows[0]
        assert row["action_class"] == "admission_release_set"
        assert row["idempotency_key"] == key
        assert row["reason"] == reason
        assert row["dry_run"] is False
        assert row["rows_affected"] == 1
        # The scope names the release, so the ledger says WHICH switch this
        # was without a join back to a table whose rows may since have moved.
        assert row["target_scope"] == (
            "admission_release_pointer:%s" % r1)
        # The actor is session-derived, never caller-supplied.
        assert row["actor"]
        assert row["audit_id"] == result["audit_id"]
    finally:
        _cleanup(conn, releases=(r1,))


def test_a_dry_run_is_audited_too_and_claims_no_rows(conn):
    """A rehearsal is recorded — visibility is the control (030's design).

    And 030's CHECK forbids a dry run claiming rows changed, so the
    rehearsal's row must carry `rows_affected = 0`. This also documents the
    intended workflow: rehearse and apply under ONE key, because a dry run
    does not consume the key (`derived.mutation_replay` matches only
    `NOT dry_run` rows). If the rehearsal consumed it, every apply would
    replay its own preview and nothing would ever happen.
    """
    r1 = _register_release(conn, _release("r1-rehearse"))
    key = _key("rehearse")
    try:
        preview = _set_release(conn, key, r1,
                               "brief H criterion 5: rehearsal", dry_run=True)
        assert preview["dry_run"] is True
        assert preview["rows_affected"] == 0

        applied = _set_release(conn, key, r1,
                               "brief H criterion 5: now for real",
                               dry_run=False)
        assert applied.get("replayed") is not True, (
            "the apply replayed its own dry run — the key was consumed by "
            "the rehearsal, which would make rehearse-then-apply impossible")
        assert applied["rows_affected"] == 1
        assert _current_release(conn) == r1

        rows = _audit_rows(conn, key)
        assert len(rows) == 2, (
            "the rehearsal and the apply should both be recorded; found %d "
            "row(s)" % len(rows))
        assert [r["dry_run"] for r in rows] == [True, False]
        assert [r["rows_affected"] for r in rows] == [0, 1]
    finally:
        _cleanup(conn, releases=(r1,))
