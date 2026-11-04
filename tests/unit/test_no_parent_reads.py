"""No pipeline code, operator script or named query reads a parent of child tables.

``sources`` and ``xsources`` keep their rows in child tables by date and SCA, and
``objects``, ``memberships``, ``xobjects``, ``xmemberships``, ``packets`` and ``forced``
theirs in child tables by field
(``database/schema.sql``); a read of a parent or its ``_official`` view visits every
child. The pipeline reads a set's child by name (``rapidpipe.foundation.children``), SQL
through ``sources_of`` and ``xsources_of`` (rapid_docs decisions.md, children-reads) and
``<parent>_in``, ``<parent>_of`` and ``objects_official_in`` (field-children).
``schema.sql`` and the dated files define the parents and views and are not readers.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
#: A FROM or JOIN of a parent or its view, not of a child (``sources_20261104_8``) or a function.
PARENT_READ = re.compile(r'\b(?:FROM|JOIN)\s+"?(?:x?sources|x?objects|x?memberships|packets|forced)'
                         r'(?:_official)?"?(?=[\s;)"]|$)', re.I)
READERS = sorted([*REPO.glob("rapidpipe/**/*.py"), *REPO.glob("scripts/*.py"),
                  REPO / "database" / "queries.sql"])


@pytest.mark.parametrize("path", READERS, ids=lambda p: str(p.relative_to(REPO)))
def test_no_reader_queries_a_parent_of_child_tables(path):
    hits = [f"{path.relative_to(REPO)}:{n}: {line.strip()}"
            for n, line in enumerate(path.read_text().splitlines(), start=1)
            if PARENT_READ.search(line)]
    assert hits == []


@pytest.mark.parametrize("statement, reads_parent", [
    ("SELECT 1 FROM sources s", True), ("x JOIN sources s ON", True),
    ("FROM xsources_official", True), ('FROM "sources" s', True),
    ("FROM sources_of(ARRAY[1]) s", False), ('FROM "sources_20261104_8" s', False),
    ("FROM xsources_20261104_18", False), ("FROM memberships m", True),
    ("JOIN objects o ON", True), ("FROM packets_official p", True), ("FROM forced f", True),
    ("FROM objects_official_in(ARRAY[1]) o", False), ("FROM memberships_of(ARRAY[1]) m", False),
    ("FROM packets_in(ARRAY[1]) p", False), ('FROM "forced_1234" f', False),
    ("FROM job_fields f", False), ("FROM xobjects o", True), ("JOIN xmemberships_official m", True),
    ("FROM xobjects_in(ARRAY[1]) o", False), ("FROM xmemberships_of(ARRAY[1]) m", False),
    ("FROM xobjects_official_in(ARRAY[1]) o", False)])
def test_the_guard_tells_a_parent_from_a_child_or_a_function(statement, reads_parent):
    assert bool(PARENT_READ.search(statement)) == reads_parent
