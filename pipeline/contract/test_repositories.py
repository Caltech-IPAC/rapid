"""Contract tests: the frozen legacy layer and the carved repositories
(brief G, criterion 5).

    5. "`RAPIDDB` with broken config raises (typed) — demonstrably no
        `SystemExit` — and the two carved repositories pass contract tests
        over their real SQL."

THESE NEED NO DRAFT SCHEMA and therefore run in CI as well as on
rapid-admin: they exercise the authoritative stream's own tables
(`DiffImages`, `RefImages`, `Filters`, `sources`) and a constructor that
never reaches a database at all. Rule 17 is a code-structure rule, and its
acceptance should gate every push rather than only the acceptance run.

WHY THE REPOSITORIES ARE TESTED HERE AND NOT IN THE STUB TIER. Their whole
content is SQL — a q3c radial query whose behaviour depends on the extension
and the index, a composed identifier that must survive quoting, an inclusive
range predicate. A stub tier test would assert that a string this module
built equals a string the test also built, which is not a test of anything
the database will do. `stub-blind testing`'s rule applies directly: SQL tests
execute SQL.
"""

import pytest

from pipeline.contract import fixture
from pipeline.repositories.diffimages import DiffImageRepository
from pipeline.repositories.errors import RepositoryQueryFailed
from pipeline.repositories.skycatalogs import SkyCatalogRepository


# ---------------------------------------------------------------------------
# The frozen legacy layer: library code raises, it does not exit.
# ---------------------------------------------------------------------------
def test_rapiddb_with_broken_config_raises_and_does_not_exit(monkeypatch):
    """The rule-17 defect, asserted as absent.

    `RAPIDDB.__init__` called `exit(64)` on a missing environment
    variable — from library code, in a module 25 call sites import,
    taking the whole process with it. `pipeline/stages/alert_production
    .py` records every alert job on the mock's first wave dying at
    exactly that line, because a Batch payload carries no DBSERVER.

    THE ASSERTION IS TWO-SIDED and both halves matter. That a typed error
    is raised is the new contract; that `SystemExit` is NOT raised is the
    old defect's absence — and it needs saying separately, because
    `pytest.raises(DBCredentialError)` alone would also pass if the code
    raised the right error somewhere after having exited, and because
    `SystemExit` inherits from `BaseException` rather than `Exception`,
    so a bare `except Exception` around a call site would never have
    caught it.
    """
    from database.modules.utils.rapid_db import RAPIDDB
    from database.modules.utils.rapid_db_connect import DBCredentialError

    for name in ("DBSERVER", "DBPORT", "DBNAME", "DBUSER", "DBPASS",
                 "RAPID_DB_SECRET_ID"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(DBCredentialError) as caught:
        RAPIDDB()

    # Every missing variable is named at once, not one exit per variable.
    message = str(caught.value)
    for name in ("DBSERVER", "DBPORT", "DBNAME"):
        assert name in message

    # The exit-code contract survives, on the exception, for the
    # entrypoints that legitimately own exiting.
    assert caught.value.exit_code == 64
    assert caught.value.error_category == "config_invalid"


def test_rapiddb_raises_rather_than_exiting_even_under_baseexception(
        monkeypatch):
    """`SystemExit` is not raised — stated as its own assertion.

    Written as an explicit catch of `BaseException` rather than relying
    on the test above, because that is the distinction with operational
    consequences: a caller's `try/except Exception` never saw the old
    `exit(64)` coming.
    """
    from database.modules.utils.rapid_db import RAPIDDB

    for name in ("DBSERVER", "DBPORT", "DBNAME", "DBUSER", "DBPASS",
                 "RAPID_DB_SECRET_ID"):
        monkeypatch.delenv(name, raising=False)

    try:
        RAPIDDB()
    except SystemExit:                            # pragma: no cover
        pytest.fail("RAPIDDB still terminates the process from __init__")
    except BaseException as exc:                  # noqa: BLE001
        assert not isinstance(exc, SystemExit)


def test_rapiddb_is_documented_as_frozen():
    """The freeze note is part of the deliverable, so it is asserted.

    A convention recorded only in a brief is a convention the next author
    never sees. Asserting the docstring is unusual and deliberate: the
    freeze has no runtime expression — nothing prevents a new method —
    so the docstring IS the mechanism, and a test is what keeps it from
    being deleted by a tidy-up that did not know what it was for.
    """
    from database.modules.utils.rapid_db import RAPIDDB

    doc = RAPIDDB.__doc__ or ""
    assert "FROZEN" in doc
    assert "pipeline/repositories" in doc


# ---------------------------------------------------------------------------
# The carved repositories, over their real SQL.
# ---------------------------------------------------------------------------
def test_filters_returns_named_records(conn):
    """`Filters` through the repository, as named rows.

    The reference table may be empty in a freshly built contract
    database; the assertion is about SHAPE, which is what the carve
    changed — a caller reading `row.fid` cannot be broken by a column
    inserted before it, and a caller reading `record[0]` can.
    """
    rows = DiffImageRepository(conn).filters()
    assert isinstance(rows, list)
    for row in rows:
        assert isinstance(row.fid, int)
        assert row.name is not None
        # The named tuple is still a tuple, so the positional callers
        # this carve has not reached keep working.
        assert row[0] == row.fid


def test_possibly_overlapping_executes_the_real_q3c_query(conn):
    """The overlap query runs against the real schema and q3c extension.

    Executing it is the assertion. The query joins two tables, calls
    `q3c_dist` and `q3c_radial_query`, and casts three parameters to
    double precision; a column renamed in the stream, a missing q3c
    extension, or a botched parameter order all fail HERE — which is the
    entire reason this tier exists rather than a stub asserting the SQL
    string.
    """
    rows = DiffImageRepository(conn).possibly_overlapping(
        ppid=1, jd_earliest=0.0, field_ra0=10.0, field_dec0=-20.0,
        radius_deg=0.5)

    assert isinstance(rows, list)
    for row in rows:
        # The 25 named fields line up with the SELECT list, and `dist` —
        # the computed last column — is the one that proves it, because a
        # mis-aligned record would have put a checksum there.
        assert isinstance(row.dist, float)


def test_the_repository_raises_rather_than_returning_none(conn):
    """`RAPIDDB`'s `exit_code = 67` plus bare `return`, as a raise.

    The old shape returned `None` where the caller expected a list, so
    "the query failed" and "nothing matched" were the same value —
    `forcedPhotometryForField.py` then called `len()` on it and got a
    `TypeError` three lines from the actual fault.
    """
    repo = SkyCatalogRepository(conn)
    with pytest.raises(RepositoryQueryFailed) as caught:
        # A field name that is not a valid table suffix: refused by the
        # repository's own validation before it ever reaches the database.
        repo.astroobject_ids("not a valid field name")
    assert caught.value.method


def test_a_failed_query_leaves_the_connection_usable(conn):
    """The rollback after a failed statement, asserted.

    psycopg2 leaves a transaction in a failed state after an error, so
    every later statement raises `InFailedSqlTransaction` — a caller that
    caught the repository's error and tried a different query would get a
    confusing second failure with no relation to the first.
    """
    repo = SkyCatalogRepository(conn)
    with pytest.raises(RepositoryQueryFailed):
        repo.astroobject_ids("no_such_field_%s" % fixture.RUN_TAG)

    # The connection still works: a real query runs on it immediately.
    assert isinstance(repo.source_ids(), list)


def test_fields_reads_the_catalog_not_a_configuration_list(conn):
    """Available fields come from the tables that exist.

    Executes `pg_tables` against the real catalog and strips the prefix,
    exactly as the HATS generator's own loop did.
    """
    fields = SkyCatalogRepository(conn).fields()
    assert isinstance(fields, list)
    for name in fields:
        assert not name.startswith("merges_")


def test_source_ids_and_range_read_the_real_sources_table(conn):
    """The `sources` reads run, and the range predicate is inclusive."""
    repo = SkyCatalogRepository(conn)
    sids = repo.source_ids()
    assert isinstance(sids, list)

    if sids:
        # Inclusive at both ends: the generators compute `end_sid` as the
        # last element of a slice, not one past it, so an exclusive bound
        # would silently drop one row per output file.
        one = repo.sources_in_range(sids[0], sids[0])
        assert len(one) == 1
        assert one[0].sid == sids[0]


def test_a_column_list_is_quoted_not_interpolated(conn):
    """Caller-supplied columns are identifiers, and are treated as such.

    The call site this replaced interpolated a configuration string
    straight into the SELECT list. Here the columns go through
    `sql.Identifier`, and a name that is not a valid identifier is
    refused before composition rather than concatenated into a statement.
    """
    repo = SkyCatalogRepository(conn)
    with pytest.raises(RepositoryQueryFailed):
        repo.source_columns_in_range(["sid; DROP TABLE sources"], 0, 1)

    # A legitimate column list composes and executes.
    rows = repo.source_columns_in_range(["sid"], 0, 0)
    assert isinstance(rows, list)
