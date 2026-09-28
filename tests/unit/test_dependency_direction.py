"""Statically scans rapidpipe's imports against its fixed layer order.

The units are every subpackage and every top-level module under
``rapidpipe/`` (``settings/`` holds TOML only and is not a unit). They
sit in layers, and a unit may import only units strictly below its own
(stage-contract.md §The package):

    leaves (exitcodes, log, revision, seams: no rapidpipe import)
    < products < {db, science} < checks < runs < stages < launch
    < selftest < cli

``db`` and ``science`` share a layer and may not import each other.
``release`` sits beside the stack: it may import only the leaves and
``db``, and only ``cli`` imports it. :data:`ALLOWED` is that rule as one
table.

Every import counts: module-level and function-level (lazy) alike,
absolute and relative (``from ..runs import x`` is resolved against the
importing module's package), and ``from rapidpipe import runs`` names
the unit ``runs``. Third-party and standard-library imports are
irrelevant here.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "rapidpipe"

LEAVES = frozenset({"exitcodes", "log", "revision", "seams"})

#: The layer order as a table: unit -> the units it may import.
ALLOWED: dict[str, frozenset[str]] = {
    "exitcodes": frozenset(),
    "log": frozenset(),
    "revision": frozenset(),
    "seams": frozenset(),
    "products": LEAVES,
    "db": LEAVES | {"products"},
    "science": LEAVES | {"products"},
    "checks": LEAVES | {"products", "db", "science"},
    "runs": LEAVES | {"products", "db", "science", "checks"},
    "stages": LEAVES | {"products", "db", "science", "checks", "runs"},
    "launch": LEAVES | {"products", "db", "science", "checks", "runs", "stages"},
    "selftest": LEAVES | {"products", "db", "science", "checks", "runs", "stages",
                          "launch"},
    "cli": LEAVES | {"products", "db", "science", "checks", "runs", "stages",
                     "launch", "selftest", "release"},
    "release": LEAVES | {"db"},
}


@dataclass(frozen=True)
class Edge:
    source: str      # importing unit
    target: str      # imported unit
    where: str       # path:line, relative to the repository root


# ======================================================================
# The scanner
# ======================================================================


def discover_units(package_root: Path) -> set[str]:
    """Every directory under ``package_root`` holding a ``.py`` file, and
    every top-level module other than ``__init__.py``."""
    units = set()
    for child in package_root.iterdir():
        if child.is_dir() and any(child.rglob("*.py")):
            units.add(child.name)
        elif child.suffix == ".py" and child.name != "__init__.py":
            units.add(child.stem)
    return units


def _module_parts(path: Path, package_root: Path) -> tuple[list[str], bool]:
    """``(dotted parts of the module, whether it is a package __init__)``."""
    rel = path.relative_to(package_root.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        return parts[:-1], True
    return parts, False


def imported_modules(source: str, module: list[str], is_package: bool
                     ) -> list[tuple[str, int]]:
    """``(absolute dotted name, line)`` for every import in ``source``,
    relative imports resolved against ``module``'s package; ``from X
    import name`` yields ``X.name`` as well as ``X``, so a submodule
    imported by name is seen."""
    tree = ast.parse(source)
    package = module if is_package else module[:-1]
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                if node.level - 1 > len(package):
                    base = []
                if node.module:
                    base = base + node.module.split(".")
            else:
                base = node.module.split(".")
            dotted = ".".join(base)
            out.append((dotted, node.lineno))
            out.extend((f"{dotted}.{alias.name}", node.lineno) for alias in node.names)
    return out


def unit_of(dotted: str, units: set[str]) -> str | None:
    parts = dotted.split(".")
    if len(parts) > 1 and parts[0] == "rapidpipe" and parts[1] in units:
        return parts[1]
    return None


def scan(package_root: Path) -> list[Edge]:
    """Every cross-unit import edge under ``package_root``."""
    units = discover_units(package_root)
    edges = []
    for path in sorted(package_root.rglob("*.py")):
        module, is_package = _module_parts(path, package_root)
        if len(module) < 2:
            continue  # rapidpipe/__init__.py belongs to no unit
        source_unit = module[1]
        where_path = path.relative_to(package_root.parent)
        seen = set()
        for dotted, line in imported_modules(path.read_text(), module, is_package):
            target = unit_of(dotted, units)
            if target is None or target == source_unit or (target, line) in seen:
                continue
            seen.add((target, line))
            edges.append(Edge(source_unit, target, f"{where_path}:{line}"))
    return edges


@pytest.fixture(scope="module")
def edges() -> list[Edge]:
    return scan(PACKAGE_ROOT)


# ======================================================================
# The rule over the real package
# ======================================================================


def test_every_unit_has_a_row_in_the_table():
    assert discover_units(PACKAGE_ROOT) == set(ALLOWED)
    assert not list((PACKAGE_ROOT / "settings").rglob("*.py"))


def test_the_table_is_the_layer_order():
    for unit, allowed in ALLOWED.items():
        assert unit not in allowed
        for below in allowed:
            assert unit not in ALLOWED[below], f"{unit} and {below} may import each other"
    assert "science" not in ALLOWED["db"] and "db" not in ALLOWED["science"]


def test_every_import_goes_down_the_layer_order(edges):
    violations = [e for e in edges if e.target not in ALLOWED[e.source]]
    assert not violations, "\n".join(
        f"{e.where}: rapidpipe.{e.source} imports rapidpipe.{e.target}, which "
        f"the layer order forbids (allowed: {sorted(ALLOWED[e.source])})"
        for e in violations)


def test_nothing_imports_cli(edges):
    assert [e.where for e in edges if e.target == "cli"] == []


def test_only_cli_imports_release(edges):
    assert [e.where for e in edges if e.target == "release" and e.source != "cli"] == []


def test_stage_modules_do_not_import_each_other():
    shared = {"rapidpipe.stages", "rapidpipe.stages.contract", "rapidpipe.stages.settings"}
    stage_modules = {p.stem for p in (PACKAGE_ROOT / "stages").glob("*.py")} - {
        "__init__", "contract", "settings"}
    for name in sorted(stage_modules):
        path = PACKAGE_ROOT / "stages" / f"{name}.py"
        for dotted, line in imported_modules(path.read_text(), ["rapidpipe", "stages", name],
                                             False):
            parts = dotted.split(".")
            if parts[:2] != ["rapidpipe", "stages"] or ".".join(parts[:3]) in shared:
                continue
            assert parts[2] not in stage_modules or parts[2] == name, (
                f"rapidpipe/stages/{name}.py:{line} imports another stage module: {dotted}")


# ======================================================================
# The scanner sees every import form
# ======================================================================


def _targets(source: str, module: str, is_package: bool = False) -> set[str]:
    units = set(ALLOWED)
    return {u for d, _ in imported_modules(source, module.split("."), is_package)
            if (u := unit_of(d, units)) is not None}


def test_scanner_resolves_a_relative_import_to_another_unit():
    assert _targets("from ..runs import repository\n", "rapidpipe.checks.x") == {"runs"}
    assert _targets("from .. import runs\n", "rapidpipe.checks.x") == {"runs"}
    assert _targets("from ...runs.repository import f\n", "rapidpipe.checks.sub.x") == {"runs"}


def test_scanner_resolves_a_relative_import_inside_a_package_init():
    assert _targets("from . import policy\n", "rapidpipe.checks", True) == {"checks"}
    assert _targets("from .. import runs\n", "rapidpipe.checks", True) == {"runs"}


def test_scanner_maps_from_rapidpipe_import_to_the_named_unit():
    assert _targets("from rapidpipe import runs, log\n", "rapidpipe.cli.x") == {"runs", "log"}
    assert _targets("from rapidpipe import __version__\n", "rapidpipe.cli.x") == set()


def test_scanner_sees_lazy_and_plain_imports():
    source = "import rapidpipe.db.connection\n\ndef f():\n    from rapidpipe.runs import x\n"
    assert _targets(source, "rapidpipe.checks.x") == {"db", "runs"}


def test_scanner_discovers_top_level_modules_as_units(tmp_path):
    root = tmp_path / "rapidpipe"
    (root / "sub").mkdir(parents=True)
    (root / "settings").mkdir()
    (root / "__init__.py").write_text("")
    (root / "leaf.py").write_text("from . import sub\n")
    (root / "sub" / "__init__.py").write_text("")
    (root / "sub" / "a.py").write_text("def f():\n    from ..leaf import g\n")
    (root / "settings" / "x.toml").write_text("")
    assert discover_units(root) == {"leaf", "sub"}
    assert {(e.source, e.target, e.where) for e in scan(root)} == {
        ("leaf", "sub", "rapidpipe/leaf.py:1"),
        ("sub", "leaf", "rapidpipe/sub/a.py:2"),
    }
