"""Contract tests: B1's `attempts`/`show-attempt` read views (commit 4ecbef24).

B1 shipped `actions.attempts_by_state` and `actions.attempt_detail` — the
queries behind `rapidctl attempts` and `rapidctl show-attempt` — with zero
automated coverage, unlike its siblings B2 (`terminate_jobs_audited`,
`test_wrapped_operator_tools.py`) and B3 (the GC fence,
`test_gc_fence_registration.py`). This file closes that gap the same way
those did: real rows, real SQL, against real PostgreSQL.

**THE INTERESTING PART IS THE JOIN.** `attempt_detail`'s `_ATTEMPT_CORE`
query is `attempts LEFT JOIN work_units LEFT JOIN LATERAL (latest
submissions row by run_id)`. Both joins are LEFT for a documented reason
(`actions.py`'s own comment on `_ATTEMPT_CORE`): `work_unit_id` is NULL on
every pre-intent-layer attempt, and a `submissions` row exists only where
DRAFT 044 was applied at submission time. A fake connection or a mocked
cursor cannot exercise a real LATERAL's "no matching row" behaviour — only
PostgreSQL evaluating the actual join can, which is exactly why this is a
contract test and not a stub one. So the tests below deliberately include a
work unit with NO submission row (the null/absent half of the join, proven
present as SQL NULL rather than a missing key or a KeyError) as well as one
WITH a submission, so the join's two outcomes are both pinned.

`submissions` is DRAFT migration 044 — adopted into `rapid_systems` main on
2026-08-12 per `test_submission_protocol.py`'s header, so `smdc` CI's pin
(`.github/workflows/contract-tests.yml`) already carries it — but this file
still probes with `fixture.has_table` rather than assuming, matching every
other contract test that touches a once-DRAFT table.

**THE SQL IS REAL** (the phrase `test_gc_execution.py` and
`test_gc_fence_registration.py` both use, restated here because it is
equally true): the LEFT JOIN LATERAL's `ORDER BY submissions.created_at
DESC LIMIT 1` tie-break when one run_id carries more than one submission,
and the CASE-derived `success_with_no_product` boolean, are exactly what a
Python fake cannot express faithfully.

**ROWS ARE BUILT ONLY THROUGH `fixture.make_attempt` / `make_completed_
attempt` / `create_unit`, never by hand-UPDATE-ing a lifecycle's protected
columns.** `attempts` carries one CHECK constraint per lifecycle_state
(011/013/014 in the migration stream), and each one both requires a
specific set of columns NOT NULL and forbids others outright — a
`submitted` row must have `started_at`/`ended_at` NULL, a
`terminal_without_start` row must have `rapid_outcome`/`product_disposition`
NULL, and only `terminal_after_start` may carry an outcome. Backdating a
row's age therefore touches only `ended_at` on an already-terminal row
(its value is unconstrained by the CHECK, only its non-nullity is) rather
than assembling a row's lifecycle state by hand — the same discipline
`fixture.py`'s own docstrings document being learned the hard way.

NOT covered here: `main.py`'s `_cmd_attempts`/`_cmd_show_attempt` print
formatting, and `_parse_duration_seconds`'s argument parsing. Neither reads
or writes the database — they are pure Python over the dict rows
`actions.py` already returns — and no sibling command's CLI-printing layer
(`_cmd_unreconciled`, `_cmd_audit`) has a contract test either; the
convention this file follows is that the contract tier proves the SQL, not
the argparse wiring around it.
"""

import uuid

import pytest

from pipeline.contract import fixture
from pipeline.operatorctl import actions
from submission import protocol

pytestmark = pytest.mark.usefixtures("_requires_intent_schema")


@pytest.fixture
def _requires_intent_schema(conn):
    """Skip unless `work_units` (migration 036) is applied.

    `attempts_by_state` alone would run against a bare 011-era schema, but
    `attempt_detail`'s join needs `work_units` to exist at all — probed
    once, here, so every test in this file skips together rather than each
    repeating the same check.
    """
    if not fixture.has_table(conn, "work_units"):
        pytest.skip("migration 036 (work_units) is not applied; the "
                    "attempt-view join tests need it and skip cleanly "
                    "without it")


@pytest.fixture
def _requires_submissions_table(conn):
    """Skip unless DRAFT 044 (`submissions`) is applied. Probed, never assumed."""
    if not fixture.has_table(conn, "submissions"):
        pytest.skip("DRAFT migration 044 (submissions) is not applied; the "
                    "submission-join half of show-attempt needs it and "
                    "skips cleanly without it")


def _age_ended_at(conn, attempt_id, hours_ago):
    """Backdate an already-terminal row's `ended_at`, and nothing else.

    Safe against every lifecycle CHECK constraint touched in this file:
    each one requires `ended_at IS NOT NULL` on a terminal row but pins
    nothing about its VALUE, so moving it into the past changes the row's
    computed age without disturbing any constraint that fired at INSERT
    time (the same reasoning `test_gc_fence_registration.py`'s
    `_age_fence_into_the_past` documents for `gc_fences`).
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE attempts SET ended_at = now() - (%s * interval '1 hour')"
            " WHERE attempt_id = %s", [hours_ago, attempt_id])
    conn.commit()


def _age_submitted_at(conn, attempt_id, hours_ago):
    """Backdate a `submitted` row's `submitted_at`.

    `attempts_state_submitted_check` requires `started_at IS NULL` and
    `ended_at IS NULL` on this lifecycle but says nothing about
    `submitted_at`'s value, so this is the column the "no ended_at, no
    started_at — age from submitted_at" test needs to move.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE attempts SET submitted_at = now() - (%s * interval '1 hour')"
            " WHERE attempt_id = %s", [hours_ago, attempt_id])
    conn.commit()


def _submitted_attempt_with_own_run_id(conn, tag, work_unit_id=None):
    """One `submitted` attempt row under a run_id UNIQUE TO THIS CALL.

    **WHY NOT `fixture.make_attempt`.** `make_attempt` builds its parent
    through `make_logical_job(conn)` with no `run_id` override, and
    `make_logical_job` defaults an omitted `run_id` to the SAME value for
    every call in one test process — `f"contract-{fixture.RUN_TAG}"`,
    `RUN_TAG` being a module-level constant. Every attempt this file built
    through the plain `fixture.make_attempt(conn, lifecycle="submitted")`
    call would therefore share one run_id, and `_ATTEMPT_CORE`'s
    LATERAL joins `submissions` by `run_id` ALONE — so a submission
    fixtured in one test would be visible from `attempt_detail` calls in
    every OTHER test in this file that also used the shared default,
    silently, depending on execution order. `test_submission_protocol.py`'s
    own `_prepare` and `test_wrapped_operator_tools.py`'s own
    `_attempt_with_scheduler_job_id` both hit this exact trap already and
    both build their run_id explicitly per-call rather than through
    `make_attempt`; this is that same fix, reused here for the identical
    reason — fixture honesty (`fixture.py`'s own header) requires a run_id
    that is unique to what THIS test built.

    Built as a complete one-INSERT `submitted` row, matching
    `make_pending_attempt`'s and `_attempt_with_scheduler_job_id`'s own
    reasoning: `attempts_state_submitted_check` forbids every outcome
    column and requires the binding triple together at schema_version >= 2,
    so assembling the row in pieces would pass through a state the schema
    refuses.
    """
    run_id = "attempt-view-%s-%s" % (tag, uuid.uuid4().hex[:8])
    logical_job_id, run_id = fixture.make_logical_job(conn, run_id=run_id)
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(schema_version), 1) FROM attempts")
        schema_version = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO attempts"
            "  (run_id, schema_version, logical_job_id, lifecycle_state,"
            "   created_at, submitted_at, work_unit_id,"
            "   binding_job_definition_arn, binding_image_digest,"
            "   binding_manifest_checksum)"
            " VALUES (%s, %s, %s, 'submitted', now(), now(), %s, %s,"
            "         'sha256:' || %s, 'sha256:' || %s)"
            " RETURNING attempt_id",
            [run_id, schema_version, logical_job_id, work_unit_id,
             "arn:aws:batch:us-east-1:account:job-definition/%s:1" % tag,
             tag, tag])
        attempt_id = cur.fetchone()[0]
    conn.commit()
    return attempt_id, run_id


def _prepared_submission(conn, run_id, job_name, scheduler_job_id=None,
                         array_size=2):
    """One real `submissions` row for `run_id`, through the production writer.

    Goes through `submission.protocol.prepare`/`mark_calling`/`mark_bound`
    rather than a hand-written INSERT — the same discipline
    `fixture.create_unit` documents for work units: a fixture that writes
    rows the production writer would never write tests a schema the
    application does not use. Left BOUND when `scheduler_job_id` is given
    (matching `submissions_job_id_ck`'s requirement that a job id
    accompany exactly the resolved-positive states), PREPARED otherwise.
    """
    execute = fixture.executor(conn)
    submission_id = protocol.prepare(
        execute, run_id=run_id, job_type=fixture.JOB_TYPE, job_name=job_name,
        job_queue="contract-attempt-view-queue", job_definition="contract-jobdef",
        manifest_checksum="sha256:" + "3" * 64,
        manifest_uri="s3://contract/%s/manifest.json" % run_id,
        array_size=array_size)
    if scheduler_job_id is not None:
        protocol.mark_calling(execute, submission_id)
        protocol.mark_bound(execute, submission_id, scheduler_job_id)
    conn.commit()
    return submission_id


# ---------------------------------------------------------------------------
# `attempts_by_state` — the coarse panel.
# ---------------------------------------------------------------------------
def test_attempts_by_state_returns_seeded_rows_in_the_named_state(conn):
    """A row in the named state, older than the bound, comes back — with the
    exact columns `_cmd_attempts` prints, including the derived anomaly flag
    correctly reading False for an ordinary published outcome.
    """
    attempt_id = fixture.make_completed_attempt(conn, rapid_outcome="success")
    _age_ended_at(conn, attempt_id, hours_ago=5)

    rows = actions.attempts_by_state(conn, "terminal_after_start",
                                     older_than_seconds=3600)
    matching = [r for r in rows if r["attempt_id"] == attempt_id]
    assert len(matching) == 1, (
        "a seeded terminal_after_start row older than the bound was not "
        "returned")
    row = matching[0]
    assert row["lifecycle_state"] == "terminal_after_start"
    assert row["rapid_outcome"] == "success"
    assert row["product_disposition"] == "published"
    assert row["success_with_no_product"] is False


def test_attempts_by_state_flags_success_with_no_product(conn):
    """The named anomaly: rapid_outcome='success' AND product_disposition='none'.

    `actions.py`'s own comment on `_ATTEMPTS_BY_STATE` calls this "success+
    none on a product route" and states it is surfaced rather than left for
    the caller to notice — this pins that the derived boolean is exactly
    this AND, computed by PostgreSQL, not approximated in Python.
    `make_completed_attempt`'s own docstring hard-codes `product_disposition
    = 'published'`, so the 'none' case is written directly here, over an
    otherwise-legal terminal_after_start row (`product_disposition` is one
    of the four values `attempts_product_disposition_check` admits, and nothing
    else that constraint depends on changes).
    """
    attempt_id = fixture.make_completed_attempt(conn, rapid_outcome="success")
    with conn.cursor() as cur:
        cur.execute("UPDATE attempts SET product_disposition = 'none'"
                    " WHERE attempt_id = %s", [attempt_id])
    conn.commit()
    _age_ended_at(conn, attempt_id, hours_ago=5)

    rows = actions.attempts_by_state(conn, "terminal_after_start",
                                     older_than_seconds=3600)
    matching = [r for r in rows if r["attempt_id"] == attempt_id]
    assert len(matching) == 1
    assert matching[0]["success_with_no_product"] is True, (
        "success+none was not flagged by the query's own derived column")


def test_attempts_by_state_excludes_rows_younger_than_the_bound(conn):
    """The age filter is exclusionary, not merely sorting: a fresh row in the
    exact same state must not appear when the bound asks for older rows.
    """
    attempt_id = fixture.make_completed_attempt(conn, rapid_outcome="success")
    # No backdating: ended_at stays at "now" from make_completed_attempt.

    rows = actions.attempts_by_state(conn, "terminal_after_start",
                                     older_than_seconds=3600)
    assert attempt_id not in [r["attempt_id"] for r in rows], (
        "a row younger than --older-than was returned")


def test_attempts_by_state_excludes_other_states(conn):
    """The state filter is exact — a row in a DIFFERENT lifecycle_state,
    however old, is not returned for a query naming another state.
    """
    attempt_id = fixture.make_attempt(conn, error_category="APPLICATION_FAILED")
    _age_ended_at(conn, attempt_id, hours_ago=5)

    rows = actions.attempts_by_state(conn, "terminal_after_start",
                                     older_than_seconds=3600)
    assert attempt_id not in [r["attempt_id"] for r in rows], (
        "a row in a different lifecycle_state matched a state-scoped query")


def test_attempts_by_state_ages_from_submitted_at_with_no_other_timestamp(
        conn):
    """A `submitted` row has no `ended_at`/`started_at` at all — the
    docstring's own COALESCE(ended_at, started_at, submitted_at) is
    exercised down to its last fallback, not assumed.
    """
    logical_job_id, run_id = fixture.make_logical_job(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(schema_version), 1) FROM attempts")
        schema_version = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO attempts"
            "  (run_id, schema_version, logical_job_id, lifecycle_state,"
            "   created_at, submitted_at)"
            " VALUES (%s, %s, %s, 'submitted', now(), now())"
            " RETURNING attempt_id",
            [run_id, schema_version, logical_job_id])
        attempt_id = cur.fetchone()[0]
    conn.commit()
    _age_submitted_at(conn, attempt_id, hours_ago=5)

    rows = actions.attempts_by_state(conn, "submitted",
                                     older_than_seconds=3600)
    matching = [r for r in rows if r["attempt_id"] == attempt_id]
    assert len(matching) == 1, (
        "a row with no ended_at or started_at was not aged from "
        "submitted_at as the COALESCE ordering documents")
    assert matching[0]["started_at"] is None
    assert matching[0]["ended_at"] is None


# ---------------------------------------------------------------------------
# `attempt_detail` — the joined view.
# ---------------------------------------------------------------------------
def test_attempt_detail_not_found_returns_none(conn):
    """`show-attempt` on an attempt id that does not exist: None, cleanly.

    `_cmd_show_attempt` turns this into EXIT_USAGE and a stderr message —
    this pins the layer under that, the function contract `actions.py`'s
    own docstring states ("Returns None if no such attempt").
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT coalesce(max(attempt_id), 0) + 1000000 FROM attempts")
        missing_id = cur.fetchone()[0]

    assert actions.attempt_detail(conn, missing_id) is None


def test_attempt_detail_returns_the_core_row_with_no_work_unit_or_submission(
        conn):
    """The plain case: an attempt with neither a work unit nor a submission.

    `work_unit_id` is NULL on every pre-intent-layer attempt
    (`actions.py`'s own comment) — this is that row, and both LEFT JOINs
    must produce SQL NULL for their columns rather than dropping the
    attempt row or raising. `success_with_no_product` is itself NULL here
    (NULL AND NULL, under `attempts_state_terminal_after_start_check`'s
    ban on setting outcome facts before the columns exist) — this is a
    `submitted` row, so both operands of the AND are unset, which is a
    genuinely different, and equally real, absence than the False case
    `test_attempts_by_state_returns_seeded_rows_in_the_named_state` covers.
    """
    attempt_id, _ = _submitted_attempt_with_own_run_id(conn, "no-join")

    detail = actions.attempt_detail(conn, attempt_id)
    assert detail is not None
    assert detail["attempt_id"] == attempt_id
    assert detail["work_unit_id"] is None
    assert detail["work_unit_job_type"] is None
    assert detail["work_unit_state"] is None
    assert detail["submission_id"] is None
    assert detail["submission_job_name"] is None
    assert detail["submission_state"] is None
    assert not detail["success_with_no_product"]
    assert detail["stages"] == []


def test_attempt_detail_joins_the_linked_work_unit(conn):
    """A real `work_units` row, linked by `attempts.work_unit_id`, surfaces
    its job_type/state/operational_class through the LEFT JOIN — the
    populated half of the join this file's header names as the interesting
    property to pin alongside the absent half.
    """
    work_unit_id = fixture.create_unit(conn, fixture.scope("attempt-detail-wu"))
    attempt_id, _ = _submitted_attempt_with_own_run_id(
        conn, "work-unit-join", work_unit_id=work_unit_id)

    detail = actions.attempt_detail(conn, attempt_id)
    assert detail["work_unit_id"] == work_unit_id
    assert detail["work_unit_job_type"] == fixture.JOB_TYPE
    assert detail["work_unit_state"] == "ready"
    assert detail["work_unit_operational_class"] == "prompt-processing"
    # No submission was created for this run_id — the two joins are
    # independent, and this pins that a populated work_unit join does not
    # somehow manufacture a submission row.
    assert detail["submission_id"] is None


@pytest.mark.usefixtures("_requires_submissions_table")
def test_attempt_detail_joins_the_most_recent_submission_by_run_id(conn):
    """`submissions` joins on `run_id` alone, and picks the NEWEST row when
    more than one exists for the same run_id — `_ATTEMPT_CORE`'s own
    comment states this is deliberate ("the submission an operator asking
    about a specific attempt almost always means"), and this is the case
    that comment describes, not merely the single-row case.
    """
    attempt_id, run_id = _submitted_attempt_with_own_run_id(
        conn, "newest-submission")

    _prepared_submission(conn, run_id, job_name="rapid-%s-older" % run_id,
                         scheduler_job_id="job-older")
    # Force the first submission's created_at strictly earlier so the second
    # one is unambiguously the newest by the LATERAL's own ORDER BY, rather
    # than depending on two INSERTs in the same transaction landing on
    # different clock ticks.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE submissions SET created_at = now() - interval '1 hour'"
            " WHERE run_id = %s", [run_id])
    conn.commit()
    newest_id = _prepared_submission(
        conn, run_id, job_name="rapid-%s-newest" % run_id,
        scheduler_job_id="job-newest")

    detail = actions.attempt_detail(conn, attempt_id)
    assert detail["submission_id"] == newest_id, (
        "show-attempt's LATERAL did not pick the most recently created "
        "submission for this run_id")
    assert detail["submission_job_name"] == "rapid-%s-newest" % run_id


@pytest.mark.usefixtures("_requires_submissions_table")
def test_attempt_detail_submission_join_is_scoped_to_this_run_id(conn):
    """A submission under a DIFFERENT run_id must never appear on this
    attempt's detail — the LATERAL is correlated on `a.run_id`, and a bug
    that dropped the correlation (an unscoped "most recent submission
    anywhere") would only be caught by having a second, unrelated
    submission actually present in the database when the query runs.
    """
    attempt_id, run_id = _submitted_attempt_with_own_run_id(
        conn, "run-id-scoping")

    other_run_id = "other-" + run_id
    _prepared_submission(conn, other_run_id,
                         job_name="rapid-%s" % other_run_id,
                         scheduler_job_id="job-other-run")

    detail = actions.attempt_detail(conn, attempt_id)
    assert detail["submission_id"] is None, (
        "a submission under a different run_id leaked into this attempt's "
        "joined detail")


def test_attempt_detail_includes_stages_ordered_by_started_at(conn):
    """`attempt_stages` is read as a SEPARATE one-to-many query, per
    `attempt_detail`'s own docstring — this proves the split actually
    returns every stage row for the attempt, in the documented order,
    rather than silently dropping the one-to-many side.

    Named so alphabetical stage_name order DISAGREES with started_at
    order ('stage-z' is EARLIEST): `_ATTEMPT_STAGES` orders by
    `started_at, stage_name`, and a query that accidentally sorted by
    `stage_name` alone would pass a same-named-as-ordered fixture without
    ever being caught.

    Uses the plain `fixture.make_attempt` (shared default run_id) rather
    than `_submitted_attempt_with_own_run_id`: `attempt_stages` is keyed by
    `attempt_id` alone (its own FK), never by `run_id`, so this test is not
    exposed to the shared-run_id/submissions-LATERAL trap that helper
    exists to avoid.
    """
    attempt_id = fixture.make_attempt(conn, lifecycle="submitted")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attempt_stages"
            "  (attempt_id, stage_name, started_at, duration_ms, outcome)"
            " VALUES (%s, 'stage-a', now() - interval '1 minute', 500,"
            "         'success'),"
            "        (%s, 'stage-z', now() - interval '2 minutes', 250,"
            "         'success')",
            [attempt_id, attempt_id])
    conn.commit()

    detail = actions.attempt_detail(conn, attempt_id)
    assert [s["stage_name"] for s in detail["stages"]] == \
        ["stage-z", "stage-a"], (
        "attempt_stages rows were not returned in started_at order")


def test_attempt_detail_reads_registration_outcome_off_the_core_row(conn):
    """`registration_outcome` is an existing jsonb column on `attempts`, read
    straight off the core row rather than joined — `attempt_detail`'s
    docstring is explicit that this is deliberate, so this pins that the
    value written at INSERT survives through to the returned detail dict
    unmodified.

    `attempts_registration_outcome_state_check` (migration 024) forbids a
    non-NULL `registration_outcome` while `lifecycle_state` is `submitted`
    or `started`, so this needs a row past that point —
    `terminal_without_start` (via `error_category`) satisfies the check
    without pulling in `terminal_after_start`'s much larger required-column
    set. Uses the plain `fixture.make_attempt` (shared default run_id)
    rather than `_submitted_attempt_with_own_run_id`, which only builds a
    `submitted` row — `registration_outcome` is a plain column read off the
    core row, unaffected by any other test's rows under the same run_id,
    so the shared-run_id/submissions-LATERAL trap does not apply here.
    """
    attempt_id = fixture.make_attempt(conn, error_category="APPLICATION_FAILED")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE attempts SET registration_outcome = %s::jsonb"
            " WHERE attempt_id = %s",
            ['{"accepted": true, "products": 3}', attempt_id])
    conn.commit()

    detail = actions.attempt_detail(conn, attempt_id)
    assert detail["registration_outcome"] == {"accepted": True, "products": 3}
