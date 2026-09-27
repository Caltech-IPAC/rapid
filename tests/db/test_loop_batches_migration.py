"""DB-backed proof of migration ``20260926-01-loop-batches.sql``:
``loop_dates`` gains ``batch``/``kind`` and a widened primary
key, and the new ``loop_deliveries`` table exists with its state ``CHECK``
and primary key. Each test runs inside the never-committed outer transaction
``tests/db/conftest.py`` gives every test, so nothing here needs cleanup.
"""

from __future__ import annotations

import pytest

from rapidpipe.db.ids import new_ulid
from rapidpipe.runs import repository


def _make_run(conn) -> str:
    """A minimal run, only to satisfy ``loop_dates.run``'s foreign key."""
    return repository.create_run(
        conn, "scratch", "test", "loop batches migration test", ["difference"], "a" * 40,
        None, "20260926-01-loop-batches.sql", None, None, "prompt", "default", "rapid",
        1, False, None)


def test_loop_dates_has_batch_and_kind_columns_with_defaults(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type, column_default, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'loop_dates' AND column_name IN ('batch', 'kind')
            """)
        columns = {row[0]: row[1:] for row in cur.fetchall()}
    assert set(columns) == {"batch", "kind"}
    data_type, default, nullable = columns["batch"]
    assert data_type == "integer"
    assert default == "1"
    assert nullable == "NO"
    data_type, default, nullable = columns["kind"]
    assert data_type == "text"
    assert default == "'batch'::text"
    assert nullable == "NO"


def test_loop_dates_primary_key_is_schedule_date_batch(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.attname
            FROM pg_constraint c
            JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
            WHERE c.conrelid = 'loop_dates'::regclass AND c.contype = 'p'
            ORDER BY k.ord
            """)
        pk_columns = [row[0] for row in cur.fetchall()]
    assert pk_columns == ["schedule", "processing_date", "batch"]


def test_loop_dates_kind_check_allows_batch_and_switch_only(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'loop_dates'::regclass AND contype = 'c' "
            "AND pg_get_constraintdef(oid) LIKE '%kind%'")
        (constraint_def,) = cur.fetchone()
    assert "'batch'" in constraint_def and "'switch'" in constraint_def


def test_loop_dates_accepts_two_batches_of_one_date_but_not_a_duplicate_batch(conn):
    schedule = f"test-batches-{new_ulid()}"
    run1, run2, run3 = _make_run(conn), _make_run(conn), _make_run(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO loop_dates (schedule, processing_date, run, state, batch) "
            "VALUES (%s, '2027-11-01', %s, 'complete', 1)", (schedule, run1))
        cur.execute(
            "INSERT INTO loop_dates (schedule, processing_date, run, state, batch) "
            "VALUES (%s, '2027-11-01', %s, 'open', 2)", (schedule, run2))
        cur.execute(
            "SELECT batch, kind FROM loop_dates WHERE schedule = %s ORDER BY batch",
            (schedule,))
        assert cur.fetchall() == [(1, "batch"), (2, "batch")]

        with pytest.raises(Exception) as exc_info:
            cur.execute(
                "INSERT INTO loop_dates (schedule, processing_date, run, state, batch) "
                "VALUES (%s, '2027-11-01', %s, 'open', 1)", (schedule, run3))
        assert "loop_dates" in str(exc_info.value).lower() or "duplicate" in \
            str(exc_info.value).lower()
    conn.rollback()  # the failed INSERT aborts the transaction; conftest rolls back too


def test_loop_dates_existing_rows_default_to_batch_1_kind_batch(conn):
    """A row inserted the pre-batches way (no batch/kind named) still gets
    batch=1, kind='batch' from the column defaults (existing rows keep
    batch 1)."""
    schedule = f"test-default-{new_ulid()}"
    run_id = _make_run(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO loop_dates (schedule, processing_date, run, state) "
            "VALUES (%s, '2027-11-01', %s, 'open')", (schedule, run_id))
        cur.execute("SELECT batch, kind FROM loop_dates WHERE schedule = %s", (schedule,))
        assert cur.fetchone() == (1, "batch")


def test_loop_deliveries_exists_with_state_check_and_pk(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('loop_deliveries') IS NOT NULL")
        (exists,) = cur.fetchone()
        assert exists

        cur.execute(
            """
            SELECT a.attname
            FROM pg_constraint c
            JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
            WHERE c.conrelid = 'loop_deliveries'::regclass AND c.contype = 'p'
            ORDER BY k.ord
            """)
        pk_columns = [row[0] for row in cur.fetchall()]
        assert pk_columns == ["schedule", "location"]

        cur.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'loop_deliveries'::regclass AND contype = 'c' "
            "AND pg_get_constraintdef(oid) LIKE '%state%'")
        (constraint_def,) = cur.fetchone()
    for state in ("batched", "refused", "quarantined", "deferred"):
        assert f"'{state}'" in constraint_def


def test_loop_deliveries_rejects_an_unknown_state(conn):
    schedule = f"test-state-{new_ulid()}"
    with conn.cursor() as cur:
        with pytest.raises(Exception) as exc_info:
            cur.execute(
                "INSERT INTO loop_deliveries (schedule, location, processing_date, state) "
                "VALUES (%s, 's3://bucket/x', '2027-11-01', 'bogus')", (schedule,))
        assert "check" in str(exc_info.value).lower() or "constraint" in \
            str(exc_info.value).lower()
    conn.rollback()


def test_loop_deliveries_two_locations_same_identity_both_insert(conn):
    """Identity (exposure, detector, version) is indexed, not unique: the
    index only speeds classification lookups; a re-delivery is refused
    by the code, not the schema, so two rows may share an identity (a
    ``batched`` one and a ``refused``/``quarantined`` one for the repeat)."""
    schedule = f"test-identity-{new_ulid()}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO loop_deliveries (schedule, location, processing_date, exposure, "
            "detector, version, checksum, state, batch) VALUES "
            "(%s, 's3://bucket/a', '2027-11-01', 'exp1', '1', '1', 'sha-a', 'batched', 1)",
            (schedule,))
        cur.execute(
            "INSERT INTO loop_deliveries (schedule, location, processing_date, exposure, "
            "detector, version, checksum, state, reason) VALUES "
            "(%s, 's3://bucket/b', '2027-11-01', 'exp1', '1', '1', 'sha-a', 'refused', "
            "'identical re-delivery')", (schedule,))
        cur.execute("SELECT location, state FROM loop_deliveries WHERE schedule = %s "
                    "ORDER BY location", (schedule,))
        assert cur.fetchall() == [("s3://bucket/a", "batched"), ("s3://bucket/b", "refused")]
