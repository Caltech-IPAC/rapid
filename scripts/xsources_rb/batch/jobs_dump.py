"""Dump a run's jobs (id, stage, scope, batch ids, instance_type, started, ended, metrics) to JSON."""
import json, sys
from rapidpipe.db.connection import connect
runs = [int(x) for x in sys.argv[1].split(",")]; out = sys.argv[2]
with connect(application_name="ort-threads-batch") as conn, conn.cursor() as cur:
    cur.execute("SELECT id, run, stage, scope, state, instance_type, batch_job_id, attempts, started, ended,"
                " manifest -> 'metrics' FROM jobs WHERE run = ANY(%s) ORDER BY id", (runs,))
    rows = [dict(id=r[0], run=r[1], stage=r[2], scope=r[3], state=r[4], instance_type=r[5], batch_job_id=r[6],
                 attempts=r[7], started=r[8].isoformat() if r[8] else None,
                 ended=r[9].isoformat() if r[9] else None, metrics=r[10]) for r in cur.fetchall()]
    conn.rollback()
json.dump(rows, open(out, "w"), default=str)
print(len(rows))
