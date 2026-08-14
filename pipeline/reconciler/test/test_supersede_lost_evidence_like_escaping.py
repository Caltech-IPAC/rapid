"""Fix round 2 (wave B, item 7): `--run-prefix` reaches a `LIKE` pattern
unescaped.

`supersede_lost_evidence.select_rows` builds `f"{run_prefix}%"` and matches
it with `run_id LIKE %s` — no `ESCAPE` clause. `_`  matches any one
character and `%` matches any run of characters under `LIKE`; an operator
typing a prefix that happens to contain either (this repo's `run_id`s are
typically hyphenated like `fixd-chain-...`, but nothing enforces that) had
it silently reinterpreted as a wildcard, widening the row selection beyond
what they named. The fix, `_escape_like_pattern`, is pure string logic —
tested here directly, with no database at all, and separately with a fake
cursor that pins the exact SQL/parameter shape `select_rows` sends.
"""

import unittest

from pipeline.reconciler import supersede_lost_evidence as tool


class EscapeLikePatternTests(unittest.TestCase):
    """The escaping function in isolation — no SQL, no connection."""

    def test_plain_text_is_unchanged(self):
        self.assertEqual(tool._escape_like_pattern("fixd-chain-"),
                         "fixd-chain-")

    def test_underscore_is_escaped(self):
        """`_` matches any ONE character under LIKE; a literal underscore
        in a run_id prefix (this repo does not forbid one) must not become
        a wildcard.
        """
        self.assertEqual(tool._escape_like_pattern("run_1"), "run\\_1")

    def test_percent_is_escaped(self):
        self.assertEqual(tool._escape_like_pattern("100%done"),
                         "100\\%done")

    def test_backslash_itself_is_escaped_first(self):
        """The escape character must itself be escaped, and BEFORE `_`/`%`
        are handled — escaping it after would double-escape the backslashes
        those two insertions just introduced. A literal backslash followed
        by an underscore pins the ordering: if backslash-escaping ran
        second, the underscore's own inserted backslash would itself be
        escaped again, corrupting the pattern.
        """
        self.assertEqual(tool._escape_like_pattern("a\\_b"), "a\\\\\\_b")

    def test_combination_of_all_three(self):
        self.assertEqual(tool._escape_like_pattern("a_b%c\\d"),
                         "a\\_b\\%c\\\\d")

    def test_empty_string_is_unchanged(self):
        self.assertEqual(tool._escape_like_pattern(""), "")


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    @property
    def description(self):
        return [("attempt_id",)]

    def execute(self, sql, params):
        self._conn.calls.append((sql, params))

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self):
        self.calls = []
        self.rolled_back = False

    def cursor(self):
        return _FakeCursor(self)

    def rollback(self):
        self.rolled_back = True


class SelectRowsSendsAnEscapedPatternTests(unittest.TestCase):
    """`select_rows` itself: the pattern that reaches the database is the
    ESCAPED one, with an explicit `ESCAPE` clause naming the escape
    character — not the raw, operator-typed prefix concatenated with `%`.
    """

    def test_the_pattern_parameter_is_escaped(self):
        conn = _FakeConn()
        tool.select_rows(conn, "fixd_chain-")
        self.assertEqual(len(conn.calls), 1)
        _sql, params = conn.calls[0]
        pattern = params[0]
        self.assertEqual(pattern, "fixd\\_chain-%", (
            "the underscore in the operator's own prefix must be escaped "
            "before the trailing wildcard %% is appended"))

    def test_the_sql_declares_an_escape_clause(self):
        conn = _FakeConn()
        tool.select_rows(conn, "fixd-chain-")
        sql, _params = conn.calls[0]
        self.assertIn("ESCAPE", sql.upper(), (
            "a LIKE pattern with no ESCAPE clause cannot make `_`/%% "
            "literal no matter how the parameter is built"))

    def test_a_plain_hyphenated_prefix_is_unaffected(self):
        """The ordinary case (this repo's real run_ids) round-trips with
        only the trailing wildcard added — escaping must not perturb text
        that had nothing to escape.
        """
        conn = _FakeConn()
        tool.select_rows(conn, "fixd-chain-20260806T203403Z")
        _sql, params = conn.calls[0]
        self.assertEqual(params[0], "fixd-chain-20260806T203403Z%")

    def test_select_rows_still_rolls_back_read_only(self):
        conn = _FakeConn()
        tool.select_rows(conn, "any-")
        self.assertTrue(conn.rolled_back)


if __name__ == "__main__":
    unittest.main()
