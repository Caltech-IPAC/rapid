"""Stub-tier tests for the `run` command group (migrations 108/109).

Three properties this file exists to pin, each a regression guard named in
the task ruling:

  * THE FAILURE PREDICATE. `run status` must count as a failure both a
    TERMINAL attempt with a non-success outcome (NULL included) and a
    `missing_or_contradictory` row -- a prior defect used `rapid_outcome
    <> 'success'` alone and reported a clean pass over 218 dead letters
    whose `rapid_outcome` was NULL (never started at all, so `<>` against
    NULL is NULL, neither true nor false, and the row vanished from both
    sides of the count). Tested here directly against
    `actions._RUN_FAILURE_PREDICATE`'s SQL text evaluated in SQLite (no
    live Postgres in the stub tier), over scripted rows including a NULL
    outcome and a `missing_or_contradictory` row -- a real predicate
    evaluation, not a string match on the SQL.
  * PREFIX MATCHING, NEVER EQUALITY. An attempt with `run_id = '<name>-3'`
    (a split-pass batch suffix) must be counted for `<name>` — matching by
    equality was the defect 108's own COMMENT ON TABLE names as "the ninth
    defect of the 8/21 rerun".
  * `run archive` without `--apply` performs NO write and its rendered
    output states nothing was changed, via `render_plan` — not a
    hand-rolled string.
  * `run create` passes the idempotency key as the FIRST positional
    argument to `derived.create_run`, matching 109's `p_idempotency_key`
    being the function's first parameter.

Follows `test_batch.py`'s pattern throughout: `_FakeConn`/`_FakeCursor`
script jsonb-shaped return values and record every `(sql, params)` call so
ordering and parameter content can both be asserted, and the `boto3`/
`psycopg2` stub-injection preamble is copied verbatim (this module's import
chain reaches `pipeline.operatorctl.contract.call_function`, which needs
`psycopg2.Error` to exist as an exception class at call time).
"""

import argparse
import io
import re
import sqlite3
import sys
import types
import unittest
from unittest import mock

if "boto3" not in sys.modules:
    try:
        import boto3  # noqa: F401
    except ImportError:
        sys.modules["boto3"] = types.ModuleType("boto3")

if "psycopg2" not in sys.modules:
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        stub = types.ModuleType("psycopg2")
        stub.Error = type("Error", (Exception,), {})
        sys.modules["psycopg2"] = stub

from pipeline.operatorctl import actions
from pipeline.operatorctl import main as operatorctl_main
from pipeline.operatorctl.contract import render_plan


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self._conn.calls.append((" ".join(sql.split()), params))
        script = self._conn.script
        if not script:
            raise AssertionError(
                "no more scripted responses; unexpected statement: %s" % sql)
        self._result = script.pop(0)

    def fetchone(self):
        return (self._result,)

    @property
    def description(self):
        return [("result",)]


class _FakeConn:
    """`script` is a list of jsonb-shaped return values, one per statement
    the module issues, in order — identical shape to `test_batch.py`'s
    `_FakeConn`.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.committed = 0
        self.rolled_back = 0

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


# ---------------------------------------------------------------------------
# The failure predicate, evaluated for real over scripted rows.
# ---------------------------------------------------------------------------
# SQLite stands in for Postgres here ONLY to evaluate a boolean expression
# over a table of rows — `IS DISTINCT FROM` is valid SQLite syntax (3.39+)
# and means exactly the same thing there as in Postgres: unlike `<>`, it
# treats NULL as an ordinary comparable value rather than propagating NULL
# through the comparison. This is a real evaluation of the SQL text
# `actions.py` ships, not a Python reimplementation of the predicate that
# could silently drift from what is actually sent to the database.
#
# `_RUN_FAILURE_PREDICATE`'s `terminal%%` is psycopg2-escaped (doubled,
# because the ONLY place this constant is used in production splices it
# into a query executed WITH a parameter, where psycopg2 treats a bare `%`
# as the start of a placeholder). SQLite has no such convention -- it would
# read `%%` as two literal percent characters and match nothing -- so the
# doubling is undone here before the text reaches SQLite. This keeps the
# test exercising the real LIKE semantics (`terminal%` as "any suffix")
# rather than silently drifting to a different, SQLite-only meaning.
def _count_failures(rows):
    """`rows` is a list of (lifecycle_state, rapid_outcome) pairs. Returns
    how many the real predicate text in `actions._RUN_FAILURE_PREDICATE`
    counts as failures.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE attempts (lifecycle_state TEXT, rapid_outcome TEXT)")
    conn.executemany("INSERT INTO attempts VALUES (?, ?)", rows)
    predicate = actions._RUN_FAILURE_PREDICATE.replace("%%", "%")
    sql = "SELECT count(*) FROM attempts WHERE " + predicate
    return conn.execute(sql).fetchone()[0]


class FailurePredicateTests(unittest.TestCase):
    """THE REGRESSION GUARD for the sci-c defect: a NULL rapid_outcome on a
    terminal row, and a missing_or_contradictory row, both count.
    """

    def test_a_null_outcome_terminal_attempt_counts_as_a_failure(self):
        # This is the exact sci-c shape: 218 attempts dead-lettered with
        # started_at IS NULL, rapid_outcome never set (still NULL) because
        # the container exited before start_attempt. lifecycle_state here
        # is a stand-in "terminal_after_start"-shaped value (LIKE
        # 'terminal%').
        rows = [("terminal_after_start", None)]
        self.assertEqual(_count_failures(rows), 1, (
            "a terminal attempt with rapid_outcome IS NULL must count as "
            "a failure -- `rapid_outcome <> 'success'` alone evaluates to "
            "NULL here and silently drops the row, which is the exact "
            "defect that reported a clean pass over 218 dead letters"))

    def test_a_missing_or_contradictory_row_counts_as_a_failure(self):
        # NOT itself a `terminal%` lifecycle_state (see live_w9_ramp's own
        # `_dead_lettered_pairs` comment) -- must be caught by the SECOND
        # disjunct, not the first.
        rows = [("missing_or_contradictory", None)]
        self.assertEqual(_count_failures(rows), 1)

    def test_a_successful_terminal_attempt_does_not_count(self):
        rows = [("terminal_after_start", "success")]
        self.assertEqual(_count_failures(rows), 0)

    def test_a_non_terminal_attempt_with_null_outcome_does_not_count(self):
        # In flight, not dead -- e.g. lifecycle_state = 'submitted'. Must
        # not be swept in just because rapid_outcome is NULL.
        rows = [("submitted", None)]
        self.assertEqual(_count_failures(rows), 0)

    def test_mixed_population_counts_exactly_the_failing_rows(self):
        rows = [
            ("terminal_after_start", "success"),      # not a failure
            ("terminal_after_start", None),            # failure (NULL)
            ("terminal_after_start", "failure"),        # failure (explicit)
            ("missing_or_contradictory", None),         # failure
            ("submitted", None),                        # not a failure
        ]
        self.assertEqual(_count_failures(rows), 3)


# ---------------------------------------------------------------------------
# THE psycopg2-ESCAPING REGRESSION GUARD.
# ---------------------------------------------------------------------------
# The sci-c `run status` defect: `_RUN_FAILURE_PREDICATE` embedded the SQL
# literal `terminal%`, spliced into `_RUN_ATTEMPT_TALLY`, which IS executed
# with a parameter (`run_id LIKE %s`). psycopg2 scans the ENTIRE query
# string for `%`-placeholders whenever any parameters are supplied at all --
# not just inside the part the caller thinks of as "the placeholder" -- so
# the bare `%` in `terminal%` was read as the start of a second placeholder.
# `run_attempt_tally` passes exactly one parameter, so psycopg2's internal
# substitution over a 2-placeholder-shaped query against a 1-tuple raises
# `IndexError: tuple index out of range` from inside `cur.execute` -- not a
# SQL syntax error, so it does not look like a query-text bug at the call
# site, and the SQLite-based `FailurePredicateTests` above cannot see it at
# all: SQLite has no `%`-placeholder convention, so a stray `%` is just a
# LIKE wildcard there regardless of how many parameters are bound.
#
# Two layers, per the task's own menu:
#
#   * `PsycopgEscapingTextInvariantTests` -- a cheap, honest text-level
#     check: every literal `%` in a query constant that is executed WITH
#     parameters must be doubled. This is the same rule a human reviewer
#     would apply, made mechanical.
#   * `PsycopgPlaceholderCountingCursorTests` -- exercises the real code
#     path (`run_attempt_tally` -> `_rows` -> `cur.execute`) through a fake
#     cursor that replicates psycopg2's OWN placeholder-counting contract
#     (documented and verified against the installed psycopg2 2.9.12: `%%`
#     is a literal percent, every other `%` starts a placeholder, and
#     `execute` raises `IndexError` when the placeholder count and the
#     parameter count disagree) rather than psycopg2's stub in this test
#     module's preamble, which only records `(sql, params)` and does no
#     substitution at all -- that stub is what let this bug ship covered
#     by tests in the first place, since every OTHER test in this file
#     that calls `run_attempt_tally` etc. goes through `_FakeCursor` too.
class _RealPsycopg2SubstitutionCursor:
    """A cursor whose `execute` replicates psycopg2's actual `%`-handling,
    not a permissive stub. Built directly from psycopg2's documented
    contract (`%s` positional placeholders, `%%` an escaped literal
    percent) and confirmed against the installed psycopg2 2.9.12: any
    unescaped `%` that is not part of a `%s` token is what the C extension
    treats as a second placeholder, and a params tuple shorter than the
    placeholder count raises `IndexError`, not a SQL-syntax error --
    exactly the live traceback this test is guarding against.

    Matches `actions._rows`'s real contract (columns from `description`,
    rows from `fetchall()` as tuples) -- the same shape `_RowsFakeCursor`
    below uses, duplicated here rather than forward-referenced since this
    class is defined earlier in the file.
    """

    _TOKEN_RE = re.compile(r"%%|%s|%")

    def __init__(self, conn, columns, rows):
        self._conn = conn
        self._columns = columns
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        params = () if params is None else params
        placeholders = 0
        for token in self._TOKEN_RE.findall(sql):
            if token == "%%":
                continue  # escaped literal percent -- not a placeholder
            placeholders += 1  # "%s", or a bare stray "%" (the bug)
        # This is the exact mechanism of the live failure: psycopg2 walks
        # the placeholder positions against `params` by index, and an
        # extra placeholder (from a stray `%`) makes it read past the end
        # of the tuple.
        if placeholders != len(params):
            raise IndexError("tuple index out of range")
        self._conn.calls.append((" ".join(sql.split()), params))

    @property
    def description(self):
        return [(c,) for c in self._columns]

    def fetchall(self):
        return self._rows


class _RealPsycopg2SubstitutionConn:
    """`columns`/`rows` describe the single scripted result `run_attempt_
    tally` reads back through `_rows`; `calls` records every
    `(sql, params)` pair `execute` saw, matching `_FakeConn`'s own
    `calls` convention.
    """

    def __init__(self, columns, rows):
        self._columns = columns
        self._rows = rows
        self.calls = []

    def cursor(self):
        return _RealPsycopg2SubstitutionCursor(self, self._columns,
                                               self._rows)


class PsycopgEscapingTextInvariantTests(unittest.TestCase):
    """Every literal `%` in a query constant executed WITH parameters must
    be doubled -- the cheap, mechanical form of the same rule.
    """

    def test_run_failure_predicate_has_no_bare_percent(self):
        # The regression itself: `terminal%` (one percent) is what broke
        # `run_attempt_tally`; `terminal%%` is correct.
        self.assertIn("terminal%%", actions._RUN_FAILURE_PREDICATE)
        self.assertNotRegex(
            actions._RUN_FAILURE_PREDICATE, r"(?<!%)%(?!%)",
            "a bare (undoubled) literal percent here collides with "
            "psycopg2's own %s-placeholder scanning the moment this text "
            "is spliced into a query executed with parameters, exactly "
            "as _RUN_ATTEMPT_TALLY is")

    def test_run_attempt_tally_has_exactly_one_placeholder(self):
        # `run_attempt_tally` passes exactly one parameter
        # (`_run_prefix_pattern(name)`); the query text must ask for
        # exactly one, counting %%-escaped percents as non-placeholders.
        tokens = _RealPsycopg2SubstitutionCursor._TOKEN_RE.findall(
            actions._RUN_ATTEMPT_TALLY)
        placeholder_count = sum(1 for t in tokens if t != "%%")
        self.assertEqual(placeholder_count, 1, (
            "_RUN_ATTEMPT_TALLY must contain exactly one psycopg2 "
            "placeholder -- a stray unescaped literal % (from the "
            "embedded _RUN_FAILURE_PREDICATE) would raise IndexError "
            "against the single parameter run_attempt_tally actually "
            "passes"))


class PsycopgPlaceholderCountingCursorTests(unittest.TestCase):
    """`run_attempt_tally` through a cursor that actually counts
    placeholders the way psycopg2 does -- this is the test that fails
    against the unescaped predicate and passes against the fix.
    """

    def test_run_attempt_tally_does_not_raise_indexerror(self):
        conn = _RealPsycopg2SubstitutionConn(
            columns=["total", "failures"], rows=[(5, 2)])
        result = actions.run_attempt_tally(conn, "w9-ramp-science-18")
        self.assertEqual(result, {"total": 5, "failures": 2})
        sql, params = conn.calls[0]
        self.assertEqual(params, ("w9-ramp-science-18%",))


# ---------------------------------------------------------------------------
# Prefix matching, never equality.
# ---------------------------------------------------------------------------
class PrefixMatchingTests(unittest.TestCase):
    """A split-pass batch's `<name>-<n>` suffix must be counted for `name`.

    THE WILDCARD LIVES ON THE PARAMETER, NOT IN THE SQL TEXT — `actions.
    _run_prefix_pattern(name)` returns `name + "%"`, and every query here is
    a plain `run_id LIKE %s`. This is deliberate: splicing a literal `%`
    into the SQL string next to psycopg2's own `%s` placeholder syntax is
    exactly the kind of thing that is easy to get subtly wrong (`%%`
    doubling), so the wildcard is kept out of the SQL text entirely,
    matching `pipeline.registration.consumer.candidates`'s own convention
    for its `run_id_prefix` parameter.
    """

    def test_run_attempt_tally_sql_matches_by_like_not_equality(self):
        # Asserted on the SQL TEXT actions.py actually sends, not on a
        # mocked return value -- the property under test is which operator
        # reaches the database, and a query built with `=` would still let
        # a test pass if the fixture data happened not to exercise a
        # suffixed run_id.
        self.assertIn("run_id LIKE %s", actions._RUN_ATTEMPT_TALLY)
        self.assertNotIn("run_id = %s", actions._RUN_ATTEMPT_TALLY)

    def test_run_prefix_pattern_appends_the_wildcard_to_the_parameter(self):
        self.assertEqual(actions._run_prefix_pattern("w9-ramp-science-18-x"),
                         "w9-ramp-science-18-x%")

    def test_a_split_batch_suffix_is_counted_for_its_run_via_like(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE attempts (run_id TEXT)")
        conn.executemany("INSERT INTO attempts VALUES (?)", [
            ("w9-ramp-science-18-x",),
            ("w9-ramp-science-18-x-0",),   # split-batch suffix
            ("w9-ramp-science-18-x-1",),   # split-batch suffix
            ("some-other-run",),
        ])
        # The SAME pattern actions.run_attempt_tally builds, evaluated for
        # real against a LIKE.
        pattern = actions._run_prefix_pattern("w9-ramp-science-18-x")
        count = conn.execute(
            "SELECT count(*) FROM attempts WHERE run_id LIKE ?",
            (pattern,)).fetchone()[0]
        self.assertEqual(count, 3, (
            "all three attempts under the run's prefix must be counted, "
            "including the two split-batch suffixes -- matching by `=` "
            "would find only the first"))

    def test_run_stage_walltime_sql_also_matches_by_like_not_equality(self):
        self.assertIn("run_id LIKE %s", actions._RUN_STAGE_WALLTIME)
        self.assertNotIn("run_id = %s", actions._RUN_STAGE_WALLTIME)

    def test_run_product_counts_sql_also_matches_by_like_not_equality(self):
        self.assertIn("run_id LIKE %s", actions._RUN_PRODUCT_COUNTS)
        self.assertNotIn("run_id = %s", actions._RUN_PRODUCT_COUNTS)

    def test_release_dead_letter_candidates_sql_matches_by_like_not_equality(
            self):
        from pipeline.operatorctl.run import _RELEASE_CANDIDATES_SQL
        self.assertIn("run_id LIKE %s", _RELEASE_CANDIDATES_SQL)
        self.assertNotIn("run_id = %s", _RELEASE_CANDIDATES_SQL)

    def test_run_resource_usage_sql_also_matches_by_like_not_equality(self):
        # D7: the two run_id predicates (peak_rss_kb half, cpu_seconds half
        # of the UNION ALL) must both be LIKE, not equality.
        self.assertEqual(
            actions._RUN_RESOURCE_USAGE.count("run_id LIKE %s"), 2)
        self.assertNotIn("run_id = %s", actions._RUN_RESOURCE_USAGE)


# ---------------------------------------------------------------------------
# D7: per-job resource usage (peak RSS, CPU seconds) joins the walltime
# panel, read from `attempts` alone (a per-attempt measurement, unlike
# per-stage walltime).
# ---------------------------------------------------------------------------
class _RowsFakeCursor:
    """Matches `actions._rows`'s real contract: columns come from
    `description`, rows from `fetchall()` as plain tuples zipped against
    them -- the shape `_FakeCursor` above (built for single jsonb-result
    keyed calls) does not support.
    """

    def __init__(self, columns, rows):
        self._columns = columns
        self._rows = rows
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    @property
    def description(self):
        return [(c,) for c in self._columns]

    def fetchall(self):
        return self._rows


class _RowsFakeConn:
    def __init__(self, columns, rows):
        self._cursor = _RowsFakeCursor(columns, rows)

    def cursor(self):
        return self._cursor

    @property
    def calls(self):
        return self._cursor.calls


class ResourceUsageTests(unittest.TestCase):
    """`run_resource_usage` reads the D7 columns' distribution, keyed by
    metric name so the caller can print `peak_rss_kb` and `cpu_seconds`
    each as one row of the same shape `run_stage_walltime` already uses.
    """

    def test_reads_both_metrics_with_prefix_matching(self):
        conn = _RowsFakeConn(
            columns=["metric", "n", "min_v", "p50_v", "p90_v", "max_v"],
            rows=[
                ("peak_rss_kb", 3, 100_000, 150_000, 190_000, 200_000),
                ("cpu_seconds", 3, 10.0, 15.0, 19.0, 20.0),
            ])
        rows = actions.run_resource_usage(conn, "w9-ramp-science-18-x")
        sql, params = conn.calls[0]
        self.assertIn("run_id LIKE %s", sql)
        self.assertEqual(params, ("w9-ramp-science-18-x%",
                                  "w9-ramp-science-18-x%"))
        self.assertEqual([row["metric"] for row in rows],
                         ["peak_rss_kb", "cpu_seconds"])
        self.assertEqual(rows[0]["max_v"], 200_000)
        self.assertEqual(rows[1]["max_v"], 20.0)

    def test_a_run_with_no_measured_attempts_reports_zero_n(self):
        # Every attempt in the run predates the columns, or every rusage
        # read failed -- n=0 for both metrics, not an empty result set (the
        # UNION ALL of two aggregates always returns exactly two rows).
        conn = _RowsFakeConn(
            columns=["metric", "n", "min_v", "p50_v", "p90_v", "max_v"],
            rows=[
                ("peak_rss_kb", 0, None, None, None, None),
                ("cpu_seconds", 0, None, None, None, None),
            ])
        rows = actions.run_resource_usage(conn, "some-run")
        self.assertEqual(rows[0]["n"], 0)
        self.assertEqual(rows[1]["n"], 0)


_UNSET = object()


# ---------------------------------------------------------------------------
# `run status`'s printed panel: the walltime rows (from `attempt_stages`,
# one row per stage that actually ran) and the resource-usage rows (from
# `attempts` itself, read once from rusage at terminal) are two different
# measurements at two different grains, and must not be gated on each
# other. A run whose attempts died before any stage completed has no
# walltime rows and can still have good rusage for every attempt; before
# the fix, the resource-usage loop was nested inside `if walltime:` and
# that run printed no panel at all -- not "n=0", not an empty heading, no
# line, as if the columns had never been populated.
# ---------------------------------------------------------------------------
class _StatusFakeConn:
    """`_cmd_run_status` never touches the connection directly -- every
    read goes through `actions`, which this test class patches. The conn
    object itself only needs to exist to be passed through.
    """


def _run_status_out(name="some-run", walltime=(), resource_usage=(),
                    name_positional=_UNSET, name_flag=None):
    """Run `_cmd_run_status` with `actions.run_row`/`run_attempt_tally`/
    `run_state_breakdown`/`run_stage_walltime`/`run_resource_usage` all
    patched to fixed, scripted values, and return what it printed.

    `run_row`, the tally, and the breakdown are held constant across every
    test in this class -- only `walltime` and `resource_usage`, the two
    panels under test, vary per call.

    By default `name` is passed as the positional (the current preferred
    form); `name_positional`/`name_flag` let ArgumentResolutionTests drive
    the positional and `--name` independently, including leaving the
    positional unset (`None`, argparse's own default when it is omitted).
    """
    if name_positional is _UNSET:
        name_positional = name
    run = {"name": name, "run_id": "rid-1", "kind": "science",
           "state": "running", "owner": "sci-c", "purpose": "test",
           "branch": "main", "created_at": "2026-09-11T00:00:00Z"}
    tally = {"total": 5, "failures": 0}
    breakdown = []
    args = argparse.Namespace(name_positional=name_positional,
                              name=name_flag, placement=False, queue=None,
                              region=None, profile=None)
    out = io.StringIO()
    with mock.patch.object(actions, "run_row", return_value=run), \
         mock.patch.object(actions, "run_attempt_tally", return_value=tally), \
         mock.patch.object(actions, "run_state_breakdown",
                           return_value=breakdown), \
         mock.patch.object(actions, "run_stage_walltime",
                           return_value=list(walltime)), \
         mock.patch.object(actions, "run_resource_usage",
                           return_value=list(resource_usage)):
        rc = operatorctl_main._cmd_run_status(_StatusFakeConn(), args, out)
    return rc, out.getvalue()


class RunStatusResourceUsagePanelTests(unittest.TestCase):
    _WALLTIME_HEADING = "walltime by stage (ms; min/p50/p90/max, n):"

    def test_empty_walltime_with_populated_resource_usage_still_prints(self):
        # The regression: attempts died before any stage completed, so
        # `run_stage_walltime` returns nothing, but rusage was captured
        # for every attempt. Before the fix this printed no heading and
        # no rusage lines at all.
        rc, output = _run_status_out(
            walltime=[],
            resource_usage=[
                {"metric": "peak_rss_kb", "n": 5, "min_v": 100_000,
                 "p50_v": 150_000, "p90_v": 190_000, "max_v": 200_000},
                {"metric": "cpu_seconds", "n": 5, "min_v": 10.0,
                 "p50_v": 15.0, "p90_v": 19.0, "max_v": 20.0},
            ])
        self.assertEqual(rc, 0)
        self.assertIn(self._WALLTIME_HEADING, output)
        self.assertIn("peak_rss_kb", output)
        self.assertIn("cpu_seconds", output)

    def test_populated_walltime_with_all_zero_resource_usage(self):
        # Every attempt predates the D7 columns (or every rusage read
        # failed): n=0 for both metrics. The walltime rows still print,
        # but no rusage line does -- n=0 stays hidden.
        rc, output = _run_status_out(
            walltime=[
                {"stage_name": "align", "min_ms": 100, "p50_ms": 150,
                 "p90_ms": 190, "max_ms": 200, "n": 5},
            ],
            resource_usage=[
                {"metric": "peak_rss_kb", "n": 0, "min_v": None,
                 "p50_v": None, "p90_v": None, "max_v": None},
                {"metric": "cpu_seconds", "n": 0, "min_v": None,
                 "p50_v": None, "p90_v": None, "max_v": None},
            ])
        self.assertEqual(rc, 0)
        self.assertIn(self._WALLTIME_HEADING, output)
        self.assertIn("align", output)
        self.assertNotIn("peak_rss_kb", output)
        self.assertNotIn("cpu_seconds", output)

    def test_both_populated_appear_under_one_shared_heading(self):
        rc, output = _run_status_out(
            walltime=[
                {"stage_name": "align", "min_ms": 100, "p50_ms": 150,
                 "p90_ms": 190, "max_ms": 200, "n": 5},
            ],
            resource_usage=[
                {"metric": "peak_rss_kb", "n": 5, "min_v": 100_000,
                 "p50_v": 150_000, "p90_v": 190_000, "max_v": 200_000},
                {"metric": "cpu_seconds", "n": 5, "min_v": 10.0,
                 "p50_v": 15.0, "p90_v": 19.0, "max_v": 20.0},
            ])
        self.assertEqual(rc, 0)
        self.assertEqual(output.count(self._WALLTIME_HEADING), 1)
        self.assertIn("align", output)
        self.assertIn("peak_rss_kb", output)
        self.assertIn("cpu_seconds", output)

    def test_both_empty_prints_no_heading_and_does_not_crash(self):
        rc, output = _run_status_out(
            walltime=[],
            resource_usage=[
                {"metric": "peak_rss_kb", "n": 0, "min_v": None,
                 "p50_v": None, "p90_v": None, "max_v": None},
                {"metric": "cpu_seconds", "n": 0, "min_v": None,
                 "p50_v": None, "p90_v": None, "max_v": None},
            ])
        self.assertEqual(rc, 0)
        self.assertNotIn(self._WALLTIME_HEADING, output)
        self.assertNotIn("peak_rss_kb", output)
        self.assertNotIn("cpu_seconds", output)


# ---------------------------------------------------------------------------
# `run status` takes its name POSITIONALLY, matching `run archive`. `--name`
# stays accepted as a deprecated alias for one release; the positional wins
# when both are given; neither given is a usage error.
# ---------------------------------------------------------------------------
class ArgumentResolutionTests(unittest.TestCase):
    def test_positional_name_alone_works(self):
        rc, output = _run_status_out(name="some-run", name_positional="some-run",
                                     name_flag=None)
        self.assertEqual(rc, 0)
        self.assertIn("RUN some-run", output)

    def test_name_flag_alone_still_works_with_a_deprecation_note(self):
        stderr = io.StringIO()
        with mock.patch.object(sys, "stderr", stderr):
            rc, output = _run_status_out(
                name="some-run", name_positional=None, name_flag="some-run")
        self.assertEqual(rc, 0)
        self.assertIn("RUN some-run", output)
        self.assertIn("deprecated", stderr.getvalue())

    def test_positional_wins_when_both_are_given(self):
        stderr = io.StringIO()
        with mock.patch.object(sys, "stderr", stderr):
            rc, output = _run_status_out(
                name="positional-run", name_positional="positional-run",
                name_flag="flag-run")
        self.assertEqual(rc, 0)
        self.assertIn("RUN positional-run", output)
        # Both notes fire: the deprecation (--name was used at all) and the
        # conflict (both forms were given, positional wins).
        self.assertIn("deprecated", stderr.getvalue())
        self.assertIn("using the positional NAME", stderr.getvalue())

    def test_neither_form_is_a_usage_error(self):
        stderr = io.StringIO()
        with mock.patch.object(sys, "stderr", stderr):
            rc, _output = _run_status_out(
                name="unused", name_positional=None, name_flag=None)
        self.assertEqual(rc, operatorctl_main.EXIT_USAGE)
        self.assertIn("requires a run name", stderr.getvalue())


# ---------------------------------------------------------------------------
# `run archive` without --apply: no write, and render_plan's own wording.
# ---------------------------------------------------------------------------
class ArchiveDryRunTests(unittest.TestCase):
    def test_dry_run_performs_no_write_and_reports_nothing_changed(self):
        # `derived.archive_run`'s own jsonb result shape for a dry run
        # (109): dry_run True, rows_affected counts what an apply WOULD
        # touch, but nothing is actually written -- the fake conn's
        # `calls` list is what proves that, not the returned dict.
        conn = _FakeConn([
            {"action": "run_archive", "dry_run": True, "replayed": False,
             "rows_affected": 1, "audit_id": 42, "kind": "campaign",
             "prior_state": "complete", "refimages_demoted": 0,
             "diffimages_demoted": 0, "psfs_demoted": 0,
             "nothing_deleted": True},
        ])
        result = actions.archive_run(
            conn, "archive-key-1", "w9-ramp-science-18-x", "wrap up",
            dry_run=True)

        # ONE call only: the SELECT derived.archive_run(...) itself. No
        # second statement, no commit beyond call_function's own (which
        # the real function's dry-run path performs with nothing changed
        # inside its own transaction) -- what matters here is that this
        # module issued exactly one statement, not a write followed by a
        # rollback the caller has to trust the database did correctly.
        self.assertEqual(len(conn.calls), 1)
        sql, params = conn.calls[0]
        self.assertIn("derived.archive_run", sql)
        # idempotency key first, name second, matching 109's signature.
        self.assertEqual(params[0], "archive-key-1")
        self.assertEqual(params[1], "w9-ramp-science-18-x")

        rendered = render_plan("run_archive", "runs:w9-ramp-science-18-x",
                               "wrap up", "archive-key-1", result, False)
        self.assertIn("Nothing was changed", rendered)
        self.assertIn("DRY RUN", rendered)


# ---------------------------------------------------------------------------
# `run create` passes the idempotency key FIRST.
# ---------------------------------------------------------------------------
class CreateRunKeyOrderingTests(unittest.TestCase):
    def test_idempotency_key_is_the_first_argument_to_derived_create_run(self):
        conn = _FakeConn([
            {"action": "run_create", "dry_run": True, "replayed": False,
             "rows_affected": 0, "audit_id": 7, "already_present": False,
             "run_id": None, "would_add": True},
        ])
        actions.create_run(
            conn, "create-key-1", "w9-ramp-science-18-x", "ben", "campaign",
            reason="new ramp step", dry_run=True)

        self.assertEqual(len(conn.calls), 1)
        sql, params = conn.calls[0]
        self.assertIn("derived.create_run", sql)
        self.assertEqual(params[0], "create-key-1", (
            "the idempotency key must be the FIRST positional argument to "
            "derived.create_run, matching migration 109's "
            "p_idempotency_key-first signature"))
        self.assertEqual(params[1], "w9-ramp-science-18-x")
        self.assertEqual(params[2], "ben")
        self.assertEqual(params[3], "campaign")


# ---------------------------------------------------------------------------
# `run release-dead-letters`: expected-state refusal and per-candidate
# independence, over a fake WorkUnitWriter transition.
# ---------------------------------------------------------------------------
class ReleaseDeadLettersTests(unittest.TestCase):
    def test_dry_run_writes_nothing_and_records_the_candidate_count(self):
        from pipeline.operatorctl import run as run_mod

        class _FakeConnWithCandidates(_FakeConn):
            def cursor(self):
                return _FindCandidatesCursor(self)

        class _FindCandidatesCursor(_FakeCursor):
            def execute(self, sql, params=None):
                self._conn.calls.append((" ".join(sql.split()), params))
                if "FROM attempts a" in sql and "JOIN work_units" in sql:
                    self._rows = [(101, 501, "job-1"), (102, 502, "job-2")]
                else:
                    self._result = self._conn.script.pop(0)
                    self._rows = None

            def fetchall(self):
                return self._rows or []

        conn = _FakeConnWithCandidates([
            None,   # mutation_replay: no prior row
            {"action": "run_release_dead_letters", "dry_run": True,
             "replayed": False, "rows_affected": 0, "audit_id": 9},
        ])
        result, scope = run_mod.release_dead_letters_audited(
            conn, "release-key-1", "w9-ramp-science-18-x", "recover sci-c",
            dry_run=True, out=_null_out())
        self.assertEqual(result["rows_affected"], 0)
        self.assertEqual(scope, "run:w9-ramp-science-18-x:release-dead-letters")

    def test_expected_state_mismatch_is_raised_before_any_release(self):
        from pipeline.operatorctl import run as run_mod
        from pipeline.operatorctl.contract import ExpectedStateMismatch

        class _FakeConnWithCandidates(_FakeConn):
            def cursor(self):
                return _FindCandidatesCursor(self)

        class _FindCandidatesCursor(_FakeCursor):
            def execute(self, sql, params=None):
                self._conn.calls.append((" ".join(sql.split()), params))
                if "FROM attempts a" in sql and "JOIN work_units" in sql:
                    self._rows = [(101, 501, "job-1")]
                else:
                    self._result = self._conn.script.pop(0)
                    self._rows = None

            def fetchall(self):
                return self._rows or []

        conn = _FakeConnWithCandidates([None])  # only the replay lookup
        with self.assertRaises(ExpectedStateMismatch):
            run_mod.release_dead_letters_audited(
                conn, "release-key-2", "w9-ramp-science-18-x", "recover",
                expected_state={"candidates": 5}, dry_run=True,
                out=_null_out())
        # No record_external_action call: the mismatch must be raised
        # before the audit write, matching every other expected-state
        # check in this package.
        writes = [c for c in conn.calls if "record_external_action" in c[0]]
        self.assertEqual(len(writes), 0)


def _null_out():
    import io
    return io.StringIO()


# ---------------------------------------------------------------------------
# `run start --phase reference/science` (throughput-sitting ruling,
# 2026-09-11): `gather_for_run` dispatches the two MJD-windowed phases to
# the right gatherer with the right window, distinct from the four
# post-DB-chain phases' `_phase_table()` dispatch. Tested against
# `gather_for_run` directly, with `submission.gathering`'s two windowed
# gatherers replaced by fakes that record their call -- no database, no
# AWS, matching this file's own stub-tier convention throughout.
# ---------------------------------------------------------------------------
class WindowedPhaseDispatchTests(unittest.TestCase):

    def setUp(self):
        from pipeline.operatorctl import run as run_mod
        from submission import gathering

        self.run_mod = run_mod
        self.calls = []

        def fake_reference(handle, start, end, start_mjdobs, end_mjdobs,
                           min_images_to_coadd, s3_client, job_bucket,
                           run_id, fids=None, run_scope=None):
            self.calls.append({
                "gatherer": "reference", "start": start, "end": end,
                "start_mjdobs": start_mjdobs, "end_mjdobs": end_mjdobs,
                "min_images_to_coadd": min_images_to_coadd,
                "s3_client": s3_client, "job_bucket": job_bucket,
                "run_id": run_id, "fids": fids, "run_scope": run_scope})
            return iter(())

        def fake_science(handle, start, end, start_mjdobs, end_mjdobs,
                         min_images_to_coadd, fids=None,
                         make_references=False, run_scope=None):
            self.calls.append({
                "gatherer": "science", "start": start, "end": end,
                "start_mjdobs": start_mjdobs, "end_mjdobs": end_mjdobs,
                "min_images_to_coadd": min_images_to_coadd,
                "fids": fids, "make_references": make_references,
                "run_scope": run_scope})
            return iter(())

        patcher_ref = mock.patch.object(
            gathering, "gather_reference_units", fake_reference)
        patcher_sci = mock.patch.object(
            gathering, "gather_science_units", fake_science)
        patcher_ref.start()
        patcher_sci.start()
        self.addCleanup(patcher_ref.stop)
        self.addCleanup(patcher_sci.stop)

    def _window(self, start_mjd=61600.0, end_mjd=61700.0, min_coadd=3):
        return ("2027-10-01 00:00:00", "2027-10-08 00:00:00",
               start_mjd, end_mjd, min_coadd)

    def test_phase_reference_dispatches_to_gather_reference_units(self):
        from submission import routes

        job_type, units = self.run_mod.gather_for_run(
            dbh=object(), phase="reference", window=self._window(),
            run_name="w9-campaign-1", s3_client="fake-s3",
            job_bucket="fake-bucket")

        self.assertEqual(list(units), [])
        self.assertEqual(job_type, routes.JOB_TYPE_REFERENCE_IMAGE)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["gatherer"], "reference")

    def test_phase_science_dispatches_to_gather_science_units(self):
        from submission import routes

        job_type, units = self.run_mod.gather_for_run(
            dbh=object(), phase="science", window=self._window(),
            run_name="w9-campaign-1")

        self.assertEqual(list(units), [])
        self.assertEqual(job_type, routes.JOB_TYPE_SCIENCE)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["gatherer"], "science")

    def test_the_window_reaches_the_gatherer_as_mjd_bounds(self):
        self.run_mod.gather_for_run(
            dbh=object(), phase="science",
            window=self._window(start_mjd=61601.5, end_mjd=61701.5),
            run_name="w9-campaign-1")

        self.assertEqual(self.calls[0]["start_mjdobs"], 61601.5)
        self.assertEqual(self.calls[0]["end_mjdobs"], 61701.5)

    def test_run_name_reaches_the_gate_as_run_scope_for_science(self):
        self.run_mod.gather_for_run(
            dbh=object(), phase="science", window=self._window(),
            run_name="w9-campaign-1")

        self.assertEqual(self.calls[0]["run_scope"], "w9-campaign-1")

    def test_run_name_reaches_both_run_id_and_run_scope_for_reference(self):
        # THE JUDGMENT CALL this task ruling asked to be stated explicitly:
        # `gather_reference_units`' own `run_id` (publish-key prefix) and
        # the new `run_scope` (gate scope) are different parameters, but
        # `run start`'s single `--name` is passed to BOTH -- a run
        # publishes its own artifacts under its own name and is gated only
        # on its own prior work. See `gathering.gather_reference_units`'s
        # docstring for the full reasoning.
        self.run_mod.gather_for_run(
            dbh=object(), phase="reference", window=self._window(),
            run_name="w9-campaign-1", s3_client="fake-s3",
            job_bucket="fake-bucket")

        self.assertEqual(self.calls[0]["run_id"], "w9-campaign-1")
        self.assertEqual(self.calls[0]["run_scope"], "w9-campaign-1")

    def test_fids_is_passed_through_unchanged(self):
        self.run_mod.gather_for_run(
            dbh=object(), phase="science", window=self._window(),
            run_name="w9-campaign-1", fids=[8])

        self.assertEqual(self.calls[0]["fids"], [8])

    def test_reference_without_s3_client_or_bucket_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self.run_mod.gather_for_run(
                dbh=object(), phase="reference", window=self._window(),
                run_name="w9-campaign-1")
        self.assertIn("reference", str(ctx.exception))

    def test_a_windowed_phase_without_a_window_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self.run_mod.gather_for_run(
                dbh=object(), phase="science", window=None,
                run_name="w9-campaign-1")
        self.assertIn("window", str(ctx.exception))

    def test_a_windowed_phase_without_a_run_name_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self.run_mod.gather_for_run(
                dbh=object(), phase="science", window=self._window(),
                run_name=None)
        self.assertIn("run name", str(ctx.exception))

    def test_cap_still_applies_to_a_windowed_gather(self):
        from submission import gathering

        def five_units(handle, start, end, start_mjdobs, end_mjdobs,
                       min_images_to_coadd, fids=None, make_references=False,
                       run_scope=None):
            return iter(range(5))

        with mock.patch.object(gathering, "gather_science_units",
                               five_units):
            _job_type, units = self.run_mod.gather_for_run(
                dbh=object(), phase="science", window=self._window(),
                run_name="w9-campaign-1", cap=2)

        self.assertEqual(units, [0, 1])

    def test_the_four_post_db_chain_phases_ignore_window_and_run_name(self):
        # The non-windowed phases must keep working with NO new required
        # arguments -- `window`/`run_name`/`s3_client`/`job_bucket`/`fids`
        # all default to None and are simply unused for these four.
        from submission import gathering

        with mock.patch.object(gathering, "gather_statistics_units",
                               lambda handle: iter(())):
            job_type, units = self.run_mod.gather_for_run(
                dbh=object(), phase="statistics")

        from submission import routes
        self.assertEqual(job_type, routes.JOB_TYPE_STATISTICS)
        self.assertEqual(list(units), [])
        # Neither fake windowed gatherer was called.
        self.assertEqual(self.calls, [])

    def test_an_unknown_phase_still_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.run_mod.gather_for_run(
                dbh=object(), phase="not-a-real-phase")


class SubmitRunSubmissionRoleTests(unittest.TestCase):
    """Stub-tier tests for the identity-fix ruling (2026-09-11):
    `submit_run` must perform its `seams.submit_gathered` call inside
    `session.submission_role(conn)`, since `rapid_operator` holds only
    SELECT and creating a work unit needs INSERT/UPDATE (see
    `submission_role()`'s docstring in `session.py`). Pinned here by
    patching `pipeline.operatorctl.session.submission_role` with a spy
    that records entry/exit around the `seams.submit_gathered` call,
    rather than a live role switch -- the actual `SET ROLE` behaviour is
    covered directly against `_FakeConn` in `test_session.py`.
    """

    def setUp(self):
        from pipeline.operatorctl import run as run_mod
        from pipeline.operatorctl import session as session_mod
        self.run_mod = run_mod
        self.session_mod = session_mod
        self.role_events = []

        import contextlib

        @contextlib.contextmanager
        def fake_submission_role(conn):
            self.role_events.append(("enter", conn))
            try:
                yield conn
            finally:
                self.role_events.append(("exit", conn))

        patcher = mock.patch.object(
            run_mod, "submission_role", fake_submission_role)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.submit_calls = []

        def fake_submit_gathered(units, **kwargs):
            # Recording that we are INSIDE the switched block at the
            # moment of the call is the whole point of this test class.
            self.submit_calls.append((units, kwargs, list(self.role_events)))
            return [("submission-1", ["attempt-1", "attempt-2"])]

        import pipeline.seams as seams_mod
        seams_patcher = mock.patch.object(
            seams_mod, "submit_gathered", fake_submit_gathered)
        seams_patcher.start()
        self.addCleanup(seams_patcher.stop)

    def test_submit_gathered_runs_inside_the_submission_role_block(self):
        # `types.SimpleNamespace`, not a bare `object()`: `submit_run` now
        # reads `conn.commit` (fix-txn-core, `protocol_commit=conn.commit`)
        # to build the call this test inspects, even though the mocked
        # `seams.submit_gathered` never invokes it. Identity is still all
        # this double needs to provide -- `role_events`/`submit_calls`
        # below assert against object identity, not attribute values.
        conn = types.SimpleNamespace(commit=lambda: None)
        context = {
            "queue": "q", "job_definition": "jd", "binding": "b",
            "manifest_bucket": "mb", "manifest_prefix": "mp",
            "s3_client": "s3", "batch_client": "batch"}

        results = self.run_mod.submit_run(
            conn, "campaign-1", "job-type-x", ["unit-a"], "reason",
            context=context)

        self.assertEqual(results,
                         [("submission-1", ["attempt-1", "attempt-2"])])
        self.assertEqual(len(self.submit_calls), 1)
        _units, _kwargs, events_at_call_time = self.submit_calls[0]
        # At the moment `seams.submit_gathered` ran, the block must
        # already have been entered and not yet exited.
        self.assertEqual(events_at_call_time, [("enter", conn)])
        # And it must have exited again by the time `submit_run` returns.
        self.assertEqual(self.role_events, [("enter", conn), ("exit", conn)])

    def test_empty_units_short_circuits_before_the_role_is_ever_assumed(
            self):
        # `submit_run` returns `[]` for empty `units` without calling
        # `seams.submit_gathered` at all -- the role must not be assumed
        # for a call that submits nothing.
        conn = object()
        context = {
            "queue": "q", "job_definition": "jd", "binding": "b",
            "manifest_bucket": "mb", "manifest_prefix": "mp",
            "s3_client": "s3", "batch_client": "batch"}

        results = self.run_mod.submit_run(
            conn, "campaign-1", "job-type-x", [], "reason", context=context)

        self.assertEqual(results, [])
        self.assertEqual(self.submit_calls, [])
        self.assertEqual(self.role_events, [])


class _CrashableConn:
    """A fake connection recording durability the way a real one would.

    `cursor()` returns a cursor whose `execute` appends `(sql, params)` to
    `self.pending`; nothing reaches `self.durable` until `self.commit()`
    runs. This is deliberately the ONLY thing this double does -- it does
    NOT itself decide when to autocommit. Whether `commit()` gets called
    after every statement (the bug) or only when `submit_units`'s
    `protocol_commit` calls it (the fix) is entirely up to the REAL
    `ConnectionExecutor` under test: its own `execute()` calls
    `self._conn.commit()` itself when `autocommit_each=True` (see
    `database/modules/utils/rapid_db_connect.py`), so this double just
    needs to answer `commit()` truthfully, not reimplement the policy.
    """

    def __init__(self):
        self.durable = []
        self.pending = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def description(self):
        return None

    @property
    def rowcount(self):
        return 1

    def fetchall(self):
        return []

    def execute(self, sql, params=None):
        self.pending.append((sql, params))

    def commit(self):
        self.durable.extend(self.pending)
        self.pending = []

    def rollback(self):
        self.pending = []

    def close(self):
        pass


class SubmitRunTransactionBoundaryTests(unittest.TestCase):
    """fix-txn-core, extended to the operatorctl submission path
    (2026-09-11): `submit_run` used to build `ConnectionExecutor(conn)`
    with the default `autocommit_each=True`, exactly the defect
    `pipeline.operator.service._execute_factory`'s docstring already
    describes and fixed for the VPO path (read it in full before touching
    this test) -- every statement `seams.submit_units` issues through that
    executor committed AS ITS OWN TRANSACTION, so the work-unit CAS UPDATE
    and the `unit_events` INSERT it exists to pair with had no atomicity
    between them.

    This class drives `submit_run` through the REAL `ConnectionExecutor`
    (only `seams.submit_gathered` is stubbed, standing in for a real
    `submit_units` call issuing exactly those two statements in order) and
    proves the property the docstring above claims: a crash between the
    CAS UPDATE and the `unit_events` INSERT must leave NEITHER durable.
    Before the fix, the CAS UPDATE survives the crash (autocommit) while
    the INSERT does not -- a torn write. After the fix, both are still
    `pending` when the crash hits, so closing the connection without a
    commit (`conn.close()`, never reached mid-transaction, but standing in
    for the real driver's rollback-on-close) leaves neither durable.
    """

    def setUp(self):
        from pipeline.operatorctl import run as run_mod
        from pipeline.operatorctl import session as session_mod
        self.run_mod = run_mod
        self.context = {
            "queue": "q", "job_definition": "jd", "binding": "b",
            "manifest_bucket": "mb", "manifest_prefix": "mp",
            "s3_client": "s3", "batch_client": "batch"}

        import contextlib

        @contextlib.contextmanager
        def fake_submission_role(conn):
            yield conn

        patcher = mock.patch.object(
            run_mod, "submission_role", fake_submission_role)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_crash_between_the_cas_and_the_event_insert_leaves_neither_durable(
            self):
        conn = _CrashableConn()
        # This test reads whichever `autocommit_each` `submit_run` itself
        # actually passes to `ConnectionExecutor`, by observing durability
        # after a simulated crash, rather than hard-coding an expectation
        # that would just restate the fix instead of proving it.

        # Stands in for `seams.submit_units`'s first two statements,
        # issued through whatever `execute` `submit_run` actually
        # constructed -- the real object under test.
        def fake_submit_gathered_crash(units, execute, protocol_commit=None,
                                       **kwargs):
            execute("UPDATE work_units SET state = 'submitted' ...",
                   ("wu-1",))
            # Simulated crash: the process dies here, before the
            # `unit_events` INSERT and before any closing commit.
            raise RuntimeError("simulated crash")

        import pipeline.seams as seams_mod
        with mock.patch.object(seams_mod, "submit_gathered",
                              fake_submit_gathered_crash):
            with self.assertRaises(RuntimeError):
                self.run_mod.submit_run(
                    conn, "campaign-1", "job-type-x", ["unit-a"], "reason",
                    context=self.context)

        # THE ASSERTION. Under the bug (autocommit_each=True, the
        # ConnectionExecutor default `submit_run` used to pass), the CAS
        # UPDATE committed as its own transaction the instant `execute()`
        # returned -- it is durable even though the crash happened one
        # statement later. Under the fix, it is not: nothing commits
        # until `protocol_commit` runs, which the crash pre-empted, so
        # `conn.durable` must be empty.
        self.assertEqual([], conn.durable,
                         "the work-unit CAS UPDATE must not be durable "
                         "when the crash pre-empts the closing commit -- "
                         "a non-empty conn.durable here means the fix's "
                         "autocommit_each=False did not reach the real "
                         "executor `submit_run` constructs")

    def test_protocol_commit_is_wired_so_a_full_pass_still_commits(self):
        # The companion to the crash test above: `autocommit_each=False`
        # with NO `protocol_commit` would turn the correctness bug into a
        # data-loss bug (nothing would ever commit) -- `submit_gathered`'s
        # docstring on `protocol_commit` and `submit_units`'s docstring
        # (around its two commit boundaries) are why this argument is not
        # optional garnish. A full, uninterrupted pass must still leave
        # both statements durable.
        conn = _CrashableConn()

        # Stands in for `seams.submit_units`'s two statements, issued
        # through whatever `execute`/`protocol_commit` `submit_run`
        # actually constructed -- the real object under test.
        def fake_submit_gathered(units, execute, protocol_commit=None,
                                 **kwargs):
            execute("UPDATE work_units SET state = 'submitted' ...",
                   ("wu-1",))
            execute("INSERT INTO unit_events ...", ("wu-1", "submitted"))
            if protocol_commit is not None:
                protocol_commit()
            return [("submission-1", ["attempt-1"])]

        import pipeline.seams as seams_mod
        with mock.patch.object(seams_mod, "submit_gathered",
                              fake_submit_gathered):
            results = self.run_mod.submit_run(
                conn, "campaign-1", "job-type-x", ["unit-a"], "reason",
                context=self.context)

        self.assertEqual(results, [("submission-1", ["attempt-1"])])
        self.assertEqual(2, len(conn.durable),
                         "both the CAS UPDATE and the unit_events INSERT "
                         "must be durable after a full pass -- an empty "
                         "or short conn.durable here means submit_run "
                         "passed autocommit_each=False without also "
                         "wiring protocol_commit, which would silently "
                         "discard every submission")


class StartRunAuditedOrderingTests(unittest.TestCase):
    """`start_run_audited` must call `submit_run` (which now wraps its
    real work in `submission_role`) and THEN `record_external_action` --
    never the reverse, and the audit call must never itself be inside the
    submission role switch. This is the property the task ruling singles
    out: the audited ledger row is written under whatever tier the
    session actually assumed, unaffected by the submission widening.

    A full `start_run_audited` call pulls in gathering, `RAPIDDB`, and
    submission-env resolution that are exercised elsewhere in this file
    and in `WindowedPhaseDispatchTests` -- this class instead pins the
    ORDERING directly against `submit_run` and `record_external_action`
    as two mocked collaborators, which is the level the ruling's
    property actually lives at.
    """

    def setUp(self):
        from pipeline.operatorctl import run as run_mod
        self.run_mod = run_mod

    def test_submit_run_is_called_before_record_external_action(self):
        import inspect

        source = inspect.getsource(self.run_mod.start_run_audited)
        submit_pos = source.index("submit_run(")
        audit_positions = [
            i for i in self._all_indices(source, "record_external_action(")]
        # There are two `record_external_action` call sites in this
        # function (dry-run and apply); the one that matters for this
        # ordering guarantee is the one reached on the SAME branch as
        # `submit_run` -- i.e. the LAST one in source order, since the
        # apply branch's audit call is written after its `submit_run`
        # call and after the dry-run branch's own (unreachable-together)
        # audit call.
        self.assertTrue(audit_positions)
        self.assertLess(submit_pos, audit_positions[-1],
                        "submit_run(...) must appear, in source, before "
                        "the apply-branch's record_external_action(...) "
                        "call -- the audit row must be written AFTER "
                        "submission, never before")

    @staticmethod
    def _all_indices(haystack, needle):
        start = 0
        while True:
            idx = haystack.find(needle, start)
            if idx == -1:
                return
            yield idx
            start = idx + 1


if __name__ == "__main__":
    unittest.main()


class ReleaseOneSubmissionRoleTests(unittest.TestCase):
    """`_release_one` must widen for its transition, exactly as
    `submit_run` does for its submission.

    FOUND LIVE, 2026-09-11. `run release-dead-letters --apply` failed on
    every candidate with `permission denied for function
    transition_work_unit`: the release goes through
    `derived.transition_work_unit`, and the operate tier does not hold
    EXECUTE on it, while `rapid_admin` and `rapid_orchestrator` do. Same
    defect shape as the one that stopped `run start --apply` — a command
    that records an operator's decision while performing the pipeline's
    own work, running both halves under the identity that exists for the
    recording half.
    """

    def test_the_transition_runs_inside_the_submission_role_block(self):
        import contextlib

        from pipeline.operatorctl import run as run_mod

        events = []

        @contextlib.contextmanager
        def fake_submission_role(conn):
            events.append("enter")
            try:
                yield conn
            finally:
                events.append("exit")

        seen = {}

        class _FakeWriter:
            def __init__(self, execute):
                pass

            def transition_unit(self, unit_id, frm, to, writer=None,
                                reason=None):
                # Recording the role events AT THE MOMENT of the call is
                # the whole point: the transition must be inside them.
                seen["at_call"] = list(events)
                seen["unit_id"] = unit_id

        import pipeline.intent.writer as writer_mod
        with mock.patch.object(run_mod, "submission_role",
                               fake_submission_role), \
             mock.patch.object(writer_mod, "WorkUnitWriter", _FakeWriter):
            run_mod._release_one(
                object(), {"work_unit_id": 4242}, "because")

        self.assertEqual(seen["unit_id"], 4242)
        self.assertEqual(seen["at_call"], ["enter"],
                         "the transition must run INSIDE the widened role")
        self.assertEqual(events, ["enter", "exit"],
                         "and the role must be restored afterwards")


# ---------------------------------------------------------------------------
# `run reconcile-stranded` — the stranded-unit reconciler (Batch-discovery
# release beyond `release-dead-letters`' reach). No AWS, no database: a
# fake paginating Batch client and a fake conn/cursor pair that scripts the
# two SELECTs `find_stranded_candidates` issues (array ids, then stranded
# units), matching this file's stub-tier convention throughout.
# ---------------------------------------------------------------------------
class _FakePaginator:
    def __init__(self, pages_by_key):
        self._pages_by_key = pages_by_key

    def paginate(self, arrayJobId=None, jobStatus=None):   # noqa: N803
        return self._pages_by_key.get((arrayJobId, jobStatus), [])


class _FakeBatchClient:
    """A Batch client whose `list_jobs` pages are scripted per
    `(arrayJobId, jobStatus)` key — the same paginator idiom
    `_PartiallyRefusingBatchClient` in `test_batch.py` uses, keyed for
    array/status instead of queue/status since `batch_child_fate` and
    `wave_in_flight` both call `list_jobs(arrayJobId=..., jobStatus=...)`.

    `pages_by_key[(array_job_id, status)]` is a list of pages, each
    `{"jobSummaryList": [...]}` — an absent key means zero pages (no jobs
    in that array/status), never an error, matching real `list_jobs`
    behaviour for a status with no matches.
    """

    def __init__(self, pages_by_key):
        self._pages_by_key = pages_by_key

    def get_paginator(self, name):
        assert name == "list_jobs"
        return _FakePaginator(self._pages_by_key)


def _job(job_id):
    return {"jobId": job_id}


class _StrandedFakeCursor:
    """Routes on SQL text between the two SELECTs `find_stranded_candidates`
    issues and whatever the caller (`reconcile_stranded_audited`) also
    issues via `_replay_lookup`/`record_external_action` — those two go
    through `contract.call_function`, which this fake does not intercept,
    so `array_ids_rows`/`stranded_rows` cover only the two SELECTs this
    module's own cursor calls issue directly.
    """

    def __init__(self, conn):
        self._conn = conn
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        self._conn.calls.append((text, params))
        if "split_part(scheduler_job_id" in text:
            self._rows = self._conn.array_ids_rows
        elif "FROM work_units w" in text and "JOIN attempts a" in text:
            self._rows = self._conn.stranded_rows
        else:
            raise AssertionError("unexpected statement: %s" % sql)

    def fetchall(self):
        return self._rows


class _StrandedFakeConn:
    """`array_ids_rows`/`stranded_rows` script the two SELECTs;
    `replay_and_audit_script` scripts whatever `_replay_lookup` and
    `record_external_action` read through `contract.call_function` —
    patched directly in each test rather than modeled here, since those
    two go through a `psycopg2`-shaped cursor this fake does not emulate
    (see `ReleaseDeadLettersTests` for the same split in the existing
    dead-letter tests).
    """

    def __init__(self, array_ids_rows, stranded_rows):
        self.array_ids_rows = array_ids_rows
        self.stranded_rows = stranded_rows
        self.calls = []
        self.committed = 0
        self.rolled_back = 0

    def cursor(self):
        return _StrandedFakeCursor(self)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


class BatchChildFateTests(unittest.TestCase):
    def test_maps_terminal_children_to_their_status(self):
        from pipeline.operatorctl import run as run_mod

        client = _FakeBatchClient({
            ("array-1", "SUCCEEDED"): [
                {"jobSummaryList": [_job("array-1:0"), _job("array-1:1")]}],
            ("array-1", "FAILED"): [
                {"jobSummaryList": [_job("array-1:2")]}],
        })
        fate = run_mod.batch_child_fate(client, ["array-1"])
        self.assertEqual(fate, {
            "array-1:0": "SUCCEEDED",
            "array-1:1": "SUCCEEDED",
            "array-1:2": "FAILED",
        })

    def test_paginates_across_multiple_pages(self):
        from pipeline.operatorctl import run as run_mod

        client = _FakeBatchClient({
            ("array-1", "FAILED"): [
                {"jobSummaryList": [_job("array-1:0")]},
                {"jobSummaryList": [_job("array-1:1")]},
            ],
        })
        fate = run_mod.batch_child_fate(client, ["array-1"])
        self.assertEqual(fate, {"array-1:0": "FAILED", "array-1:1": "FAILED"})

    def test_in_flight_children_are_simply_absent(self):
        from pipeline.operatorctl import run as run_mod

        client = _FakeBatchClient({})
        fate = run_mod.batch_child_fate(client, ["array-1"])
        self.assertEqual(fate, {})


class ArrayJobIdsForRunTests(unittest.TestCase):
    def test_derives_distinct_array_ids_from_the_database(self):
        from pipeline.operatorctl import run as run_mod

        conn = _StrandedFakeConn(
            array_ids_rows=[("array-1",), ("array-2",)], stranded_rows=[])
        ids = run_mod.array_job_ids_for_run(conn, "w9-ramp")
        self.assertEqual(ids, ["array-1", "array-2"])
        sql, params = conn.calls[0]
        self.assertIn("split_part(scheduler_job_id", sql)
        self.assertEqual(params, ["w9-ramp%"])


class FindStrandedCandidatesTests(unittest.TestCase):
    """The correctness rule, tested directly: candidate/exclusion outcomes
    for each of the required cases, over a fake Batch client and a fake
    database cursor -- no AWS, no Postgres.
    """

    def test_all_children_failed_no_successful_attempt_is_a_candidate(self):
        from pipeline.operatorctl import run as run_mod

        conn = _StrandedFakeConn(
            array_ids_rows=[("array-1",)],
            stranded_rows=[(501, "blocked", ["array-1:0"], False)])
        client = _FakeBatchClient({
            ("array-1", "FAILED"): [{"jobSummaryList": [_job("array-1:0")]}],
        })
        candidates, excluded = run_mod.find_stranded_candidates(
            conn, "w9-ramp", client)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["work_unit_id"], 501)
        self.assertEqual(candidates[0]["state"], "blocked")
        self.assertEqual(excluded, [])

    def test_failed_child_with_a_successful_attempt_is_excluded(self):
        # THE REVIEW QUESTION'S EXACT CASE: the application succeeded and
        # published, then the container exited nonzero on teardown. Batch
        # says FAILED; the attempt row says success; the unit must not be
        # released back to READY.
        from pipeline.operatorctl import run as run_mod

        conn = _StrandedFakeConn(
            array_ids_rows=[("array-1",)],
            stranded_rows=[(64, "submitted", ["array-1:0"], True)])
        client = _FakeBatchClient({
            ("array-1", "FAILED"): [{"jobSummaryList": [_job("array-1:0")]}],
        })
        candidates, excluded = run_mod.find_stranded_candidates(
            conn, "w9-ramp", client)
        self.assertEqual(candidates, [])
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0]["work_unit_id"], 64)
        self.assertEqual(excluded[0]["reason"], "successful_sibling_attempt")

    def test_succeeded_child_is_excluded_even_with_no_successful_attempt(self):
        # The retry case: Batch's own FAILED->SUCCEEDED retry means the
        # child now reports SUCCEEDED even though no attempt row recorded
        # success (the pipeline attempt row is for the earlier failed
        # try). Must be excluded as batch_child_not_failed, not released.
        from pipeline.operatorctl import run as run_mod

        conn = _StrandedFakeConn(
            array_ids_rows=[("array-1",)],
            stranded_rows=[(1604, "blocked", ["array-1:0"], False)])
        client = _FakeBatchClient({
            ("array-1", "SUCCEEDED"): [
                {"jobSummaryList": [_job("array-1:0")]}],
        })
        candidates, excluded = run_mod.find_stranded_candidates(
            conn, "w9-ramp", client)
        self.assertEqual(candidates, [])
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0]["work_unit_id"], 1604)
        self.assertEqual(excluded[0]["reason"], "batch_child_not_failed")
        self.assertEqual(excluded[0]["scheduler_job_id"], "array-1:0")

    def test_submitted_and_blocked_units_both_become_candidates(self):
        from pipeline.operatorctl import run as run_mod

        conn = _StrandedFakeConn(
            array_ids_rows=[("array-1",)],
            stranded_rows=[
                (10, "submitted", ["array-1:0"], False),
                (11, "blocked", ["array-1:1"], False),
            ])
        client = _FakeBatchClient({
            ("array-1", "FAILED"): [
                {"jobSummaryList": [_job("array-1:0"), _job("array-1:1")]}],
        })
        candidates, _excluded = run_mod.find_stranded_candidates(
            conn, "w9-ramp", client)
        states = {c["work_unit_id"]: c["state"] for c in candidates}
        self.assertEqual(states, {10: "submitted", 11: "blocked"})


class ReleaseStrandedOneSubmissionRoleTests(unittest.TestCase):
    """`_release_stranded_one` must widen for its transition and must
    transition from the CANDIDATE'S OWN state, exactly as
    `ReleaseOneSubmissionRoleTests` proves for `_release_one` -- never a
    raw UPDATE, always through `WorkUnitWriter.transition_unit`.
    """

    def test_transitions_from_the_candidate_s_own_state_inside_the_role(self):
        import contextlib

        from pipeline.operatorctl import run as run_mod

        events = []

        @contextlib.contextmanager
        def fake_submission_role(conn):
            events.append("enter")
            try:
                yield conn
            finally:
                events.append("exit")

        seen = {}

        class _FakeWriter:
            def __init__(self, execute):
                pass

            def transition_unit(self, unit_id, frm, to, writer=None,
                                reason=None):
                seen["at_call"] = list(events)
                seen["unit_id"] = unit_id
                seen["from_state"] = frm
                seen["to_state"] = to

        import pipeline.intent.writer as writer_mod
        with mock.patch.object(run_mod, "submission_role",
                               fake_submission_role), \
             mock.patch.object(writer_mod, "WorkUnitWriter", _FakeWriter):
            run_mod._release_stranded_one(
                object(), {"work_unit_id": 1604, "state": "submitted"},
                "reconcile")

        self.assertEqual(seen["unit_id"], 1604)
        self.assertEqual(seen["from_state"], writer_mod.SUBMITTED)
        self.assertEqual(seen["to_state"], writer_mod.READY)
        self.assertEqual(seen["at_call"], ["enter"],
                         "the transition must run INSIDE the widened role")
        self.assertEqual(events, ["enter", "exit"])

    def test_blocked_candidate_transitions_from_blocked(self):
        import contextlib

        from pipeline.operatorctl import run as run_mod

        @contextlib.contextmanager
        def fake_submission_role(conn):
            yield conn

        seen = {}

        class _FakeWriter:
            def __init__(self, execute):
                pass

            def transition_unit(self, unit_id, frm, to, writer=None,
                                reason=None):
                seen["from_state"] = frm

        import pipeline.intent.writer as writer_mod
        with mock.patch.object(run_mod, "submission_role",
                               fake_submission_role), \
             mock.patch.object(writer_mod, "WorkUnitWriter", _FakeWriter):
            run_mod._release_stranded_one(
                object(), {"work_unit_id": 252, "state": "blocked"},
                "reconcile")

        self.assertEqual(seen["from_state"], writer_mod.BLOCKED)


class WaveSplitTests(unittest.TestCase):
    def test_splits_into_waves_of_the_max_size(self):
        from pipeline.operatorctl import run as run_mod

        candidates = [{"work_unit_id": i} for i in range(10)]
        waves = run_mod.wave_split(candidates, max_wave=4)
        self.assertEqual([len(w) for w in waves], [4, 4, 2])
        self.assertEqual(waves[0][0]["work_unit_id"], 0)
        self.assertEqual(waves[-1][-1]["work_unit_id"], 9)

    def test_default_wave_size_is_4000(self):
        from pipeline.operatorctl import run as run_mod
        self.assertEqual(run_mod.DEFAULT_WAVE_SIZE, 4000)

    def test_empty_candidate_list_yields_no_waves(self):
        from pipeline.operatorctl import run as run_mod
        self.assertEqual(run_mod.wave_split([], max_wave=4), [])


class WaveInFlightTests(unittest.TestCase):
    def test_true_when_a_child_is_runnable_or_running(self):
        from pipeline.operatorctl import run as run_mod

        client = _FakeBatchClient({
            ("array-1", "RUNNING"): [{"jobSummaryList": [_job("array-1:0")]}],
        })
        self.assertTrue(run_mod.wave_in_flight(client, ["array-1"]))

    def test_false_when_nothing_is_runnable_or_running(self):
        from pipeline.operatorctl import run as run_mod

        client = _FakeBatchClient({})
        self.assertFalse(run_mod.wave_in_flight(client, ["array-1"]))


class ReconcileStrandedAuditedTests(unittest.TestCase):
    """`reconcile_stranded_audited`'s own contract: a distinct action name,
    the dry-run branch writing no transition, and the wave/drain machinery
    being consulted between waves on a real (non-dry-run) release.
    """

    def _patch_replay_and_audit(self, replay_result, audit_result):
        """`_replay_lookup`/`record_external_action` both go through
        `contract.call_function`, which the `_StrandedFakeConn` cursor
        does not emulate (see that class's docstring) -- patched directly
        here, matching how `ReleaseDeadLettersTests` scripts them via
        `conn.script` for the OTHER fake-conn shape; this reconciler's
        fake conn scripts only the two SELECTs `find_stranded_candidates`
        issues, so replay/audit are patched at the function level instead.
        """
        from pipeline.operatorctl import run as run_mod
        patchers = [
            mock.patch.object(run_mod, "_replay_lookup",
                              return_value=replay_result),
            mock.patch.object(run_mod, "record_external_action",
                              return_value=audit_result),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_dry_run_writes_no_transition(self):
        from pipeline.operatorctl import run as run_mod

        self._patch_replay_and_audit(
            None, {"action": "run_reconcile_stranded", "dry_run": True,
                   "replayed": False, "rows_affected": 0, "audit_id": 1})

        conn = _StrandedFakeConn(
            array_ids_rows=[("array-1",)],
            stranded_rows=[(501, "blocked", ["array-1:0"], False)])
        client = _FakeBatchClient({
            ("array-1", "FAILED"): [{"jobSummaryList": [_job("array-1:0")]}],
        })

        with mock.patch.object(run_mod, "_release_stranded_one") as release:
            result, scope = run_mod.reconcile_stranded_audited(
                conn, "recon-key-1", "w9-ramp", "reconcile", client,
                dry_run=True, out=_null_out())

        release.assert_not_called()
        self.assertEqual(result["rows_affected"], 0)
        self.assertEqual(scope, "run:w9-ramp:reconcile-stranded")

    def test_action_class_is_distinct_from_release_dead_letters(self):
        from pipeline.operatorctl import run as run_mod

        self._patch_replay_and_audit(
            None, {"action": "run_reconcile_stranded", "dry_run": True,
                   "replayed": False, "rows_affected": 0, "audit_id": 2})

        conn = _StrandedFakeConn(array_ids_rows=[], stranded_rows=[])
        client = _FakeBatchClient({})

        with mock.patch.object(run_mod, "_replay_lookup") as replay_mock:
            replay_mock.return_value = None
            run_mod.reconcile_stranded_audited(
                conn, "recon-key-2", "w9-ramp", "reconcile", client,
                dry_run=True, out=_null_out())
        replay_mock.assert_called_once()
        action_class = replay_mock.call_args[0][2]
        self.assertEqual(action_class, "run_reconcile_stranded")
        self.assertNotEqual(action_class, "run_release_dead_letters")

    def test_expected_state_mismatch_is_raised_before_any_release(self):
        from pipeline.operatorctl import run as run_mod
        from pipeline.operatorctl.contract import ExpectedStateMismatch

        self._patch_replay_and_audit(None, None)

        conn = _StrandedFakeConn(
            array_ids_rows=[("array-1",)],
            stranded_rows=[(501, "blocked", ["array-1:0"], False)])
        client = _FakeBatchClient({
            ("array-1", "FAILED"): [{"jobSummaryList": [_job("array-1:0")]}],
        })

        with mock.patch.object(run_mod, "record_external_action") as audit:
            with self.assertRaises(ExpectedStateMismatch):
                run_mod.reconcile_stranded_audited(
                    conn, "recon-key-3", "w9-ramp", "reconcile", client,
                    expected_state={"candidates": 5}, dry_run=True,
                    out=_null_out())
            audit.assert_not_called()

    def test_drain_check_is_consulted_between_waves_on_a_real_release(self):
        from pipeline.operatorctl import run as run_mod

        self._patch_replay_and_audit(
            None, {"action": "run_reconcile_stranded", "dry_run": False,
                   "replayed": False, "rows_affected": 6, "audit_id": 3})

        stranded_rows = [(i, "submitted", ["array-1:%d" % i], False)
                         for i in range(6)]
        conn = _StrandedFakeConn(
            array_ids_rows=[("array-1",)], stranded_rows=stranded_rows)
        client = _FakeBatchClient({
            ("array-1", "FAILED"): [
                {"jobSummaryList": [_job("array-1:%d" % i)
                                    for i in range(6)]}],
        })

        drain_calls = []

        def fake_wave_in_flight(batch_client, array_job_ids):
            drain_calls.append(list(array_job_ids))
            return False

        with mock.patch.object(run_mod, "_release_stranded_one"), \
             mock.patch.object(run_mod, "wave_in_flight",
                               fake_wave_in_flight):
            run_mod.reconcile_stranded_audited(
                conn, "recon-key-4", "w9-ramp", "reconcile", client,
                dry_run=False, max_wave=2, out=_null_out())

        # 6 candidates, max_wave=2 -> 3 waves -> drain consulted before
        # waves 2 and 3 (never before the first wave).
        self.assertEqual(len(drain_calls), 2)
        self.assertEqual(drain_calls[0], ["array-1"])

    def test_a_failed_candidate_does_not_abort_the_rest(self):
        from pipeline.operatorctl import run as run_mod

        self._patch_replay_and_audit(
            None, {"action": "run_reconcile_stranded", "dry_run": False,
                   "replayed": False, "rows_affected": 1, "audit_id": 4})

        conn = _StrandedFakeConn(
            array_ids_rows=[("array-1",)],
            stranded_rows=[
                (501, "blocked", ["array-1:0"], False),
                (502, "submitted", ["array-1:1"], False),
            ])
        client = _FakeBatchClient({
            ("array-1", "FAILED"): [
                {"jobSummaryList": [_job("array-1:0"), _job("array-1:1")]}],
        })

        calls = []

        def fake_release(conn, candidate, reason):
            calls.append(candidate["work_unit_id"])
            if candidate["work_unit_id"] == 501:
                raise RuntimeError("CAS miss")

        with mock.patch.object(run_mod, "_release_stranded_one",
                               fake_release), \
             mock.patch.object(run_mod, "wave_in_flight",
                               return_value=False):
            run_mod.reconcile_stranded_audited(
                conn, "recon-key-5", "w9-ramp", "reconcile", client,
                dry_run=False, out=_null_out())

        self.assertEqual(calls, [501, 502],
                         "both candidates must be attempted despite the "
                         "first one's failure")
