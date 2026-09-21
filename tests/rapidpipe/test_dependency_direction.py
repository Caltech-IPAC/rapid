"""Statically scans imports in each rapidpipe subpackage against the stage
contract's fixed dependency direction:

- products: no runs, db, stages
- db: no runs, stages
- runs: composes products and db; no stages, launch, cli
- science: no stages, launch, cli
- stages: no other stage module, no launch, no cli
- launch: may use products/db/runs/stages; not itself imported by a stage
  (checked from the stages side, since launch may import stages)
- cli: may import anything; nothing else imports cli

Only imports of other rapidpipe subpackages are checked -- third-party and
standard-library imports are irrelevant to this rule.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "rapidpipe"

SUBPACKAGES = (
    "stages", "products", "db", "runs", "launch", "cli", "science",
)


def _iter_python_files(subpackage: str):
    subpackage_dir = PACKAGE_ROOT / subpackage
    if not subpackage_dir.is_dir():
        return
    yield from subpackage_dir.rglob("*.py")


def _imported_subpackages(path: Path) -> set[str]:
    """Return the set of rapidpipe subpackage names ``path`` imports from."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()

    for node in ast.walk(tree):
        module = None
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name
                found |= _subpackage_from_dotted(module)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # A relative import (e.g. "from . import x" inside
                # rapidpipe.stages) stays within its own subpackage by
                # construction and needs no cross-subpackage check.
                continue
            module = node.module
            if module:
                found |= _subpackage_from_dotted(module)
    return found


def _subpackage_from_dotted(dotted: str) -> set[str]:
    parts = dotted.split(".")
    if parts and parts[0] == "rapidpipe" and len(parts) > 1:
        return {parts[1]}
    return set()


def _all_imports(subpackage: str) -> set[str]:
    imports: set[str] = set()
    for path in _iter_python_files(subpackage):
        imports |= _imported_subpackages(path)
    # A subpackage's own name showing up (e.g. rapidpipe.stages.contract
    # importing rapidpipe.stages.settings) is not a cross-subpackage
    # dependency for this rule's purposes.
    imports.discard(subpackage)
    return imports


FORBIDDEN = {
    "products": {"runs", "db", "stages", "launch", "cli"},
    "db": {"runs", "stages", "launch", "cli"},
    "runs": {"stages", "launch", "cli"},
    "science": {"stages", "launch", "cli"},
    "stages": {"launch", "cli"},
}


@pytest.mark.parametrize("subpackage", sorted(FORBIDDEN))
def test_subpackage_does_not_import_forbidden_dependencies(subpackage):
    imports = _all_imports(subpackage)
    forbidden = FORBIDDEN[subpackage]
    violations = imports & forbidden
    assert not violations, (
        f"rapidpipe.{subpackage} imports {sorted(violations)}, which the "
        "stage contract's fixed dependency direction forbids")


def test_stage_modules_do_not_import_each_other():
    stages_dir = PACKAGE_ROOT / "stages"
    for path in stages_dir.glob("*.py"):
        if path.name in ("__init__.py", "contract.py", "settings.py"):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("rapidpipe.stages.") or (
                    node.module in ("rapidpipe.stages.contract", "rapidpipe.stages.settings")
                ), f"{path.name} imports another stage module: {node.module}"


def test_nothing_imports_cli():
    for subpackage in SUBPACKAGES:
        if subpackage == "cli":
            continue
        imports = _all_imports(subpackage)
        assert "cli" not in imports, (
            f"rapidpipe.{subpackage} imports rapidpipe.cli, but nothing in "
            "the package may import the command-line tool back")
