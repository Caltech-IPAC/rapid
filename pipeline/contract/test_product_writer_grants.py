"""The six product-writer functions keep their EXECUTE grants after a
drop-and-recreate migration, and the product tables keep their INSERT grant.

**THE DEFECT THIS CLOSES.** Registering the acceptance run on 2026-09-11
failed with

    psycopg2.errors.InsufficientPrivilege: permission denied for table
    diffimages
    CONTEXT: SQL statement "insert into DiffImages (...)"

on a login that provably held the privilege: measured against the live
database, `has_table_privilege('rapid_admin', 'diffimages', 'INSERT')` is
true, `rapid_orchestrator` inherits the same through
`rapid_pipeline_write`, and the six writer functions are NOT `SECURITY
DEFINER`, so they execute with the caller's privileges. The widening also
holds: inside `submission_role` the session's `current_user` is
`rapid_admin` with `INSERT=True`, verified live 2026-09-12.

The explanation is the migration stream's own timing.
`115-product-writers-run-id.sql` DROPS and recreates all six product
writers to thread `run_id`, and **a DROP takes the function's EXECUTE
grants with it** -- 115's own header documents this as an observed live
defect, citing 018's drop-and-create silently leaving
`addrefimage`/`adddiffimage` with a NULL `proacl` until 043 repaired them.
115 was applied at 2026-09-11 20:47 UTC; the failing registration ran
before that, against functions whose grants were mid-flight. 115's
explicitly reissued grants closed it.

So there is no missing grant to add and no role to widen: the cause is
understood and already repaired by a migration in the authoritative
stream. What was missing is a test that would have caught it, and would
catch the next drop-and-recreate that forgets to reissue -- which is the
third time this same defect shape has appeared (018, and now 115's
window). That test is this module.

**WHY IT ASSERTS THE GRANT AND NOT A REGISTRATION.** A test that registered
an attempt would write product rows, which a contract-tier test must not do
outside its own fixture rows -- and it would pass or fail for any of a dozen
reasons besides privilege. The privilege is the invariant that broke, so the
privilege is what is asserted, by the same `has_*_privilege` reads used to
diagnose it.

Migrations 101-105 are part of the floor this module judges against. They
were reserved for a time and the reserve was retired on 2026-09-12: `smdc`'s
`RAPIDDB.add_l2file_{fourth,fifth}_order` bind `overlapfields_` by name, and
that parameter exists only once 102 has landed, so a database missing the
range cannot register an L2 file from this branch at all. Asserting their
PRESENCE is therefore the same kind of floor assertion as 115-118 below:
it names the schema a run from this code requires.
"""

import pytest

from pipeline.contract import fixture

#: The role the pipeline's product writers run as. `rapid_admin` and
#: `rapid_orchestrator` both hold it, and it is the role 115 reissues the
#: six EXECUTE grants to.
WRITER_ROLE = "rapid_pipeline_write"

#: The three product tables the writers insert into.
PRODUCT_TABLES = ("diffimages", "refimages", "psfs")

#: The six functions `115-product-writers-run-id.sql` drops and recreates.
#: Named without argument lists: the point is that SOME overload of each
#: name is executable by the writer role, and pinning the exact signature
#: here would make this test fail on the next legitimate signature change
#: rather than on the grant regression it exists to catch.
WRITER_FUNCTIONS = ("addrefimage", "adddiffimage", "addpsf",
                    "updaterefimage", "updatediffimage", "updatepsf")


@pytest.fixture
def conn():
    """A connection of this module's own, NOT the shared `conn` fixture.

    The session-scoped fixture in `conftest.py` calls
    `fixture.ensure_definition`, which INSERTs into `workflow_definitions`
    -- a write this module neither needs nor is allowed to make: every
    assertion here is a `has_*_privilege` or `pg_proc`/`schema_migrations`
    read, and the operate-tier login does not hold INSERT on that table
    (`permission denied for table workflow_definitions`). Depending on a
    fixture that writes would make a read-only privilege test fail for
    want of a privilege it is not testing.

    Rolled back and closed on the way out, so the module leaves no
    transaction and no connection behind.
    """
    own = fixture.connect()
    own.autocommit = False
    try:
        yield own
    finally:
        try:
            own.rollback()
        except Exception:                          # noqa: BLE001
            pass
        own.close()


@pytest.fixture
def target_named(conn):
    """Print WHERE this ran, so a failure names the database it judged."""
    with conn.cursor() as cur:
        cur.execute("SELECT current_database(), inet_server_addr()")
        database, address = cur.fetchone()
    # THE TARGET DICT IS NOT PRINTED. `fixture.connection_target()` returns
    # the libpq settings INCLUDING the password, so printing it puts a live
    # credential in every CI log and transcript that runs this module. Only
    # the two facts a failure needs are printed -- which database, which
    # server address -- read back from the connection itself rather than
    # from the settings that opened it, which is the better evidence anyway.
    #
    # Recorded as proposed work, not fixed here: `connection_target()`'s
    # own repr should redact the password, since any test printing it has
    # the same problem (observed 2026-09-12 while adding this module).
    print("contract target: db=%s addr=%s" % (database, address))
    return database, address


def test_the_writer_role_can_insert_into_every_product_table(conn,
                                                             target_named):
    """The privilege whose absence produced `permission denied for table
    diffimages`. Asserted for all three product tables, not just the one
    that happened to fail first: 115 recreated the writers for all three.
    """
    missing = []
    for table in PRODUCT_TABLES:
        with conn.cursor() as cur:
            cur.execute("SELECT has_table_privilege(%s, %s, 'INSERT')",
                        (WRITER_ROLE, table))
            if not cur.fetchone()[0]:
                missing.append(table)
    assert not missing, (
        "%s cannot INSERT into %s -- a product registration will fail with "
        "`permission denied for table <name>` exactly as the acceptance run "
        "did on 2026-09-11" % (WRITER_ROLE, ", ".join(missing)))


def test_every_product_writer_is_executable_by_the_writer_role(conn,
                                                               target_named):
    """THE REGRESSION GUARD. A DROP takes EXECUTE grants with it, so any
    future migration that drops and recreates one of these six without
    reissuing its grant fails here rather than in a production
    registration.

    `proacl IS NULL` is deliberately treated as a FAILURE, not a pass. A
    NULL ACL means default privileges -- EXECUTE TO PUBLIC -- which is
    what 018's drop left behind and what 043 had to repair: the writer
    role can execute the function, so a naive privilege check passes,
    while the grant the migration stream is supposed to guarantee is
    simply gone. The next DROP then has nothing to carry forward and the
    failure surfaces somewhere else entirely.
    """
    ungranted = []
    for name in WRITER_FUNCTIONS:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT p.oid::regprocedure::text, p.proacl IS NULL,"
                "       has_function_privilege(%s, p.oid, 'EXECUTE')"
                "  FROM pg_proc p"
                "  JOIN pg_namespace n ON n.oid = p.pronamespace"
                " WHERE lower(p.proname) = %s"
                "   AND n.nspname = 'public'",
                (WRITER_ROLE, name))
            overloads = cur.fetchall()
        assert overloads, (
            "%s does not exist in the public schema; migration 115 creates "
            "it" % name)
        # At least one overload must carry an EXPLICIT grant to the writer
        # role. An overload with a NULL acl does not count, per the
        # docstring.
        if not any(granted and not acl_is_null
                   for _signature, acl_is_null, granted in overloads):
            ungranted.append(
                "%s (%s)" % (name, "; ".join(
                    "%s: acl_null=%s execute=%s" % (sig, null, ex)
                    for sig, null, ex in overloads)))
    assert not ungranted, (
        "no overload of these functions carries an explicit EXECUTE grant "
        "to %s: %s -- a drop-and-recreate migration dropped the grant "
        "without reissuing it, which is 115's own documented hazard "
        "(observed live after 018, repaired by 043)"
        % (WRITER_ROLE, ", ".join(ungranted)))


def test_the_product_writers_run_with_the_callers_privileges(conn,
                                                             target_named):
    """None of the six is `SECURITY DEFINER`, which is WHY the caller's
    grant is what matters and this module asserts grants at all.

    If one were `SECURITY DEFINER` it would insert with its owner's
    privileges and the caller's grant would be irrelevant -- so this
    assertion is the premise the other two rest on, not a separate policy
    preference. It is also a real hazard to pin: making a product writer
    `SECURITY DEFINER` would silently let any role that can execute it
    write product rows.
    """
    definers = []
    for name in WRITER_FUNCTIONS:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT p.oid::regprocedure::text"
                "  FROM pg_proc p"
                "  JOIN pg_namespace n ON n.oid = p.pronamespace"
                " WHERE lower(p.proname) = %s AND n.nspname = 'public'"
                "   AND p.prosecdef",
                (name,))
            definers.extend(row[0] for row in cur.fetchall())
    assert not definers, (
        "these product writers are SECURITY DEFINER and would insert with "
        "their owner's privileges regardless of the caller's grant: %s"
        % ", ".join(definers))


def test_the_schema_floor_this_module_judges_against_is_applied(conn,
                                                                target_named):
    """The schema floor this module judges against, asserted rather than
    assumed -- 101-105 and 115-118 both.

    115 is what threads `run_id` through the writers and reissues their
    grants, so every assertion above is only meaningful on a database
    where it has landed.

    101-105 add `l2files.overlapfields` and make it a written column, and
    they are a floor for the same reason: `RAPIDDB.add_l2file_fourth_order`
    and `..._fifth_order` on this branch bind `overlapfields_` BY NAME, so
    against a database missing 102 every L2 registration from this code
    raises `function ... does not exist`. They were reserved outside an
    earlier campaign and that reserve was retired on 2026-09-12 (project
    ruling) precisely because the consumer had moved: asserting their
    absence would now pin the database to a schema this branch cannot
    register against.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM public.schema_migrations")
        applied = {row[0] for row in cur.fetchall()}

    floor = [f for f in applied if f.startswith(("115-", "116-", "117-",
                                                 "118-"))]
    assert len(floor) == 4, (
        "migrations 115-118 must all be applied; found %s" % sorted(floor))

    overlapfields = sorted(f for f in applied
                           if f.startswith(("101-", "102-", "103-", "104-",
                                            "105-")))
    assert len(overlapfields) == 5, (
        "migrations 101-105 add and populate l2files.overlapfields, which "
        "this branch's add_l2file_* bind by name; all five must be applied, "
        "found %s" % overlapfields)
