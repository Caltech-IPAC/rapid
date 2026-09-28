"""The command-line tool takes from ``rapidpipe.launch`` only public
functions and the exception classes they raise: ``launch`` owns
orchestration, ``cli`` parses and prints (stage-contract.md §The package).

Statically collects every name a ``rapidpipe/cli/*.py`` module takes from
a ``rapidpipe.launch`` module, whether imported by name (``from
rapidpipe.launch.walk import start``, or relatively, ``from ..launch.walk
import start``) or reached as an attribute through a name bound to a
module (``from rapidpipe.launch import batch as launch_batch`` then
``launch_batch.reconcile``; ``import rapidpipe.launch.walk`` then
``rapidpipe.launch.walk.start``; ``from rapidpipe import launch`` then
``launch.walk.start``), at module level or inside a function, and checks
each one, resolved at runtime. Relative imports resolve as
test_dependency_direction.py resolves them."""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest

from tests.unit.test_dependency_direction import (
    PACKAGE_ROOT,
    _module_parts,
    import_from_base,
)

CLI = PACKAGE_ROOT / "cli"
LAUNCH = "rapidpipe.launch"


def _is_module(dotted: str) -> bool:
    try:
        importlib.import_module(dotted)
    except ModuleNotFoundError:
        return False
    return True


def _in_launch(dotted: str) -> bool:
    return dotted == LAUNCH or dotted.startswith(LAUNCH + ".")


def _chain(node: ast.Attribute) -> tuple[str, list[str]] | None:
    """``("a", ["b", "c"])`` for ``a.b.c``; None unless the chain ends in a name."""
    attrs: list[str] = []
    value: ast.expr = node
    while isinstance(value, ast.Attribute):
        attrs.insert(0, value.attr)
        value = value.value
    return (value.id, attrs) if isinstance(value, ast.Name) else None


def launch_names(source: str, module: list[str], is_package: bool = False
                 ) -> set[tuple[str, str, int]]:
    """``(module, name, line)`` for every name ``source`` takes from launch."""
    tree = ast.parse(source)
    bound: dict[str, str] = {}     # local name -> the module it is bound to
    taken: set[tuple[str, str, int]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = import_from_base(node, module, is_package)
            for alias in node.names:
                dotted = f"{base}.{alias.name}"
                if _is_module(dotted):
                    bound[alias.asname or alias.name] = dotted
                elif _in_launch(base):
                    taken.add((base, alias.name, node.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bound[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    bound[top] = top
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or (chain := _chain(node)) is None:
            continue
        name, attrs = chain
        if name not in bound:
            continue
        current = bound[name]
        for attr in attrs:
            if _is_module(f"{current}.{attr}"):
                current = f"{current}.{attr}"
                continue
            if _in_launch(current):
                taken.add((current, attr, node.lineno))
            break
    return taken


def _launch_names(path: Path) -> set[tuple[str, str, int]]:
    module, is_package = _module_parts(path, PACKAGE_ROOT)
    return launch_names(path.read_text(), module, is_package)


def _all_taken() -> list[tuple[str, str, str, int]]:
    return [(path.name, module, name, line)
            for path in sorted(CLI.glob("*.py"))
            for module, name, line in sorted(_launch_names(path))]


def test_the_scan_sees_the_cli_s_launch_uses():
    names = {(module, name) for _, module, name, _ in _all_taken()}
    assert ("rapidpipe.launch.walk", "start") in names
    assert ("rapidpipe.launch.batch", "reconcile") in names
    assert ("rapidpipe.launch.loop", "run_loop") in names


@pytest.mark.parametrize("filename,module,name,line", _all_taken(),
                         ids=lambda v: str(v))
def test_cli_takes_only_public_functions_and_exceptions_from_launch(
        filename, module, name, line):
    where = f"rapidpipe/cli/{filename}:{line} takes {module}.{name}"
    assert not name.startswith("_"), f"{where}, a private name"
    value = getattr(importlib.import_module(module), name)
    is_exception = inspect.isclass(value) and issubclass(value, Exception)
    assert inspect.isroutine(value) or is_exception, (
        f"{where}, which is neither a function nor an exception class")


# ======================================================================
# The scan sees every way of reaching a launch name
# ======================================================================

_CLI_MODULE = ["rapidpipe", "cli", "x"]


def _names(source: str) -> set[tuple[str, str]]:
    return {(m, n) for m, n, _ in launch_names(source, _CLI_MODULE)}


@pytest.mark.parametrize("source", [
    "from rapidpipe.launch.walk import _declaration\n",
    "from ..launch.walk import _declaration\n",
    "from ..launch import walk\nwalk._declaration\n",
    "from . import runctl\nfrom .. import launch\nlaunch.walk._declaration\n",
    "from rapidpipe.launch import walk as w\nw._declaration\n",
    "import rapidpipe.launch.walk as w\nw._declaration\n",
    "import rapidpipe.launch.walk\nrapidpipe.launch.walk._declaration\n",
    "import rapidpipe.db\ndef f():\n    return rapidpipe.launch.walk._declaration()\n",
    "from rapidpipe import launch\nlaunch.walk._declaration.__name__\n",
    "import rapidpipe.launch as L\nL.walk._declaration\n",
], ids=["absolute-from", "relative-from", "relative-module", "relative-package-chain",
        "from-alias", "import-alias", "import-unaliased", "import-other-then-chain",
        "nested-chain", "package-alias-chain"])
def test_the_scan_sees_each_import_form(source):
    assert ("rapidpipe.launch.walk", "_declaration") in _names(source)


def test_the_scan_ignores_names_outside_launch():
    source = ("from ..runs import create\nimport rapidpipe.runs.create\n"
              "create.create_run_record\nrapidpipe.runs.create._x\n")
    assert _names(source) == set()
