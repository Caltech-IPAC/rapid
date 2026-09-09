"""Guards against the positional-`debug` defect in `execute_sql_queries`
call sites.

`RAPIDDB.execute_sql_queries(self, sql_queries, params_list=None, debug=1)`
used to take `debug` as its second positional parameter, before commit
364aef1d inserted `params_list` ahead of it. Thirteen call sites across the
repo still called it as `execute_sql_queries(sql_queries, debug)` — a call
that still type-checks and still runs, but now binds the caller's `debug`
value into `params_list`. Inside the method, `params_list[i]` then
subscripts that integer (`params_list is not None` is true whenever
`debug` is 0 or 1, so the guard never catches it), raising
`TypeError: 'int' object is not subscriptable` the first time the query
loop runs. The fix is at each call site, not in the method: pass `debug`
by keyword (`debug=debug`), so a caller that also needs `params_list` puts
it in the correct slot and one that doesn't leaves it at its default.

This test parses every `*.py` file under the repository root with `ast`
and asserts that no call to a method named `execute_sql_queries` passes
two or more positional arguments — the shape that lets `debug` (or
anything else) land in `params_list` by accident. It does not import any
of the call sites (several need psycopg2/boto3/healpy at import time), so
it runs anywhere `ast` and `pathlib` do.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]

SKIP_DIR_NAMES = {".venv", ".git", "rapid_pipeline.egg-info"}


def _iter_repo_python_files():
    for path in REPO_ROOT.rglob("*.py"):
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        yield path


def _offending_calls(path):
    """Calls in `path` to a method named `execute_sql_queries` with 2+
    positional arguments (the call itself, e.g. `dbh.execute_sql_queries`,
    counts as the receiver, not a positional arg)."""
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        # Not a defect this test is responsible for catching.
        return []

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute)
                and func.attr == "execute_sql_queries"):
            continue
        if len(node.args) >= 2:
            offenders.append(node.lineno)
    return offenders


def test_no_call_site_passes_debug_positionally():
    violations = {}
    for path in _iter_repo_python_files():
        offenders = _offending_calls(path)
        if offenders:
            violations[str(path.relative_to(REPO_ROOT))] = offenders

    assert not violations, (
        "execute_sql_queries() called with 2+ positional arguments -- "
        "the second one lands in params_list, not debug (pass debug by "
        "keyword instead):\n"
        + "\n".join(f"  {file}:{','.join(map(str, lines))}"
                     for file, lines in sorted(violations.items()))
    )
