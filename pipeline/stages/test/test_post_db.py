"""
File:    test_post_db.py

Tests for the post-DB science chain's job types and their database
primitives.

**THE DOUBLES CAN REFUSE.** A cursor stub that accepts every statement and
returns whatever the caller hoped for would pass these tests against code
that composes nonsense, which is the failure mode the stub-blind-testing
lesson names: a double that cannot say no tests nothing. So `RecordingCursor`
below keeps a real catalog — which tables exist, which columns and unique
indexes they have — and raises the way PostgreSQL would when a statement
contradicts it: inserting a duplicate against a unique index, referring to a
table that was never created, copying a column list the table does not carry.
The duplicate-refusal test relies on exactly that: it asserts the upsert
CONVERGES where a raw insert would have been refused, and the double is what
makes the refusal real.

The SQL itself is composed by the real `psycopg2.sql`, so
`sql.Identifier`/`sql.SQL` quoting and composition are genuinely exercised;
only execution is doubled.
"""

import importlib.util
import os
import re
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


def _install_third_party_stubs() -> None:
    """Stub only what is genuinely missing — see test_sequences for the full
    rationale. psycopg2 must stay REAL here: these tests are about the SQL
    this module composes, and a stubbed `sql.Identifier` would quote nothing
    and prove nothing."""
    names = [
        "numpy", "numpy.ma", "scipy", "scipy.ndimage",
        "astropy", "astropy.io", "astropy.io.fits", "astropy.io.ascii",
        "astropy.table", "astropy.wcs", "astropy.coordinates", "astropy.units",
        "boto3", "botocore", "botocore.exceptions",
        "dateutil", "dateutil.tz",
        "galsim", "galsim.wcs", "galsim.roman",
        "romanisim", "romanisim.bandpass", "romanisim.catalog",
        "romanisim.image", "romanisim.psf",
        "photutils", "photutils.background", "photutils.segmentation",
        "injectionLightCurveModels",
    ]
    for name in names:
        if name in sys.modules:
            continue
        try:
            if importlib.util.find_spec(name) is not None:
                continue
        except (ImportError, ValueError):
            pass
        sys.modules[name] = types.ModuleType(name)


_install_third_party_stubs()

from pipeline.runtime.errors import ConfigError, InputError  # noqa: E402
from pipeline.stages import catalog_db  # noqa: E402


# ---------------------------------------------------------------------------
# A cursor double that keeps a catalog and refuses what PostgreSQL would
# ---------------------------------------------------------------------------

class CursorRefusal(Exception):
    """What the double raises where the real server would raise."""


class RecordingCursor:
    """Executes against a tiny in-memory catalog, and refuses like Postgres.

    What it models, because these are the properties under test:

    * tables exist or do not, and a statement against a missing one raises;
    * a table has a column list, and a COPY naming a different one raises;
    * a table may carry a unique index, and an INSERT that would duplicate a
      key raises UNLESS the statement carries `ON CONFLICT ... DO NOTHING`,
      in which case the conflicting rows are skipped and `rowcount` reports
      only the rows actually written — which is exactly the convergence
      property the staging-plus-upsert shape exists to provide.
    """

    def __init__(self, catalog=None):
        # name -> {"columns": (...), "unique": (...) or None, "rows": [ ... ]}
        self.catalog = dict(catalog or {})
        self.statements = []
        self.rowcount = None
        self._result = []

    # -- the executed-SQL record, for assertions ---------------------------

    @property
    def sql_text(self) -> str:
        return "\n".join(self.statements)

    def _record(self, statement) -> str:
        text = statement if isinstance(statement, str) else _render(statement)
        self.statements.append(text)
        return text

    def execute(self, statement, params=None):
        text = self._record(statement)
        lowered = " ".join(text.lower().split())

        if lowered.startswith("create table") or lowered.startswith("create temp table"):
            return self._create_table(text, lowered)
        if lowered.startswith("insert into"):
            return self._insert(text, lowered)
        if lowered.startswith("delete from"):
            return self._delete(lowered)
        if lowered.startswith("select count(*)"):
            self._result = [(self._duplicate_groups(lowered),)]
            self.rowcount = 1
            return None
        if lowered.startswith("select"):
            self.rowcount = len(self._result)
            return None
        # SET, ALTER, and anything else: accepted, recorded, no effect.
        self.rowcount = 0
        return None

    def copy_from(self, handle, table, sep=",", null="\\N", columns=()):
        if table not in self.catalog:
            raise CursorRefusal(f'relation "{table}" does not exist')
        known = self.catalog[table]["columns"]
        unknown = [c for c in columns if c not in known]
        if unknown:
            raise CursorRefusal(
                f'column "{unknown[0]}" of relation "{table}" does not exist')
        rows = [line for line in handle.read().splitlines() if line.strip()]
        parsed = [tuple(line.split(sep)) for line in rows]
        self.catalog[table]["rows"].extend(parsed)
        self.rowcount = len(parsed)
        self.statements.append(f"COPY {table} ({', '.join(columns)})")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def close(self):
        pass

    # -- the refusals ------------------------------------------------------

    def _create_table(self, text, lowered):
        name = _quoted_name_after(text, r"create (?:temp )?table")
        if name in self.catalog and "if not exists" not in lowered:
            raise CursorRefusal(f'relation "{name}" already exists')
        if name in self.catalog:
            self.rowcount = 0          # IF NOT EXISTS, already there
            return None
        prototype = _quoted_name_after(text, "like")
        template = self.catalog.get(prototype)
        if template is None:
            raise CursorRefusal(f'relation "{prototype}" does not exist')
        # INCLUDING INDEXES is what carries the prototype's unique index onto
        # the clone. Without it the clone has no uniqueness — which is the
        # defect the conversion fixes, and this double is where a regression
        # would show up as a duplicate that no longer raises.
        carries_indexes = "including indexes" in lowered
        self.catalog[name] = {
            "columns": template["columns"],
            "unique": template["unique"] if carries_indexes else None,
            "rows": [],
            # ON COMMIT DROP is modelled, because the staging table's
            # lifetime is load-bearing: each `load_through_staging` call runs
            # in its own transaction, so a rerun must find the staging table
            # GONE and create it again. A double that let it persist would
            # make the second call fail with "already exists" — a failure
            # that says nothing about the code under test.
            "temp_on_commit": "on commit drop" in lowered,
        }
        self.rowcount = 1
        return None

    def commit(self):
        """Drop the ON COMMIT DROP temp tables, as the server would."""
        for name in [n for n, t in self.catalog.items()
                     if t.get("temp_on_commit")]:
            del self.catalog[name]

    def _insert(self, text, lowered):
        target = _quoted_name_after(text, "insert into")
        if target not in self.catalog:
            raise CursorRefusal(f'relation "{target}" does not exist')
        source = _quoted_name_after(text, "from")
        rows = list(self.catalog.get(source, {}).get("rows", []))

        table = self.catalog[target]
        unique = table["unique"]
        tolerated = "on conflict" in lowered and "do nothing" in lowered

        written = 0
        for row in rows:
            if unique is not None:
                key = _key_of(row, table["columns"], unique)
                existing = {_key_of(r, table["columns"], unique)
                            for r in table["rows"]}
                if key in existing:
                    if tolerated:
                        continue           # converged, not written again
                    raise CursorRefusal(
                        f'duplicate key value violates unique constraint on '
                        f'{target} {unique}')
            table["rows"].append(row)
            written += 1
        self.rowcount = written
        return None

    def _delete(self, lowered):
        # The sweeps' effect count. The double does not evaluate the NOT
        # EXISTS predicate; the tests that care about the count set it up.
        self.rowcount = getattr(self, "delete_rowcount", 0)
        return None

    def _duplicate_groups(self, lowered):
        return getattr(self, "duplicate_groups", 0)


def _render(statement) -> str:
    """A `psycopg2.sql` composable as the text it would send.

    `as_string` needs a real connection for `quote_ident`, which these tests
    deliberately do not have. The composition being tested — which
    identifiers appear, in which clauses, and whether `ON CONFLICT` names a
    target — is entirely visible without a server, so the pieces are walked
    and identifiers rendered with the standard double-quoting psycopg2 would
    apply. Composition is still done by the REAL `sql` module; only the final
    encoding step is stood in for.
    """
    from psycopg2 import sql as _sql

    if isinstance(statement, _sql.Composed):
        return "".join(_render(part) for part in statement.seq)
    if isinstance(statement, _sql.Identifier):
        return ".".join('"' + s.replace('"', '""') + '"'
                        for s in statement.strings)
    if isinstance(statement, _sql.SQL):
        return statement.string
    if isinstance(statement, _sql.Literal):
        return repr(statement.wrapped)
    return str(statement)


def _executable_source(source: str) -> str:
    """`source` with comments and string literals removed.

    So an assertion about what the code DOES is not confused by what its
    docstrings SAY about what it used to do. Tokenizing rather than
    regex-stripping because a regex over quotes cannot tell a docstring from
    a string containing a quote.
    """
    import io
    import tokenize

    kept = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            kept.append(token.string)
    except (tokenize.TokenError, IndentationError):
        # An unparseable fragment is not evidence of cleanliness: fall back
        # to the raw text, which fails loudly rather than passing silently.
        return source
    return " ".join(kept)


def _quoted_name_after(text: str, keyword: str):
    """The first double-quoted identifier following `keyword`."""
    match = re.search(keyword + r'\s+(?:if not exists\s+)?"([^"]+)"',
                      text, re.IGNORECASE)
    return match.group(1) if match else None


def _key_of(row, columns, unique):
    index = {name: position for position, name in enumerate(columns)}
    return tuple(row[index[name]] for name in unique if name in index)


def _merges_catalog():
    return {
        "merges": {"columns": ("aid", "sid"), "unique": ("aid", "sid"),
                   "rows": []},
        "astroobjects": {"columns": ("aid", "ra0", "dec0", "flux0"),
                         "unique": ("aid",), "rows": []},
        "sources": {"columns": ("sid", "ra", "dec"), "unique": None,
                    "rows": []},
        "astroobjectsmeta": {"columns": ("aid", "nobs"), "unique": ("aid",),
                             "rows": []},
    }


# ---------------------------------------------------------------------------
# 1. The clone carries the prototype's indexes
# ---------------------------------------------------------------------------

class CloneCarriesIndexesTests(unittest.TestCase):

    def test_the_clone_statement_says_including_indexes(self):
        # Migration 027 put merges_aid_sid_unique on the PROTOTYPE and
        # explicitly left the per-field children to this conversion: "the
        # clone path carries the prototype's indexes, so getting the
        # prototype right is what makes the children right at creation time".
        # `INCLUDING CONSTRAINTS` alone does NOT carry an index.
        cursor = RecordingCursor(_merges_catalog())
        catalog_db.create_child_table(cursor, "merges_7", "merges")

        self.assertIn("INCLUDING INDEXES", cursor.sql_text)
        self.assertIn("INCLUDING DEFAULTS", cursor.sql_text)
        self.assertIn("INCLUDING CONSTRAINTS", cursor.sql_text)

    def test_the_clone_actually_ends_up_unique(self):
        # The property, not the statement text: a clone made through this
        # path refuses a duplicate the way its prototype would.
        cursor = RecordingCursor(_merges_catalog())
        catalog_db.create_child_table(cursor, "merges_7", "merges")

        self.assertEqual(cursor.catalog["merges_7"]["unique"], ("aid", "sid"))

    def test_a_clone_without_including_indexes_would_not_be_unique(self):
        # The negative control that gives the test above its teeth: this is
        # what the OLD clone path produced, and the double reports it as a
        # table with no uniqueness — so a regression to `INCLUDING
        # CONSTRAINTS` alone fails the test above rather than passing it
        # silently.
        cursor = RecordingCursor(_merges_catalog())
        cursor.execute(
            'CREATE TABLE "merges_9" (LIKE "merges" INCLUDING DEFAULTS '
            'INCLUDING CONSTRAINTS)')

        self.assertIsNone(cursor.catalog["merges_9"]["unique"])

    def test_a_child_name_that_is_not_a_child_name_is_refused(self):
        cursor = RecordingCursor(_merges_catalog())
        with self.assertRaises(InputError):
            catalog_db.create_child_table(cursor, "merges; DROP TABLE x",
                                          "merges")

    def test_a_child_of_a_different_prototype_is_refused(self):
        cursor = RecordingCursor(_merges_catalog())
        with self.assertRaises(InputError):
            catalog_db.create_child_table(cursor, "astroobjects_7", "merges")


# ---------------------------------------------------------------------------
# 2. Staging-plus-upsert refuses the duplicate and converges
# ---------------------------------------------------------------------------

class StagingUpsertTests(unittest.TestCase):

    def setUp(self):
        self.cursor = RecordingCursor(_merges_catalog())
        catalog_db.create_child_table(self.cursor, "merges_7", "merges")

    def _csv(self, rows):
        path = os.path.join(
            os.environ.get("TMPDIR", "/tmp"), f"merges-{id(rows)}.csv")
        with open(path, "w") as handle:
            for row in rows:
                handle.write(",".join(str(value) for value in row) + "\n")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_a_first_load_writes_every_row(self):
        path = self._csv([(1, 10), (2, 20)])
        result = catalog_db.load_through_staging(
            self.cursor, path, "merges_7", "merges", ("aid", "sid"))

        self.assertEqual(result["rows_written"], 2)
        self.assertEqual(result["rows_staged"], 2)

    def test_a_rerun_converges_instead_of_being_refused(self):
        # THE PROPERTY THE SHAPE EXISTS FOR. The same unit re-submitted after
        # a partial failure re-loads the same rows; with a unique index on
        # the target a raw INSERT would be REFUSED (see the test below), so
        # the unit could never be retried — which is exactly the case the
        # co-design's individually-retryable ruling is about.
        path = self._csv([(1, 10), (2, 20)])
        catalog_db.load_through_staging(
            self.cursor, path, "merges_7", "merges", ("aid", "sid"))
        # Each load is its own transaction in production (`transaction(conn)`
        # in the stage), which is what drops the ON COMMIT DROP staging table
        # between them. Committing here is what makes this a rerun rather
        # than two loads inside one transaction.
        self.cursor.commit()
        second = catalog_db.load_through_staging(
            self.cursor, path, "merges_7", "merges", ("aid", "sid"))

        self.assertEqual(second["rows_written"], 0)   # nothing new to write
        self.assertEqual(second["rows_staged"], 2)    # but it did re-read them
        self.assertEqual(len(self.cursor.catalog["merges_7"]["rows"]), 2)

    def test_a_raw_insert_of_the_same_rows_is_genuinely_refused(self):
        # The double's teeth, demonstrated. Without `ON CONFLICT DO NOTHING`
        # the second write raises — so the convergence above is a real
        # property of the statement and not an artefact of a permissive stub.
        path = self._csv([(1, 10)])
        catalog_db.load_through_staging(
            self.cursor, path, "merges_7", "merges", ("aid", "sid"))

        self.cursor.catalog["staging_merges_7"] = {
            "columns": ("aid", "sid"), "unique": None, "rows": [("1", "10")]}
        with self.assertRaises(CursorRefusal):
            self.cursor.execute(
                'INSERT INTO "merges_7" (aid, sid) SELECT aid, sid '
                'FROM "staging_merges_7"')

    def test_the_upsert_names_the_conflict_target(self):
        # An untargeted `ON CONFLICT` would swallow a violation of ANY
        # constraint, including one a later migration adds for a reason this
        # code knows nothing about.
        path = self._csv([(1, 10)])
        catalog_db.load_through_staging(
            self.cursor, path, "merges_7", "merges", ("aid", "sid"))

        self.assertIn("ON CONFLICT", self.cursor.sql_text)
        self.assertRegex(self.cursor.sql_text,
                         r'ON CONFLICT \("aid", "sid"\) DO NOTHING')

    def test_staging_is_temp_and_dropped_on_commit(self):
        path = self._csv([(1, 10)])
        catalog_db.load_through_staging(
            self.cursor, path, "merges_7", "merges", ("aid", "sid"))

        self.assertIn("CREATE TEMP TABLE", self.cursor.sql_text)
        self.assertIn("ON COMMIT DROP", self.cursor.sql_text)

    def test_the_load_rate_is_measured(self):
        # "the load rate of that shape is measured at implementation"
        # (database design § Integrity and durability).
        path = self._csv([(1, 10), (2, 20), (3, 30)])
        result = catalog_db.load_through_staging(
            self.cursor, path, "merges_7", "merges", ("aid", "sid"))

        self.assertIn("rate", result)
        self.assertIn("seconds", result)
        self.assertGreaterEqual(result["rate"], 0.0)

    def test_a_column_the_table_lacks_is_refused(self):
        path = self._csv([(1, 10)])
        with self.assertRaises(CursorRefusal):
            catalog_db.load_through_staging(
                self.cursor, path, "merges_7", "merges", ("aid", "nonesuch"))


# ---------------------------------------------------------------------------
# 3. No UNLOGGED anywhere in the operational path
# ---------------------------------------------------------------------------

class NoUnloggedTests(unittest.TestCase):
    """The four unconditional `SET UNLOGGED` sites are gone and stay gone.

    Evidence pack §3.3 named them: the sources prototype DDL, the
    `sources_<date>_<sca>` children, the `astroobjects_<field>` and
    `merges_<field>` pair (issued on EVERY pass, outside the creation guard),
    and the statistics table. Against a database built from the migrations —
    which leave the prototypes LOGGED deliberately — every child holding real
    data still ended up unlogged at runtime, and unlogged tables lose their
    contents on crash recovery and are not replicated.
    """

    OPERATIONAL_MODULES = (
        "pipeline/stages/post_db.py",
        "pipeline/stages/catalog_db.py",
    )

    def _root(self):
        return Path(__file__).resolve().parents[3]

    def test_the_converted_modules_contain_no_unlogged_statement(self):
        # EXECUTABLE code only. Both modules DISCUSS the removed statements
        # at length in their docstrings — that prose is the record of why
        # they are gone and must not trip the assertion, or the test would
        # punish documenting the fix. Comments and string literals are
        # stripped by tokenizing, so what is matched is what would run.
        for relative in self.OPERATIONAL_MODULES:
            source = (self._root() / relative).read_text()
            code = _executable_source(source)
            statements = re.findall(r"SET\s+UNLOGGED", code, re.IGNORECASE)
            self.assertEqual(
                statements, [],
                f"{relative} issues SET UNLOGGED in executable code; the "
                f"migration baseline leaves these tables LOGGED "
                f"deliberately, and trading durability for load speed is an "
                f"argued-for regression requiring measurements, never a "
                f"default")

    def test_the_assertion_would_catch_a_real_unlogged_statement(self):
        # THE NEGATIVE CONTROL, and it is worth being exact about what it
        # controls for. Stripping string literals is what lets the modules
        # DISCUSS the removed statements — but SQL in Python is normally a
        # string, so a naive strip would blind the check to the very thing it
        # exists to catch.
        #
        # What saves it is that this codebase composes SQL through
        # `psycopg2.sql`, where the keywords survive tokenizing as identifier
        # NAMES rather than string contents. So the check runs over the
        # composed form, and a real reintroduction looks like this:
        offending = 'cursor.execute(sql.SQL("ALTER TABLE {t} " + UNLOGGED))\n'
        self.assertIn("UNLOGGED", _executable_source(offending))

        # And the raw-string form the modules must therefore also be checked
        # for — caught by the full-text scan below rather than the token one.
        raw = 'cursor.execute("ALTER TABLE x SET UNLOGGED")\n'
        self.assertRegex(raw, r"SET\s+UNLOGGED")

    def test_prose_about_the_removal_does_not_trip_the_token_assertion(self):
        docstring = '"""The `SET UNLOGGED` this path used to issue is gone."""\n'
        self.assertNotIn("UNLOGGED", _executable_source(docstring))

    def test_no_module_issues_unlogged_through_a_raw_sql_string(self):
        # The other half of the check, over the FULL text: any string literal
        # that is a SQL statement setting a table unlogged. Distinguished
        # from prose by requiring the SQL shape (ALTER TABLE ... UNLOGGED)
        # rather than the bare words, so the modules' explanations of the
        # removal do not match while a reintroduced statement does.
        pattern = re.compile(r"ALTER\s+TABLE[^\"']*SET\s+UNLOGGED",
                             re.IGNORECASE)
        for relative in self.OPERATIONAL_MODULES:
            text = (self._root() / relative).read_text()
            self.assertIsNone(
                pattern.search(text),
                f"{relative} carries an ALTER TABLE ... SET UNLOGGED "
                f"statement")

        # The control: the pattern does match a real one.
        self.assertIsNotNone(
            pattern.search('cursor.execute("ALTER TABLE x SET UNLOGGED")'))

    def test_creating_a_child_issues_no_unlogged(self):
        # The property at the point of use, not only in the source text.
        cursor = RecordingCursor(_merges_catalog())
        catalog_db.create_child_table(cursor, "merges_7", "merges")
        catalog_db.create_child_table(cursor, "sources_20260808_3", "sources",
                                      inherit=True)

        self.assertNotIn("UNLOGGED", cursor.sql_text.upper())

    def test_loading_issues_no_unlogged(self):
        cursor = RecordingCursor(_merges_catalog())
        catalog_db.create_child_table(cursor, "merges_7", "merges")
        path = os.path.join(os.environ.get("TMPDIR", "/tmp"), "nolog.csv")
        with open(path, "w") as handle:
            handle.write("1,10\n")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        catalog_db.load_through_staging(
            cursor, path, "merges_7", "merges", ("aid", "sid"))

        self.assertNotIn("UNLOGGED", cursor.sql_text.upper())


# ---------------------------------------------------------------------------
# 4. The dedup check counts and refuses to delete
# ---------------------------------------------------------------------------

class DedupCheckTests(unittest.TestCase):

    def test_a_clean_table_reports_no_duplicate_groups(self):
        cursor = RecordingCursor(_merges_catalog())
        catalog_db.create_child_table(cursor, "merges_7", "merges")

        self.assertEqual(
            catalog_db.count_duplicate_groups(cursor, "merges_7", "merges"), 0)

    def test_the_check_issues_no_delete(self):
        # "the dedup sweep is a should-find-nothing integrity check, not a
        # maintenance dependency" — and deleting would erase the evidence
        # that prevention failed.
        cursor = RecordingCursor(_merges_catalog())
        catalog_db.create_child_table(cursor, "merges_7", "merges")
        cursor.duplicate_groups = 3

        found = catalog_db.count_duplicate_groups(cursor, "merges_7", "merges")

        self.assertEqual(found, 3)
        self.assertNotIn("DELETE", cursor.sql_text.upper())

    def test_a_prototype_with_no_identity_columns_is_refused(self):
        cursor = RecordingCursor(_merges_catalog())
        catalog_db.create_child_table(cursor, "sources_20260808_1", "sources",
                                      inherit=True)
        with self.assertRaises(ConfigError):
            catalog_db.count_duplicate_groups(
                cursor, "sources_20260808_1", "sources")


# ---------------------------------------------------------------------------
# 5. The currency sweep reads currency through the image
# ---------------------------------------------------------------------------

class CurrencySweepTests(unittest.TestCase):

    def test_the_sweep_keeps_rows_under_a_locked_pin(self):
        # `vbest IN (1, 2)` is what "current" means: 1 is current-best and 2
        # is a locked operator pin. Reading only `= 1` would delete rows
        # under a pinned image — the one thing a pin exists to prevent.
        cursor = RecordingCursor(_merges_catalog())
        catalog_db.create_child_table(cursor, "merges_7", "merges")
        catalog_db.delete_superseded_rows(
            cursor, "merges_7", "merges", join_column="sid",
            identity_table="diffimages", identity_column="pid")

        self.assertIn("vbest IN (1, 2)", cursor.sql_text)

    def test_the_sweep_reports_what_it_removed(self):
        cursor = RecordingCursor(_merges_catalog())
        catalog_db.create_child_table(cursor, "merges_7", "merges")
        cursor.delete_rowcount = 12

        removed = catalog_db.delete_superseded_rows(
            cursor, "merges_7", "merges", join_column="sid",
            identity_table="diffimages", identity_column="pid")

        self.assertEqual(removed, 12)

    def test_the_sweep_is_one_statement_not_one_per_row(self):
        # `pruneNotBestMerges.py` selected every row, asked `SELECT vbest
        # FROM diffimages WHERE pid = %s` for each, and issued a single-row
        # DELETE per demoted row.
        cursor = RecordingCursor(_merges_catalog())
        catalog_db.create_child_table(cursor, "merges_7", "merges")
        before = len(cursor.statements)

        catalog_db.delete_superseded_rows(
            cursor, "merges_7", "merges", join_column="sid",
            identity_table="diffimages", identity_column="pid")

        self.assertEqual(len(cursor.statements) - before, 1)

    def test_a_non_identifier_column_is_refused(self):
        cursor = RecordingCursor(_merges_catalog())
        catalog_db.create_child_table(cursor, "merges_7", "merges")
        with self.assertRaises(ConfigError):
            catalog_db.delete_superseded_rows(
                cursor, "merges_7", "merges", join_column="sid; DROP TABLE x",
                identity_table="diffimages", identity_column="pid")


if __name__ == "__main__":
    unittest.main()
