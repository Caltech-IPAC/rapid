"""Live, READ-ONLY proof of the connection helper's explicit interface (O1).

NOT a unit test, and deliberately not part of `live_roundtrip.py`: that
probe writes attempt rows, and this one exists to be runnable when writing
to the operations database is not allowed. It opens two connections and
runs `SELECT`s. It creates nothing, updates nothing, and deletes nothing.

What it proves that the unit suite cannot. The environment policy retired
the process's write of its own endpoint and credential into `os.environ`:
the payload entrypoint and the reconciler now PASS them to `connect`. The
unit suite proves the values reach `psycopg2.connect`; only a live
connection proves the server accepts a credential delivered that way, and
that is the claim the reconciler's startup rests on.

The environment is CLEARED around the explicit connection, so a connection
that succeeded by quietly falling back to the boundary read fails here
rather than passing for the wrong reason. The second connection then
re-establishes that the boundary read still works, because the explicit
interface added a path and did not replace one.

Usage: python3 -m database.modules.utils.test.live_explicit_interface
Exits 0 and prints LIVE-EXPLICIT-OK only if every check passes.
"""

import os
import sys

from database.modules.utils import rapid_db_connect as dbc

failures = []


def check(name, expected, got):
    if expected == got:
        print(f"PASS  {name}  (expected={expected!r} got={got!r})")
    else:
        print(f"FAIL  {name}  (expected={expected!r} got={got!r})")
        failures.append(name)


def main():
    print("=== live: the connection helper's explicit interface ===")
    print(f">> DBSERVER={os.getenv('DBSERVER')} DBPORT={os.getenv('DBPORT')} "
          f"DBNAME={os.getenv('DBNAME')}")
    print(f">> RAPID_DB_SECRET_ID={os.getenv('RAPID_DB_SECRET_ID')}")

    endpoint = dbc.Endpoint(host=os.environ["DBSERVER"],
                            port=os.environ["DBPORT"],
                            dbname=os.environ["DBNAME"])
    # Resolved through the boundary read ONCE, under the instance role —
    # which is what the reconciler's `_database_credentials` does through
    # Secrets Manager, and what the entrypoint's
    # `database_connection_inputs` does from the parameter tree.
    credentials = dbc.Credentials(*dbc.resolve_credentials())

    saved = {name: os.environ.pop(name, None)
             for name in ("DBSERVER", "DBPORT", "DBNAME",
                          "RAPID_DB_SECRET_ID", "DBUSER", "DBPASS")}
    try:
        # Nothing that could reach a database is left in the environment.
        check("environment/endpoint-removed", None, os.environ.get("DBSERVER"))
        check("environment/secret-id-removed", None,
              os.environ.get("RAPID_DB_SECRET_ID"))

        with dbc.connection("live-explicit", lane=dbc.LANE_TRANSACTION,
                            endpoint=endpoint,
                            credentials=credentials) as conn:
            executor = dbc.ConnectionExecutor(conn)
            rows = executor(
                "SELECT current_user, current_setting('application_name')", [])
            print(f">> connected as {rows[0][0]!r} "
                  f"application_name={rows[0][1]!r}")
            check("explicit/connected-with-an-empty-environment",
                  "rapid_pipeline", rows[0][0])
            check("explicit/application_name-carries-lane",
                  "live-explicit[transaction]", rows[0][1])
            # The property the reconciler change exists to establish: the
            # password is not in this process's environment while a
            # connection made from it is open.
            check("explicit/no-password-in-environment", None,
                  os.environ.get("DBPASS"))
            check("explicit/no-user-in-environment", None,
                  os.environ.get("DBUSER"))

        # The boundary read must still refuse when there is nothing to read:
        # the explicit interface is an addition, not a silent default.
        try:
            dbc.connect("live-explicit-should-fail")
        except dbc.DBCredentialError as exc:
            check("boundary/refuses-an-empty-environment", True,
                  "DBSERVER" in str(exc))
        else:
            check("boundary/refuses-an-empty-environment", True, False)
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value

    # And the boundary read still works when the environment does carry it —
    # the path every standalone script still takes.
    with dbc.connection("live-boundary", lane=dbc.LANE_TRANSACTION) as conn:
        rows = dbc.ConnectionExecutor(conn)("SELECT current_user", [])
        check("boundary/still-connects-from-the-environment",
              "rapid_pipeline", rows[0][0])

    print()
    if failures:
        print(f"!! {len(failures)} check(s) FAILED: " + ", ".join(failures))
        return 1
    print("LIVE-EXPLICIT-OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
