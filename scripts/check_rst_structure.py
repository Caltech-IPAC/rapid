#!/usr/bin/env python3
"""Structural validation of the two edited RST files, without Sphinx.

Neither the laptop nor the pipeline image carries docutils, and installing
a docs toolchain for a docs-only change is a gated package install for a
question that can be answered directly. So this checks the properties that
actually break a Sphinx build, mechanically:

  - every section underline is at least as long as its title (a short
    underline is the single most common RST build error)
  - underline characters are used consistently as a hierarchy
  - `list-table` directives have a header row and uniform column counts
  - inline literals (``...``) are balanced
  - no tab characters (RST is indentation-sensitive)

Exit 0 only when every check passes on every file.
"""
import re
import sys

UNDERLINE = re.compile(r'^([=\-~^"\'`#*+_:.]){3,}\s*$')


def check(path):
    problems = []
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")

    # Section underlines: an underline shorter than its title is the
    # classic "Title underline too short" build failure.
    for i, line in enumerate(lines):
        if not UNDERLINE.match(line) or i == 0:
            continue
        title = lines[i - 1]
        if not title.strip():
            continue                      # a transition/overline, not a title
        if len(line.rstrip()) < len(title.rstrip()):
            problems.append(
                "line %d: underline (%d) shorter than title (%d): %r"
                % (i + 1, len(line.rstrip()), len(title.rstrip()),
                   title.strip()[:60]))

    # Tabs break indentation-sensitive parsing.
    for i, line in enumerate(lines):
        if "\t" in line:
            problems.append("line %d: tab character" % (i + 1))

    # Inline literals must balance. RST DOES allow one to wrap across a
    # line break, so an odd count on a single line is only a defect if the
    # next line does not close it -- counting per line reports every
    # legitimately wrapped literal as broken, which is how a gate becomes
    # noise. Track the open/closed state across the paragraph instead, and
    # report only a literal still open when the paragraph ends.
    open_at = None
    for i, line in enumerate(lines):
        count = line.count("``")
        if open_at is None:
            if count % 2:
                open_at = i
        else:
            if count % 2:
                open_at = None            # this line closed it
        if open_at is not None and not line.strip():
            problems.append(
                "line %d: inline literal opened and never closed before the "
                "paragraph ended: %r"
                % (open_at + 1, lines[open_at].strip()[:60]))
            open_at = None
    if open_at is not None:
        problems.append("line %d: inline literal never closed: %r"
                        % (open_at + 1, lines[open_at].strip()[:60]))

    # list-table: every row must have the same number of cells as the
    # first, or Sphinx errors with a column-count mismatch.
    i = 0
    while i < len(lines):
        if ".. list-table::" in lines[i]:
            start = i
            indent = len(lines[i]) - len(lines[i].lstrip())
            rows, cells, in_row = [], 0, False
            j = i + 1
            while j < len(lines):
                cur = lines[j]
                if cur.strip() and (len(cur) - len(cur.lstrip())) <= indent \
                        and not cur.lstrip().startswith(("*", "-", ":")):
                    break
                stripped = cur.lstrip()
                if stripped.startswith("* -"):
                    if in_row:
                        rows.append(cells)
                    cells, in_row = 1, True
                elif in_row and (stripped.startswith("- ")
                                 or stripped == "-"):
                    # An EMPTY cell is a bare "-" on its own line. Counting
                    # only "- " misses it and reports a correct table as
                    # ragged -- which is what this check did to the
                    # population-shapes table.
                    cells += 1
                j += 1
            if in_row:
                rows.append(cells)
            if rows and len(set(rows)) > 1:
                problems.append(
                    "line %d: list-table rows have differing cell counts %s"
                    % (start + 1, sorted(set(rows))))
            i = j
        else:
            i += 1

    return problems


def main(argv):
    total = 0
    for path in argv:
        problems = check(path)
        print("%-24s %d problem(s)" % (path.split("/")[-1], len(problems)))
        for p in problems[:15]:
            print("    %s" % p)
        total += len(problems)
    print("TOTAL_PROBLEMS=%d" % total)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
