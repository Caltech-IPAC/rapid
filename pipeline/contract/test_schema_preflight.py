"""Contract tests: the startup schema preflight (rule 18's preflight clause).

The preflight reads `schema_migrations`, which is populated by the APPLIER and
by nothing else — every migration file's own trailer says so. So the only
honest test of it is against a database an applier has actually run against,
which is what the contract harness builds.

The negative case is produced with a session-local temporary table shadowing
`schema_migrations` in this session's `search_path` — the same technique brief
A used for `workflow_definitions`. The check's own SELECT runs unmodified
against real PostgreSQL and resolves to the empty temp relation for the
duration of the test, so "a database that never had the applier run" is
demonstrated without deleting the rows every other test depends on.
"""

import pytest

from pipeline.contract import fixture
from pipeline.intent.schema_contract import (REQUIRED_MIGRATIONS,
                                             SchemaContractUnmet,
                                             applied_migrations,
                                             verify_schema_contract)


def test_the_preflight_passes_against_the_applied_stream(conn):
    """Every required migration is present on a database built by the harness.

    This is also the assertion that keeps `REQUIRED_MIGRATIONS` honest: a
    migration named there that the stream does not contain fails here, which
    is the only place that typo can be caught.
    """
    verified = verify_schema_contract(fixture.executor(conn))
    assert verified == len(REQUIRED_MIGRATIONS)


def test_the_preflight_fails_closed_naming_every_missing_migration(conn):
    """An empty `schema_migrations` fails closed and names ALL of them.

    Not just the first: an operator fixing a deployment wants the whole list,
    and failing one at a time turns one restart into ten.
    """
    execute = fixture.executor(conn)
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE schema_migrations"
                    " (LIKE public.schema_migrations INCLUDING ALL)")

    try:
        with pytest.raises(SchemaContractUnmet) as caught:
            verify_schema_contract(execute)

        message = str(caught.value)
        assert len(caught.value.missing) == len(REQUIRED_MIGRATIONS)
        for name, _why in REQUIRED_MIGRATIONS:
            assert name in message, f"{name} is not named in the failure"
        # The remedy is named, because a start failure that does not say what
        # to run is a start failure an operator escalates instead of fixing.
        assert "apply-db-migrations.sh" in message
    finally:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE schema_migrations")   # the temp one only
        conn.commit()


def test_a_partial_stream_names_only_what_is_missing(conn):
    """One missing migration is reported alone, with its reason.

    The check is a FLOOR, so a database carrying most of the stream must fail
    on exactly the gap — not on the whole list, which would tell an operator
    nothing about which step was skipped.
    """
    execute = fixture.executor(conn)
    absent = "036-intent-schema-v1.sql"
    present = [name for name, _why in REQUIRED_MIGRATIONS if name != absent]

    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE schema_migrations"
                    " (LIKE public.schema_migrations INCLUDING ALL)")
        for name in present:
            cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s)",
                        [name])

    try:
        with pytest.raises(SchemaContractUnmet) as caught:
            verify_schema_contract(execute)
        assert [name for name, _why in caught.value.missing] == [absent]
        # The reason travels with the name — this is what an operator reads.
        assert "work_units" in str(caught.value)
    finally:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE schema_migrations")
        conn.commit()


def test_a_surplus_migration_is_allowed(conn):
    """Expand/contract: a database AHEAD of this build starts anyway.

    Rule 18 requires old workers' results to stay acceptable during a
    deployment, which means old code MUST start against a newer schema. A
    preflight demanding equality would refuse exactly the deployment step the
    rule mandates, so the surplus is logged and allowed.
    """
    execute = fixture.executor(conn)
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE schema_migrations"
                    " (LIKE public.schema_migrations INCLUDING ALL)")
        for name, _why in REQUIRED_MIGRATIONS:
            cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s)",
                        [name])
        cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s)",
                    ["099-a-migration-from-the-future.sql"])

    try:
        assert verify_schema_contract(execute) == len(REQUIRED_MIGRATIONS)
        assert "099-a-migration-from-the-future.sql" in applied_migrations(
            execute)
    finally:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE schema_migrations")
        conn.commit()
