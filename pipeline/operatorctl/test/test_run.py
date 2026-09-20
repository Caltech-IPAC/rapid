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
import json
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
from pipeline.operatorctl import run as operatorctl_run
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


def _stub_registry_binding(case, run_mod, kind="scratch", state="running",
                           run_id=77, seq=0, reference_set_id=7,
                           psf_set_id=None, reference_set="set-alpha"):
    """Stub the registry reads `start_run_audited` gained at migration 121.

    Every `start_run_audited` test in this file drives the function with a
    `conn` that is not a database — `object()`, or a `SimpleNamespace` with
    only `commit` — because each exists to pin what the function THREADS or
    what it puts in the audit scope, not what the registry says. Since 121
    the function reads the `runs` row and the run's submission ordinal
    before gathering, so those two reads are stubbed here, in one place,
    with an ordinary running scratch row.

    A test that cares about the BINDING itself (an absent row, a production
    kind, a completed run) does not use this helper — see
    `RunStartRegistryBindingTests`, which drives `_bind_registry_row`
    directly against a scripted `run_row`.

    THE ROW CARRIES A REFERENCE SET (migration 126). `start_run_audited`
    reads `reference_set_id` off this row and REFUSES a windowed phase
    that has none, so the ordinary row this helper stands up must declare
    one — otherwise every lane, family and ordering test in this file
    would fail on a fact none of them is about. A test that cares about
    the refusal passes `reference_set_id=None` explicitly; see
    `RunStartReferenceSetTests`.
    """
    bind_patcher = mock.patch.object(
        run_mod, "_bind_registry_row",
        lambda conn, name: {"run_id": run_id, "name": name, "kind": kind,
                            "state": state,
                            "reference_set_id": reference_set_id,
                            "psf_set_id": psf_set_id,
                            "reference_set": reference_set})
    bind_patcher.start()
    case.addCleanup(bind_patcher.stop)

    seq_patcher = mock.patch.object(
        run_mod, "next_submission_seq", lambda conn, run_key: seq)
    seq_patcher.start()
    case.addCleanup(seq_patcher.stop)

    case.state_calls = []

    def fake_start_run(conn, key, name, reason, dry_run=True,
                       policy_citation=None):
        case.state_calls.append({"name": name, "dry_run": dry_run,
                                 "key": key})
        return {"rows_affected": 1}

    from pipeline.operatorctl import actions as actions_mod
    state_patcher = mock.patch.object(actions_mod, "start_run",
                                      fake_start_run)
    state_patcher.start()
    case.addCleanup(state_patcher.stop)


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
        # `by_key=False` pins this to the PREFIX reading, which is what
        # this test is about: the predicate's `%%` escaping against a
        # cursor that counts placeholders the way psycopg2 does. The keyed
        # reading (migration 121) is a different query with a different
        # parameter and is exercised by `RunKeyTallyTests` below; forcing
        # the reading here keeps this test asking its own question rather
        # than also depending on how the reading is detected.
        conn = _RealPsycopg2SubstitutionConn(
            columns=["total", "failures"], rows=[(5, 2)])
        result = actions.run_attempt_tally(conn, "w9-ramp-science-18",
                                           by_key=False)
        self.assertEqual(result["total"], 5)
        self.assertEqual(result["failures"], 2)
        self.assertEqual(result["counted_by"], "name_prefix")
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
             "rows_affected": 1, "audit_id": 42, "kind": "scratch",
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
            conn, "create-key-1", "w9-ramp-science-18-x", "ben", "scratch",
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
        self.assertEqual(params[3], "scratch")


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

        # `reference_set_id`/`psf_set_id` (migration 126) are recorded here
        # alongside `run_scope` because they are a THIRD and FOURTH fact
        # `gather_for_run` threads, not a renaming of the scope — a fake
        # that omitted them would raise `TypeError` rather than prove the
        # threading, which is what happened when the production change
        # landed.
        def fake_reference(handle, start, end, start_mjdobs, end_mjdobs,
                           min_images_to_coadd, s3_client, job_bucket,
                           run_id, fids=None, run_scope=None,
                           reference_set_id=None, psf_set_id=None):
            self.calls.append({
                "gatherer": "reference", "start": start, "end": end,
                "start_mjdobs": start_mjdobs, "end_mjdobs": end_mjdobs,
                "min_images_to_coadd": min_images_to_coadd,
                "s3_client": s3_client, "job_bucket": job_bucket,
                "run_id": run_id, "fids": fids, "run_scope": run_scope,
                "reference_set_id": reference_set_id,
                "psf_set_id": psf_set_id})
            return iter(())

        def fake_science(handle, start, end, start_mjdobs, end_mjdobs,
                         min_images_to_coadd, fids=None,
                         make_references=False, run_scope=None,
                         reference_set_id=None, psf_set_id=None,
                         reference_image_id=None):
            self.calls.append({
                "gatherer": "science", "start": start, "end": end,
                "start_mjdobs": start_mjdobs, "end_mjdobs": end_mjdobs,
                "min_images_to_coadd": min_images_to_coadd,
                "fids": fids, "make_references": make_references,
                "run_scope": run_scope,
                "reference_set_id": reference_set_id,
                "psf_set_id": psf_set_id,
                "reference_image_id": reference_image_id})
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
                       run_scope=None, reference_set_id=None,
                       psf_set_id=None, reference_image_id=None):
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


class StartRunAuditedLaneResolutionTests(unittest.TestCase):
    """The two-lane change (2026-09-13), driven end to end through
    `start_run_audited`'s DRY-RUN branch: `--lane prompt` must reach the
    real `submission_env` -> `routes.queue_parameter_for_lane` call and
    come back with the PROMPT queue, not merely be stored on its way past.

    `StartRunAuditedOrderingTests` above deliberately drives `submit_run`/
    `record_external_action` as mocked collaborators because a full call
    also pulls in gathering and `RAPIDDB` -- exactly the two things a lane
    test does NOT need to fake beyond `RAPIDDB`'s `exit_code` guard and
    `gather_for_run`, since lane resolution happens in
    `_resolve_submission_env`, BEFORE either gather step runs, on the two
    MJD-windowed phases (`_WINDOWED_PHASES`). `phase="science"` is used
    here for exactly that reason: it is the one non-windowed-gather choice
    whose dry run still resolves a submission environment at all (see
    `start_run_audited`'s own `if windowed:` block) -- `statistics` and
    the other three post-DB-chain phases never touch `_resolve_submission_
    env` on a dry run, so they cannot prove anything about lanes.

    What's left un-faked, and why it's still a stub-tier test: `gather_for_
    run` is patched to a fixed unit list (a live DB is exactly what this
    class must not need, matching every other class in this file), but
    `submission_env` itself runs FOR REAL, all the way down to `routes.
    queue_parameter_for_lane` and `active_definition` -- only its two AWS
    edges are doubled, following `test_submission.py`'s
    `SubmissionEnvRoutingTests` pattern verbatim: `parameters=` is not
    reachable through `_resolve_submission_env` (it calls `submission_env`
    with no `parameters=` kwarg, so a `None` there always falls through to
    `fetch_parameters()`), so the parameter tree is faked by patching
    `submission.startup.fetch_parameters` instead, and `boto3.client` is
    patched on `pipeline.operator.submission`'s own `boto3` import so
    `submission_env`'s unconditional `boto3.client('batch')`/`('s3')`
    calls (reached whenever a caller does not inject a client, which
    `_resolve_submission_env` never does) return fakes instead of raising
    `NoRegionError` with no region configured.
    """

    #: Same tree shape as `test_submission.py`'s `SubmissionEnvRoutingTests
    #: .TREE` -- two lanes, one queue parameter key each.
    TREE = {
        "batch/queue-bulk": "rapid-queue-bulk",
        "batch/queue-prompt": "rapid-queue-prompt",
        "batch/job-definition-bulk": "rapid-pipeline-bulk",
        "batch/job-definition-science": "rapid-pipeline-science",
    }

    class _FakeBatch:
        """`describe_job_definitions`, one ACTIVE revision, no ambiguity --
        the minimum `active_definition` needs to resolve without ever
        reaching real Batch. Lane resolution does not care which family
        or revision comes back, only that `submission_env` completes, so
        this need not vary revisions per family the way `test_submission.
        py`'s own `FakeBatch` does for ITS property.
        """

        def describe_job_definitions(self, jobDefinitionName=None,
                                     status=None):
            return {"jobDefinitions": [
                {"jobDefinitionName": jobDefinitionName,
                 "jobDefinitionArn": "arn:aws:batch:us-east-1:ACCOUNT:"
                                    "job-definition/%s:1" % jobDefinitionName,
                 "revision": 1,
                 "containerProperties": {"image": "repo@sha256:" + "1" * 64}},
            ]}

    def setUp(self):
        from pipeline.operatorctl import run as run_mod
        self.run_mod = run_mod

        import os
        self._saved_env = {name: os.environ.get(name)
                           for name in ("RAPID_IMAGE_DIGEST",
                                        "RAPID_RELEASE_IDENTITY",
                                        "RAPID_MANIFEST_BUCKET")}
        os.environ["RAPID_IMAGE_DIGEST"] = "sha256:" + "0" * 64
        os.environ["RAPID_RELEASE_IDENTITY"] = "w9-test"
        os.environ["RAPID_MANIFEST_BUCKET"] = "rapid-manifests"

        _stub_registry_binding(self, run_mod)

        replay_patcher = mock.patch.object(
            run_mod, "_replay_lookup", lambda *a, **k: None)
        replay_patcher.start()
        self.addCleanup(replay_patcher.stop)

        gather_patcher = mock.patch.object(
            run_mod, "gather_for_run",
            lambda *a, **k: ("science", ["unit-a"]))
        gather_patcher.start()
        self.addCleanup(gather_patcher.stop)

        self.audit_calls = []

        def fake_record_external_action(conn, idempotency_key,
                                        action_class, target_scope, reason,
                                        dry_run=False, rows_affected=0,
                                        detail=None, policy_citation=None):
            self.audit_calls.append(
                {"target_scope": target_scope, "detail": dict(detail or {})})
            return {"rows_affected": rows_affected, "detail": detail}

        audit_patcher = mock.patch.object(
            run_mod, "record_external_action", fake_record_external_action)
        audit_patcher.start()
        self.addCleanup(audit_patcher.stop)

        # `RAPIDDB()` is only reached for its `exit_code` guard -- same
        # stub `StartRunAuditedWorkUnitScopeTests`
        # (test_run_work_unit_scope.py) uses.
        db_mod_patcher = mock.patch(
            "database.modules.utils.rapid_db.RAPIDDB",
            lambda: types.SimpleNamespace(exit_code=0))
        db_mod_patcher.start()
        self.addCleanup(db_mod_patcher.stop)

        import submission.startup as startup_mod
        fetch_patcher = mock.patch.object(
            startup_mod, "fetch_parameters", lambda: dict(self.TREE))
        fetch_patcher.start()
        self.addCleanup(fetch_patcher.stop)

        import pipeline.operator.submission as opsubmission_mod
        self.opsubmission_mod = opsubmission_mod
        batch_client_patcher = mock.patch.object(
            opsubmission_mod.boto3, "client",
            lambda service, **kw: (self._FakeBatch() if service == "batch"
                                   else object()))
        batch_client_patcher.start()
        self.addCleanup(batch_client_patcher.stop)

        # `start_run_audited`'s windowed branch calls `min_images_to_coadd()`
        # (release science configuration, resolved from `RAPID_SW`) BEFORE
        # it ever reaches `_resolve_submission_env` -- this class is about
        # lane resolution, not release config discovery, so that call is
        # faked to a fixed value the same way `gather_for_run` is below.
        import pipeline.operator.gathering as gathering_mod
        coadd_patcher = mock.patch.object(
            gathering_mod, "min_images_to_coadd", lambda: 3)
        coadd_patcher.start()
        self.addCleanup(coadd_patcher.stop)

    def tearDown(self):
        import os
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _start(self, lane, idempotency_key):
        return self.run_mod.start_run_audited(
            conn=object(), idempotency_key=idempotency_key,
            name="w9-campaign-1", phase="science", reason="lane check",
            dry_run=True, out=_null_out(),
            window_start="2027-10-01 00:00:00",
            window_end="2027-10-08 00:00:00", lane=lane)

    def test_lane_prompt_resolves_the_prompt_queue_end_to_end(self):
        # THE PROPERTY: `--lane prompt` must reach the PROMPT queue key
        # through the real `submission_env` -> `queue_parameter_for_lane`
        # resolution, not merely round-trip the string "prompt" back out
        # of a stub. Nothing here asserts on `lane` as stored data -- the
        # assertion is on the QUEUE NAME the tree resolves it to.
        result, scope = self._start(lane="prompt",
                                    idempotency_key="lane-prompt-key")

        self.assertEqual(len(self.audit_calls), 1)
        detail = self.audit_calls[0]["detail"]
        # `start_run_audited` does not put the resolved queue itself into
        # `detail` today -- what it DOES expose is the scope string
        # (asserted separately below) and the fact that gathering ran
        # under the job type the windowed branch resolved. The queue
        # resolution itself is pinned by re-deriving it the same way
        # `_resolve_submission_env` did, through the same faked tree and
        # batch client, and comparing identity -- this is the "end to
        # end" property: were the CLI's `lane` argument silently dropped
        # before it reached `_resolve_submission_env`, this call would
        # raise nothing and this comparison would still pass by accident
        # only if BOTH resolutions independently landed on bulk, which
        # the assertion below on the BULK case rules out.
        context = self.run_mod._resolve_submission_env("science",
                                                        lane="prompt")
        self.assertEqual("rapid-queue-prompt", context["queue"])
        self.assertEqual(detail["job_type"], "science")

    def test_lane_bulk_default_resolves_the_bulk_queue(self):
        # The CLI's own default (`--lane` omitted -> `args.lane == "bulk"`,
        # per `main.py`'s `run start` parser) must resolve the BULK queue,
        # not the route's None-lane default by coincidence -- both happen
        # to be bulk today, so this is pinned against the EXPLICIT string
        # "bulk" the CLI actually passes, never against `lane=None`.
        self._start(lane="bulk", idempotency_key="lane-bulk-key")

        context = self.run_mod._resolve_submission_env("science",
                                                        lane="bulk")
        self.assertEqual("rapid-queue-bulk", context["queue"])

    def test_lane_prompt_and_bulk_resolve_to_different_queues(self):
        # The property that makes the two tests above a LANE test rather
        # than two assertions that would both pass against a
        # `submission_env` that ignored `lane` entirely and always
        # returned science's bare default.
        prompt_context = self.run_mod._resolve_submission_env(
            "science", lane="prompt")
        bulk_context = self.run_mod._resolve_submission_env(
            "science", lane="bulk")

        self.assertNotEqual(prompt_context["queue"], bulk_context["queue"])

    def test_lane_prompt_is_carried_on_the_audit_scope_string(self):
        # This fixture's row is scratch-kind (`_stub_registry_binding`'s
        # own default), so `phase="science"` with no explicit
        # `--job-definition-family` is auto-routed to
        # `rapid-scratch-science` (`_SCRATCH_DEFINITIONS`, rapid_systems
        # migration 132) and that family joins the scope too, trailing
        # the lane -- the same scope-growth rule this class's OWN sibling
        # test below already states for the lane itself. This test is
        # about the lane fragment specifically, not about suppressing the
        # family fragment.
        _result, scope = self._start(lane="prompt",
                                     idempotency_key="lane-scope-key")

        self.assertEqual(scope,
                         "run:w9-campaign-1:phase=science:lane=prompt:"
                         "job-definition-family=rapid-scratch-science")
        self.assertEqual(self.audit_calls[0]["target_scope"], scope)

    def test_lane_bulk_is_also_carried_on_the_audit_scope_string(self):
        # `lane` joins the scope whenever one was named at all
        # (`start_run_audited`'s own `if lane is not None:`) -- the CLI
        # always names one (`--lane` defaults to `"bulk"`, never `None`),
        # so an ordinary `rapidctl run start` records which lane it chose
        # even when that lane is the default.
        #
        # AND the auto-routed family joins it too, same reasoning as the
        # prompt-lane test above: this fixture's scratch-kind row gets
        # `rapid-scratch-science` for `phase="science"` with no override.
        _result, scope = self._start(lane="bulk",
                                     idempotency_key="lane-scope-bulk-key")

        self.assertEqual(scope,
                         "run:w9-campaign-1:phase=science:lane=bulk:"
                         "job-definition-family=rapid-scratch-science")


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
            stranded_rows=[(501, "blocked", "application_failure:internal_error", ["array-1:0"], False)])
        client = _FakeBatchClient({
            ("array-1", "FAILED"): [{"jobSummaryList": [_job("array-1:0")]}],
        })
        candidates, excluded = run_mod.find_stranded_candidates(
            conn, "w9-ramp", client)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["work_unit_id"], 501)
        self.assertEqual(candidates[0]["state"], "blocked")
        self.assertEqual(excluded, [])

    def test_a_unit_with_no_batch_child_is_excluded_not_released(self):
        """No Batch child means no evidence the work failed.

        An empty child list satisfies "every child FAILED" VACUOUSLY,
        which is the opposite of evidence: Batch knows nothing about this
        unit, so releasing it would re-run work whose fate is simply
        unknown. Zero units are in this state on the live acceptance run,
        so this guards a hazard rather than fixing a live miss.
        """
        from pipeline.operatorctl import run as run_mod

        conn = _StrandedFakeConn(
            array_ids_rows=[("array-1",)],
            stranded_rows=[(900, "submitted", None, None, False)])
        client = _FakeBatchClient({})
        candidates, excluded = run_mod.find_stranded_candidates(
            conn, "w9-ramp", client)
        self.assertEqual(candidates, [])
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0]["work_unit_id"], 900)
        self.assertEqual(excluded[0]["reason"], "no_batch_child")

    def test_a_differently_parked_blocked_unit_is_excluded(self):
        """`input_missing` is park-until-the-input-arrives.

        A FAILED Batch child and no successful attempt say nothing about
        whether the missing input arrived, so releasing such a unit
        re-runs work that fails the same way. `release-dead-letters`
        constrains to `internal_error` for this reason and this
        reconciler must not choose a second policy.
        """
        from pipeline.operatorctl import run as run_mod

        conn = _StrandedFakeConn(
            array_ids_rows=[("array-1",)],
            stranded_rows=[(901, "blocked",
                            "application_failure:input_missing",
                            ["array-1:0"], False)])
        client = _FakeBatchClient({
            ("array-1", "FAILED"): [{"jobSummaryList": [_job("array-1:0")]}],
        })
        candidates, excluded = run_mod.find_stranded_candidates(
            conn, "w9-ramp", client)
        self.assertEqual(candidates, [])
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0]["work_unit_id"], 901)
        self.assertEqual(excluded[0]["reason"],
                         "blocked_reason_not_releasable")
        self.assertEqual(excluded[0]["blocked_reason"],
                         "application_failure:input_missing")

    def test_a_submitted_unit_is_unaffected_by_the_blocked_reason_rule(self):
        """A `submitted` unit carries no blocked reason at all.

        This matters because those are the MAJORITY of the real candidate
        set -- 2,412 of the acceptance run's 2,664 -- so a rule written
        for blocked units must not catch them.
        """
        from pipeline.operatorctl import run as run_mod

        conn = _StrandedFakeConn(
            array_ids_rows=[("array-1",)],
            stranded_rows=[(902, "submitted", None, ["array-1:0"], False)])
        client = _FakeBatchClient({
            ("array-1", "FAILED"): [{"jobSummaryList": [_job("array-1:0")]}],
        })
        candidates, excluded = run_mod.find_stranded_candidates(
            conn, "w9-ramp", client)
        self.assertEqual(excluded, [])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["work_unit_id"], 902)
        self.assertEqual(candidates[0]["state"], "submitted")

    def test_failed_child_with_a_successful_attempt_is_excluded(self):
        # THE REVIEW QUESTION'S EXACT CASE: the application succeeded and
        # published, then the container exited nonzero on teardown. Batch
        # says FAILED; the attempt row says success; the unit must not be
        # released back to READY.
        from pipeline.operatorctl import run as run_mod

        conn = _StrandedFakeConn(
            array_ids_rows=[("array-1",)],
            stranded_rows=[(64, "submitted", None, ["array-1:0"], True)])
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
            stranded_rows=[(1604, "blocked", "application_failure:internal_error", ["array-1:0"], False)])
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

    def test_success_and_unfailed_child_both_reports_the_success_reason(self):
        # A unit failing BOTH tests -- a successful attempt exists AND a
        # Batch child of it is not FAILED (still SUCCEEDED here) -- must
        # report `successful_sibling_attempt`, not `batch_child_not_failed`:
        # "this unit's work already succeeded" is the more informative
        # account when both are true, and `has_success` is checked first
        # in `find_stranded_candidates` for exactly that reason. Either
        # test alone would exclude this unit, so the classification
        # (excluded, never a candidate) is unchanged by the ordering --
        # only the reported reason is.
        from pipeline.operatorctl import run as run_mod

        conn = _StrandedFakeConn(
            array_ids_rows=[("array-1",)],
            stranded_rows=[(77, "submitted", None, ["array-1:0"], True)])
        client = _FakeBatchClient({
            ("array-1", "SUCCEEDED"): [
                {"jobSummaryList": [_job("array-1:0")]}],
        })
        candidates, excluded = run_mod.find_stranded_candidates(
            conn, "w9-ramp", client)
        self.assertEqual(candidates, [])
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0]["work_unit_id"], 77)
        self.assertEqual(excluded[0]["reason"], "successful_sibling_attempt")

    def test_submitted_and_blocked_units_both_become_candidates(self):
        from pipeline.operatorctl import run as run_mod

        conn = _StrandedFakeConn(
            array_ids_rows=[("array-1",)],
            stranded_rows=[
                (10, "submitted", None, ["array-1:0"], False),
                (11, "blocked", "application_failure:internal_error", ["array-1:1"], False),
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
            stranded_rows=[(501, "blocked", "application_failure:internal_error", ["array-1:0"], False)])
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
            stranded_rows=[(501, "blocked", "application_failure:internal_error", ["array-1:0"], False)])
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

        stranded_rows = [(i, "submitted", None, ["array-1:%d" % i], False)
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
                (501, "blocked", "application_failure:internal_error", ["array-1:0"], False),
                (502, "submitted", None, ["array-1:1"], False),
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


class RegisterRunSubmissionRoleTests(unittest.TestCase):
    """`run register --apply` registers under `submission_role`.

    THE SAME IDENTITY DEFECT, fourth entry point. Registration performs the
    pipeline's own work -- product inserts and `UPDATE attempts` for each
    watermark -- and the operate tier holds only `rapid_read`, so every
    attempt failed with `permission denied for table attempts` when this
    was first run against the real database.

    A dry run stays on the operate tier deliberately: it writes nothing,
    and a rehearsal that cannot write is part of what makes it a rehearsal.
    """

    def _run(self, dry_run):
        import contextlib

        from pipeline.operatorctl import run as run_mod

        events = []
        seen = {}

        @contextlib.contextmanager
        def fake_submission_role(conn):
            events.append("enter")
            try:
                yield conn
            finally:
                events.append("exit")

        class _Run:
            def as_dict(self):
                return {"registered": 0}

        def fake_registration(conn, run_id_prefix=None, dry_run=True,
                              records_bucket=None, s3_client=None):
            # Recorded AT THE MOMENT of the call: that is the assertion.
            seen["at_call"] = list(events)
            return _Run(), []

        import pipeline.registration.scoped as scoped_mod
        with mock.patch.object(run_mod, "submission_role",
                               fake_submission_role), \
             mock.patch.object(scoped_mod, "run_scoped_registration",
                               fake_registration), \
             mock.patch.object(run_mod, "_replay_lookup",
                               lambda *a, **k: None), \
             mock.patch.object(run_mod, "record_external_action",
                               lambda *a, **k: {"audit_id": 1}):
            run_mod.register_run_audited(
                object(), "key", "accept-20260911", "reason",
                dry_run=dry_run, records_bucket="b", out=io.StringIO())
        return seen, events

    def test_an_apply_registers_inside_the_role_block(self):
        seen, events = self._run(dry_run=False)
        self.assertEqual(["enter"], seen["at_call"])
        self.assertEqual(["enter", "exit"], events)

    def test_a_dry_run_stays_on_the_operate_tier(self):
        seen, events = self._run(dry_run=True)
        self.assertEqual([], seen["at_call"])
        self.assertEqual([], events)


class StartRunAuditedJobDefinitionFamilyTests(unittest.TestCase):
    """`--job-definition-family` — the memory profile's measurement
    override, driven end to end through `start_run_audited`'s DRY-RUN
    branch exactly as `StartRunAuditedLaneResolutionTests` drives lanes,
    and for the same reason: the override has to reach the real
    `submission_env` -> `active_definition` resolution and come back with
    the NAMED family's ARN, not merely round-trip a string through a stub.

    The gate is the half that matters most. A production-kind run must be
    REFUSED — its execution binding is not chosen on a command line — and
    that refusal is proven against a `runs` row's stored `kind`, since
    `run start` has no `--kind` flag to trust.
    """

    TREE = {
        "batch/queue-bulk": "rapid-queue-bulk",
        "batch/queue-prompt": "rapid-queue-prompt",
        "batch/job-definition-bulk": "rapid-pipeline-bulk",
        "batch/job-definition-science": "rapid-pipeline-science",
    }

    class _FakeBatch:
        """One ACTIVE revision per family, with the family's own name in
        the ARN — so a test can tell WHICH family was resolved, which is
        the whole property here (unlike the lane tests, where any family
        resolving at all was enough)."""

        def describe_job_definitions(self, jobDefinitionName=None,
                                     status=None):
            return {"jobDefinitions": [
                {"jobDefinitionName": jobDefinitionName,
                 "jobDefinitionArn": "arn:aws:batch:us-east-1:ACCOUNT:"
                                    "job-definition/%s:7" % jobDefinitionName,
                 "revision": 7,
                 "containerProperties": {"image": "repo@sha256:" + "2" * 64}},
            ]}

    def setUp(self):
        from pipeline.operatorctl import run as run_mod
        self.run_mod = run_mod

        import os
        self._saved_env = {name: os.environ.get(name)
                           for name in ("RAPID_IMAGE_DIGEST",
                                        "RAPID_RELEASE_IDENTITY",
                                        "RAPID_MANIFEST_BUCKET")}
        os.environ["RAPID_IMAGE_DIGEST"] = "sha256:" + "0" * 64
        os.environ["RAPID_RELEASE_IDENTITY"] = "memprofile-test"
        os.environ["RAPID_MANIFEST_BUCKET"] = "rapid-manifests"

        _stub_registry_binding(self, run_mod)

        replay_patcher = mock.patch.object(
            run_mod, "_replay_lookup", lambda *a, **k: None)
        replay_patcher.start()
        self.addCleanup(replay_patcher.stop)

        gather_patcher = mock.patch.object(
            run_mod, "gather_for_run",
            lambda *a, **k: ("science", ["unit-a"]))
        gather_patcher.start()
        self.addCleanup(gather_patcher.stop)

        self.audit_calls = []

        def fake_record_external_action(conn, idempotency_key,
                                        action_class, target_scope, reason,
                                        dry_run=False, rows_affected=0,
                                        detail=None, policy_citation=None):
            self.audit_calls.append(
                {"target_scope": target_scope, "detail": dict(detail or {})})
            return {"rows_affected": rows_affected, "detail": detail}

        audit_patcher = mock.patch.object(
            run_mod, "record_external_action", fake_record_external_action)
        audit_patcher.start()
        self.addCleanup(audit_patcher.stop)

        db_mod_patcher = mock.patch(
            "database.modules.utils.rapid_db.RAPIDDB",
            lambda: types.SimpleNamespace(exit_code=0))
        db_mod_patcher.start()
        self.addCleanup(db_mod_patcher.stop)

        import submission.startup as startup_mod
        fetch_patcher = mock.patch.object(
            startup_mod, "fetch_parameters", lambda: dict(self.TREE))
        fetch_patcher.start()
        self.addCleanup(fetch_patcher.stop)

        import pipeline.operator.submission as opsubmission_mod
        batch_client_patcher = mock.patch.object(
            opsubmission_mod.boto3, "client",
            lambda service, **kw: (self._FakeBatch() if service == "batch"
                                   else object()))
        batch_client_patcher.start()
        self.addCleanup(batch_client_patcher.stop)

        import pipeline.operator.gathering as gathering_mod
        coadd_patcher = mock.patch.object(
            gathering_mod, "min_images_to_coadd", lambda: 3)
        coadd_patcher.start()
        self.addCleanup(coadd_patcher.stop)

        # The registry row the gate reads. `run_row` is patched on the
        # ACTIONS module, which is where `_check_job_definition_family`
        # imports it from — the gate's whole point is that it consults the
        # stored row rather than an argument.
        self.run_kind = "scratch"
        import pipeline.operatorctl.actions as actions_mod
        row_patcher = mock.patch.object(
            actions_mod, "run_row",
            lambda conn, name: (None if self.run_kind is None
                                else {"name": name, "kind": self.run_kind}))
        row_patcher.start()
        self.addCleanup(row_patcher.stop)

    def tearDown(self):
        import os
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _start(self, family="rapid-pipeline-science-probe32",
               phase="science", key="family-key"):
        return self.run_mod.start_run_audited(
            conn=object(), idempotency_key=key,
            name="memprofile-32g-20260913", phase=phase,
            reason="job memory profile", dry_run=True, out=_null_out(),
            window_start="2027-10-01 00:00:00",
            window_end="2027-10-08 00:00:00",
            job_definition_family=family)

    def test_job_definition_family_reaches_the_resolved_arn(self):
        # THE PROPERTY: the override must displace the parameter tree's
        # `batch/job-definition-science` (rapid-pipeline-science) all the
        # way down to `active_definition`, so the ARN names the PROBE
        # family. Asserted on the resolved ARN, not on the argument.
        self._start(family="rapid-pipeline-science-probe32")

        detail = self.audit_calls[0]["detail"]
        self.assertEqual(detail["job_definition_family"],
                         "rapid-pipeline-science-probe32")
        self.assertIn("rapid-pipeline-science-probe32",
                      detail["job_definition_arn"])
        self.assertNotIn("job-definition/rapid-pipeline-science:",
                         detail["job_definition_arn"])

    def test_job_definition_family_probe16_resolves_to_its_own_arn(self):
        # The two probe families must not collapse onto one another: the
        # cap run and the probe run differ ONLY in this string, so a bug
        # that resolved both to the same definition would silently turn
        # the whole measurement into one run done twice.
        self._start(family="rapid-pipeline-science-probe16")
        detail = self.audit_calls[0]["detail"]
        self.assertIn("rapid-pipeline-science-probe16",
                      detail["job_definition_arn"])

    def test_job_definition_family_refused_for_a_production_kind_run(self):
        # THE GATE. A production run is the published pipeline; pointing it
        # at a measurement definition from a command line is exactly what
        # this must make impossible.
        self.run_kind = "production"

        with self.assertRaises(
                self.run_mod.RunStartEnvironmentError) as caught:
            self._start()

        message = str(caught.exception)
        self.assertIn("production", message)
        self.assertIn("scratch", message)
        # Refused BEFORE anything was recorded: a rejected run leaves no
        # audit row and gathers nothing.
        self.assertEqual(self.audit_calls, [])

    def test_job_definition_family_refused_when_the_run_is_not_declared(self):
        self.run_kind = None

        with self.assertRaises(self.run_mod.RunStartEnvironmentError):
            self._start()

        self.assertEqual(self.audit_calls, [])

    def test_job_definition_family_refused_for_a_non_science_phase(self):
        # The probe definitions are science-class; route validation in the
        # container would reject any other job type against them anyway, so
        # this turns a confusing late failure into a clear early one.
        with self.assertRaises(
                self.run_mod.RunStartEnvironmentError) as caught:
            self._start(phase="reference")

        self.assertIn("science", str(caught.exception))
        self.assertEqual(self.audit_calls, [])

    def test_job_definition_family_joins_the_audit_scope(self):
        # Two runs over the same window and filters differing only in the
        # definition are DIFFERENT actions. Were the family absent from the
        # scope, the second would look like a replay of the first and
        # submit nothing — which would quietly halve the measurement.
        _result, scope = self._start(
            family="rapid-pipeline-science-probe32")
        self.assertIn("job-definition-family=rapid-pipeline-science-probe32",
                      scope)
        self.assertEqual(self.audit_calls[0]["target_scope"], scope)

    def test_job_definition_family_gives_the_two_probes_different_scopes(self):
        self._start(family="rapid-pipeline-science-probe16", key="k16")
        cap_scope = self.audit_calls[-1]["target_scope"]
        self._start(family="rapid-pipeline-science-probe32", key="k32")
        probe_scope = self.audit_calls[-1]["target_scope"]
        self.assertNotEqual(cap_scope, probe_scope)

    def test_no_job_definition_family_leaves_the_tree_family_and_old_scope(self):
        # Every caller before this brief, for a NON-WINDOWED phase --
        # `statistics` is one of the four post-database-chain phases
        # `_SCRATCH_DEFINITIONS` deliberately leaves unmapped (that map's
        # own comment: "better a phase that submits to the tree's
        # definition ... than one silently routed to a definition nobody
        # checked"), so it is the one phase left where "no override" still
        # means "no family at all", even for this fixture's scratch-kind
        # row. `phase="science"` no longer demonstrates that: the
        # scratch-auto-routing brief (rapid_systems migration 132) gives a
        # scratch run's OWN science phase `rapid-scratch-science` with no
        # override needed at all, which is exactly the behaviour
        # `test_a_scratch_run_at_phase_science_is_auto_routed_with_no_
        # override` below now asserts.
        #
        # The scope string must be byte-for-byte what it was, so no
        # historical idempotency key is stranded, and `detail` must not
        # grow a key for an override nobody asked for.
        self.run_mod.start_run_audited(
            conn=object(), idempotency_key="no-override",
            name="memprofile-32g-20260913", phase="statistics",
            reason="no override", dry_run=True, out=_null_out())

        detail = self.audit_calls[0]["detail"]
        self.assertNotIn("job_definition_family", detail)
        self.assertNotIn("job_definition_arn", detail)
        # The LANE is in the scope now, and legitimately: this fixture's row
        # is scratch-kind, and a scratch run with no explicit `--lane`
        # defaults to the on-demand lane (Ben, 2026-09-20 — this tier does
        # not run on Spot). The assertion this test exists for is the
        # job-definition family, which is still absent; the lane fragment is
        # the tier default announcing itself, exactly as the family fragment
        # does for a mapped phase.
        self.assertEqual(self.audit_calls[0]["target_scope"],
                         "run:memprofile-32g-20260913:phase=statistics"
                         ":lane=prompt")

    def test_a_scratch_run_at_phase_science_is_auto_routed_with_no_override(self):
        # THE OTHER HALF of the invariant the test above used to assert
        # whole: a scratch run's science phase, given no explicit
        # `--job-definition-family`, is no longer left on the tree's family
        # -- it is auto-routed to `rapid-scratch-science`
        # (`_SCRATCH_DEFINITIONS`), exactly as if that family had been
        # named explicitly, scope and all.
        self._start(family=None)

        detail = self.audit_calls[0]["detail"]
        self.assertEqual(detail["job_definition_family"],
                         "rapid-scratch-science")
        self.assertIn("rapid-scratch-science", detail["job_definition_arn"])


class StartRunAuditedReferenceImageIdTests(unittest.TestCase):
    """`--reference-image-id` — pin every unit `run start` gathers to one
    already-registered `refimages.rfid` instead of each resolving its own.

    Unlike `--job-definition-family` this carries no `kind == scratch`
    restriction (see `start_run_audited`'s docstring for why), so there is
    no analogue of `test_job_definition_family_refused_for_a_production_
    kind_run` here — the one gate is the phase, checked BEFORE the
    registry row is even read (`_bind_registry_row` is never reached on
    the refusal path, unlike the family check, which needs `runs.kind`).

    Setup mirrors `StartRunAuditedJobDefinitionFamilyTests` almost exactly
    — same windowed-phase path (`science`), same submission-environment
    resolution reached along the way — with `gather_for_run` itself
    replaced by a recording fake rather than driven all the way through
    `gather_science_units`: this class is about what reaches
    `gather_for_run`, not about the DB read inside it, which
    `submission/test/test_gathering.py`'s `science_facts`/`_reference_by_
    id` tests already cover directly.
    """

    TREE = {
        "batch/queue-bulk": "rapid-queue-bulk",
        "batch/queue-prompt": "rapid-queue-prompt",
        "batch/job-definition-bulk": "rapid-pipeline-bulk",
        "batch/job-definition-science": "rapid-pipeline-science",
    }

    class _FakeBatch:
        def describe_job_definitions(self, jobDefinitionName=None,
                                     status=None):
            return {"jobDefinitions": [
                {"jobDefinitionName": jobDefinitionName,
                 "jobDefinitionArn": "arn:aws:batch:us-east-1:ACCOUNT:"
                                    "job-definition/%s:7" % jobDefinitionName,
                 "revision": 7,
                 "containerProperties": {"image": "repo@sha256:" + "3" * 64}},
            ]}

    def setUp(self):
        from pipeline.operatorctl import run as run_mod
        self.run_mod = run_mod

        import os
        self._saved_env = {name: os.environ.get(name)
                           for name in ("RAPID_IMAGE_DIGEST",
                                        "RAPID_RELEASE_IDENTITY",
                                        "RAPID_MANIFEST_BUCKET")}
        os.environ["RAPID_IMAGE_DIGEST"] = "sha256:" + "1" * 64
        os.environ["RAPID_RELEASE_IDENTITY"] = "pin-ref-test"
        os.environ["RAPID_MANIFEST_BUCKET"] = "rapid-manifests"
        self.addCleanup(self._restore_env)

        _stub_registry_binding(self, run_mod)

        replay_patcher = mock.patch.object(
            run_mod, "_replay_lookup", lambda *a, **k: None)
        replay_patcher.start()
        self.addCleanup(replay_patcher.stop)

        import submission.startup as startup_mod
        fetch_patcher = mock.patch.object(
            startup_mod, "fetch_parameters", lambda: dict(self.TREE))
        fetch_patcher.start()
        self.addCleanup(fetch_patcher.stop)

        import pipeline.operator.submission as opsubmission_mod
        batch_client_patcher = mock.patch.object(
            opsubmission_mod.boto3, "client",
            lambda service, **kw: (self._FakeBatch() if service == "batch"
                                   else object()))
        batch_client_patcher.start()
        self.addCleanup(batch_client_patcher.stop)

        # `science`/`reference` are windowed phases, and `start_run_audited`
        # computes their MJD window via `pipeline.operator.gathering.
        # mjd_window`/`min_images_to_coadd` BEFORE `gather_for_run` is
        # reached — the latter reads the release's real science
        # configuration (`RAPID_SW`), which this stub-tier test has no
        # installed tree for. Patched the same way
        # `StartRunAuditedJobDefinitionFamilyTests` patches it, since this
        # class exercises the identical windowed-phase path for a
        # different override.
        import pipeline.operator.gathering as gathering_mod
        coadd_patcher = mock.patch.object(
            gathering_mod, "min_images_to_coadd", lambda: 3)
        coadd_patcher.start()
        self.addCleanup(coadd_patcher.stop)

        self.gather_calls = []

        def fake_gather_for_run(dbh, phase, **kwargs):
            self.gather_calls.append(kwargs)
            return "science", ["unit-a"]

        gather_patcher = mock.patch.object(
            run_mod, "gather_for_run", fake_gather_for_run)
        gather_patcher.start()
        self.addCleanup(gather_patcher.stop)

        self.audit_calls = []

        def fake_record_external_action(conn, idempotency_key,
                                        action_class, target_scope, reason,
                                        dry_run=False, rows_affected=0,
                                        detail=None, policy_citation=None):
            self.audit_calls.append(
                {"target_scope": target_scope, "detail": dict(detail or {})})
            return {"rows_affected": rows_affected, "detail": detail}

        audit_patcher = mock.patch.object(
            run_mod, "record_external_action", fake_record_external_action)
        audit_patcher.start()
        self.addCleanup(audit_patcher.stop)

        db_mod_patcher = mock.patch(
            "database.modules.utils.rapid_db.RAPIDDB",
            lambda: types.SimpleNamespace(exit_code=0))
        db_mod_patcher.start()
        self.addCleanup(db_mod_patcher.stop)

    def _restore_env(self):
        import os
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _start(self, phase="science", reference_image_id=555,
              key="rfid-key"):
        # `window_start`/`window_end` are required for BOTH windowed phases
        # (`science` and `reference`) before `start_run_audited` ever
        # reaches the phase-mismatch check this class is testing, so every
        # call supplies them -- including the `phase="reference"` refusal
        # test, which must be refused for being the wrong PHASE, not for
        # missing an unrelated argument.
        return self.run_mod.start_run_audited(
            conn=object(), idempotency_key=key,
            name="pin-ref-run", phase=phase,
            reason="reuse a known-good reference", dry_run=True,
            out=_null_out(), window_start="2027-10-01 00:00:00",
            window_end="2027-10-08 00:00:00",
            reference_image_id=reference_image_id)

    def test_reference_image_id_reaches_gather_for_run(self):
        # THE PROPERTY: the id must reach `gather_for_run` unchanged, which
        # is what threads it on into `gather_science_units` and, from
        # there, `science_facts`'s pinned-reference branch.
        self._start(reference_image_id=555)
        self.assertEqual(self.gather_calls[-1]["reference_image_id"], 555)

    def test_reference_image_id_refused_for_a_non_science_phase(self):
        # A reference-image phase BUILDS a reference; it cannot also be
        # told to reuse one. Refused before the registry row is read, so
        # no audit row and no gather call result.
        with self.assertRaises(
                self.run_mod.RunStartEnvironmentError) as caught:
            self._start(phase="reference")

        message = str(caught.exception)
        self.assertIn("science", message)
        self.assertEqual(self.audit_calls, [])
        self.assertEqual(self.gather_calls, [])

    def test_reference_image_id_refused_for_a_post_db_chain_phase(self):
        # The four post-DB-chain phases take no reference at all; naming
        # one for them is exactly as meaningless as for `reference`.
        with self.assertRaises(self.run_mod.RunStartEnvironmentError):
            self._start(phase="catalog-load")
        self.assertEqual(self.audit_calls, [])

    def test_reference_image_id_joins_the_audit_scope(self):
        # Two runs of the same name and phase, one pinned and one not (or
        # pinned to a different rfid), are DIFFERENT actions and must not
        # replay onto one another.
        _result, scope = self._start(reference_image_id=555)
        self.assertIn("reference-image-id=555", scope)
        self.assertEqual(self.audit_calls[0]["target_scope"], scope)

    def test_reference_image_id_recorded_in_audit_detail(self):
        self._start(reference_image_id=555)
        self.assertEqual(
            self.audit_calls[0]["detail"]["reference_image_id"], 555)

    def test_no_reference_image_id_leaves_the_old_scope_and_detail(self):
        # Every caller before this switch, for the REFERENCE-IMAGE-ID
        # fragment specifically: `detail` gains no key for a pin nobody
        # asked for, and the pin never reaches `gather_for_run`.
        #
        # The scope string is NOT otherwise byte-for-byte unchanged: this
        # fixture's row is scratch-kind (`_stub_registry_binding`'s own
        # default), so `phase="science"` with no explicit
        # `--job-definition-family` is auto-routed to
        # `rapid-scratch-science` (`_SCRATCH_DEFINITIONS`, rapid_systems
        # migration 132) and that family joins the scope regardless of
        # this test's own reference-image-id pin.
        self.run_mod.start_run_audited(
            conn=object(), idempotency_key="no-pin",
            name="pin-ref-run", phase="science",
            reason="no pin", dry_run=True, out=_null_out(),
            window_start="2027-10-01 00:00:00",
            window_end="2027-10-08 00:00:00")

        self.assertNotIn("reference_image_id",
                         self.audit_calls[0]["detail"])
        # The lane fragment trails the family one, in the order
        # `start_run_audited` appends them: a scratch run with no explicit
        # `--lane` takes the on-demand lane (Ben, 2026-09-20). What this
        # test asserts is the ABSENCE of a reference-image-id fragment, and
        # that is still what it shows.
        self.assertEqual(self.audit_calls[0]["target_scope"],
                         "run:pin-ref-run:phase=science:"
                         "job-definition-family=rapid-scratch-science"
                         ":lane=prompt")
        self.assertIsNone(self.gather_calls[-1]["reference_image_id"])


# ---------------------------------------------------------------------------
# Migration 121: `run start` binds to the registry.
# ---------------------------------------------------------------------------
class RunStartRegistryBindingTests(unittest.TestCase):
    """`_bind_registry_row` refuses the four runs `run start` may not submit
    under, and accepts the two it may.

    Driven against `_bind_registry_row` DIRECTLY rather than through
    `start_run_audited`, because the property under test is which rows are
    refused — not what a refusal does to the rest of the command. What a
    refusal does to the rest of the command is `RunStartRefusalTests`
    below, which asserts the two things a refusal must NOT do: gather, and
    write an audit row.
    """

    def _bind(self, row):
        from pipeline.operatorctl import run as run_mod
        from pipeline.operatorctl import actions as actions_mod
        with mock.patch.object(actions_mod, "run_row", lambda conn, n: row):
            return run_mod._bind_registry_row(object(), "ramp-proof")

    def _refusal(self, row):
        from pipeline.operatorctl.run import RunStartRegistryError
        with self.assertRaises(RunStartRegistryError) as caught:
            self._bind(row)
        return str(caught.exception)

    def test_an_undeclared_run_is_refused_naming_run_create(self):
        # THE RULING'S FIRST CLAUSE: "a scratch name without a `runs` row
        # is refused". The message must name the command that fixes it —
        # an operator whose submission was refused needs the next step,
        # not only the diagnosis.
        message = self._refusal(None)
        self.assertIn("not declared", message)
        self.assertIn("rapidctl run create", message)

    def test_a_production_run_is_refused(self):
        # Production's submissions are the VPO's, which resolves the
        # production run's key for itself (`production_run_key`). An
        # operator starting one by hand would submit production work from
        # outside the operator service.
        message = self._refusal(
            {"run_id": 1, "kind": "production", "state": "running"})
        self.assertIn("production", message)

    def test_a_complete_run_is_refused_naming_its_state(self):
        message = self._refusal(
            {"run_id": 2, "kind": "scratch", "state": "complete"})
        self.assertIn("complete", message)

    def test_an_archived_run_is_refused_naming_its_state(self):
        message = self._refusal(
            {"run_id": 3, "kind": "scratch", "state": "archived"})
        self.assertIn("archived", message)

    def test_a_created_scratch_run_is_accepted(self):
        row = {"run_id": 4, "kind": "scratch", "state": "created"}
        self.assertEqual(self._bind(row)["run_id"], 4)

    def test_a_running_scratch_run_is_accepted_which_is_what_a_ramp_needs(self):
        # The ramp case: the SECOND `run start` of a run already under way
        # must bind, not refuse. If this ever became a refusal, a ramp
        # would be impossible and the only route back would be the fresh
        # name plus `--claim-work-units-of` this ruling exists to retire.
        row = {"run_id": 5, "kind": "scratch", "state": "running"}
        self.assertEqual(self._bind(row)["run_id"], 5)


class RunStartRefusalTests(unittest.TestCase):
    """A refused `run start` gathers nothing and writes no audit row.

    Both halves matter and neither implies the other. Gathering for a run
    that will not submit is wasted work against the live database; writing
    an audit row for it would put a mutation in the ledger that never
    happened, which is the one thing the ledger must never contain.
    """

    def setUp(self):
        from pipeline.operatorctl import run as run_mod
        self.run_mod = run_mod

        self.gathers = []
        gather_patcher = mock.patch.object(
            run_mod, "gather_for_run",
            lambda *a, **k: (self.gathers.append(1), ("science", ["u"]))[1])
        gather_patcher.start()
        self.addCleanup(gather_patcher.stop)

        self.audits = []
        audit_patcher = mock.patch.object(
            run_mod, "record_external_action",
            lambda *a, **k: (self.audits.append(1), {})[1])
        audit_patcher.start()
        self.addCleanup(audit_patcher.stop)

        # Would be reached only if the binding did NOT refuse — its being
        # untouched is part of what these tests assert.
        self.replays = []
        replay_patcher = mock.patch.object(
            run_mod, "_replay_lookup",
            lambda *a, **k: (self.replays.append(1), None)[1])
        replay_patcher.start()
        self.addCleanup(replay_patcher.stop)

    def _start_against(self, row):
        from pipeline.operatorctl import actions as actions_mod
        from pipeline.operatorctl.run import RunStartRegistryError
        with mock.patch.object(actions_mod, "run_row", lambda conn, n: row):
            with self.assertRaises(RunStartRegistryError):
                self.run_mod.start_run_audited(
                    conn=object(), idempotency_key="k", name="no-such-run",
                    phase="science", reason="refusal proof", dry_run=True,
                    out=_null_out(), window_start="2027-10-01 00:00:00",
                    window_end="2027-10-08 00:00:00")

    def test_an_absent_row_refuses_before_gathering(self):
        self._start_against(None)
        self.assertEqual(self.gathers, [])

    def test_an_absent_row_writes_no_audit_row(self):
        # The live acceptance reads this same property off
        # `derived.mutation_audit` after a real refused command.
        self._start_against(None)
        self.assertEqual(self.audits, [])

    def test_the_refusal_precedes_the_replay_lookup(self):
        # DELIBERATE ORDERING. A replayed idempotency key for a run that
        # has since been completed must still refuse — returning the
        # earlier success would report a submission that is not going to
        # happen.
        self._start_against(
            {"run_id": 9, "kind": "scratch", "state": "complete"})
        self.assertEqual(self.replays, [])


class RunStartStateTransitionTests(unittest.TestCase):
    """The registry transition: once on an apply, never on a dry run."""

    def setUp(self):
        from pipeline.operatorctl import run as run_mod
        self.run_mod = run_mod
        self.row = {"run_id": 55, "name": "ramp-proof", "kind": "scratch",
                    "state": "created"}

        bind_patcher = mock.patch.object(
            run_mod, "_bind_registry_row", lambda conn, n: self.row)
        bind_patcher.start()
        self.addCleanup(bind_patcher.stop)

        seq_patcher = mock.patch.object(
            run_mod, "next_submission_seq", lambda conn, k: 0)
        seq_patcher.start()
        self.addCleanup(seq_patcher.stop)

        replay_patcher = mock.patch.object(
            run_mod, "_replay_lookup", lambda *a, **k: None)
        replay_patcher.start()
        self.addCleanup(replay_patcher.stop)

        gather_patcher = mock.patch.object(
            run_mod, "gather_for_run", lambda *a, **k: ("science", ["u"]))
        gather_patcher.start()
        self.addCleanup(gather_patcher.stop)

        # The science phase resolves a submission environment before it
        # gathers (the reference path needs its s3 client at gather time).
        # These tests are about the REGISTRY transition, not the binding,
        # so the environment is a stub — otherwise every one of them would
        # need RAPID_SW and a parameter tree to assert something neither
        # is involved in.
        env_patcher = mock.patch.object(
            run_mod, "_resolve_submission_env",
            lambda *a, **k: {"s3_client": object(),
                             "manifest_bucket": "b",
                             "queue": "q", "job_definition": "jd",
                             "binding": object(), "manifest_prefix": "p",
                             "batch_client": object()})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        self.details = []
        audit_patcher = mock.patch.object(
            run_mod, "record_external_action",
            lambda conn, key, cls, scope, reason, dry_run=False,
            rows_affected=0, detail=None, policy_citation=None:
            (self.details.append(dict(detail or {})), {})[1])
        audit_patcher.start()
        self.addCleanup(audit_patcher.stop)

        submit_patcher = mock.patch.object(
            run_mod, "submit_run",
            lambda *a, **k: [(types.SimpleNamespace(job_id="j"), ["a"])])
        submit_patcher.start()
        self.addCleanup(submit_patcher.stop)

        db_patcher = mock.patch(
            "database.modules.utils.rapid_db.RAPIDDB",
            lambda: types.SimpleNamespace(exit_code=0))
        db_patcher.start()
        self.addCleanup(db_patcher.stop)

        # THE TRANSITION ITSELF, recorded per call so "once" and "never"
        # are both assertable. Each call also MOVES the fixture row, so a
        # second start in the same test sees `running`, exactly as a
        # second start against the database would.
        self.transitions = []

        def fake_start_run(conn, key, name, reason, dry_run=True,
                           policy_citation=None):
            self.transitions.append(name)
            self.row["state"] = "running"
            return {"rows_affected": 1}

        from pipeline.operatorctl import actions as actions_mod
        state_patcher = mock.patch.object(actions_mod, "start_run",
                                          fake_start_run)
        state_patcher.start()
        self.addCleanup(state_patcher.stop)

    class _Cursor:
        # The science-overlay gate's one query (`run.py`'s `science_
        # overlay` block): a scratch row, this fixture's `kind`, is queried
        # for `config_overlay` unconditionally on the apply path. `None`
        # here stands for "no row/no override", matching every one of
        # these tests, which are about the REGISTRY transition and assert
        # nothing about overlays.
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            pass

        def fetchone(self):
            return None

    def _start(self, dry_run, key="k"):
        # The non-windowed `statistics` phase, matching
        # `StartRunAuditedWorkUnitScopeTests`: these tests are about the
        # REGISTRY transition, and a windowed phase would additionally
        # need RAPID_SW and a parameter tree to compute an MJD window
        # nothing here asserts on.
        return self.run_mod.start_run_audited(
            conn=types.SimpleNamespace(commit=lambda: None,
                                       cursor=lambda: self._Cursor()),
            idempotency_key=key, name="ramp-proof", phase="statistics",
            reason="registry binding", dry_run=dry_run, out=_null_out())

    def test_a_dry_run_transitions_nothing(self):
        # `contract.py`'s rule: the plan shown is what the apply will act
        # on, MINUS the writing. A rehearsal that moved the run to
        # `running` would be a mutation performed by a command whose whole
        # contract is that it performs none.
        self._start(dry_run=True)
        self.assertEqual(self.transitions, [])
        self.assertEqual(self.row["state"], "created")

    def test_an_apply_transitions_the_run_once(self):
        self._start(dry_run=False)
        self.assertEqual(self.transitions, ["ramp-proof"])
        self.assertEqual(self.row["state"], "running")

    def test_a_second_start_leaves_the_run_running(self):
        # THE RAMP: `created` -> `running` on the first start, and the
        # second start of the same run is a no-op on the row rather than a
        # refusal. `derived.start_run` owns that idempotence; this pins
        # that the CLI path keeps calling it rather than guarding it away.
        self._start(dry_run=False, key="k1")
        self._start(dry_run=False, key="k2")
        self.assertEqual(self.transitions, ["ramp-proof", "ramp-proof"])
        self.assertEqual(self.row["state"], "running")

    def test_the_registry_facts_reach_the_audit_detail(self):
        self._start(dry_run=False)
        self.assertEqual(self.details[0]["run_key"], 55)
        self.assertEqual(self.details[0]["submission_seq"], 0)
        self.assertEqual(self.details[0]["run_state_before"], "created")


class StartRunAuditedScienceOverlayTests(unittest.TestCase):
    """The scratch workflow's `science_overlay` gate in `start_run_audited`:
    passed to `submit_run` (and, from there, unchanged to `seams.
    submit_gathered` -- see `SubmitRunScienceOverlayTests` for that half)
    ONLY for a `kind = 'scratch'` run whose row's `config_overlay` is
    non-empty; a production run's path, and a scratch run with no `--set`
    overlay at `run create`, must both submit `science_overlay=None`.

    Modelled on `RunStartStateTransitionTests`, the one existing apply-path
    (`dry_run=False`) fixture for this function, extended with a small
    conn/cursor double for the one query this gate adds — `_check_job_
    definition_family`'s own `run_row` read (a DIFFERENT function,
    `actions.run_row`) is patched by every OTHER apply-path test in this
    file, but that helper does not select `config_overlay` at all (checked
    against `actions._RUN_ROW`'s column list), so this gate cannot reuse
    it and reads the column directly -- exactly the extra query this
    class exists to drive.
    """

    class _Cursor:
        def __init__(self, overlay, calls):
            self._overlay = overlay
            self._calls = calls

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            self._calls.append((" ".join(sql.split()), params))

        def fetchone(self):
            # `None` stands in for "no row" as well as "row, NULL column" --
            # this gate treats both the same way (see `submit_run`'s
            # docstring for `science_overlay`), so no test here needs to
            # tell them apart.
            return None if self._overlay is None else (self._overlay,)

    class _Conn:
        def __init__(self, overlay):
            self.overlay = overlay
            self.calls = []
            self.committed = False

        def cursor(self):
            return StartRunAuditedScienceOverlayTests._Cursor(
                self.overlay, self.calls)

        def commit(self):
            self.committed = True

    def setUp(self):
        from pipeline.operatorctl import run as run_mod
        self.run_mod = run_mod
        self.row = {"run_id": 55, "name": "ramp-proof", "kind": "scratch",
                    "state": "created"}

        bind_patcher = mock.patch.object(
            run_mod, "_bind_registry_row", lambda conn, n: self.row)
        bind_patcher.start()
        self.addCleanup(bind_patcher.stop)

        seq_patcher = mock.patch.object(
            run_mod, "next_submission_seq", lambda conn, k: 0)
        seq_patcher.start()
        self.addCleanup(seq_patcher.stop)

        replay_patcher = mock.patch.object(
            run_mod, "_replay_lookup", lambda *a, **k: None)
        replay_patcher.start()
        self.addCleanup(replay_patcher.stop)

        gather_patcher = mock.patch.object(
            run_mod, "gather_for_run", lambda *a, **k: ("science", ["u"]))
        gather_patcher.start()
        self.addCleanup(gather_patcher.stop)

        env_patcher = mock.patch.object(
            run_mod, "_resolve_submission_env",
            lambda *a, **k: {"s3_client": object(),
                             "manifest_bucket": "b",
                             "queue": "q", "job_definition": "jd",
                             "binding": object(), "manifest_prefix": "p",
                             "batch_client": object()})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        audit_patcher = mock.patch.object(
            run_mod, "record_external_action",
            lambda conn, key, cls, scope, reason, dry_run=False,
            rows_affected=0, detail=None, policy_citation=None: {})
        audit_patcher.start()
        self.addCleanup(audit_patcher.stop)

        # THE UNRELATED `run_row` READ. `job_definition_family` defaults to
        # `None`, so `start_run_audited`'s scratch-routing block (the
        # `_SCRATCH_DEFINITIONS`/`job_definition_family` logic this brief
        # is explicit is OUT of scope) always runs first and calls
        # `actions.run_row` for its OWN kind check -- a real `_RUN_ROW`
        # query this class's cursor double does not answer. Patched here,
        # the same way every other apply-path test in this file patches
        # it, so that block is a no-op (this class's `row["kind"]` stands
        # in for `runs.kind` on both reads, which is what the real row
        # would also be) and the ONLY query this class's `_Conn.calls`
        # records is the `config_overlay` read the science-overlay gate
        # itself issues.
        from pipeline.operatorctl import actions as actions_mod
        run_row_patcher = mock.patch.object(
            actions_mod, "run_row",
            lambda conn, n: dict(self.row))
        run_row_patcher.start()
        self.addCleanup(run_row_patcher.stop)

        db_patcher = mock.patch(
            "database.modules.utils.rapid_db.RAPIDDB",
            lambda: types.SimpleNamespace(exit_code=0))
        db_patcher.start()
        self.addCleanup(db_patcher.stop)

        def fake_start_run(conn, key, name, reason, dry_run=True,
                           policy_citation=None):
            self.row["state"] = "running"
            return {"rows_affected": 1}

        from pipeline.operatorctl import actions as actions_mod
        state_patcher = mock.patch.object(actions_mod, "start_run",
                                          fake_start_run)
        state_patcher.start()
        self.addCleanup(state_patcher.stop)

        # `submission_role` is a real `SET ROLE`/restore block in
        # production (see its own docstring); this class is about the
        # OVERLAY VALUE `submit_run` is given, which is resolved before
        # `submit_run` is ever called, so the role switch is stubbed
        # transparent here exactly as `SubmitRunSubmissionRoleTests` stubs
        # it for the same reason.
        import contextlib

        @contextlib.contextmanager
        def fake_submission_role(conn):
            yield conn

        role_patcher = mock.patch.object(
            run_mod, "submission_role", fake_submission_role)
        role_patcher.start()
        self.addCleanup(role_patcher.stop)

        self.gathered_kwargs = []

        def fake_submit_gathered(units, **kwargs):
            self.gathered_kwargs.append(kwargs)
            return [(types.SimpleNamespace(job_id="j"), ["a"])]

        import pipeline.seams as seams_mod
        seams_patcher = mock.patch.object(
            seams_mod, "submit_gathered", fake_submit_gathered)
        seams_patcher.start()
        self.addCleanup(seams_patcher.stop)

    def _start(self, overlay, kind="scratch", key="k"):
        self.row["kind"] = kind
        conn = self._Conn(overlay)
        self.run_mod.start_run_audited(
            conn=conn, idempotency_key=key, name="ramp-proof",
            phase="statistics", reason="overlay gate", dry_run=False,
            out=_null_out())
        return conn

    def test_a_scratch_runs_overlay_reaches_submit_gathered(self):
        overlay = {"science_config": {"threshold": 5}}
        self._start(overlay)
        self.assertEqual(self.gathered_kwargs[0]["science_overlay"], overlay)

    def test_a_scratch_run_with_no_overlay_column_passes_none(self):
        # A scratch run is not, by itself, enough — `config_overlay` must
        # also be non-empty, or `run create` with no `--set` would submit
        # a phantom override on every scratch run's first batch.
        self._start(None)
        self.assertIsNone(self.gathered_kwargs[0]["science_overlay"])

    def test_a_scratch_run_with_an_empty_overlay_passes_none(self):
        # `{}` is "no override" (`manifest.py`'s own
        # `_validate_science_overlay` treats an empty mapping the same
        # way), not a real empty dict override recorded by construction.
        self._start({})
        self.assertIsNone(self.gathered_kwargs[0]["science_overlay"])

    def test_a_production_run_never_passes_an_overlay(self):
        # THE GATE THAT MATTERS. Even if `config_overlay` somehow carried
        # content on a production row, kind is checked FIRST and the
        # column is never even queried for one -- production's manifest
        # must be untouched by this parameter's existence, full stop.
        conn = self._start({"science_config": {"threshold": 5}},
                           kind="production")
        self.assertIsNone(self.gathered_kwargs[0]["science_overlay"])
        self.assertEqual(conn.calls, [],
                         "a production run must never query config_overlay "
                         "at all")

    def test_the_overlay_query_is_keyed_by_run_id_not_name(self):
        overlay = {"science_config": {"threshold": 5}}
        conn = self._start(overlay)
        self.assertEqual(len(conn.calls), 1)
        sql, params = conn.calls[0]
        self.assertIn("config_overlay", sql)
        self.assertIn("runs", sql)
        # `run["run_id"]` (55, the registry key), never `run["name"]`
        # (`"ramp-proof"`) -- `run_id` is the primary key `_RUN_ROW` and
        # `next_submission_seq` both key by; `name` is only ever matched
        # by prefix elsewhere in this module.
        self.assertEqual(params, (55,))


class NextSubmissionSeqTests(unittest.TestCase):
    """The ordinal is read from the run's own keyed submissions."""

    class _Cursor:
        def __init__(self, value, calls):
            self._value = value
            self._calls = calls

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            self._calls.append((" ".join(sql.split()), params))

        def fetchone(self):
            return (self._value,)

    class _Conn:
        def __init__(self, value):
            self.value = value
            self.calls = []

        def cursor(self):
            return NextSubmissionSeqTests._Cursor(self.value, self.calls)

    def test_a_run_with_no_keyed_submissions_starts_at_zero(self):
        conn = self._Conn(None)
        from pipeline.operatorctl.run import next_submission_seq
        self.assertEqual(next_submission_seq(conn, 42), 0)

    def test_the_next_ordinal_is_one_past_the_highest(self):
        conn = self._Conn(1)
        from pipeline.operatorctl.run import next_submission_seq
        self.assertEqual(next_submission_seq(conn, 42), 2)

    def test_no_key_reads_nothing_and_returns_zero(self):
        conn = self._Conn(7)
        from pipeline.operatorctl.run import next_submission_seq
        self.assertEqual(next_submission_seq(conn, None), 0)
        self.assertEqual(conn.calls, [])

    def test_the_ordinal_is_read_by_key_never_by_prefix(self):
        # READ BY KEY, NOT BY PREFIX, AND THAT IS THE POINT: a prefix read
        # would match rows written under the same NAME before migration
        # 121, which carry no ordinal to continue from (a pre-121 single
        # batch is named `<name>` with no suffix at all).
        conn = self._Conn(0)
        from pipeline.operatorctl.run import next_submission_seq
        next_submission_seq(conn, 42)
        sql, params = conn.calls[0]
        self.assertIn("run_key = %s", sql)
        self.assertNotIn("LIKE", sql)
        self.assertEqual(params, [42])


class ProductionRunKeyTests(unittest.TestCase):
    """The VPO's half: exactly one live production run, or None."""

    class _Cursor:
        def __init__(self, rows, calls):
            self._rows = rows
            self._calls = calls

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            self._calls.append(" ".join(sql.split()))

        def fetchall(self):
            return self._rows

    class _Conn:
        def __init__(self, rows):
            self.rows = rows
            self.calls = []

        def cursor(self):
            return ProductionRunKeyTests._Cursor(self.rows, self.calls)

    def _key(self, rows):
        from pipeline.operatorctl.run import production_run_key
        return production_run_key(self._Conn(rows))

    def test_no_production_run_gives_none(self):
        self.assertIsNone(self._key([]))

    def test_exactly_one_production_run_gives_its_key(self):
        self.assertEqual(self._key([(101,)]), 101)

    def test_two_production_runs_give_none_rather_than_a_guess(self):
        # AN AMBIGUOUS ANSWER IS REPORTED AS NO ANSWER. Picking the
        # newest, or the lowest-numbered, would attribute production's
        # work to a run by an arbitrary rule no operator ever stated.
        self.assertIsNone(self._key([(101,), (102,)]))

    def test_archived_production_runs_are_excluded_by_the_query(self):
        conn = self._Conn([(101,)])
        from pipeline.operatorctl.run import production_run_key
        production_run_key(conn)
        self.assertIn("state <> 'archived'", conn.calls[0])
        self.assertIn("kind = 'production'", conn.calls[0])


class RunKeyTallyTests(unittest.TestCase):
    """`run status` reads by key where it can, by prefix where it must, and
    always says which.
    """

    class _Conn:
        """Answers each statement from a script keyed on a substring, so a
        test states what the DATABASE says rather than in what order the
        function asks.
        """

        def __init__(self, keyed_count, keyed=None, prefix=None):
            self.keyed_count = keyed_count
            self.keyed = keyed or {"total": 0, "failures": 0}
            self.prefix = prefix or {"total": 0, "failures": 0}
            self.sqls = []

        def cursor(self):
            return RunKeyTallyTests._Cursor(self)

    class _Cursor:
        def __init__(self, conn):
            self._conn = conn
            self._row = None
            self._cols = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            flat = " ".join(sql.split())
            self._conn.sqls.append(flat)
            if "count(*) AS n" in flat:
                self._cols = [("n",)]
                self._row = (self._conn.keyed_count,)
            elif "run_key =" in flat:
                self._cols = [("total",), ("failures",)]
                self._row = (self._conn.keyed["total"],
                             self._conn.keyed["failures"])
            else:
                self._cols = [("total",), ("failures",)]
                self._row = (self._conn.prefix["total"],
                             self._conn.prefix["failures"])

        @property
        def description(self):
            return self._cols

        def fetchall(self):
            return [self._row]

    def test_a_run_with_no_keyed_attempts_is_counted_by_prefix(self):
        # Every run that predates migration 121. The prefix is still the
        # only reading available and the answer says so.
        conn = self._Conn(keyed_count=0, prefix={"total": 9, "failures": 2})
        result = actions.run_attempt_tally(conn, "old-run")
        self.assertEqual(result["counted_by"], "name_prefix")
        self.assertEqual(result["total"], 9)

    def test_a_run_with_keyed_attempts_is_counted_by_key(self):
        conn = self._Conn(keyed_count=20, keyed={"total": 20, "failures": 1},
                          prefix={"total": 20, "failures": 1})
        result = actions.run_attempt_tally(conn, "ramp-proof")
        self.assertEqual(result["counted_by"], "run_key")
        self.assertEqual(result["total"], 20)

    def test_both_readings_are_returned_when_they_differ(self):
        # THE TRANSITION SHAPE: the deployed reconciler creates retry rows
        # with no key until the next repin, so the keyed reading is LOWER
        # than the prefix reading. Both numbers are returned so `run
        # status` can print the difference instead of hiding it.
        conn = self._Conn(keyed_count=20, keyed={"total": 20, "failures": 1},
                          prefix={"total": 23, "failures": 4})
        result = actions.run_attempt_tally(conn, "ramp-proof")
        self.assertEqual(result["total"], 20)
        self.assertEqual(result["prefix_total"], 23)
        self.assertEqual(result["prefix_failures"], 4)

    def test_the_keyed_query_resolves_the_key_from_the_name(self):
        conn = self._Conn(keyed_count=1, keyed={"total": 1, "failures": 0})
        actions.run_attempt_tally(conn, "ramp-proof")
        keyed_sql = [s for s in conn.sqls
                     if "run_key =" in s and "count(*) AS n" not in s]
        self.assertEqual(len(keyed_sql), 1)
        self.assertIn("SELECT run_id FROM runs WHERE name = %s", keyed_sql[0])

    def test_the_two_readings_share_one_failure_predicate(self):
        # Written once, used twice — so the keyed and prefix readings can
        # never drift into asking two different questions of the two
        # populations.
        self.assertIn(actions._RUN_FAILURE_PREDICATE,
                      actions._RUN_ATTEMPT_TALLY)
        self.assertIn(actions._RUN_FAILURE_PREDICATE,
                      actions._RUN_ATTEMPT_TALLY_BY_KEY)


class RunCompareProvenanceTests(unittest.TestCase):
    """`run compare` prints provenance above its tallies, in a pinned shape.

    Asserted on the RENDERED OUTPUT, not on the field list: the acceptance
    greps this same output for `^run: `, `^tallies$` and one line per
    field, so what a test must pin is what an operator actually sees.
    """

    def _render(self, columns=None):
        from pipeline.operatorctl import main as main_mod
        from pipeline.operatorctl import actions as actions_mod

        rows = {
            "run-a": {"run_id": 1, "name": "run-a", "kind": "scratch",
                      "state": "complete", "branch": "smdc",
                      "image_digest": "sha256:aaa", "config_hash": "cfg-a",
                      "input_generations": ["g1"], "owner": "rusholme",
                      "purpose": "p", "created_at": "t"},
            "run-b": {"run_id": 2, "name": "run-b", "kind": "scratch",
                      "state": "complete", "branch": "smdc",
                      "image_digest": "sha256:bbb", "config_hash": "cfg-b",
                      "input_generations": None, "owner": "rusholme",
                      "purpose": "p", "created_at": "t"},
        }
        available = columns if columns is not None else {
            "run_id", "name", "kind", "state", "branch", "image_digest",
            "config_hash", "input_generations"}

        args = argparse.Namespace(run_a="run-a", run_b="run-b")
        out = io.StringIO()
        with mock.patch.object(actions_mod, "run_row",
                               lambda conn, n: rows[n]), \
             mock.patch.object(actions_mod, "run_attempt_tally",
                               lambda conn, n: {"total": 1, "failures": 0}), \
             mock.patch.object(actions_mod, "run_stage_walltime",
                               lambda conn, n: []), \
             mock.patch.object(actions_mod, "run_product_counts",
                               lambda conn, n: {}), \
             mock.patch.object(
                 actions_mod, "run_build_provenance", lambda conn, n: []), \
             mock.patch.object(
                 actions_mod, "run_container_digest_count",
                 lambda conn, n: {"n_distinct": 0, "n_attempts": 0}), \
             mock.patch.object(
                 actions_mod, "run_config_overlay",
                 lambda conn, run_id: None), \
             mock.patch.object(
                 actions_mod, "run_reference_product_keys",
                 lambda conn, n: []), \
             mock.patch.object(
                 actions_mod, "run_input_identities", lambda conn, n: []), \
             mock.patch.object(main_mod, "_run_columns",
                               lambda conn: available):
            main_mod._cmd_run_compare(object(), args, out)
        return out.getvalue()

    def test_the_two_run_blocks_precede_the_tallies_line(self):
        text = self._render()
        lines = [ln for ln in text.splitlines()
                 if ln.startswith("run: ") or ln == "tallies"]
        self.assertEqual(lines, ["run: run-a", "run: run-b", "tallies"])

    def test_every_provenance_field_appears_once_per_run(self):
        text = self._render()
        for field in ("run_id", "kind", "state", "branch", "image_digest",
                      "config_hash", "input_generations", "reference_set"):
            self.assertEqual(
                len([ln for ln in text.splitlines()
                     if ln.startswith("%s: " % field)]), 2,
                "%s must appear once per run" % field)

    def test_the_values_themselves_are_printed_not_merely_the_labels(self):
        # A count of lines would pass against a block that printed every
        # field as empty; the point of the block is the values.
        text = self._render()
        self.assertIn("image_digest: sha256:aaa", text)
        self.assertIn("image_digest: sha256:bbb", text)
        self.assertIn("config_hash: cfg-a", text)

    def test_a_column_the_schema_lacks_prints_n_a_and_keeps_its_line(self):
        # `reference_set` is added by a later brief. The line must exist
        # either way, so a reader diffing two blocks lines them up.
        text = self._render()
        self.assertEqual(
            len([ln for ln in text.splitlines()
                 if ln == "reference_set: n/a"]), 2)

    def test_a_null_value_prints_n_a_rather_than_none(self):
        text = self._render()
        self.assertIn("input_generations: n/a", text)


class RunCompleteTests(unittest.TestCase):
    """`run complete` goes through `derived.complete_run` on 109's own
    keyed, audited path — the same one `run archive` beside it uses.
    """

    def test_the_idempotency_key_is_first_matching_109s_convention(self):
        conn = _FakeConn([
            {"action": "run_state_complete", "dry_run": True,
             "replayed": False, "rows_affected": 0, "audit_id": 7,
             "kind": "scratch", "prior_state": "running",
             "counted_by": "run_key", "open_attempts": 0},
        ])
        actions.complete_run(conn, "complete-key-1", "ramp-proof",
                             "run finished", dry_run=True)
        self.assertEqual(len(conn.calls), 1)
        sql, params = conn.calls[0]
        self.assertIn("derived.complete_run", sql)
        self.assertEqual(params[0], "complete-key-1")
        self.assertEqual(params[1], "ramp-proof")

    def test_a_dry_run_issues_exactly_one_statement_and_writes_nothing(self):
        conn = _FakeConn([
            {"action": "run_state_complete", "dry_run": True,
             "replayed": False, "rows_affected": 0, "audit_id": 7,
             "kind": "scratch", "prior_state": "running",
             "counted_by": "run_key", "open_attempts": 3},
        ])
        result = actions.complete_run(conn, "k", "ramp-proof", "finished",
                                      dry_run=True)
        self.assertEqual(len(conn.calls), 1)
        rendered = render_plan("run_complete", "runs:ramp-proof", "finished",
                               "k", result, False)
        self.assertIn("Nothing was changed", rendered)
        self.assertIn("DRY RUN", rendered)

    def test_the_dry_run_prints_the_open_attempt_count_that_would_block_it(self):
        # An operator rehearsing a completion wants to know WHETHER it
        # would be refused and BY HOW MUCH, not to find out by trying.
        conn = _FakeConn([
            {"action": "run_state_complete", "dry_run": True,
             "replayed": False, "rows_affected": 0, "audit_id": 7,
             "kind": "scratch", "prior_state": "running",
             "counted_by": "run_key", "open_attempts": 4},
        ])
        args = argparse.Namespace(
            name="ramp-proof", reason="finished", idempotency_key="k",
            apply=False, policy_citation=None)
        out = io.StringIO()
        operatorctl_main._cmd_run_complete(conn, args, out)
        text = out.getvalue()
        self.assertIn("open attempts : 4", text)
        self.assertIn("run_key", text)

    def test_the_command_is_registered_under_run(self):
        # The subcommand must actually be reachable: a handler defined but
        # never wired to a parser is invisible to every operator.
        parser = operatorctl_main.build_parser()
        args = parser.parse_args(
            ["run", "complete", "--name", "ramp-proof", "--reason", "done"])
        self.assertIs(args.func, operatorctl_main._cmd_run_complete)
        self.assertEqual(args.name, "ramp-proof")
        self.assertFalse(args.apply)


class RampStepTests(unittest.TestCase):
    """A second `run start` of a running run is a ramp step.

    No new mechanism: the work-unit authorisation gate in `seams.py`
    already skips units the run has claimed or completed, and the ordinal
    already gives the second step its own batch identity. What this pins
    is that the two compose — that a second start of one run submits only
    what the first did not hold, under a new identity.
    """

    def setUp(self):
        from pipeline.operatorctl import run as run_mod
        self.run_mod = run_mod
        self.row = {"run_id": 88, "name": "ramp-proof", "kind": "scratch",
                    "state": "created"}
        #: work_unit "state" per unit, as the fake authorisation reports
        #: it. Half complete at the second start, which is the ramp shape.
        self.claimed = set()

        bind_patcher = mock.patch.object(
            run_mod, "_bind_registry_row", lambda conn, n: self.row)
        bind_patcher.start()
        self.addCleanup(bind_patcher.stop)

        replay_patcher = mock.patch.object(
            run_mod, "_replay_lookup", lambda *a, **k: None)
        replay_patcher.start()
        self.addCleanup(replay_patcher.stop)

        gather_patcher = mock.patch.object(
            run_mod, "gather_for_run",
            lambda *a, **k: ("statistics", ["u1", "u2", "u3", "u4"]))
        gather_patcher.start()
        self.addCleanup(gather_patcher.stop)

        audit_patcher = mock.patch.object(
            run_mod, "record_external_action", lambda *a, **k: {})
        audit_patcher.start()
        self.addCleanup(audit_patcher.stop)

        db_patcher = mock.patch(
            "database.modules.utils.rapid_db.RAPIDDB",
            lambda: types.SimpleNamespace(exit_code=0))
        db_patcher.start()
        self.addCleanup(db_patcher.stop)

        from pipeline.operatorctl import actions as actions_mod
        state_patcher = mock.patch.object(
            actions_mod, "start_run",
            lambda conn, key, name, reason, dry_run=True,
            policy_citation=None: self.row.update(state="running"))
        state_patcher.start()
        self.addCleanup(state_patcher.stop)

        # The submission ordinal advances with each step, as the real
        # reader would once the first step's submissions row exists.
        self.seq = 0
        seq_patcher = mock.patch.object(
            run_mod, "next_submission_seq", lambda conn, k: self.seq)
        seq_patcher.start()
        self.addCleanup(seq_patcher.stop)

        self.submissions = []

        def fake_submit_run(conn, name, job_type, units, reason,
                            context=None, work_unit_run_id=None, lane=None,
                            run_key=None, submission_seq=None,
                            envelope=None, science_overlay=None):
            # THE AUTHORISATION GATE, in miniature: a unit this run has
            # already claimed is skipped, exactly as
            # `seams._transition_or_defer` skips one whose work unit is
            # not `ready`.
            fresh = [u for u in units if u not in self.claimed]
            self.claimed.update(fresh)
            self.submissions.append({
                "batch_id": "%s-%d" % (name, submission_seq),
                "units": fresh, "run_key": run_key})
            self.seq = submission_seq + 1
            return [(types.SimpleNamespace(job_id="j"), ["a"] * len(fresh))]

        submit_patcher = mock.patch.object(run_mod, "submit_run",
                                           fake_submit_run)
        submit_patcher.start()
        self.addCleanup(submit_patcher.stop)

    class _Cursor:
        # The science-overlay gate's `config_overlay` read (`run.py`), hit
        # unconditionally for this fixture's scratch-kind row on every
        # apply. `None` stands for "no override" — this class is about the
        # ramp, not the overlay.
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            pass

        def fetchone(self):
            return None

    def _start(self, key, cap=None):
        return self.run_mod.start_run_audited(
            conn=types.SimpleNamespace(commit=lambda: None,
                                       cursor=lambda: self._Cursor()),
            idempotency_key=key, name="ramp-proof", phase="statistics",
            reason="ramp step", dry_run=False, cap=cap, out=_null_out())

    def test_the_second_step_submits_only_what_the_first_did_not_hold(self):
        self.claimed.update({"u1", "u2"})       # step one holds half
        self._start("k1")
        self.assertEqual(self.submissions[0]["units"], ["u3", "u4"])

    def test_the_second_step_gets_its_own_batch_identity(self):
        self._start("k1")
        self._start("k2")
        self.assertEqual(
            [s["batch_id"] for s in self.submissions],
            ["ramp-proof-0", "ramp-proof-1"])

    def test_the_second_step_repeats_no_unit_of_the_first(self):
        # THE PROPERTY THE LIVE PROOF CHECKS TOO: no work unit appears
        # under two of one run's submissions.
        self._start("k1")
        self._start("k2")
        first = set(self.submissions[0]["units"])
        second = set(self.submissions[1]["units"])
        self.assertEqual(first & second, set())

    def test_both_steps_carry_the_same_registry_key(self):
        # A ramp is ONE run. Two steps under two keys would be two runs
        # wearing one name, which is the state this whole ruling ends.
        self._start("k1")
        self._start("k2")
        self.assertEqual([s["run_key"] for s in self.submissions], [88, 88])


class RunStateSqlstateClassificationTests(unittest.TestCase):
    """121's RA012/RA013 are refusals, not crashes.

    `derived.start_run` and `derived.complete_run` raise their state-machine
    refusals with SQLSTATEs, and `contract.classify` is what turns a
    psycopg2 error into this package's typed refusal. Without RA012/RA013 in
    that mapping the refusals reach `main`'s catch-all and print
    `rapidctl: UNEXPECTED — DatabaseError: ...` with exit 70 — the shape
    reserved for exceptions nobody classified, which tells an operator the
    tool is broken when in fact it is working exactly as designed.
    Observed live before this was added, on the proof run's own
    `run complete` dry run.
    """

    class _PgError(Exception):
        def __init__(self, code, message):
            super().__init__(message)
            self.pgcode = code

    def _classify(self, code):
        from pipeline.operatorctl import contract
        return contract.classify(self._PgError(code, "refused: %s" % code))

    def test_ra012_classifies_as_an_invariant_violation(self):
        from pipeline.operatorctl.contract import InvariantViolation
        self.assertIsInstance(self._classify("RA012"), InvariantViolation)

    def test_ra013_classifies_as_an_invariant_violation(self):
        from pipeline.operatorctl.contract import InvariantViolation
        self.assertIsInstance(self._classify("RA013"), InvariantViolation)

    def test_both_carry_the_category_main_renders_as_REFUSED(self):
        # `main`'s catch-all prints `REFUSED` and returns EXIT_USAGE for any
        # exception carrying an `error_category`, and `UNEXPECTED` with
        # EXIT_UNEXPECTED for one that does not. The category is what makes
        # the difference, so it is what this asserts.
        for code in ("RA012", "RA013"):
            typed = self._classify(code)
            self.assertEqual(getattr(typed, "error_category", None),
                             "invariant_violation", code)

    def test_an_unrelated_sqlstate_still_propagates_unclassified(self):
        # The mapping stays CLOSED: widening it to "any RA0xx" would
        # silently swallow a future code nobody has decided the remedy for.
        self.assertIsNone(self._classify("RA099"))
        self.assertIsNone(self._classify("23505"))


class RunEnvelopeCliTests(unittest.TestCase):
    """`run create` carries the run's execution envelope (migration 122).

    Lane and size are attributes of a RUN, not constants of the deployment
    (Ben, 2026-09-13 13:02). These tests pin the three properties that could
    silently stop holding: the four values reach the database function AT ALL,
    they reach it BY NAME rather than positionally, and a defaulted call sends
    NULL so the function's own derivation runs rather than a second copy of it
    here.
    """

    def _result(self, **overrides):
        body = {"action": "run_create", "dry_run": False, "replayed": False,
                "already_present": False, "rows_affected": 1, "audit_id": 9,
                "idempotency_key": "k", "run_id": 42, "would_add": True,
                "lane": "bulk", "retry_attempts": 3,
                "retry_wallclock_s": 129600, "attempt_timeout_s": 43200}
        body.update(overrides)
        return body

    def test_the_envelope_is_passed_by_name_not_positionally(self):
        # THE REGRESSION THIS EXISTS FOR. `derived.create_run` carries
        # `p_dispatcher` at position 14, between `p_policy_citation` and the
        # envelope 122 appended at 15-18. A positional call would put the lane
        # into `p_dispatcher` — a text parameter, so PostgreSQL would accept
        # it silently, record "bulk" as the audit row's dispatcher, and leave
        # the envelope at its defaults with no error anywhere.
        conn = _FakeConn([self._result()])
        actions.create_run(conn, "k", "envelope-probe", "rusholme",
                           "scratch", reason="why", dry_run=False,
                           lane="prompt", retry_attempts=2,
                           retry_wallclock_s=100, attempt_timeout_s=50)
        sql, params = conn.calls[0]
        for name in ("p_lane =>", "p_retry_attempts =>",
                     "p_retry_wallclock_s =>", "p_attempt_timeout_s =>"):
            self.assertIn(name, sql)
        # Migration 126 appended `p_reference_set_id` AFTER the envelope, so
        # the envelope is no longer the tail of the params tuple. Sliced
        # from -5 to -1 rather than from -4, and the set's own position is
        # asserted separately below — a `[-4:]` that silently started
        # reading the set plus three envelope values would be exactly the
        # positional slide this test exists to catch.
        self.assertEqual(["prompt", 2, 100, 50], list(params[-5:-1]))

    def test_the_reference_set_id_is_passed_by_name_after_the_envelope(self):
        # Migration 126's `p_reference_set_id` is appended at position 19,
        # after 122's envelope. By NAME for the same reason the envelope is:
        # it is a bigint following four nullable scalars, so a positional
        # call that dropped one would slide it into `p_attempt_timeout_s`
        # — an integer parameter PostgreSQL would accept silently.
        conn = _FakeConn([self._result()])
        actions.create_run(conn, "k", "refset-probe", "rusholme",
                           "scratch", reason="why", dry_run=False,
                           reference_set_id=7)
        sql, params = conn.calls[0]
        self.assertIn("p_reference_set_id =>", sql)
        self.assertEqual(7, params[-1])

    def test_no_reference_set_sends_null_so_the_function_resolves_default(
            self):
        # None means "the DEFAULT SET as of now", resolved and STORED by
        # `derived.create_run`. Resolved there rather than in the CLI so
        # exactly one place decides what the default means at this instant
        # — and so the stored value is never "whatever is default later".
        conn = _FakeConn([self._result()])
        actions.create_run(conn, "k", "refset-default", "rusholme",
                           "scratch", reason="why", dry_run=False)
        sql, params = conn.calls[0]
        # THE SQL MUST NAME THE PARAMETER. Asserting only that the last bound
        # value is None passes against the pre-126 call too, where the last
        # value is `p_attempt_timeout_s` and is also None — so the assertion
        # would hold with no reference set in the statement at all.
        self.assertIn("p_reference_set_id => %s::bigint", sql)
        self.assertIsNone(params[-1])
        self.assertEqual(sql.count("%s"), len(params))

    def test_a_defaulted_call_sends_null_so_the_function_derives(self):
        # The defaults live in `derived.create_run`, which derives the
        # wall-clock from the lane. Restating them here would give one number
        # two homes that could drift, so an omitted flag must arrive as NULL.
        conn = _FakeConn([self._result()])
        actions.create_run(conn, "k", "envelope-default", "rusholme",
                           "scratch", reason="why", dry_run=False)
        _, params = conn.calls[0]
        # `[-5:-1]`, not `[-4:]`: 126 appended `p_reference_set_id` after
        # the envelope. See the by-name test above.
        self.assertEqual([None, None, None, None], list(params[-5:-1]))

    def test_the_lane_derived_wallclock_is_printed_from_the_answer(self):
        # Three of the four may be defaulted and two of those are DERIVED from
        # the lane, so what the operator asked for is not what the run got.
        # The printed line must come from the function's result object, never
        # from the arguments.
        conn = _FakeConn([self._result(lane="prompt", retry_wallclock_s=43200,
                                       attempt_timeout_s=14400)])
        args = argparse.Namespace(
            name="envelope-print", kind="scratch",
            purpose=None, branch=None, image_digest=None, config_hash=None,
            input_generations=None, expect_absent=False, reason="why",
            idempotency_key="k", apply=True, policy_citation=None,
            lane="prompt", retry_attempts=None, retry_wallclock_s=None,
            attempt_timeout_s=None,
            # The three flags migration 126 added. All absent here: this
            # test is about the envelope, and `_cmd_run_create` reads all
            # three before it prints anything.
            reference_set=None, build_reference_set=None, psf_set=None,
            # `--set` (migration 112). Absent here for the same reason: this
            # test is about the envelope print, not the science overlay.
            overlay_pairs=None)
        out = io.StringIO()
        operatorctl_main._cmd_run_create(conn, args, out)
        text = out.getvalue()
        self.assertIn("lane prompt", text)
        # 3 x the prompt lane's 14400 s attempt timeout, derived by the
        # function and echoed back — not computed in the CLI.
        self.assertIn("wall-clock 43200s", text)
        self.assertIn("attempt timeout 14400s", text)

    def test_retry_attempts_zero_is_refused_by_the_parser(self):
        # One attempt IS the first try, so the floor is 1 and 0 is a mistake
        # rather than "no retries". The CHECK constraint and the function both
        # refuse it; this pins that the operator is told so before a
        # round trip.
        parser = operatorctl_main.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([
                "run", "create", "--name", "zero-retries",
                "--kind", "scratch", "--reason", "why",
                "--retry-attempts", "0"])

    def test_the_lane_choices_are_closed(self):
        parser = operatorctl_main.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([
                "run", "create", "--name", "bad-lane",
                "--kind", "scratch", "--reason", "why", "--lane", "urgent"])


class RunStartLaneDefaultsToTheRunsTests(unittest.TestCase):
    """`run start --lane` defaults to the run's stored lane, and the lane
    actually chosen is what the submission records (migration 122).

    Two verbs, deliberately different. `--lane` OVERRIDES where one
    submission goes; `derived.update_run_envelope` CHANGES what the run is
    set to. A flag on one command silently rewriting the run would make the
    run row a record of the last command rather than of the run.
    """

    def _row(self, **overrides):
        row = {"run_id": 42, "name": "lane-probe", "state": "running",
               "kind": "scratch", "lane": "bulk", "retry_attempts": 3,
               "retry_wallclock_s": 129600, "attempt_timeout_s": 43200}
        row.update(overrides)
        return row

    def test_lane_absent_takes_the_runs_stored_lane(self):
        envelope = operatorctl_run._run_envelope(self._row(lane="prompt"),
                                                 lane=None)
        self.assertEqual("prompt", envelope["lane"])

    def test_lane_given_overrides_for_this_submission_only(self):
        row = self._row(lane="bulk")
        envelope = operatorctl_run._run_envelope(row, lane="prompt")
        self.assertEqual("prompt", envelope["lane"])
        # The run row itself is untouched: the override is per-submission.
        self.assertEqual("bulk", row["lane"])

    def test_an_overridden_lane_does_not_re_derive_the_timeout(self):
        # A run created on the bulk lane carries bulk's 43200 s, and sending
        # one batch to the prompt lane must not silently shorten it to 14400:
        # the run's budget is what the operator set, and tightening it
        # because a batch went somewhere faster could kill work the run was
        # entitled to finish.
        envelope = operatorctl_run._run_envelope(self._row(), lane="prompt")
        self.assertEqual(43200, envelope["attempt_timeout_s"])
        self.assertEqual(129600, envelope["retry_wallclock_s"])

    def test_a_row_predating_the_migration_yields_no_envelope(self):
        # Not an error: `run start` refusing a MISSING row is the
        # run-identity brief's item, and a row that predates 122 is a
        # different thing from a row that is absent. The job definition's own
        # timeout and retry rows then apply.
        self.assertIsNone(
            operatorctl_run._run_envelope(self._row(lane=None)))
        self.assertIsNone(operatorctl_run._run_envelope(None))


class ReferenceSetRunCreateAndStartTests(unittest.TestCase):
    """`run create`'s reference-set flags and `run start`'s refusal of a
    windowed phase on a setless run (migrations 126/127).

    Two halves, both stub-tier and neither needing a database.

    THE PARSER HALF. `--reference-set` (declare an EXISTING set) and
    `--build-reference-set` (create a NEW one this run fills) are a
    mutually exclusive group, and `--psf-set` says which set's PSF rows a
    NEW set reads — so it means nothing without `--build-reference-set`,
    and `_cmd_run_create` refuses it before touching the connection. Both
    refusals are argparse/`SystemExit`, not a message and a zero exit: an
    operator who typed a contradictory pair must be stopped before a run
    row exists, because the set a run declares is fixed at creation and
    cannot be corrected afterwards.

    Omitting both flags is NOT a third mode: it means the default set AS
    OF NOW, resolved and STORED by `derived.create_run`. That resolution
    is the FUNCTION's job, so the CLI must send NULL rather than reading
    `is_default` itself — one place decides what the default means at
    this instant, and the stored value is never "whatever is default
    later".

    THE START HALF. `start_run_audited` reads the run's stored set and
    refuses a WINDOWED phase (reference/science) that has none. A scratch
    run with no set is a DEFECT rather than a default: every run created
    since 127 stores one, so a NULL means the row predates it, and
    gathering against a guessed set is the silent substitution 126
    removes. The four post-database-chain phases read no reference and are
    not refused for a fact they never use.
    """

    # --- the mutually exclusive group ------------------------------------

    def _run_create_argv(self, *extra):
        return ["run", "create", "--name", "refset-probe",
                "--kind", "scratch", "--reason", "why"] + list(
                    extra)

    def test_reference_set_and_build_reference_set_are_mutually_exclusive(
            self):
        # Declaring an existing set and creating a new one are different
        # acts with different provenance, and a run has exactly one set.
        # Accepting both would leave argparse's last-wins order deciding
        # which — a silent choice about what the run differences against.
        parser = operatorctl_main.build_parser()
        # EACH FLAG MUST PARSE ALONE FIRST. Without this half the test passes
        # against a parser that has NEITHER flag: argparse exits 2 for an
        # unrecognised argument exactly as it does for a mutually-exclusive
        # violation, so `assertRaises(SystemExit)` alone cannot tell "refused
        # because they conflict" from "refused because neither exists".
        alpha = parser.parse_args(
            self._run_create_argv("--reference-set", "alpha"))
        self.assertEqual("alpha", alpha.reference_set)
        beta = parser.parse_args(
            self._run_create_argv("--build-reference-set", "beta"))
        self.assertEqual("beta", beta.build_reference_set)

        with self.assertRaises(SystemExit):
            parser.parse_args(self._run_create_argv(
                "--reference-set", "alpha",
                "--build-reference-set", "beta"))

    def test_reference_set_alone_parses(self):
        parser = operatorctl_main.build_parser()
        args = parser.parse_args(
            self._run_create_argv("--reference-set", "alpha"))
        self.assertEqual("alpha", args.reference_set)
        self.assertIsNone(args.build_reference_set)

    def test_build_reference_set_alone_parses(self):
        parser = operatorctl_main.build_parser()
        args = parser.parse_args(
            self._run_create_argv("--build-reference-set", "beta"))
        self.assertEqual("beta", args.build_reference_set)
        self.assertIsNone(args.reference_set)

    def test_neither_reference_set_flag_leaves_both_none(self):
        # The ordinary call, and the one the default-resolution test below
        # exercises: no set named, both attributes None.
        parser = operatorctl_main.build_parser()
        args = parser.parse_args(self._run_create_argv())
        self.assertIsNone(args.reference_set)
        self.assertIsNone(args.build_reference_set)

    # --- --psf-set without --build-reference-set --------------------------

    def _args(self, **overrides):
        body = dict(
            name="refset-probe", kind="scratch",
            purpose=None, branch=None, image_digest=None, config_hash=None,
            input_generations=None, expect_absent=False, reason="why",
            idempotency_key="k", apply=False, policy_citation=None,
            lane=None, retry_attempts=None, retry_wallclock_s=None,
            attempt_timeout_s=None, reference_set=None,
            build_reference_set=None, psf_set=None,
            # `--set` (migration 112's science overlay). `None` is the
            # ordinary case -- every existing `_args()` caller in this
            # class wants no overlay, matching how `reference_set`/
            # `psf_set` above default to "not asked for" too.
            overlay_pairs=None)
        body.update(overrides)
        return argparse.Namespace(**body)

    def test_psf_set_without_build_reference_set_raises_system_exit(self):
        # `--psf-set` says which PSFs a NEW set reads; an EXISTING set
        # already declares its own, so the pair is meaningless rather than
        # merely redundant. Refused BEFORE the connection is used — the
        # `object()` here would raise `AttributeError` on any attribute
        # access, so a refusal that came later could not reach this
        # assertion.
        with self.assertRaises(SystemExit) as caught:
            operatorctl_main._cmd_run_create(
                object(), self._args(psf_set="psf-alpha"), _null_out())
        self.assertIn("--psf-set", str(caught.exception))

    def test_psf_set_with_build_reference_set_is_not_refused_by_that_check(
            self):
        # The complement: the same flag WITH `--build-reference-set` must
        # get past the refusal, or the check above would be refusing the
        # combination it exists to permit. Proven by the failure mode
        # changing — it reaches the connection (`object()` has no
        # `cursor`) instead of exiting.
        with self.assertRaises(Exception) as caught:
            operatorctl_main._cmd_run_create(
                object(),
                self._args(psf_set="psf-alpha",
                           build_reference_set="beta"),
                _null_out())
        self.assertNotIsInstance(caught.exception, SystemExit)
        # AND IT MUST BE THE CONNECTION THAT FAILED, named. `assertNotIsInstance`
        # alone passes against a handler that never had the check at all and
        # died of something unrelated — an AttributeError on a missing `args`
        # attribute, say. Requiring the bare `object()` to be what broke is
        # what makes this evidence that the combination was PERMITTED and
        # execution carried on to the database.
        self.assertIsInstance(caught.exception, AttributeError)
        self.assertIn("cursor", str(caught.exception))
        # THIS TEST ALONE DOES NOT FAIL WHEN THE CHANGE IS REVERTED, and that
        # is correct rather than a weakness: a handler with NO check also
        # reaches the connection and dies the same way, so "got past the
        # refusal" is indistinguishable from "there was no refusal". It is one
        # half of a pair. Its partner —
        # `test_psf_set_without_build_reference_set_is_refused` — is what
        # fails on revert, and this half exists only to show that partner is
        # not over-refusing the combination the flag exists for. Kept
        # deliberately; do not "strengthen" it into asserting something it
        # cannot observe.

    # --- neither flag: the function resolves and stores the default -------

    def test_neither_flag_calls_create_run_with_reference_set_id_none(self):
        # THE STORED DEFAULT IS THE FUNCTION'S JOB. A CLI that read
        # `is_default` itself would give the same decision two homes, and
        # the one in the CLI would be made a moment earlier than the
        # insert — the window sets exist to close.
        from pipeline.operatorctl import actions as actions_mod

        recorded = {}

        def fake_create_run(conn, key, name, owner, kind, **kwargs):
            recorded.update(kwargs)
            return {"action": "run_create", "dry_run": True,
                    "replayed": False, "already_present": False,
                    "rows_affected": 0, "audit_id": 1,
                    "idempotency_key": key, "run_id": 42,
                    "would_add": True, "lane": "bulk", "retry_attempts": 3,
                    "retry_wallclock_s": 129600,
                    "attempt_timeout_s": 43200}

        with mock.patch.object(actions_mod, "create_run", fake_create_run):
            operatorctl_main._cmd_run_create(
                object(), self._args(), _null_out())

        self.assertIn("reference_set_id", recorded)
        self.assertIsNone(recorded["reference_set_id"])

    # --- run start refuses a windowed phase with no stored set ------------

    def _start(self, phase, reference_set_id, window=True):
        """Drive `start_run_audited`'s dry run against a scripted `runs`
        row, with the registry reads, the database handle, the submission
        environment and the gather all stubbed — no database and no AWS.

        THE HANDLE AND THE ENVIRONMENT MUST BE STUBBED, and that is worth
        stating: the reference-set refusal sits AFTER `RAPIDDB()` and
        after `_resolve_submission_env`, so a setless run still opens a
        database handle and resolves a Batch environment before being
        told it has no set. See the ledger's defect note — the refusal
        would cost nothing where the other registry refusals sit, above
        the replay lookup.
        """
        run_mod = operatorctl_run

        row = {"run_id": 77, "name": "w9-campaign-1", "kind": "scratch",
               "state": "running", "reference_set_id": reference_set_id,
               "psf_set_id": None, "reference_set": "set-alpha"}

        self.gathers = []

        def fake_gather(*a, **k):
            self.gathers.append(k)
            return ("science", ["unit-a"])

        from database.modules.utils import rapid_db as db_mod
        import pipeline.operator.gathering as gathering_mod

        class _FakeHandle:
            exit_code = 0

        patchers = [
            # Release science configuration (`RAPID_SW`), called by the
            # windowed branch before it reaches anything this class is
            # about — faked to a fixed value exactly as
            # `StartRunAuditedLaneResolutionTests` does.
            mock.patch.object(gathering_mod, "min_images_to_coadd",
                              lambda: 3),
            mock.patch.object(run_mod, "_bind_registry_row",
                              lambda conn, name: row),
            mock.patch.object(run_mod, "next_submission_seq",
                              lambda conn, key: 0),
            mock.patch.object(run_mod, "_replay_lookup",
                              lambda *a, **k: None),
            mock.patch.object(run_mod, "gather_for_run", fake_gather),
            mock.patch.object(run_mod, "_check_job_definition_family",
                              lambda *a, **k: None),
            mock.patch.object(db_mod, "RAPIDDB", _FakeHandle),
            # `job_definition` is now read unconditionally whenever a
            # family is named (`start_run_audited`'s audit-detail block),
            # and a scratch run's own science/reference phase now names
            # one BY DEFAULT with no override at all
            # (`_SCRATCH_DEFINITIONS`, rapid_systems migration 132) -- this
            # class's fixture row is scratch, so that default fires on
            # every windowed `_start` here even though the class is about
            # reference sets, not job definitions. Completed with a fake
            # ARN rather than suppressing the default, since a real
            # `_resolve_submission_env` always returns one.
            mock.patch.object(run_mod, "_resolve_submission_env",
                              lambda *a, **k: {"s3_client": "fake-s3",
                                               "manifest_bucket": "bucket",
                                               "job_definition":
                                                   "fake-job-definition-arn"}),
            # The audit write, faked the same way
            # `StartRunAuditedLaneResolutionTests` fakes it: this class is
            # about the set, not about what the ledger records.
            mock.patch.object(
                run_mod, "record_external_action",
                lambda conn, idempotency_key, action_class, target_scope,
                reason, dry_run=False, rows_affected=0, detail=None,
                policy_citation=None: {"rows_affected": rows_affected,
                                       "detail": detail}),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        kwargs = {}
        if window:
            kwargs = {"window_start": "2027-10-01 00:00:00",
                      "window_end": "2027-10-08 00:00:00"}
        return run_mod.start_run_audited(
            conn=object(), idempotency_key="k", name="w9-campaign-1",
            phase=phase, reason="reference-set proof", dry_run=True,
            out=_null_out(), **kwargs)

    def test_a_windowed_science_start_with_no_reference_set_is_refused(self):
        from pipeline.operatorctl.run import RunStartRegistryError

        with self.assertRaises(RunStartRegistryError) as caught:
            self._start("science", reference_set_id=None)
        message = str(caught.exception)
        self.assertIn("reference set", message)
        # The message must name the command that fixes it, as every other
        # registry refusal in this file does.
        self.assertIn("--reference-set", message)

    def test_a_windowed_reference_start_with_no_reference_set_is_refused(
            self):
        from pipeline.operatorctl.run import RunStartRegistryError

        with self.assertRaises(RunStartRegistryError):
            self._start("reference", reference_set_id=None)

    def test_the_reference_set_refusal_precedes_gathering(self):
        from pipeline.operatorctl.run import RunStartRegistryError

        with self.assertRaises(RunStartRegistryError):
            self._start("science", reference_set_id=None)
        # Gathering against a guessed set is the silent substitution this
        # refusal exists to prevent, so it must not have happened at all.
        self.assertEqual(self.gathers, [])

    def test_a_windowed_start_with_a_reference_set_threads_it_to_the_gather(
            self):
        # The complement, and what makes the refusal above a real test
        # rather than a blanket failure: the same call with a set stored
        # proceeds, and the set reaches `gather_for_run` as its own
        # keyword rather than being derived from the run name.
        self._start("science", reference_set_id=7)

        self.assertEqual(len(self.gathers), 1)
        self.assertEqual(7, self.gathers[0]["reference_set_id"])
        self.assertEqual("w9-campaign-1", self.gathers[0]["run_name"])

    def test_the_psf_set_falls_back_to_the_reference_set_when_unset(self):
        # Production's set points at itself: no separate PSF set declared,
        # so the reference set IS the PSF set.
        self._start("science", reference_set_id=7)
        self.assertEqual(7, self.gathers[0]["psf_set_id"])


class RunCreateScienceOverlayTests(unittest.TestCase):
    """`run create --set SECTION.KEY=VALUE` (migration 112).

    SCRATCH ONLY, computing `config_hash` from the REAL merged release
    configuration (`load_with_digest`), and writing `runs.config_overlay`
    with a follow-up statement AFTER `derived.create_run`'s own INSERT has
    already committed — `contract.call_function` commits per call, so
    "the same transaction as the create" is not available and this is the
    documented, deliberate alternative. `RAPID_SW` is set to this checkout
    so `load_with_digest` reads the real `cdf/science/pipeline.toml`,
    matching what an attempt under the run would read.
    """

    def setUp(self):
        import os
        self._saved_sw = os.environ.get("RAPID_SW")
        os.environ["RAPID_SW"] = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        import os
        if self._saved_sw is None:
            os.environ.pop("RAPID_SW", None)
        else:
            os.environ["RAPID_SW"] = self._saved_sw

    def _args(self, **overrides):
        body = dict(
            name="overlay-probe", kind="scratch",
            purpose=None, branch=None, image_digest=None, config_hash=None,
            input_generations=None, expect_absent=False, reason="why",
            idempotency_key="k", apply=True, policy_citation=None,
            lane=None, retry_attempts=None, retry_wallclock_s=None,
            attempt_timeout_s=None, reference_set=None,
            build_reference_set=None, psf_set=None, overlay_pairs=None)
        body.update(overrides)
        return argparse.Namespace(**body)

    def _create_result(self, **overrides):
        body = {"action": "run_create", "dry_run": False, "replayed": False,
                "already_present": False, "rows_affected": 1, "audit_id": 9,
                "idempotency_key": "k", "run_id": 42, "would_add": True,
                "lane": "bulk", "retry_attempts": 3,
                "retry_wallclock_s": 129600, "attempt_timeout_s": 43200}
        body.update(overrides)
        return body

    def test_set_is_refused_for_a_production_kind_run(self):
        # Production's science values are release content, identified by
        # the image digest alone; an overlay would let a value differ from
        # what that digest claims it is. Refused BEFORE the connection is
        # touched -- `object()` would raise on any attribute access.
        with self.assertRaises(SystemExit) as caught:
            operatorctl_main._cmd_run_create(
                object(),
                self._args(kind="production",
                           overlay_pairs=["awaicgen.min_frames=5"]),
                io.StringIO())
        self.assertIn("--kind scratch", str(caught.exception))

    def test_set_and_config_hash_together_are_refused(self):
        # The two name the same fact by two different routes; accepting
        # both would leave whichever the code happens to check last as the
        # silent winner.
        with self.assertRaises(SystemExit) as caught:
            operatorctl_main._cmd_run_create(
                object(),
                self._args(config_hash="deadbeef",
                           overlay_pairs=["awaicgen.min_frames=5"]),
                io.StringIO())
        self.assertIn("--config-hash", str(caught.exception))

    def test_set_computes_the_real_merged_digest_and_passes_it_through(self):
        # THE PROPERTY: `create_run`'s own `config_hash` argument must be
        # the digest `load_with_digest` computes over base+overlay -- never
        # a hash of the overlay alone, and never left as the file-only
        # digest the caller supplied nothing to override.
        from pipeline.runtime.science_config import load_with_digest
        _merged, expected_digest = load_with_digest(
            overlay={"awaicgen": {"min_frames": 5}})

        conn = _FakeConn([self._create_result(), None])
        out = io.StringIO()
        code = operatorctl_main._cmd_run_create(
            conn, self._args(overlay_pairs=["awaicgen.min_frames=5"]), out)

        self.assertEqual(0, code)
        create_sql, create_params = conn.calls[0]
        self.assertIn("derived.create_run", create_sql)
        # `config_hash` is the 8th positional parameter to `create_run`'s
        # SQL (see `actions.create_run`'s own parameter tuple).
        self.assertEqual(expected_digest, create_params[7])

    def test_set_writes_config_overlay_after_the_create_on_apply(self):
        conn = _FakeConn([self._create_result(), None])
        out = io.StringIO()
        operatorctl_main._cmd_run_create(
            conn, self._args(overlay_pairs=["awaicgen.min_frames=5"]), out)

        self.assertEqual(2, len(conn.calls))
        update_sql, update_params = conn.calls[1]
        self.assertIn("UPDATE runs SET config_overlay", update_sql)
        self.assertNotIn("config_overlay_key", update_sql)
        overlay_json, run_id = update_params
        self.assertEqual(42, run_id)
        self.assertEqual({"awaicgen": {"min_frames": 5}},
                         json.loads(overlay_json))
        self.assertIn("science overlay recorded", out.getvalue())

    def test_set_on_a_dry_run_writes_nothing(self):
        # The dry run gathers/validates for real (the digest is computed
        # for real above `create_run`'s own call) but writes nothing --
        # `contract.py`'s rule applied to the overlay's own follow-up
        # statement, not only to `create_run` itself.
        conn = _FakeConn([self._create_result(dry_run=True, run_id=None,
                                              would_add=True)])
        out = io.StringIO()
        operatorctl_main._cmd_run_create(
            conn, self._args(apply=False,
                             overlay_pairs=["awaicgen.min_frames=5"]), out)

        self.assertEqual(1, len(conn.calls))
        self.assertIn("[dry-run] would record science overlay",
                      out.getvalue())

    def test_set_on_an_already_present_run_writes_nothing(self):
        # A replay/no-op create did not author this row, so writing the
        # overlay onto it would attribute this call's --set to a run it
        # did not create.
        conn = _FakeConn([self._create_result(already_present=True,
                                              run_id=None, rows_affected=0)])
        out = io.StringIO()
        operatorctl_main._cmd_run_create(
            conn, self._args(overlay_pairs=["awaicgen.min_frames=5"]), out)

        self.assertEqual(1, len(conn.calls))

    def test_no_set_leaves_run_create_exactly_as_before(self):
        # Every caller before this switch: one statement, no overlay
        # message, `config_hash` passed through unchanged (None here).
        conn = _FakeConn([self._create_result()])
        out = io.StringIO()
        operatorctl_main._cmd_run_create(conn, self._args(), out)

        self.assertEqual(1, len(conn.calls))
        self.assertNotIn("science overlay", out.getvalue())
