"""``exposure_ids``: the id the database hands out for a producer's exposure name within a dataset.

Contracts (rapid_docs exposure-id): two connections resolving one
``(dataset, name)`` at once get one id, the second waiting on the first's
uncommitted insert and reading the row it committed; two names that pack to
one old ``expid`` (program 1043's two execution plans) get two ids; a name
keeps its id on a later pass and under another connection, and a second
row for one ``(dataset, name)`` is refused by the table; one identity
serves every dataset, so the same name in two datasets gets two ids. Run by
foundation.yml against database/schema.sql.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import psycopg2.errors
import pytest

from rapidpipe.foundation import loop
from tests.db.foundation_helpers import wait_until_blocked

DATASET = "flight"
#: Program 1043's plan 01 and plan 02 of one pass, observation, visit and exposure: one packed expid.
PLAN_1 = "r0104301001001001001_0001"
PLAN_2 = "r0104302001001001001_0001"


def test_two_connections_resolving_one_name_at_once_get_one_id(connect):
    first, second, observer = connect(), connect(), connect()
    with second.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        pid = cur.fetchone()[0]
    second.commit()
    # The first inserts the name and holds its transaction open.
    with first.cursor() as cur:
        cur.execute("INSERT INTO exposure_ids (dataset, name) VALUES (%s, %s)"
                    " ON CONFLICT (dataset, name) DO NOTHING", (DATASET, PLAN_1))
    with ThreadPoolExecutor(1) as pool:
        # The second's resolution waits on it rather than inserting a second row or finding none.
        waiting = pool.submit(loop.exposure_ids, second, DATASET, [PLAN_1])
        wait_until_blocked(observer, pid)
        assert not waiting.done()
        first.commit()
        theirs = waiting.result(timeout=10)
    mine = loop.exposure_ids(first, DATASET, [PLAN_1])
    assert theirs == mine and set(mine) == {PLAN_1}
    with observer.cursor() as cur:
        cur.execute("SELECT count(*) FROM exposure_ids WHERE dataset = %s AND name = %s", (DATASET, PLAN_1))
        assert cur.fetchone() == (1,)
    observer.commit()


def test_two_names_that_pack_to_one_old_expid_get_two_ids(connect):
    ids = loop.exposure_ids(connect(), DATASET, [PLAN_1, PLAN_2])
    assert set(ids) == {PLAN_1, PLAN_2} and ids[PLAN_1] != ids[PLAN_2]


def test_a_name_keeps_its_id_and_a_second_row_for_it_is_refused_by_the_table(connect):
    conn = connect()
    (first,) = loop.exposure_ids(conn, DATASET, [PLAN_1]).values()
    later = loop.exposure_ids(connect(), DATASET, [PLAN_1, PLAN_2])      # a later pass, another connection
    assert later[PLAN_1] == first and later[PLAN_2] != first
    with pytest.raises(psycopg2.errors.UniqueViolation):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO exposure_ids (dataset, name) VALUES (%s, %s)", (DATASET, PLAN_1))
    conn.rollback()


def test_one_identity_serves_every_dataset_so_a_name_in_two_datasets_gets_two_ids(connect):
    conn = connect()
    here = loop.exposure_ids(conn, DATASET, [PLAN_1])[PLAN_1]
    there = loop.exposure_ids(conn, "flight-trial", [PLAN_1])[PLAN_1]
    assert here != there
    assert loop.exposure_ids(conn, DATASET, []) == {}
