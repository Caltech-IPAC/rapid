"""scripts/schema_diagram.py: it reads every table of schema.sql, and the committed diagram matches it."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from rapidpipe.foundation.children import FIELD_PARENTS

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("schema_diagram", ROOT / "scripts" / "schema_diagram.py")
schema_diagram = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(schema_diagram)

SCHEMA = (ROOT / "database" / "schema.sql").read_text()
TABLES = schema_diagram.parse(SCHEMA)
PARENTS = schema_diagram.child_parents(SCHEMA)


def test_every_table_is_read_with_its_columns():
    assert sorted(TABLES) == sorted(re.findall(r"^CREATE TABLE (\w+) \(", SCHEMA, re.M))
    assert all(TABLES.values())


def test_constraint_lines_are_not_read_as_columns():
    names = [c[0] for cols in TABLES.values() for c in cols]
    assert not {"UNIQUE", "CHECK", "PRIMARY"} & set(names)
    assert [c[0] for c in TABLES["official"]] == [
        "id", "product", "key", "job", "run", "survey", "dataset",
        "opened_group", "opened_by", "opened_at", "opened_why",
        "closed_group", "closed_by", "closed_at", "closed_why"]


def test_every_declared_reference_is_read():
    read = sum(1 for cols in TABLES.values() for c in cols if c[3])
    assert read == len(re.findall(r"REFERENCES \w+ \(\w+\)", SCHEMA))
    assert ("jobs", "id") in [c[3] for c in TABLES["memberships"]]


def test_a_convention_column_the_schema_lacks_is_refused():
    tables = {t: [c for c in cols if not (t == "forced" and c[0] == "pid")] for t, cols in TABLES.items()}
    with pytest.raises(ValueError, match="forced.pid"):
        schema_diagram.check(tables)


def test_the_committed_dot_matches_schema_sql():
    committed = (ROOT / "database" / "schema.dot").read_text()
    assert committed == schema_diagram.to_dot(TABLES, PARENTS), (
        "database/schema.dot is stale: run python scripts/schema_diagram.py and commit "
        "database/schema.dot and database/schema.svg")


def test_dot_only_writes_the_dot_beside_out(tmp_path):
    out = tmp_path / "schema.svg"
    assert schema_diagram.main(["--out", str(out), "--dot-only"]) == 0
    assert (tmp_path / "schema.dot").read_text() == schema_diagram.to_dot(TABLES, PARENTS)
    assert not out.exists()


def test_the_parents_of_child_tables_are_marked_with_their_childrens_names():
    assert PARENTS == {"sources": "sources_<yyyymmdd>_<sca>", "xsources": "xsources_<yyyymmdd>_<sca>",
                       **{t: f"{t}_<field>" for t in FIELD_PARENTS}}
    dot = schema_diagram.to_dot(TABLES, PARENTS)
    for parent in ("sources", "xsources"):
        assert f"rows in children {parent}_&lt;yyyymmdd&gt;_&lt;sca&gt;" in dot
    for parent in FIELD_PARENTS:
        assert f"rows in children {parent}_&lt;field&gt;" in dot
    assert dot.count("rows in children") == 2 + len(FIELD_PARENTS)
