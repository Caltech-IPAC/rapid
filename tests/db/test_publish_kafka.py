"""``rapidpipe publish``'s reads and its stamp against the schema.

Contract: the sets offered are the run's packet jobs with an open
``packets`` binding and a null ``kafka_published_at``; a closed binding,
another run, another stage or a stamped job is not offered; ``stamp``
sets the column, after which the job is no longer offered.
"""

from __future__ import annotations

from psycopg2.extras import Json

from rapidpipe.foundation import publish_kafka


def _job(cur, run: int, stage: str = "packet") -> int:
    cur.execute("INSERT INTO jobs (run, stage, scope, state, published_attempt)"
                " VALUES (%s, %s, '{}', 'published', 1) RETURNING id", (run, stage))
    return cur.fetchone()[0]


def _bind(cur, job: int, product: str, expid: int, closed: bool = False) -> None:
    cur.execute("INSERT INTO official (product, key, job, opened_group, opened_by, opened_why,"
                " closed_group, closed_by, closed_at, closed_why) VALUES (%s, %s, %s, 1, 't', 't',"
                " %s, %s, CASE WHEN %s THEN now() END, %s)",
                (product, Json({"expid": expid}), job, 2 if closed else None,
                 "t" if closed else None, closed, "t" if closed else None))


def test_only_unstamped_official_packet_sets_of_the_run_are_offered(connect):
    conn = connect()
    with conn.cursor() as cur:
        runs = []
        for _ in range(2):
            cur.execute("INSERT INTO runs (who, why, revision, settings, survey, dataset)"
                        " VALUES ('t', 't', 'r', '{}', 'S', 'D') RETURNING id")
            runs.append(cur.fetchone()[0])
        offered, stamped, closed, other_run = (_job(cur, runs[0]) for _ in range(4))
        cur.execute("UPDATE jobs SET run = %s WHERE id = %s", (runs[1], other_run))
        associate = _job(cur, runs[0], "associate")
        _job(cur, runs[0])  # a packet set with no binding
        for expid, job in enumerate((offered, stamped, other_run)):
            _bind(cur, job, "packets", expid)
        _bind(cur, closed, "packets", 9, closed=True)
        _bind(cur, associate, "objects", 0)
        cur.execute("UPDATE jobs SET kafka_published_at = now() WHERE id = %s", (stamped,))
    conn.commit()
    assert publish_kafka.unpublished_sets(conn, runs[0]) == [offered]
    publish_kafka.stamp(conn, offered)
    assert publish_kafka.unpublished_sets(conn, runs[0]) == []
