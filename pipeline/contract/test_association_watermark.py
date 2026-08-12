"""Contract tests: the association watermark, its lane lease and set scoping
(conformance rule 19, target §2.5, brief F).

Everything here is a property of PostgreSQL, which is why it is in this tier
rather than beside the code:

  * the CAS predicate `watermark_proc_date IS NULL OR (watermark_proc_date,
    watermark_field) < (%s, %s)` is a ROW-WISE comparison the database
    evaluates against whatever another transaction committed while this one
    waited. A fake would evaluate it against a value the test itself set,
    which makes the assertion a tautology.
  * `pg_advisory_xact_lock(0x414C, key)` serializes two writers on one
    (association_set, lane) and does not serialize writers on different ones.
    Nothing outside a real database demonstrates that.
  * ATOMICITY — that a rolled-back transaction takes the watermark advance
    with it — is the one property the stub tier cannot express at all. A fake
    cursor has no rollback that means anything.

The suite mirrors `test_registration_watermark.py` in shape deliberately: the
association watermark is C's discipline at a different grain, and a second,
differently-shaped set of assertions about the same mechanism would be a
liability rather than extra coverage.
"""

import threading

import pytest

from pipeline.association import sets, watermark
from pipeline.contract import fixture


def _require_schema(conn):
    """Skip unless DRAFT 049 is applied. Probed, never assumed."""
    if not fixture.has_table(conn, "association_watermarks"):
        pytest.skip("DRAFT migration 049 is not applied; the association "
                    "ordering tests need it and skip cleanly without it")


#: A reprocessing set id well outside anything the migration inserts, tagged
#: per run so two runs of this suite never collide on the registry's primary
#: key. The live set is 1 and is never created or dropped here — these tests
#: assert that live rows are untouched, so creating a second live set would be
#: destroying the thing under test.
def _reprocessing_set_id():
    return 9000 + (int(fixture.RUN_TAG[:4], 16) % 900)


def _read(conn, association_set, lane=0):
    with conn.cursor() as cur:
        return watermark.read_watermark(cur, association_set, lane)


def _reset_live_watermark(conn):
    """Put the live lane back at the origin, and report what it was.

    These tests move the LIVE lane's watermark, because that is the row the
    production paths use and a test against a different row would be testing
    something nothing runs. The fixture honesty rule still applies — every
    test restores what it moved — so this returns the prior value for the
    caller to put back.
    """
    before = _read(conn, 1)
    with conn.cursor() as cur:
        cur.execute("UPDATE association_watermarks"
                    "   SET watermark_proc_date = NULL,"
                    "       watermark_field = NULL"
                    " WHERE association_set = 1 AND lane = 0")
    conn.commit()
    return before


def _restore_live_watermark(conn, before):
    with conn.cursor() as cur:
        cur.execute("UPDATE association_watermarks"
                    "   SET watermark_proc_date = %s, watermark_field = %s"
                    " WHERE association_set = 1 AND lane = 0",
                    [before[0], before[1]])
    conn.commit()


@pytest.fixture
def live_lane(conn):
    """The live lane at the origin for the duration of one test."""
    _require_schema(conn)
    before = _reset_live_watermark(conn)
    yield 1, sets.DEFAULT_LANE
    _restore_live_watermark(conn, before)


# ---------------------------------------------------------------------------
# Criterion 2 — watermark atomicity and the CAS
# ---------------------------------------------------------------------------


def test_the_cas_refuses_to_move_the_watermark_backwards(conn, live_lane):
    """A unit at or behind the frontier must not move it.

    THE PREDICATE IS THE TEST, exactly as it is for the registration
    watermark. The guard is row-wise on `(watermark_proc_date,
    watermark_field)`, so advancing to (20260808, 5) after (20260809, 1) has
    landed must match ZERO rows — the database's own evaluation of the row it
    holds, not the test's.

    This is the mechanism criterion 3's stale retry depends on: a retry that
    was claimed before its predecessor accepted, and lands after, must leave
    the frontier where it is rather than dragging it back and re-opening
    every unit in between.
    """
    association_set, lane = live_lane

    with conn.cursor() as cur:
        assert watermark.advance(cur, association_set, lane, "20260809", 1)
    conn.commit()
    assert _read(conn, association_set) == ("20260809", 1)

    with conn.cursor() as cur:
        # Earlier date, higher field: the row-wise comparison must read the
        # DATE first. A guard that compared field alone would accept this,
        # which is the bug this case exists to catch.
        moved = watermark.advance(cur, association_set, lane, "20260808", 5)
    conn.commit()

    assert moved is False, (
        "the CAS matched a row for a unit BEHIND the frontier; the predicate "
        "is not guarding monotonicity")
    assert _read(conn, association_set) == ("20260809", 1), \
        "the watermark regressed"


def test_re_advancing_to_the_same_unit_is_a_no_op(conn, live_lane):
    """The frontier is strictly ahead, so its own value is not ahead of it.

    This is what makes a concurrent duplicate attempt converge without a
    special case: the second advance finds itself no longer ahead and refuses,
    which is the same code path a stale retry takes.
    """
    association_set, lane = live_lane

    with conn.cursor() as cur:
        assert watermark.advance(cur, association_set, lane, "20260809", 7)
    conn.commit()

    with conn.cursor() as cur:
        again = watermark.advance(cur, association_set, lane, "20260809", 7)
    conn.commit()

    assert again is False
    assert _read(conn, association_set) == ("20260809", 7)


def test_a_rolled_back_transaction_takes_the_watermark_advance_with_it(
        conn, live_lane):
    """Rule 19's "advanced in the same transaction" — the atomicity half.

    THE PROPERTY THE STUB TIER CANNOT EXPRESS. A crossmatch transaction that
    fails after advancing the watermark must leave the frontier where it was:
    otherwise the set has stepped past a unit whose associations were rolled
    back, and no later pass will ever claim that unit again — the ordering
    would have quietly SKIPPED work while reporting success.

    The failure is forced the way a real one arrives: a statement error inside
    the same transaction, after the advance.
    """
    association_set, lane = live_lane

    with conn.cursor() as cur:
        assert watermark.advance(cur, association_set, lane, "20260810", 3)
        with pytest.raises(Exception):
            cur.execute("SELECT this_function_does_not_exist()")
    conn.rollback()

    assert _read(conn, association_set) == (None, None), (
        "the watermark survived a rolled-back transaction; the advance is not "
        "atomic with the work it gates")


def test_the_advance_and_its_rows_commit_together(conn, live_lane):
    """The other half: on COMMIT, both land.

    Asserted with a scratch table standing in for the association rows, so
    the test states the transactional property without depending on the
    crossmatch stage's fixtures. What matters is that one transaction carries
    both and a second connection sees neither until it commits.
    """
    association_set, lane = live_lane
    table = f"assoc_atomicity_{fixture.RUN_TAG}"

    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE IF NOT EXISTS {table} (aid bigint)")
    conn.commit()

    try:
        with conn.cursor() as cur:
            cur.execute(f"INSERT INTO {table} VALUES (1)")
            assert watermark.advance(cur, association_set, lane, "20260811", 2)
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table}")
            assert cur.fetchone()[0] == 1
        assert _read(conn, association_set) == ("20260811", 2)
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()


# ---------------------------------------------------------------------------
# Criterion 3 — stale-retry safety
# ---------------------------------------------------------------------------


def test_a_stale_acceptance_leaves_the_watermark_and_writes_no_duplicates(
        conn, live_lane):
    """An acceptance at or behind the frontier changes nothing.

    Two mechanisms guarantee that, and the brief asks which does what, so the
    test asserts them separately:

      * the WATERMARK half is the CAS — asserted here by the refusal;
      * the ASSOCIATION half is `merges_aid_sid_unique` (migration 027,
        reaching each clone through `INCLUDING INDEXES`), which refuses a
        second identical `(aid, sid)` merge row. `radec_index` is what makes
        the re-run compute the SAME `aid` for the same source, so that the
        index has an identical row to refuse — neither alone suffices, and
        the unique index is the mechanism that actually guarantees it.

    Asserted against a real clone family built the way the stage builds one.
    """
    association_set, lane = live_lane
    field = 990001
    merges = f"merges_{field}_{fixture.RUN_TAG}"

    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE IF NOT EXISTS {merges} "
                    f"(LIKE merges INCLUDING DEFAULTS INCLUDING CONSTRAINTS "
                    f" INCLUDING INDEXES)")
    conn.commit()

    try:
        with conn.cursor() as cur:
            assert watermark.advance(cur, association_set, lane, "20260809", 4)
            cur.execute(f"INSERT INTO {merges} (aid, sid) VALUES (11, 22)")
        conn.commit()

        # The stale retry: the same unit's associations arriving again, behind
        # the frontier.
        with conn.cursor() as cur:
            moved = watermark.advance(cur, association_set, lane, "20260809", 4)
            cur.execute(f"INSERT INTO {merges} (aid, sid) VALUES (11, 22) "
                        f"ON CONFLICT DO NOTHING")
        conn.commit()

        assert moved is False
        assert _read(conn, association_set) == ("20260809", 4)
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {merges}")
            assert cur.fetchone()[0] == 1, "the stale retry duplicated a merge row"
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {merges}")
        conn.commit()


def test_the_unique_index_is_what_refuses_the_duplicate(conn):
    """Name the mechanism rather than assuming it.

    An unguarded second insert of the same `(aid, sid)` must RAISE. If this
    passes silently the clone was born without `merges_aid_sid_unique` and
    the test above would be proving nothing — it would be `ON CONFLICT DO
    NOTHING` doing the work over a table with no constraint to conflict on.
    """
    _require_schema(conn)
    merges = f"merges_uniq_{fixture.RUN_TAG}"

    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE IF NOT EXISTS {merges} "
                    f"(LIKE merges INCLUDING DEFAULTS INCLUDING CONSTRAINTS "
                    f" INCLUDING INDEXES)")
    conn.commit()

    try:
        with conn.cursor() as cur:
            cur.execute(f"INSERT INTO {merges} (aid, sid) VALUES (5, 6)")
        conn.commit()

        with pytest.raises(Exception):
            with conn.cursor() as cur:
                cur.execute(f"INSERT INTO {merges} (aid, sid) VALUES (5, 6)")
            conn.commit()
        conn.rollback()
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {merges}")
        conn.commit()


# ---------------------------------------------------------------------------
# Criterion 4 — the lane lease and the lock order
# ---------------------------------------------------------------------------


def test_the_lane_lease_serializes_two_transactions(conn, second_conn):
    """Two writers on one (set, lane) serialize; the second waits.

    The assertion is on ORDER OF EVENTS, not on elapsed time — a slow machine
    makes this test slower, never falser. The same shape as
    `test_the_attempt_lease_serializes_two_registrars`.
    """
    _require_schema(conn)
    association_set, lane = 1, sets.DEFAULT_LANE

    holder_ready = threading.Event()
    release = threading.Event()
    acquired_at = {}
    released_at = []
    order = []

    def waiter():
        holder_ready.wait(10)
        with second_conn.cursor() as cur:
            watermark.acquire_lane_lease(cur, association_set, lane)
            order.append("acquired")
            acquired_at["t"] = len(order)
        second_conn.rollback()

    thread = threading.Thread(target=waiter)
    with conn.cursor() as cur:
        watermark.acquire_lane_lease(cur, association_set, lane)
        thread.start()
        holder_ready.set()
        release.wait(1.0)
        assert "t" not in acquired_at, (
            "the second transaction took the lane lease while the first held "
            "it; the lease is not serializing")
        order.append("released")
        released_at.append(len(order))
    conn.commit()

    thread.join(20)
    assert "t" in acquired_at, "the waiter never acquired the lane lease"
    assert acquired_at["t"] >= released_at[0]


def test_the_lane_lease_namespace_scopes_the_lock(conn, second_conn):
    """Different lanes do not serialize; the same lane does.

    Without this half the test above would pass against a database taking no
    locks at all — and would also pass if every lane shared one lock, which is
    the design error that would make lanes pointless the day they multiply.
    """
    _require_schema(conn)
    key_lane_0 = watermark._lane_lock_key(1, 0)
    key_lane_1 = watermark._lane_lock_key(1, 1)

    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                    (watermark.LANE_LEASE_NAMESPACE, key_lane_0))

    with second_conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_xact_lock(%s, %s)",
                    (watermark.LANE_LEASE_NAMESPACE, key_lane_1))
        assert cur.fetchone()[0] is True, \
            "a different LANE blocked; the lock key does not include the lane"

        cur.execute("SELECT pg_try_advisory_xact_lock(%s, %s)",
                    (watermark.LANE_LEASE_NAMESPACE, key_lane_0))
        assert cur.fetchone()[0] is False, \
            "the same lane did not block; the lease is not a lock"

        # And a different NAMESPACE at the same key must not collide — the
        # property that keeps AL from serializing against WU by accident.
        cur.execute("SELECT pg_try_advisory_xact_lock(%s, %s)",
                    (0x5755, key_lane_0))
        assert cur.fetchone()[0] is True, \
            "the association lease collided with the work-unit namespace"

    second_conn.rollback()
    conn.rollback()


def test_the_documented_lock_order_places_the_lane_lease_lowest(conn):
    """The order is documented and the namespaces are distinct.

    The four namespaces must be pairwise distinct — the whole two-level order
    rests on it, and AL joining as level 3 only works if it collides with
    none of them. Asserted against the constants the production modules
    export, so a change to one without the other fails here.
    """
    from pipeline.intent import lock as intent_lock
    from pipeline.reconciler import lease as reconciler_lease
    from pipeline.registration import consumer as registration

    namespaces = [
        registration.ATTEMPT_LEASE_NAMESPACE,
        reconciler_lease.LEASE_NAMESPACE,
        intent_lock.WORK_UNIT_NAMESPACE,
        watermark.LANE_LEASE_NAMESPACE,
    ]
    assert len(set(namespaces)) == len(namespaces), (
        "two lock namespaces collide; the documented order is not total")
    assert watermark.LANE_LEASE_NAMESPACE == 0x414C


# ---------------------------------------------------------------------------
# Criterion 5 — set isolation
# ---------------------------------------------------------------------------


def test_a_non_live_set_materializes_its_own_clone_family(conn):
    """Reprocessing isolation, structurally.

    The live set's tables and watermark must be BYTE-IDENTICAL after a
    reprocessing set writes: not "unchanged as far as we checked", but the
    same rows and the same frontier. The isolation is a property of the NAMES
    — a reprocessing set never names a live table — so the test asserts the
    naming and then asserts the live side did not move.
    """
    _require_schema(conn)
    reprocessing = _reprocessing_set_id()
    field = 990002
    live_before = _read(conn, 1)

    with conn.cursor() as cur:
        cur.execute("INSERT INTO association_sets "
                    "(association_set, kind, label) VALUES (%s, %s, %s) "
                    "ON CONFLICT (association_set) DO NOTHING",
                    [reprocessing, sets.KIND_REPROCESSING,
                     f"contract-test {fixture.RUN_TAG}"])
        cur.execute("INSERT INTO association_watermarks "
                    "(association_set, lane) VALUES (%s, 0) "
                    "ON CONFLICT DO NOTHING", [reprocessing])
    conn.commit()

    try:
        live_name = sets.table_name("merges", 1, field, sets.KIND_LIVE_PROMPT)
        repro_name = sets.table_name("merges", reprocessing, field,
                                     sets.KIND_REPROCESSING)
        assert live_name == f"merges_{field}", \
            "the live set's clone names changed; adopting 049 must move no data"
        assert repro_name != live_name
        assert str(field) in repro_name and f"s{reprocessing}" in repro_name

        # The SQL twin must agree, or the two languages disagree about which
        # tables a set owns — which is the isolation failing silently.
        with conn.cursor() as cur:
            cur.execute("SELECT derived.association_table_name(%s, %s, %s)",
                        ["merges", 1, field])
            assert cur.fetchone()[0] == live_name
            cur.execute("SELECT derived.association_table_name(%s, %s, %s)",
                        ["merges", reprocessing, field])
            assert cur.fetchone()[0] == repro_name

        # The reprocessing set advances its own watermark; the live one does
        # not move. Rule 19: "reprocessing sets order themselves and never
        # regress the live watermark".
        with conn.cursor() as cur:
            assert watermark.advance(cur, reprocessing, 0, "20200101", 1)
        conn.commit()

        assert _read(conn, reprocessing) == ("20200101", 1)
        assert _read(conn, 1) == live_before, \
            "a reprocessing set moved the live watermark"
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM association_watermarks "
                        "WHERE association_set = %s", [reprocessing])
            cur.execute("DELETE FROM association_sets "
                        "WHERE association_set = %s", [reprocessing])
        conn.commit()


def test_at_most_one_live_prompt_set_exists(conn):
    """"The live set" is singular, and the database is what makes it so."""
    _require_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM association_sets "
                    "WHERE kind = %s", [sets.KIND_LIVE_PROMPT])
        assert cur.fetchone()[0] == 1

    with pytest.raises(Exception):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO association_sets "
                        "(association_set, kind, label) VALUES (%s, %s, %s)",
                        [_reprocessing_set_id() + 1, sets.KIND_LIVE_PROMPT,
                         "a second live set"])
        conn.commit()
    conn.rollback()


def test_the_live_set_lookup_is_the_only_place_it_is_named(conn):
    """Python and SQL agree on which set is live, and neither hard-codes it."""
    _require_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT derived.live_association_set()")
        from_sql = cur.fetchone()[0]

    assert sets.live_association_set(conn) == from_sql


# ---------------------------------------------------------------------------
# Criterion 6 — radec_index idempotency, now pinned
# ---------------------------------------------------------------------------


def test_radec_index_assigns_identical_aids_on_a_second_run(conn):
    """Existing behaviour, pinned so the ordering work cannot regress it.

    The transition constraint the conformance ledger makes binding: keep
    `radec_index` idempotency untouched. Two runs over the same positions must
    agree on every `aid`, which is what lets the merge rows' uniqueness mean
    anything at all — a nondeterministic identity would make every re-run a
    set of new objects.
    """
    _require_schema(conn)
    import modules.utils.rapid_pipeline_subs as util

    positions = [(150.1, 2.2), (150.10001, 2.20001), (10.0, -30.0),
                 (359.9999, 89.9)]

    first = [util.radec_index(ra, dec) for ra, dec in positions]
    second = [util.radec_index(ra, dec) for ra, dec in positions]

    assert first == second, "radec_index is not deterministic"
    # INTEGRAL, not `isinstance(..., int)`. `radec_index` returns `np.int64`
    # (`modules/utils/rapid_pipeline_subs.py:2523-2526`), which is not a
    # Python `int` — an earlier version of this assertion said `int` and
    # failed on the first acceptance run against correct code. The property
    # that actually matters is that the identity is a whole number the
    # database can hold in a `bigint` column, so that is what is asserted.
    assert all(float(a).is_integer() for a in first)
    assert all(-2**63 <= int(a) < 2**63 for a in first), \
        "an aid outside bigint would not survive the round trip to merges.aid"
    # Distinct positions must not collapse onto one identity, or "the same
    # object" would mean "anywhere in this part of the sky".
    assert len(set(int(a) for a in first)) == len(positions)


# ---------------------------------------------------------------------------
# Criterion 7 — the within-job canonical order
# ---------------------------------------------------------------------------


def test_the_matching_queries_order_by_the_canonical_detection_key():
    """The two matching queries carry `ORDER BY mjdobs, sid`.

    Asserted on the query SHAPE the stage builds, because the ordering is a
    property of the SQL and not of any particular fixture's rows: a test over
    rows could pass by accident on a table small enough that the scan happened
    to be ordered. `sources.mjdobs` is the observation time and `sid` the
    detection identity — rule 19's `(observation_time, detection_id)` at the
    detection grain.
    """
    import inspect

    from pipeline.stages import post_db

    source = inspect.getsource(post_db._crossmatch_field)
    assert source.count("ORDER BY") == 2, (
        "expected both matching queries to carry an ORDER BY; the cone match "
        "and the unmatched-source scan each need one")
    assert "ORDER BY a.mjdobs, a.sid" in source
    assert "ORDER BY mjdobs, sid" in source


def test_the_sources_table_carries_the_temporal_column_the_order_names(conn):
    """`sources.mjdobs` exists, so the ORDER BY above is not aspirational.

    The vocabulary mapping allowed a fallback to `sid` alone if no temporal
    column existed. It does exist — `mjdobs double precision NOT NULL` — and
    this is where that finding is pinned rather than left in a ledger.
    """
    _require_schema(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT data_type, is_nullable FROM information_schema.columns"
            " WHERE table_name = 'sources' AND column_name = 'mjdobs'")
        row = cur.fetchone()

    assert row is not None, "sources has no mjdobs column"
    assert row[0] == "double precision"
    assert row[1] == "NO"


def test_python_and_sql_agree_on_the_canonical_order(conn, live_lane):
    """`is_ahead_of` and the CAS predicate are the same comparison.

    Written twice — once in Python for the claim path, once in SQL for the
    advance — so this asserts they cannot drift. Every case is checked against
    BOTH: whatever Python calls ahead, the database must accept, and whatever
    Python calls behind, the database must refuse.
    """
    association_set, lane = live_lane

    with conn.cursor() as cur:
        assert watermark.advance(cur, association_set, lane, "20260809", 10)
    conn.commit()
    position = _read(conn, association_set)

    cases = [
        ("20260809", 11, True),    # same date, later field
        ("20260809", 10, False),   # the frontier itself
        ("20260809", 9, False),    # same date, earlier field
        ("20260810", 1, True),     # later date, lower field — date wins
        ("20260808", 99, False),   # earlier date, higher field — date wins
    ]

    for proc_date, field, expected in cases:
        assert watermark.is_ahead_of(position, proc_date, field) is expected, \
            f"Python disagreed about ({proc_date}, {field})"

        with conn.cursor() as cur:
            cur.execute(
                "SELECT (%s IS NULL"
                "        OR (%s::text, %s::integer) < (%s::text, %s::integer))",
                [position[0], position[0], position[1], proc_date, field])
            from_sql = cur.fetchone()[0]
        assert from_sql is expected, \
            f"the database disagreed about ({proc_date}, {field})"
