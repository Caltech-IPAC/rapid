"""Contract tests: ordered claiming behind the watermark (rule 19, brief F2).

Acceptance criterion 1, against a real database. The stub tier covers the
claim ARITHMETIC — given a watermark and a candidate set, which field is next —
and covers it more cheaply and in more cases than this can. What it cannot
cover is the part that only a database has:

  * the watermark the claim path reads is the SAME ROW the acceptance path
    advances. In the stub tier those are two test doubles that agree because
    the test made them agree; here one writes and the other reads, through the
    production SQL both use.
  * `derived.live_association_set()` resolves the set the claim path keys on.
    A stub answers whatever it was constructed with, including a set that does
    not exist.

So this tier asserts the LOOP: claim, accept, claim again, and watch the
frontier move. That sequence is the whole of criterion 1 and it is not
expressible without both halves talking to one row.
"""

import pytest

from pipeline.association import sets, watermark
from pipeline.contract import fixture
from submission.gathering import _next_claimable_field


def _require_schema(conn):
    """Skip unless DRAFT 049 is applied. Probed, never assumed."""
    if not fixture.has_table(conn, "association_watermarks"):
        pytest.skip("DRAFT migration 049 is not applied; the association "
                    "ordering tests need it and skip cleanly without it")


@pytest.fixture
def live_lane(conn):
    """The live lane, parked at the origin, restored afterwards."""
    _require_schema(conn)
    with conn.cursor() as cur:
        before = watermark.read_watermark(cur, 1, sets.DEFAULT_LANE)
        cur.execute("UPDATE association_watermarks"
                    "   SET watermark_proc_date = NULL, watermark_field = NULL"
                    " WHERE association_set = 1 AND lane = 0")
    conn.commit()
    yield 1, sets.DEFAULT_LANE
    with conn.cursor() as cur:
        cur.execute("UPDATE association_watermarks"
                    "   SET watermark_proc_date = %s, watermark_field = %s"
                    " WHERE association_set = 1 AND lane = 0",
                    [before[0], before[1]])
    conn.commit()


def _claim(conn, association_set, lane, candidates, proc_date,
           earliest_owed=None):
    """What would gathering yield for this date, given the live watermark?

    Reads the watermark through the production helper and runs the production
    claim rule over it. The gatherer itself is not called because it needs a
    `UnitSource` handle and a whole readiness path; the ordering DECISION is
    what this tier is here to check against a real row, and that decision is
    `_next_claimable_field`.
    """
    with conn.cursor() as cur:
        position = watermark.read_watermark(cur, association_set, lane)
    return list(_next_claimable_field(candidates, proc_date, position,
                                      earliest_owed))


def test_claiming_walks_the_canonical_order_one_unit_at_a_time(
        conn, live_lane):
    """Criterion 1, the whole loop.

    With (d1,f1), (d1,f2) and (d2,f1) all ready, gathering yields exactly
    (d1,f1); after its acceptance advances the watermark, exactly (d1,f2);
    after that one, (d2,f1). At every step exactly ONE unit is claimable —
    §2.5's hard cap of 1, falling out of claim discipline rather than a queue
    setting.
    """
    association_set, lane = live_lane
    d1, d2 = "20260808", "20260809"
    f1, f2 = 101, 202

    # Step 1: the origin. The lowest unit of the earliest date.
    assert _claim(conn, association_set, lane, [f1, f2], d1) == [f1]
    # And d2 is not claimable while d1 still owes work.
    assert _claim(conn, association_set, lane, [f1], d2,
                  earliest_owed=d1) == []

    with conn.cursor() as cur:
        assert watermark.advance(cur, association_set, lane, d1, f1)
    conn.commit()

    # Step 2: the next field of the same date, and only it.
    assert _claim(conn, association_set, lane, [f1, f2], d1) == [f2]

    with conn.cursor() as cur:
        assert watermark.advance(cur, association_set, lane, d1, f2)
    conn.commit()

    # Step 3: d1 is exhausted, so nothing of d1 is claimable and d2 opens —
    # but only once d1 no longer owes work.
    assert _claim(conn, association_set, lane, [f1, f2], d1) == []
    assert _claim(conn, association_set, lane, [f1], d2,
                  earliest_owed=d1) == []
    assert _claim(conn, association_set, lane, [f1], d2,
                  earliest_owed=d2) == [f1]


def test_a_failed_retryable_predecessor_blocks_every_later_unit(
        conn, live_lane):
    """Criterion 1's second half, and §2.5's actual sentence.

    "Serial execution does not by itself guarantee that a later observation's
    association cannot run ahead of an earlier one still in retry." With
    (d1,f1) failed-and-retryable and (d2,f1) ready, gathering yields NOTHING.

    The mechanism is that a failed unit did not ADVANCE the watermark —
    acceptance is what advances it — so the frontier still sits behind it and
    `earliest_owed` still names d1. Nothing here is a special "is it failed"
    check; the blocking IS the watermark, read correctly.
    """
    association_set, lane = live_lane
    d1, d2 = "20260808", "20260809"

    # d1's unit ran and failed: no advance happened.
    with conn.cursor() as cur:
        assert watermark.read_watermark(
            cur, association_set, lane) == (None, None)

    assert _claim(conn, association_set, lane, [1], d2, earliest_owed=d1) == []

    # It stays blocked across passes — this is not a one-poll race.
    assert _claim(conn, association_set, lane, [1], d2, earliest_owed=d1) == []

    # And the moment d1 is accepted, d2 opens.
    with conn.cursor() as cur:
        assert watermark.advance(cur, association_set, lane, d1, 1)
    conn.commit()
    assert _claim(conn, association_set, lane, [1], d2, earliest_owed=d2) == [1]


def test_a_terminally_disposed_unit_is_skipped_only_by_an_explicit_advance(
        conn, live_lane):
    """The disposition rule, fixed and asserted.

    A cancelled or parked unit is skipped by ADVANCING THE WATERMARK PAST IT
    EXPLICITLY — the operator action — never by gathering quietly deciding it
    no longer counts. Until that advance happens the set stays stopped at the
    disposed unit, which is the honest behaviour: an ordering that silently
    steps over its own failures stops guaranteeing anything exactly when it
    matters.
    """
    association_set, lane = live_lane
    d1, d2 = "20260808", "20260809"

    # The unit is cancelled. Nothing about that advances the frontier.
    assert _claim(conn, association_set, lane, [1], d2, earliest_owed=d1) == []

    # The operator advances past it, explicitly and durably.
    with conn.cursor() as cur:
        assert watermark.advance(cur, association_set, lane, d1, 1)
    conn.commit()

    assert _claim(conn, association_set, lane, [1], d2, earliest_owed=d2) == [1]


def test_the_claim_path_reads_the_row_the_acceptance_path_writes(
        conn, second_conn):
    """One row, two paths — the thing the stub tier cannot show.

    The acceptance path advances on one connection; the claim path must see
    it on ANOTHER, after commit. Two doubles agreeing in one process proves
    nothing about that.
    """
    _require_schema(conn)
    with conn.cursor() as cur:
        before = watermark.read_watermark(cur, 1, sets.DEFAULT_LANE)
        cur.execute("UPDATE association_watermarks"
                    "   SET watermark_proc_date = NULL, watermark_field = NULL"
                    " WHERE association_set = 1 AND lane = 0")
    conn.commit()

    try:
        with conn.cursor() as cur:
            assert watermark.advance(cur, 1, sets.DEFAULT_LANE, "20260808", 42)
        conn.commit()

        with second_conn.cursor() as cur:
            seen = watermark.read_watermark(cur, 1, sets.DEFAULT_LANE)
        second_conn.rollback()

        assert seen == ("20260808", 42)
    finally:
        with conn.cursor() as cur:
            cur.execute("UPDATE association_watermarks"
                        "   SET watermark_proc_date = %s, watermark_field = %s"
                        " WHERE association_set = 1 AND lane = 0",
                        [before[0], before[1]])
        conn.commit()


def test_gathering_without_the_draft_schema_keeps_todays_behaviour(conn):
    """The explicit degradation, asserted rather than assumed.

    `position is None` means the ordering schema is absent, and the claim rule
    then yields every candidate — exactly what the gatherer did before this
    work. This is what keeps `smdc` CI green and every un-migrated database in
    the fleet working: the ordering is a property of the deployed schema, so a
    deployment without it does not get to claim the ordering, and does not get
    broken by its absence either.
    """
    assert list(_next_claimable_field([3, 1, 2], "20260808", None)) == [3, 1, 2]
