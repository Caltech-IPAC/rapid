"""Replay registration over the published attempts the vocabulary defect refused.

The payload has always published `zogy_diffimage` / `sfft_diffimage` /
`naive_diffimage`; the registration reader demanded a literal
`difference_image` that nothing produces, so every science attempt refused
promotion and `diffimages` stayed empty while the published attempts piled
up. The role binding fixes the reader; this replays the backlog.

Registration is idempotent BY DESIGN — `add_diffimage` finds-or-inserts on
the (attempt_id, sequence) pair rather than minting `max(version)+1` — so
running this twice must not double any row. That is not assumed here: the
script counts before, registers, counts again, and can run a second pass to
show the guard working rather than trusting the docstring.

    python3 -m pipeline.registration.test.live_role_binding_replay [--twice]

Reads RAPID_RECORDS_BUCKET / RAPID_DB_SECRET_ID / DBSERVER / DBPORT /
DBNAME from the environment, as the operator's own registration path does.
"""

import sys

from database.modules.utils import rapid_db
from pipeline.operator.registrar import production_registrar
from pipeline.seams import run_registration
from pipeline.virtualPipelineOperator import registration_callback


COUNTS = """
SELECT (SELECT count(*) FROM diffimages)                        AS diffimages,
       (SELECT count(*) FROM diffimages WHERE vbest = 1)        AS vbest1,
       (SELECT count(*) FROM diffimmeta)                        AS diffimmeta,
       (SELECT count(*) FROM attempts
         WHERE product_disposition = 'published'
           AND rapid_outcome = 'success'
           AND registered_at IS NULL)                           AS unregistered,
       (SELECT count(*) FROM attempts
         WHERE registration_outcome IS NOT NULL)                AS with_outcome
"""

#: WHAT A DUPLICATE ACTUALLY IS. Several rows per (rid, ppid) is the
#: DESIGNED shape, not a fault: reprocessing gives one row per version and
#: the partial unique index promotes exactly one of them
#: (design/catalog.md § Promotion, "Enforcement"). Counting those as
#: duplicates reports 180 violations against a healthy table.
#:
#: The two real invariants are: at most one promoted row per identity
#: group, and never two rows at the same version.
DUPLICATES = """
SELECT (SELECT count(*) FROM (
          SELECT rid, ppid FROM diffimages WHERE vbest IN (1, 2)
           GROUP BY rid, ppid HAVING count(*) > 1) promoted)
     + (SELECT count(*) FROM (
          SELECT rid, ppid, version FROM diffimages
           GROUP BY rid, ppid, version HAVING count(*) > 1) versioned)
"""


def counts(conn):
    with conn.cursor() as cur:
        cur.execute(COUNTS)
        row = cur.fetchone()
        cur.execute(DUPLICATES)
        duplicate_groups = cur.fetchone()[0]
    return {"diffimages": row[0], "vbest1": row[1], "diffimmeta": row[2],
            "unregistered": row[3], "with_outcome": row[4],
            "duplicate_groups": duplicate_groups}


def show(label, values):
    print("{}: {}".format(label, values), flush=True)


def one_pass(factory, label):
    """One registration pass on its own connection, as the operator does."""
    dbh = rapid_db.RAPIDDB()
    if dbh.conn is None or dbh.exit_code >= 64:
        print("*** cannot reach the database; quitting")
        return None, None
    conn = dbh.conn
    try:
        before = counts(conn)
        show("{} before".format(label), before)
        run = run_registration(conn, registration_callback(factory, conn))
        conn.commit()
        after = counts(conn)
        show("{} after ".format(label), after)
        print("{} run: registered={} skipped={} deferred={} failed={}".format(
            label, run.registered, run.skipped, run.deferred, run.failed),
            flush=True)
        return before, after
    finally:
        dbh.close()


def main(argv):
    twice = "--twice" in argv
    # The replay switch, stated out loud: these records were authored before
    # bindings were recorded at all, so the running release supplies theirs.
    factory = production_registrar(replay_pre_binding_roles=True)

    before, after = one_pass(factory, "pass 1")
    if after is None:
        # An unreachable database is a FAILED replay, not an empty one: a
        # zero here would read as "nothing to do" in exactly the ledger
        # this script exists to supply.
        return 64
    if before["diffimages"] == after["diffimages"] and before["unregistered"]:
        print("*** no rows were added and candidates remain; the replay did "
              "not do what it was run to do")
        return 1

    if twice:
        # THE IDEMPOTENCE PROOF. A second pass over the same attempts must
        # add no diffimages rows and create no duplicate identity groups.
        second_before, second_after = one_pass(factory, "pass 2")
        added = second_after["diffimages"] - second_before["diffimages"]
        print("IDEMPOTENT_ADDED_ROWS={}".format(added), flush=True)
        print("DUPLICATE_GROUPS={}".format(
            second_after["duplicate_groups"]), flush=True)
        if added != 0 or second_after["duplicate_groups"] != 0:
            print("*** replay was NOT idempotent")
            return 1

    print("REPLAY_OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
