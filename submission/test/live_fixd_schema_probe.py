"""Query-only probe: what the real rapid-db holds for the FixD round.

Same shape and same reasons as `live_fixc_schema_probe.py` — facts about the
DEPLOYED database that no unit test can answer, gathered before a migration is
written rather than assumed from the repo's baseline DDL.

  1. The migration high-water mark. `database/migrations/` in the rapid repo is
     a README pointing at the rapid_systems stream, and the stream's newest
     file is 017 — but the FILE being newest is not the same as it having been
     APPLIED. `schema_migrations` is the authority.
  2. The exact shape of the four product tables registration writes, and every
     unique constraint on them. Round-3 finding #8 turns on there being no
     attempt identity anywhere in them, and on the existing uniques not
     preventing a replay because the version differs each time.
  3. Whether PSFs is still empty. That is the gate on whether the live mini-
     chain probe can register a REAL reference unit or must fall back to a
     battery-shaped synthetic product.

Writes nothing.
"""

import json

from database.modules.utils.rapid_db_connect import connection

PRODUCT_TABLES = ("refimages", "diffimages", "refimcatalogs", "diffimmeta")


def rows(cur, sql, params=None):
    cur.execute(sql, params or ())
    return cur.fetchall()


def main():
    out = {}
    with connection("rapid-fixd-schema-probe", lane="transaction") as conn:
        with conn.cursor() as cur:
            # The applier owns this table's shape, not this repo, so its
            # columns are read rather than assumed — a first pass guessed
            # `version` and got UndefinedColumn.
            out["schema_migrations_columns"] = [
                list(r) for r in rows(cur,
                    "SELECT column_name, data_type"
                    "  FROM information_schema.columns"
                    " WHERE table_name = 'schema_migrations'"
                    " ORDER BY ordinal_position")]
            out["schema_migrations"] = [
                list(r) for r in rows(cur,
                    "SELECT * FROM schema_migrations"
                    " ORDER BY 1 DESC LIMIT 8")]

            for table in PRODUCT_TABLES:
                out[f"{table}_columns"] = [
                    list(r) for r in rows(cur,
                        "SELECT column_name, data_type, is_nullable"
                        " FROM information_schema.columns"
                        " WHERE table_name = %s ORDER BY ordinal_position",
                        (table,))]

            # Every unique/primary constraint on the tables the version-
            # incrementing procs insert into. The finding's claim is that none
            # of these can stop a replay, because each replay writes a new
            # version and therefore a distinct key.
            out["product_constraints"] = [
                list(r) for r in rows(cur,
                    "SELECT c.conrelid::regclass::text, c.conname,"
                    "       pg_get_constraintdef(c.oid)"
                    "  FROM pg_constraint c"
                    " WHERE c.conrelid::regclass::text = ANY(%s)"
                    "   AND c.contype IN ('u', 'p')"
                    " ORDER BY 1, 2", (list(PRODUCT_TABLES),))]

            # Ownership decides whether migration 018 needs SET ROLE
            # rapid_admin. 006 and 008 both create under that role, unlike the
            # attempts family, which is superuser-owned — 017's first
            # rehearsal cycle failed on exactly this distinction.
            out["table_owners"] = [
                list(r) for r in rows(cur,
                    "SELECT tablename, tableowner FROM pg_tables"
                    " WHERE tablename = ANY(%s)", (list(PRODUCT_TABLES),))]

            out["function_owners"] = [
                list(r) for r in rows(cur,
                    "SELECT p.proname, pg_get_userbyid(p.proowner)"
                    "  FROM pg_proc p"
                    " WHERE p.proname = ANY(%s)"
                    " ORDER BY 1",
                    (["addrefimage", "adddiffimage",
                      "registerrefimcatalog", "registerdiffimmeta"],))]

            for table in ("l2files", "psfs", "refimages", "diffimages",
                          "attempts"):
                try:
                    out[f"count_{table}"] = rows(
                        cur, f"SELECT count(*) FROM {table}")[0][0]
                except Exception as exc:  # noqa: BLE001
                    conn.rollback()
                    out[f"count_{table}"] = f"ERROR: {exc}"

        conn.rollback()

    # The decisive facts FIRST and on one line each. The runner caps remote
    # output (`head -400`), and a pretty-printed column dump pushed exactly the
    # answers this probe exists for past the cap.
    print("== DECISIVE ==")
    print("applied_migrations:",
          json.dumps(out.get("schema_migrations"), default=str))
    print("table_owners:", json.dumps(out.get("table_owners"), default=str))
    print("function_owners:",
          json.dumps(out.get("function_owners"), default=str))
    for constraint in out.get("product_constraints", ()):
        print("constraint:", json.dumps(constraint, default=str))
    for key in sorted(k for k in out if k.startswith("count_")):
        print(f"{key}: {out[key]}")
    for table in PRODUCT_TABLES:
        names = [c[0] for c in out.get(f"{table}_columns", ())]
        print(f"{table}_columns:", ",".join(names))
        print(f"{table}_has_attempt_id:", "attempt_id" in names)
    print("== FULL ==")
    print(json.dumps(out, indent=2, default=str))
    print("FIXD-SCHEMA-PROBE-OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
