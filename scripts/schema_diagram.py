"""Draw database/schema.sql as an entity diagram: database/schema.dot, rendered to database/schema.svg.

Outside the pipeline: it reads the schema file only, never a database. Every
table and column comes from the ``CREATE TABLE`` statements, and every solid
arrow from a declared ``REFERENCES``. The dashed arrows are the references the
code follows that the schema does not declare (a child row's ``(job, rfid)``,
a membership's ``(object_job, aid)``); they are listed here by hand, in
``CONVENTION``, and the script refuses a listed column the schema lacks. The
three sets whose child rows ride on one binding are drawn as frames
(``GROUPS``, from the ``<table>_official`` views). A parent of child
tables (its ``CHECK (false) NO INHERIT``: the parent holds no rows) is
marked with its children's names: ``<parent>_<field>`` for the parents
``create_field_child`` names, ``<parent>_<yyyymmdd>_<sca>`` for the others. Every product table's
``job -> jobs.id`` is declared but not drawn.

Usage::

    python scripts/schema_diagram.py [--schema database/schema.sql] [--out database/schema.svg]

writes the DOT file beside ``--out`` and renders it with Graphviz's ``dot``
(``brew install graphviz``); ``--dot-only`` skips the render.
``tests/unit/test_schema_diagram.py`` fails when the committed
``database/schema.dot`` no longer matches ``schema.sql``, so a schema change
reruns this script.
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import sys
from pathlib import Path

from rapidpipe.exitcodes import ArgumentParser, ExitCode

ROOT = Path(__file__).resolve().parents[1]
BOOKKEEPING = ["runs", "jobs", "official", "job_inputs", "job_fields", "exposure_ids"]
STATIC = ["filters", "scas", "surveys", "fields"]
#: Sets whose child rows share one official binding (schema.sql's views).
GROUPS = {
    "refimages": ["refimages", "refimimages", "refimcatalogs"],
    "sources": ["sources", "xsources", "memberships", "objects", "xmemberships", "xobjects"],
    "packets": ["packets", "forced", "external"],
}
#: References the code follows that the schema does not declare:
#: (table, column, referenced table, referenced column).
CONVENTION = [
    ("refimimages", "rfid", "refimages", "rfid"),
    ("refimimages", "rid", "l2files", "rid"),
    ("refimcatalogs", "rfid", "refimages", "rfid"),
    ("diffimages", "rid", "l2files", "rid"),
    ("diffimages", "rfid", "refimages", "rfid"),
    ("sources", "pid", "diffimages", "pid"),
    ("xsources", "pid", "diffimages", "pid"),
    ("memberships", "aid", "objects", "aid"),
    ("memberships", "sid", "sources", "sid"),
    ("xmemberships", "xaid", "xobjects", "xaid"),
    ("xmemberships", "xsid", "xsources", "xsid"),
    ("packets", "aid", "objects", "aid"),
    ("packets", "sid", "sources", "sid"),
    ("forced", "aid", "objects", "aid"),
    ("forced", "rid", "l2files", "rid"),
    ("forced", "pid", "diffimages", "pid"),
    ("external", "sid", "sources", "sid"),
    ("external", "catalog_job", "catalogs", "job"),
]
TYPES = [
    (r"double precision", "float8"),
    (r"character varying\((\d+)\)", r"varchar(\1)"),
    (r"timestamp with time zone", "timestamptz"),
    (r"timestamp without time zone", "timestamp"),
    (r"integer\[\]", "int[]"),
    (r"\binteger\b", "int"),
]


def parse(text: str) -> dict[str, list[tuple[str, str, bool, tuple[str, str] | None]]]:
    """Read every CREATE TABLE in a schema's text into its columns and declared foreign keys.

    Parameters
    ----------
    text : str
        The schema file's text.

    Returns
    -------
    dict
        Table name to a list of ``(column, type, is_pk, references)``, in
        column order, where ``references`` is ``(table, column)`` or None.
    """
    tables = {}
    for m in re.finditer(r"CREATE TABLE (\w+) \((.*?)\n\);", text, re.S):
        cols = []
        for line in m.group(2).split("\n"):
            line = line.split("--")[0].rstrip().rstrip(",")
            cm = re.match(r"    (\w+) (.+)$", line)
            if not cm or cm.group(1) in ("UNIQUE", "CHECK", "PRIMARY"):
                continue
            name, rest = cm.groups()
            ref = re.search(r"REFERENCES (\w+) \((\w+)\)", rest)
            typ = re.split(r" (?:GENERATED|DEFAULT|NOT NULL|PRIMARY|REFERENCES|CHECK|UNIQUE)", rest)[0]
            for pat, rep in TYPES:
                typ = re.sub(pat, rep, typ)
            cols.append((name, typ, "PRIMARY KEY" in rest, ref.groups() if ref else None))
        tables[m.group(1)] = cols
    return tables


def child_parents(text: str) -> dict[str, str]:
    """The tables whose rows live in child tables (each with a ``CHECK (false) NO INHERIT``) and their children's names.

    Parameters
    ----------
    text : str
        The schema file's text.

    Returns
    -------
    dict of str to str
        Each parent to the pattern of its children's names: ``<parent>_<field>``
        for a parent ``create_field_child`` lists, else ``<parent>_<yyyymmdd>_<sca>``
        (``create_catalog_child``).
    """
    listed = re.search(r"FUNCTION create_field_child\(.*?ARRAY\[([^\]]*)\]", text, re.S)
    by_field = set(re.findall(r"'(\w+)'", listed.group(1))) if listed else set()
    return {parent: f"{parent}_<field>" if parent in by_field else f"{parent}_<yyyymmdd>_<sca>"
            for parent in re.findall(r"ALTER TABLE (\w+) ADD CONSTRAINT \w+ CHECK \(false\) NO INHERIT;",
                                     text)}


def check(tables: dict) -> None:
    """Refuse a layout or convention reference that names a table or column the schema lacks.

    Parameters
    ----------
    tables : dict
        The schema as returned by ``parse``.

    Returns
    -------
    None
        Raises ``ValueError`` naming the first missing table or column.
    """
    placed = BOOKKEEPING + STATIC + [t for g in GROUPS.values() for t in g]
    for t in placed:
        if t not in tables:
            raise ValueError(f"layout names table {t}, which schema.sql lacks")
    for t, col, rt, rc in CONVENTION:
        for table, column in ((t, col), (rt, rc)):
            if column not in [c[0] for c in tables.get(table, [])]:
                raise ValueError(f"CONVENTION names {table}.{column}, which schema.sql lacks")


def label(name: str, cols: list, fill: str, parent: str | None = None) -> str:
    """Build a Graphviz HTML-like table label with a west and an east port per column.

    Parameters
    ----------
    name : str
        Table name.
    cols : list
        The table's columns as returned by ``parse``.
    fill : str
        Header fill colour.
    parent : str, optional
        For a parent of child tables, the pattern of its children's names, shown under its header.

    Returns
    -------
    str
        The label, wrapped in ``<...>``.
    """
    rows = [f'<TR><TD COLSPAN="2" BGCOLOR="{fill}" ALIGN="LEFT"><B>{name}</B></TD></TR>']
    if parent:
        rows.append(f'<TR><TD COLSPAN="2" ALIGN="LEFT"><FONT COLOR="#c2410c">rows in children'
                    f' {html.escape(parent)}</FONT></TD></TR>')
    for col, typ, pk, _ in cols:
        c = f"<B>{html.escape(col)}</B>" if pk else html.escape(col)
        mark = '<FONT COLOR="#c2410c"> PK</FONT>' if pk else ""
        rows.append(f'<TR><TD PORT="{col}_w" ALIGN="LEFT">{c}{mark}</TD>'
                    f'<TD PORT="{col}_e" ALIGN="LEFT"><FONT COLOR="#6b7785">{html.escape(typ)}</FONT></TD></TR>')
    return ('<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" CELLPADDING="3" COLOR="#9aa8b8">'
            + "".join(rows) + "</TABLE>>")


def to_dot(tables: dict, parents: dict[str, str] | None = None) -> str:
    """Write the schema's entity diagram as DOT text.

    Parameters
    ----------
    tables : dict
        The schema as returned by ``parse``.
    parents : dict of str to str, optional
        The parents of child tables and their children's names, as :func:`child_parents` reads them.

    Returns
    -------
    str
        The DOT source, ending in a newline.
    """
    check(tables)
    parents = parents or {}
    grouped = {t for g in GROUPS.values() for t in g}
    lines = [
        "// Generated by scripts/schema_diagram.py from database/schema.sql; do not edit.",
        "digraph schema {",
        '  graph [rankdir=LR, newrank=true, nodesep=0.25, ranksep=0.9, fontname="Helvetica", fontsize=12,'
        ' label="RAPID foundation schema (database/schema.sql). Solid: declared foreign key. Dashed:'
        ' reference by convention, not enforced. Every product table also declares job -> jobs.id (not'
        ' drawn); official binds (product, key) -> job.", labelloc=t, labeljust=l];',
        '  node [shape=plaintext, fontname="Menlo", fontsize=10];',
        '  edge [arrowsize=0.6, color="#5d6b7c"];',
    ]

    def node(t: str, fill: str) -> None:
        lines.append(f"    {t} [label={label(t, tables[t], fill, parents.get(t))}];")

    lines.append('  subgraph cluster_bookkeeping { label="bookkeeping"; style=rounded; color="#9aa8b8";')
    for t in BOOKKEEPING:
        node(t, "#f6d9c8")
    lines.append("  }")
    lines.append('  subgraph cluster_static { label="static"; style=rounded; color="#9aa8b8";')
    for t in STATIC:
        node(t, "#e4e7eb")
    lines.append("  }")
    for g, members in GROUPS.items():
        lines.append(f'  subgraph cluster_{g} {{ label="one binding: {g}"; style="rounded,dashed,filled";'
                     ' fillcolor="#f2f5f8"; color="#9aa8b8";')
        for t in members:
            node(t, "#dfe6ee")
        lines.append("  }")
    for t in tables:
        if t not in BOOKKEEPING and t not in STATIC and t not in grouped:
            node(t, "#dfe6ee")

    # Edges run parent -> child with dir=back, so parents rank to the left and
    # the arrowhead still points at the referenced table.
    for t, cols in tables.items():
        for col, _, _, ref in cols:
            if not ref or (ref[0] == "jobs" and t not in BOOKKEEPING):
                continue
            style = ' color="#b4bcc6"' if ref[0] in STATIC else ""
            lines.append(f"  {ref[0]}:{ref[1]}_e:e -> {t}:{col}_w:w [dir=back{style}];")
    for t, col, rt, rc in CONVENTION:
        lines.append(f"  {rt}:{rc}_e:e -> {t}:{col}_w:w [dir=back style=dashed];")
    lines.append("}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Write the DOT file and, unless told not to, render it with Graphviz.

    Parameters
    ----------
    argv : list of str, optional
        Command-line arguments; ``sys.argv[1:]`` when None.

    Returns
    -------
    int
        The process exit code: 0 written, 64 Graphviz not on PATH, 65 the
        schema lacks a table or column the layout names, 70 Graphviz failed.
    """
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--schema", type=Path, default=ROOT / "database" / "schema.sql")
    parser.add_argument("--out", type=Path, default=ROOT / "database" / "schema.svg",
                        help="rendered diagram; its suffix (svg, pdf, png) picks the format")
    parser.add_argument("--dot-only", action="store_true", help="write the DOT file and stop")
    args = parser.parse_args(argv)

    try:
        text = args.schema.read_text()
        dot_text = to_dot(parse(text), child_parents(text))
    except ValueError as exc:
        print(f"schema_diagram: {exc}", file=sys.stderr)
        return ExitCode.INPUT_REJECTED
    dot_path = args.out.with_suffix(".dot")
    dot_path.write_text(dot_text)
    print(dot_path)
    if args.dot_only:
        return ExitCode.SUCCESS
    if shutil.which("dot") is None:
        print("schema_diagram: Graphviz's dot is not on PATH; install it or pass --dot-only", file=sys.stderr)
        return ExitCode.USAGE
    fmt = args.out.suffix.lstrip(".")
    done = subprocess.run(["dot", f"-T{fmt}", str(dot_path), "-o", str(args.out)])
    if done.returncode != 0:
        return ExitCode.STAGE_ERROR
    print(args.out)
    return ExitCode.SUCCESS


if __name__ == "__main__":
    sys.exit(main())
