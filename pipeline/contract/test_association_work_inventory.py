"""Contract tests: the two gates that decide what "science work" is must agree
(rule 19, brief F2) — and they must do it in SQL, against real rows.

**WHY THIS FILE EXISTS.** An earlier revision of the cross-date ordering gate
answered "which (date, field) pairs are science work" with its own query over
`Attempts ⋈ logical_jobs` filtered on `job_type = 'science'`, while the sibling
gate `RAPIDDB.get_fields_with_science_jobs_for_processing_date` answered the
same question over `DiffImages ⋈ Attempts` filtered on `ppid`, `vbest = 1`,
`rapid_outcome = 'success'` and a created-date window. A code comment asserted
the two "read off the same fact" and "cannot disagree".

That was a claim, not a mechanism, and it was wrong: a science attempt that
succeeded but whose difference image was superseded (`vbest = 0`) is work to
one formulation and not the other, as is a row outside the created window. The
repair was structural — `pipeline.repositories.association` derives its
inventory from the sibling's own predicate, written once — and this file is
what keeps it true. Divergence is a red test here, not a paragraph.

Every test seeds real `diffimages` and `attempts` rows and compares the two
gates PAIR BY PAIR. Comparing summaries (a count, a min) could agree by luck
on cases where the underlying sets differ; comparing inventories cannot.

This is also where the two relocated repository methods get their real-SQL
coverage. Before this file both executed only against stubs, which is the
stub-blind failure the repo's own rule names: SQL tests execute SQL.
"""

import pytest

from pipeline.contract import fixture
from pipeline.repositories.association import AssociationRepository
from submission.routes import JOB_TYPE_SCIENCE, ppid_for


def _require_schema(conn):
    """Skip unless DRAFT 049 is applied. Probed, never assumed."""
    if not fixture.has_table(conn, "association_watermarks"):
        pytest.skip("DRAFT migration 049 is not applied; the association "
                    "ordering tests need it and skip cleanly without it")


SCIENCE_PPID = ppid_for(JOB_TYPE_SCIENCE)

#: Fields well outside anything the pipeline uses, tagged per run so two runs
#: of this suite never see each other's rows. The gates read the whole corpus,
#: so the tests filter their comparisons to these fields rather than assuming
#: an empty database — a suite that only passed on an empty database would be
#: useless on rapid-admin, where other briefs' fixtures live.
def _fields(count):
    base = 970000 + (int(fixture.RUN_TAG[:4], 16) % 900) * 10
    return [base + i for i in range(count)]


def _sibling_fields(conn, proc_date):
    """The per-date gate's own answer, run as its own SQL.

    The production method lives on `RAPIDDB`, which needs a handle this tier
    does not build, so its QUERY is executed here verbatim — copied from
    `database/modules/utils/rapid_db.py`'s
    `get_fields_with_science_jobs_for_processing_date`. Verbatim matters: a
    paraphrase would make this test assert agreement between the repository
    and a paraphrase, which is exactly the failure mode it exists to catch.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select distinct d.field from DiffImages d "
            "join Attempts a on a.attempt_id = d.attempt_id "
            "where d.ppid = %s "
            "and d.vbest = 1 "
            "and a.rapid_outcome = 'success' "
            "and d.created >= cast(%s as timestamp) "
            "and d.created < cast(%s as timestamp) + cast('1 day' as interval) "
            "and d.field is not null "
            "order by d.field;",
            [SCIENCE_PPID, proc_date, proc_date])
        return [row[0] for row in cur.fetchall()]


def _repository_fields(conn, proc_date, only):
    """The repository's inventory for one date, restricted to our fields."""
    inventory = AssociationRepository(conn).science_work_inventory(proc_date)
    return sorted(field for pd, field in inventory
                  if pd == proc_date and field in only)


def _seed(conn, **kwargs):
    """One finished science attempt plus its difference image.

    The attempt is built `terminal_after_start` with every column that state's
    CHECK requires — see `fixture.make_completed_attempt`. An outcome cannot
    be bolted onto a half-built row: the schema refuses it, which is the
    invariant these tests exist inside rather than around.
    """
    attempt_id = fixture.make_completed_attempt(
        conn,
        rapid_outcome=kwargs.pop("rapid_outcome", "success"),
        field=kwargs.get("field"),
        processing_date=kwargs.pop("processing_date", None))
    fixture.make_diffimage(conn, attempt_id, ppid=kwargs.pop("ppid",
                                                             SCIENCE_PPID),
                           **kwargs)
    return attempt_id


@pytest.fixture
def clean_fields(conn):
    """Remove this run's fixture rows afterwards, whatever the test did."""
    _require_schema(conn)
    fields = _fields(6)
    yield fields
    # Children before parents — `diffimages` references `l2files`,
    # `exposures` and `refimages`, so deleting in the other order is a
    # foreign-key violation rather than a cleanup. The attempts rows are
    # detached rather than deleted: other tables reference them, and this
    # fixture owns the field, not the attempt.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM diffimages WHERE field = ANY(%s)", [fields])
        cur.execute("DELETE FROM l2files WHERE field = ANY(%s)", [fields])
        cur.execute("DELETE FROM refimages WHERE field = ANY(%s)", [fields])
        cur.execute("DELETE FROM exposures WHERE field = ANY(%s)", [fields])
        cur.execute("UPDATE attempts SET field = NULL WHERE field = ANY(%s)",
                    [fields])
    conn.commit()


def test_the_two_gates_agree_on_every_edge_state(conn, clean_fields):
    """The agreement proof, across the states that could separate them.

    Five rows, each placed where the two formulations could have disagreed:

      * a plain successful science image — work, on both readings;
      * a SUPERSEDED image (`vbest = 0`) whose attempt still succeeded. The
        `logical_jobs` formulation counted this as work because the attempt
        exists; the DiffImages formulation does not, because a superseded
        image is not the current one. This is the row that broke the claim;
      * a NON-SUCCESS outcome. The attempt is there, so an attempt-keyed
        query sees it; there is no usable difference image, so the work is
        not owed;
      * a row OUTSIDE the created-date window, which an unbounded query
        counts into the wrong date;
      * a row under a DIFFERENT ppid, which is another pipeline's work.

    Both gates are asked, and the assertion is on the inventories being equal
    — not on either one matching a list the test wrote down, which would only
    prove the test agrees with itself.
    """
    fields = clean_fields
    proc_date = "20260801"
    day = "2026-08-01T12:00:00+00:00"

    _seed(conn, field=fields[0], created=day)
    _seed(conn, field=fields[1], created=day, vbest=0)
    _seed(conn, field=fields[2], created=day, rapid_outcome="failure")
    _seed(conn, field=fields[3], created="2026-08-02T12:00:00+00:00")
    _seed(conn, field=fields[4], created=day, ppid=SCIENCE_PPID + 1)
    conn.commit()

    sibling = sorted(f for f in _sibling_fields(conn, proc_date)
                     if f in fields)
    repository = _repository_fields(conn, proc_date, fields)

    assert repository == sibling, (
        "the cross-date gate's work inventory disagrees with the per-date "
        "gate's; they are supposed to be the same predicate")
    # And the agreed answer is the RIGHT one, or both could be wrong together.
    assert sibling == [fields[0]], (
        "only the current, successful, in-window, science-ppid image is work")


def test_the_gates_agree_at_the_day_boundary(conn, clean_fields):
    """`date_trunc('day', created)` and the half-open window are one set.

    The repository truncates to the day; the sibling uses `>= proc_date AND
    < proc_date + 1 day`. Those are the same set, and this is where that is
    asserted rather than assumed — day-boundary off-by-ones are exactly the
    class of bug that survives review because both forms "obviously" mean the
    same thing.

    Three rows: the first instant of the day, the last instant before
    midnight, and the first instant of the NEXT day.
    """
    fields = clean_fields
    proc_date = "20260803"

    _seed(conn, field=fields[0], created="2026-08-03T00:00:00+00:00")
    _seed(conn, field=fields[1], created="2026-08-03T23:59:59+00:00")
    _seed(conn, field=fields[2], created="2026-08-04T00:00:00+00:00")
    conn.commit()

    sibling = sorted(f for f in _sibling_fields(conn, proc_date)
                     if f in fields)
    repository = _repository_fields(conn, proc_date, fields)

    assert repository == sibling
    assert sibling == sorted([fields[0], fields[1]]), (
        "the day window must include midnight and exclude the next midnight")


def test_the_gates_agree_on_a_date_with_mixed_states(conn, clean_fields):
    """A realistic date: some work current, some superseded, some failed."""
    fields = clean_fields
    proc_date = "20260805"
    day = "2026-08-05T06:00:00+00:00"

    _seed(conn, field=fields[0], created=day)
    _seed(conn, field=fields[1], created=day)
    _seed(conn, field=fields[2], created=day, vbest=0)
    _seed(conn, field=fields[3], created=day, rapid_outcome="failure")
    conn.commit()

    sibling = sorted(f for f in _sibling_fields(conn, proc_date)
                     if f in fields)
    repository = _repository_fields(conn, proc_date, fields)

    assert repository == sibling
    assert sibling == sorted([fields[0], fields[1]])


def test_a_vbest_flip_moves_both_gates_together(conn, clean_fields):
    """The state that broke the original claim, exercised as a transition.

    `vbest` flipping from 1 to 0 must remove the field from BOTH inventories
    in the same step. The attempt row is untouched throughout — which is
    precisely why an attempt-keyed formulation could not see this change at
    all, and why it disagreed.
    """
    fields = clean_fields
    proc_date = "20260807"
    day = "2026-08-07T06:00:00+00:00"

    _seed(conn, field=fields[0], created=day)
    conn.commit()

    assert _repository_fields(conn, proc_date, fields) == [fields[0]]
    assert sorted(f for f in _sibling_fields(conn, proc_date)
                  if f in fields) == [fields[0]]

    with conn.cursor() as cur:
        cur.execute("UPDATE diffimages SET vbest = 0 WHERE field = %s",
                    [fields[0]])
    conn.commit()

    sibling = sorted(f for f in _sibling_fields(conn, proc_date)
                     if f in fields)
    repository = _repository_fields(conn, proc_date, fields)

    assert repository == sibling == [], (
        "a superseded image left one gate's inventory and not the other's")


# ---------------------------------------------------------------------------
# Real-SQL coverage of the relocated repository methods (fix 3)
# ---------------------------------------------------------------------------


def test_earliest_unaccepted_date_executes_and_finds_the_earliest(
        conn, clean_fields):
    """`earliest_unaccepted_date` against real rows, not a stub.

    Two dates owe work; the earlier one must be named. This is the method the
    cross-date gate calls, and before this test it had never executed against
    PostgreSQL in any tier — its CTEs, its `to_char`, its LEFT JOIN and its
    watermark bound were all unexercised SQL.
    """
    fields = clean_fields
    _seed(conn, field=fields[0], created="2026-08-09T06:00:00+00:00")
    _seed(conn, field=fields[1], created="2026-08-10T06:00:00+00:00")
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("UPDATE association_watermarks"
                    "   SET watermark_proc_date = NULL, watermark_field = NULL"
                    " WHERE association_set = 1 AND lane = 0")
    conn.commit()

    owed = AssociationRepository(conn).earliest_unaccepted_date()

    # The query executes and returns a date — the point of this test is that
    # its SQL runs at all, which it never did before this file existed.
    assert owed is not None, "two dates owe work and the query found neither"
    assert len(owed) == 8 and owed.isdigit(), \
        f"the owed date should be YYYYMMDD text, got {owed!r}"
    # Bounded by OUR earlier seed rather than asserted equal to it: the
    # scratch database carries other briefs' fixture rows, so an older owed
    # date is a legitimate answer. What must hold is that the earlier of our
    # two dates is not skipped in favour of the later one.
    assert owed <= "20260809", (
        f"the query named {owed}, later than our earliest owed date 20260809; "
        "the min() is not finding the earliest")


def test_an_accepted_pair_leaves_the_owed_inventory(conn, clean_fields):
    """A crossmatch attempt that is pending-or-successful clears the pair.

    The `accepted` CTE joins `Attempts ⋈ logical_jobs` on the CROSSMATCH job
    type — the same blocking predicate the resubmission gate uses, so "in
    flight or done" means one thing in this codebase. Exercised here as SQL
    rather than trusted as a comment.
    """
    fields = clean_fields
    proc_date = "20260812"
    _seed(conn, field=fields[0], created="2026-08-12T06:00:00+00:00")
    conn.commit()

    repository = AssociationRepository(conn)

    # The assertion is on THIS pair's membership in the owed inventory, not on
    # the global minimum: rapid-admin's scratch database carries other briefs'
    # fixture rows, so `earliest_unaccepted_date()` may legitimately name an
    # older date throughout. A test that asserted on the minimum would pass or
    # fail according to what its neighbours left behind.
    def _owed_pairs():
        with conn.cursor() as cur:
            cur.execute(
                "WITH science AS ("
                "  SELECT to_char(date_trunc('day', d.created), 'YYYYMMDD')"
                "         AS pd, d.field AS field"
                "    FROM DiffImages d"
                "    JOIN Attempts a ON a.attempt_id = d.attempt_id"
                "   WHERE d.ppid = %s AND d.vbest = 1"
                "     AND a.rapid_outcome = 'success' AND d.field IS NOT NULL"
                "), accepted AS ("
                "  SELECT to_char(la.processing_date, 'YYYYMMDD') AS pd,"
                "         la.field AS field"
                "    FROM Attempts la"
                "    JOIN logical_jobs lj"
                "      ON lj.logical_job_id = la.logical_job_id"
                "   WHERE lj.job_type = 'crossmatch' AND la.field IS NOT NULL"
                "     AND (la.lifecycle_state IN ('submitted','started')"
                "          OR la.rapid_outcome = 'success')"
                ") "
                "SELECT s.pd, s.field FROM science s"
                "  LEFT JOIN accepted a ON a.pd = s.pd AND a.field = s.field"
                " WHERE a.field IS NULL AND s.field = ANY(%s)",
                [SCIENCE_PPID, fields])
            return cur.fetchall()

    assert (proc_date, fields[0]) in _owed_pairs(), \
        "the seeded pair should be owed before any crossmatch attempt exists"

    # A pending crossmatch attempt for that (date, field). `make_attempt`
    # builds a `submitted` row, whose CHECK requires the binding triple at
    # schema_version >= 2 (migration 013) — the subject columns are added by
    # the same UPDATE so the row is only ever written complete.
    crossmatch_attempt = fixture.make_attempt(conn, lifecycle="submitted")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE attempts"
            "   SET field = %s, processing_date = %s::date,"
            "       binding_job_definition_arn ="
            "         coalesce(binding_job_definition_arn, %s),"
            "       binding_image_digest ="
            "         coalesce(binding_image_digest, 'sha256:crossmatch'),"
            "       binding_manifest_checksum ="
            "         coalesce(binding_manifest_checksum, 'sha256:manifest')"
            " WHERE attempt_id = %s",
            [fields[0], proc_date,
             "arn:aws:batch:us-east-1:000000000000:job-definition/xm:1",
             crossmatch_attempt])
        cur.execute(
            "UPDATE logical_jobs SET job_type = 'crossmatch'"
            " WHERE logical_job_id ="
            "   (SELECT logical_job_id FROM attempts WHERE attempt_id = %s)",
            [crossmatch_attempt])
    conn.commit()

    assert (proc_date, fields[0]) not in _owed_pairs(), (
        "an accepted (date, field) pair is still being reported as owed")
    # And the method under test executes its own SQL over the same state.
    # Asserted as "this date is not what it names", which is true whatever
    # other briefs' rows are present: an accepted pair cannot be the reason a
    # date is owed, so if this date still has no other owed field the query
    # must have moved past it.
    owed_here = [f for pd, f in _owed_pairs() if pd == proc_date]
    if not owed_here:
        assert repository.earliest_unaccepted_date() != proc_date, (
            "no field of this date is owed, yet the query still names it")


def test_claim_position_executes_against_the_real_watermark_row(conn):
    """`claim_position` against real SQL, including its COALESCE lookup.

    The query resolves the set through `derived.live_association_set()` when
    the caller names none — a function call inside a COALESCE inside a WHERE
    clause, which no stub exercises. Asserted against a value written by a
    separate statement, so the read is proving it read.
    """
    _require_schema(conn)
    repository = AssociationRepository(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT watermark_proc_date, watermark_field"
                    "  FROM association_watermarks"
                    " WHERE association_set = 1 AND lane = 0")
        before = cur.fetchone()
        cur.execute("UPDATE association_watermarks"
                    "   SET watermark_proc_date = '20260814', "
                    "       watermark_field = 77"
                    " WHERE association_set = 1 AND lane = 0")
    conn.commit()

    try:
        position = repository.claim_position()
        assert position is not None, "the ordering schema is applied; this " \
            "must not report it absent"
        assert (position.proc_date, position.field) == ("20260814", 77)
    finally:
        with conn.cursor() as cur:
            cur.execute("UPDATE association_watermarks"
                        "   SET watermark_proc_date = %s, watermark_field = %s"
                        " WHERE association_set = 1 AND lane = 0",
                        [before[0], before[1]])
        conn.commit()


def test_the_schema_probe_distinguishes_absent_from_empty(conn):
    """`ordering_schema_present` asks the catalog, not a failing query.

    On a database carrying DRAFT 049 it must answer True — and it must do so
    by finding the table, which is what keeps "not deployed" apart from "the
    query is broken". Those two facts must never be conflated: conflating
    them turns a broken query into a silent loss of the ordering guarantee.
    """
    _require_schema(conn)
    assert AssociationRepository(conn).ordering_schema_present() is True


def test_a_repository_query_failure_raises_rather_than_returning_none(
        conn, second_conn):
    """The typed refusal, from a real driver error.

    `RepositoryQueryFailed` is what replaced `RAPIDDB`'s `exit_code = 67;
    return None`, and the difference matters most exactly here: `None` is the
    ordering gate's "schema absent" answer, so a repository that returned it
    on failure would convert a database outage into silently unordered
    gathering. Provoked with a real aborted transaction rather than a patched
    method, so the raising path is the production one.

    **ON `second_conn`, NOT `conn`.** `conn` is the session-scoped connection
    every other test in this tier shares (`conftest.py`), so aborting its
    transaction here poisons whatever runs next — a first version did exactly
    that and turned one deliberate failure into six unrelated
    `InFailedSqlTransaction` errors. A test that breaks a shared fixture is
    not testing the failure path, it is manufacturing one for its neighbours.
    """
    _require_schema(conn)
    from pipeline.repositories.errors import RepositoryQueryFailed

    with second_conn.cursor() as cur:
        with pytest.raises(Exception):
            cur.execute("SELECT this_function_does_not_exist()")

    # That connection is now in a failed transaction, so the next statement
    # errors — the repository must surface that as its typed failure.
    with pytest.raises(RepositoryQueryFailed):
        AssociationRepository(second_conn).claim_position()

    second_conn.rollback()
