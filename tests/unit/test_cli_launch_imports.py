"""The command-line tool takes from ``rapidpipe.launch`` only public
functions and the exception classes they raise: ``launch`` owns
orchestration, ``cli`` parses and prints (stage-contract.md §The package).

Statically collects every name a ``rapidpipe/cli/*.py`` module takes from
a ``rapidpipe.launch`` module, whether imported by name (``from
rapidpipe.launch.walk import start``) or used as an attribute of a module
alias (``from rapidpipe.launch import batch as launch_batch`` then
``launch_batch.reconcile``), at module level or inside a function, and
checks each one, resolved at runtime."""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parents[2] / "rapidpipe" / "cli"


def _is_module(dotted: str) -> bool:
    try:
        importlib.import_module(dotted)
    except ModuleNotFoundError:
        return False
    return True


def _launch_names(path: Path) -> list[tuple[str, str, int]]:
    """``(module, name, line)`` for every name ``path`` takes from launch."""
    tree = ast.parse(path.read_text(), filename=str(path))
    aliases: dict[str, str] = {}
    taken: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and (
                node.module == "rapidpipe.launch" or node.module.startswith("rapidpipe.launch.")):
            for alias in node.names:
                dotted = f"{node.module}.{alias.name}"
                if _is_module(dotted):
                    aliases[alias.asname or alias.name] = dotted
                else:
                    taken.append((node.module, alias.name, node.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("rapidpipe.launch.") and alias.asname:
                    aliases[alias.asname] = alias.name
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id in aliases):
            taken.append((aliases[node.value.id], node.attr, node.lineno))
    return taken


def _all_taken() -> list[tuple[str, str, str, int]]:
    return [(path.name, module, name, line)
            for path in sorted(CLI.glob("*.py"))
            for module, name, line in _launch_names(path)]


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
