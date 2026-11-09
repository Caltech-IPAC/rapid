"""rb scores of runs' SCA-3 sources and xsources, compared by (expid, sign, catalog id) to a base run."""
import sys, json
from rapidpipe.db.connection import connect
base, others, sca = int(sys.argv[1]), [int(x) for x in sys.argv[2].split(",")], int(sys.argv[3])
out = {}
with connect(application_name="ort-threads-batch") as conn, conn.cursor() as cur:
    for table, key in (("sources", "id"), ("xsources", "num")):
        got = {}
        for run in [base] + others:
            cur.execute(f"SELECT s.expid, s.isdiffpos, s.{key}, s.rb FROM {table} s JOIN jobs j ON j.id = s.job"
                        " WHERE j.run = %s AND s.sca = %s", (run, sca))
            got[run] = {(e, p, k): rb for e, p, k, rb in cur.fetchall()}
        for run in others:
            a, b = got[base], got[run]
            common = set(a) & set(b)
            exps = {k[0] for k in b}
            a_in = {k for k in a if k[0] in exps}
            equal = sum(1 for k in common if a[k] == b[k])
            diff = max((abs(a[k] - b[k]) for k in common if a[k] is not None and b[k] is not None), default=0.0)
            out[f"{table} {run} vs {base}"] = dict(rows=len(b), base_rows_same_epochs=len(a_in), common=len(common),
                                                   equal=equal, max_abs_diff=diff, epochs=len(exps))
    conn.rollback()
print(json.dumps(out, indent=1))
