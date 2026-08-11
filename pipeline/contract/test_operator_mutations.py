"""Contract tests: the operator surface and the mutation contract (brief G,
acceptance criteria 1, 2, 3 and 4).

    1. "`rapidctl` dry-run and `--apply` of an operate-tier action produce
        correct plans, one audit row per real run (assert against
        `derived.mutation_audit`), and repeat-apply with the same idempotency
        key does not double-mutate."
    2. "Expected-state mismatch refuses with a typed error and writes no
        mutation."
    3. "Break-glass open→close→reconcile through `rapidctl` leaves the
        unreconciled view empty; open-without-close shows in it."
    4. "A wrapped tool invocation (supersede path, against fixture rows)
        writes its audit row and honors dry-run default."

**THESE SKIP WHERE DRAFT MIGRATION 047 IS ABSENT**, which is everywhere the
authoritative stream is applied without this branch's drafts — CI included.
047's keyed overloads and the `idempotency_key`/`expected_state` columns are
a change request against `rapid_systems`, not part of the deployed schema, so
the tier probes for them and skips cleanly rather than failing. The
rapid-admin acceptance run applies base + drafts and therefore executes them.

WHY THIS TIER. Every property under test is a property of the DATABASE, not
of the CLI's Python: that a repeat call under one key does not double-mutate
is enforced by a partial unique index and a replay lookup; that a mismatch
writes nothing is enforced by the order of a RAISE against an INSERT inside a
plpgsql body; that the region 7 view clears is a correlated-EXISTS query over
an append-only table. A stub that answered these would be asserting its own
fixture's arithmetic. The functions are called here the way `rapidctl` calls
them — through `pipeline.operatorctl.actions`, the same module the CLI
dispatches to — so the test exercises the shipped call path rather than a
hand-written SELECT that happens to agree with it.

ROLE ASSUMPTION IS NOT EXERCISED HERE. These run as the fixture's superuser
connection, which holds EXECUTE by ownership rather than through
`rapid_operator`. The grant posture is asserted separately in
`test_operator_grants.py`, which is where a membership question belongs; a
test that both assumed the role and asserted the effect would fail for two
unrelated reasons with one message.
"""

import uuid

import pytest

from pipeline.contract import fixture
from pipeline.operatorctl import actions
from pipeline.operatorctl.contract import (ExpectedStateMismatch,
                                           IdempotencyConflict)

pytestmark = pytest.mark.usefixtures("_requires_draft_047")


@pytest.fixture
def _requires_draft_047(conn):
    """Skip unless DRAFT 047 is applied. Probed, never assumed."""
    if not actions.draft_schema_present(conn):
        pytest.skip(
            "DRAFT migration 047 (keyed mutation overloads) is not applied; "
            "the operator-contract tests need it and skip cleanly without it")


def _key(name):
    """A key unique to this test and this run.

    Idempotency keys are globally unique by construction in production; in
    the tier they must also not collide across re-runs of the same test,
    which is what `RUN_TAG` gives every other fixture row here.
    """
    return "brief-g-%s-%s-%s" % (name, fixture.RUN_TAG, uuid.uuid4().hex[:8])


def _audit_rows(conn, key):
    """Every audit row carrying this idempotency key, oldest first."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT audit_id, action_class, action_tier, target_scope, "
            "       reason, dry_run, rows_affected, idempotency_key, "
            "       expected_state, actor "
            "  FROM derived.mutation_audit "
            " WHERE idempotency_key = %s ORDER BY audit_id", [key])
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, r)) for r in cur.fetchall()]


def _category_exists(conn, category):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM derived.problem_categories"
                    " WHERE problem_category = %s", [category])
        return cur.fetchone() is not None


def _cleanup_category(conn, category):
    """Remove a category this test added.

    The audit rows are NOT removed: the table is append-only and its
    trigger refuses DELETE regardless of who asks, which is itself one of
    030's invariants. Fixture honesty here means unique keys and unique
    category names, not truncation — every row this module writes carries
    RUN_TAG and is identifiable as this run's.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM derived.problem_categories"
                    " WHERE problem_category = %s", [category])
    conn.commit()


# ---------------------------------------------------------------------------
# Criterion 1 — dry-run, apply, one audit row per real run, no double mutation
# ---------------------------------------------------------------------------
def test_dry_run_plans_without_mutating(conn):
    """The dry run computes the real plan and changes nothing.

    The distinction that matters: a dry run is not a no-op. It writes its
    own audit row (030's design makes visibility the control, so a
    rehearsal is recorded too), reports what it WOULD do, and leaves the
    target untouched — and 030's CHECK forbids it claiming rows changed.
    """
    category = "brief-g-dry-%s" % fixture.RUN_TAG
    key = _key("dry")
    try:
        result = actions.add_problem_category(
            conn, key, category, "a dry-run rehearsal", "brief G criterion 1",
            expected_state=None, dry_run=True)

        assert result["dry_run"] is True
        assert result["would_add"] is True
        assert result["rows_affected"] == 0
        # The plan is about a category that still does not exist.
        assert not _category_exists(conn, category)

        rows = _audit_rows(conn, key)
        assert len(rows) == 1, "a dry run is audited too"
        assert rows[0]["dry_run"] is True
        assert rows[0]["rows_affected"] == 0
        assert rows[0]["reason"] == "brief G criterion 1"
        assert rows[0]["idempotency_key"] == key
    finally:
        _cleanup_category(conn, category)


def test_apply_mutates_once_and_writes_one_audit_row(conn):
    """`--apply` performs the action and leaves exactly one real audit row."""
    category = "brief-g-apply-%s" % fixture.RUN_TAG
    key = _key("apply")
    try:
        result = actions.add_problem_category(
            conn, key, category, "the real thing", "brief G criterion 1",
            expected_state=None, dry_run=False)

        assert result["dry_run"] is False
        assert result["replayed"] is False
        assert result["rows_affected"] == 1
        assert _category_exists(conn, category)

        rows = _audit_rows(conn, key)
        assert len(rows) == 1
        assert rows[0]["dry_run"] is False
        assert rows[0]["rows_affected"] == 1
        assert rows[0]["action_tier"] == "operate"
        assert rows[0]["target_scope"] == "problem_categories:%s" % category
        # The actor is session-derived, never caller-supplied — the one
        # field an operator cannot lie about.
        assert rows[0]["actor"]
    finally:
        _cleanup_category(conn, category)


def test_repeat_apply_under_one_key_does_not_double_mutate(conn):
    """The idempotency key's whole purpose, asserted against the ledger.

    THE FAILURE THIS CATCHES is the one an operator actually hits: an
    apply whose connection dropped between the commit and the response,
    leaving them unable to tell whether it happened. Without a key the
    only options are to re-run (and maybe double-act) or not to (and maybe
    never act). With one, re-running is safe — and "safe" has to mean the
    ledger still shows ONE real action, not two identical ones.
    """
    category = "brief-g-repeat-%s" % fixture.RUN_TAG
    key = _key("repeat")
    try:
        first = actions.add_problem_category(
            conn, key, category, "first call", "brief G criterion 1",
            dry_run=False)
        second = actions.add_problem_category(
            conn, key, category, "second call, same key",
            "brief G criterion 1", dry_run=False)

        assert first["replayed"] is False
        assert second["replayed"] is True, "the repeat must be a replay"
        # The replay returns the PRIOR outcome, including its audit id —
        # so a caller comparing the two sees the same action, not two.
        assert second["audit_id"] == first["audit_id"]

        rows = _audit_rows(conn, key)
        assert len(rows) == 1, (
            "a second real audit row means the mutation happened twice; "
            "found %d" % len(rows))
    finally:
        _cleanup_category(conn, category)


def test_dry_run_does_not_consume_the_key(conn):
    """Rehearse-then-apply under one key is the intended workflow.

    If the dry run consumed the key, every apply would replay its own
    preview and nothing would ever happen — the contract would defeat
    itself at exactly the point it is meant to help.
    """
    category = "brief-g-rehearse-%s" % fixture.RUN_TAG
    key = _key("rehearse")
    try:
        preview = actions.add_problem_category(
            conn, key, category, "preview", "brief G criterion 1",
            dry_run=True)
        applied = actions.add_problem_category(
            conn, key, category, "now for real", "brief G criterion 1",
            dry_run=False)

        assert preview["dry_run"] is True
        assert applied["replayed"] is False, (
            "the apply replayed its own dry run — the key was consumed by "
            "the rehearsal")
        assert applied["rows_affected"] == 1
        assert _category_exists(conn, category)

        rows = _audit_rows(conn, key)
        assert len(rows) == 2
        assert [r["dry_run"] for r in rows] == [True, False]
    finally:
        _cleanup_category(conn, category)


def test_a_key_reused_for_a_different_action_is_refused(conn):
    """Same key, different intention: a caller bug, not a replay.

    Returning the prior outcome here would report success for an action
    that never ran — the one case where the replay behaviour would be
    actively harmful rather than merely surprising.
    """
    first_category = "brief-g-conflict-a-%s" % fixture.RUN_TAG
    second_category = "brief-g-conflict-b-%s" % fixture.RUN_TAG
    key = _key("conflict")
    try:
        actions.add_problem_category(
            conn, key, first_category, "the first", "brief G criterion 1",
            dry_run=False)

        with pytest.raises(IdempotencyConflict):
            actions.add_problem_category(
                conn, key, second_category, "a different target, same key",
                "brief G criterion 1", dry_run=False)

        assert not _category_exists(conn, second_category), (
            "the refused call mutated anyway")
    finally:
        _cleanup_category(conn, first_category)
        _cleanup_category(conn, second_category)


def test_scoped_retry_is_audited_and_replayable(conn):
    """The other operate-tier action, audited and replayed under one key.

    `retry_parked_attempts` records a release DECISION rather than
    modifying attempt rows (031's header explains at length why writing
    `reconciliation_class` would corrupt the reconciler's own field), so
    "did it mutate?" is a question about the ledger — which is what makes
    the idempotency assertion here the meaningful one.

    RUN AGAINST AN EMPTY CANDIDATE POPULATION, DELIBERATELY, and the
    reason is a real schema fact worth recording. The function's parked
    population requires `rapid_outcome = 'failure'` on a row whose
    `lifecycle_state` is one of the two terminal states — but
    `attempts_state_terminal_without_start_check` (017) requires
    `rapid_outcome IS NULL`, and `attempts_state_terminal_after_start_check`
    (013) requires eleven binding and provenance columns the contract
    fixture does not set. So `fixture.make_attempt` cannot construct a
    row this function would select, and a fixture that could would be
    hand-writing a row shape the production writers never produce.

    What is under test here is what BRIEF G ADDED — the key, the audit
    row, and the replay — and all three are observable with zero
    candidates. The candidate-selection query itself is 031's, unchanged
    by this brief and already covered by its own acceptance.
    """
    _, run_id = fixture.make_logical_job(conn)
    conn.commit()

    key = _key("retry")
    result = actions.retry_parked_attempts(
        conn, key, run_id, "brief G criterion 1", dry_run=False)

    assert result["replayed"] is False
    assert result["action"] == "scoped_retry"
    rows = _audit_rows(conn, key)
    assert len(rows) == 1
    assert rows[0]["action_class"] == "scoped_retry"
    assert rows[0]["target_scope"].startswith("attempts:run_id=%s" % run_id)

    replay = actions.retry_parked_attempts(
        conn, key, run_id, "brief G criterion 1", dry_run=False)
    assert replay["replayed"] is True
    assert replay["audit_id"] == result["audit_id"]
    assert len(_audit_rows(conn, key)) == 1, (
        "the repeat call wrote a second audit row")


# ---------------------------------------------------------------------------
# Criterion 2 — expected-state mismatch refuses, and writes nothing
# ---------------------------------------------------------------------------
def test_expected_state_mismatch_refuses_and_writes_no_mutation(conn):
    """The compare-and-swap at the API boundary.

    TWO assertions, and the second is the one with teeth. Refusing is
    easy; refusing without leaving a trace of a mutation that did not
    happen is the property that keeps the ledger honest. A refusal
    audited as an action would put rows in the history for things nobody
    did, and an auditor counting operate-tier actions would count them.
    """
    category = "brief-g-mismatch-%s" % fixture.RUN_TAG
    key = _key("mismatch")
    try:
        # Make the category exist, so the operator's belief that it does
        # not is genuinely stale rather than merely wrong.
        actions.add_problem_category(
            conn, _key("mismatch-setup"), category, "already here",
            "brief G criterion 2 setup", dry_run=False)
        assert _category_exists(conn, category)

        with pytest.raises(ExpectedStateMismatch):
            actions.add_problem_category(
                conn, key, category, "adding what I think is new",
                "brief G criterion 2",
                expected_state={"already_present": False}, dry_run=False)

        assert _audit_rows(conn, key) == [], (
            "the refused call wrote an audit row; a refusal is not an "
            "action and must not be recorded as one")
    finally:
        _cleanup_category(conn, category)


def test_expected_state_agreement_proceeds(conn):
    """The other half: a correct expectation does not block the action.

    Without this, a mismatch check that refused EVERYTHING would pass the
    test above and be indistinguishable from a working one.
    """
    category = "brief-g-agree-%s" % fixture.RUN_TAG
    key = _key("agree")
    try:
        result = actions.add_problem_category(
            conn, key, category, "genuinely new", "brief G criterion 2",
            expected_state={"already_present": False}, dry_run=False)

        assert result["rows_affected"] == 1
        assert _category_exists(conn, category)
        rows = _audit_rows(conn, key)
        assert len(rows) == 1
        # The expectation is recorded as evidence of what the operator
        # believed, not merely checked and discarded.
        assert rows[0]["expected_state"] == {"already_present": False}
    finally:
        _cleanup_category(conn, category)


def test_retry_refuses_when_the_candidate_population_moved(conn):
    """Expected state on the action that genuinely targets specific state.

    The operator reviews a dry run showing N parked attempts and decides
    to release those N. If the population has changed by the time they
    apply, the decision was about a different set — and the previous
    signature would have released the new set and reported success.
    """
    _, run_id = fixture.make_logical_job(conn)
    conn.commit()

    # The dry run reports the population as it actually is; the apply then
    # claims a different one. (See `test_scoped_retry_is_audited_and_
    # replayable` for why the fixture cannot construct a selectable parked
    # attempt — it does not matter here, because the mismatch is between
    # what the caller CLAIMS and what the function COUNTS, and that
    # disagreement is real at any population size.)
    preview = actions.retry_parked_attempts(
        conn, _key("moved-preview"), run_id, "brief G criterion 2",
        dry_run=True)
    seen = preview["candidates"]

    key = _key("moved")
    with pytest.raises(ExpectedStateMismatch):
        actions.retry_parked_attempts(
            conn, key, run_id, "brief G criterion 2",
            expected_state={"candidates": seen + 5}, dry_run=False)

    assert _audit_rows(conn, key) == []


# ---------------------------------------------------------------------------
# Criterion 3 — break-glass open → close → reconcile clears region 7
# ---------------------------------------------------------------------------
def _unreconciled_ids(conn):
    return {row["open_audit_id"]
            for row in actions.unreconciled_break_glass(conn)}


def test_open_without_close_appears_in_the_unreconciled_view(conn):
    """Visibility from the first second — the only control break-glass has.

    Asserted BEFORE the clearing test, because a view that returned
    nothing ever would pass "the row cleared" trivially.
    """
    audit_id = actions.break_glass_open(
        conn, "brief G criterion 3: open only",
        "contract-test:%s" % fixture.RUN_TAG)
    conn.commit()
    assert audit_id in _unreconciled_ids(conn)

    rows = [r for r in actions.unreconciled_break_glass(conn)
            if r["open_audit_id"] == audit_id]
    assert rows[0]["state"] == "open"


def test_close_alone_does_not_clear_the_row(conn):
    """Both conditions are required, so each is tested for separately."""
    audit_id = actions.break_glass_open(
        conn, "brief G criterion 3: close only",
        "contract-test:%s" % fixture.RUN_TAG)
    actions.break_glass_close(conn, audit_id, "closing",
                              "derived.problem_categories", "none")
    conn.commit()

    rows = [r for r in actions.unreconciled_break_glass(conn)
            if r["open_audit_id"] == audit_id]
    assert rows, "a closed but unreconciled session must stay on the queue"
    assert rows[0]["state"] == "closed_not_reconciled"


def test_a_failing_reconciliation_does_not_clear_the_row(conn):
    """Only a PASS clears. A failed sweep is not a resolved session."""
    audit_id = actions.break_glass_open(
        conn, "brief G criterion 3: failed sweep",
        "contract-test:%s" % fixture.RUN_TAG)
    actions.break_glass_close(conn, audit_id, "closing",
                              "derived.problem_categories", "none")
    actions.break_glass_reconcile(conn, audit_id, "sweep failed", False)
    conn.commit()

    assert audit_id in _unreconciled_ids(conn)


def test_open_close_reconcile_clears_the_row(conn):
    """The full protocol, end to end: the region 7 row clears.

    And it clears even though a FAILED reconciliation is also in the
    history for this session — the view tests for the existence of a
    pass, which is the fix 032's header records after a LEFT JOIN version
    kept a session queued forever behind its own earlier failure.
    """
    audit_id = actions.break_glass_open(
        conn, "brief G criterion 3: full protocol",
        "contract-test:%s" % fixture.RUN_TAG)
    actions.break_glass_close(conn, audit_id, "closing",
                              "derived.problem_categories",
                              "one category added by hand")
    actions.break_glass_reconcile(conn, audit_id, "first sweep failed", False)
    actions.break_glass_reconcile(conn, audit_id, "repaired, sweep passes",
                                  True)
    conn.commit()

    assert audit_id not in _unreconciled_ids(conn), (
        "a closed-and-passingly-reconciled session is still queued")


def test_close_of_an_unopened_event_is_refused(conn):
    """Closing what was never opened is record corruption, not user error."""
    import psycopg2
    with pytest.raises(psycopg2.Error):
        actions.break_glass_close(conn, -1, "closing nothing", "none", "none")
    conn.rollback()
