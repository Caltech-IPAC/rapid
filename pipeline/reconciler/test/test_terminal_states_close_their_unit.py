"""Every TERMINAL attempt state must dispose of its work unit.

**THE DEFECT THIS REFUSES, AND WHY IT IS A CLASS RATHER THAN A BUG.**
`_transition` has one branch per terminal classification. Two of them
(`CLASS_NEVER_STARTED`, `CLASS_ABRUPT_LOSS`) handed the unit to
`_close_work_unit`; the contradictory branch flagged the attempt and
returned. So a unit whose ONLY attempt died before writing a terminal
record stranded in `submitted` forever and needed a human with operator
rights to clear it. Work unit 352 was the first one ever produced —
`missing_or_contradictory` was 0 at every gate of every prior campaign —
and under launch cadence it recurs at the rate of any startup-class
failure.

The bug was one missing call. The CLASS is "a terminal branch that forgets
its unit", and nothing structural stopped a fourth branch from being added
the same way. So this file does not test the contradictory path by
example: it reads `_transition`'s own source and asserts that EVERY
terminal branch reaches the disposition helper. A future terminal state
added without one fails here, at the point it is written, rather than in
production a campaign later.
"""

import inspect
import re
import unittest

from pipeline.reconciler import service


def _code_only(text):
    """`text` with comment lines removed.

    These branches carry long explanatory comments — including prose that
    names `return` and `_close_work_unit` while describing the defect — and
    a scan that read those would slice the branch at a word in a sentence
    rather than at a statement. The property under test is what the CODE
    does, so the comments come out first.
    """
    return "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith("#"))


#: The terminal classifications `_transition` dispatches on. Read from the
#: module rather than restated, so a renamed constant surfaces here.
TERMINAL_CLASSIFICATIONS = ("CLASS_NEVER_STARTED", "CLASS_ABRUPT_LOSS")


class TerminalBranchesDisposeOfTheirUnitTests(unittest.TestCase):

    def setUp(self):
        self.source = _code_only(
            inspect.getsource(service.ReconcilerService._transition))

    def test_the_contradictory_branch_disposes_of_its_work_unit(self):
        """The specific regression: work unit 352's shape.

        The branch flags the attempt `missing_or_contradictory` and must
        then hand the unit to `_close_work_unit`, exactly as its siblings
        do. Without this the unit strands in `submitted` with no live
        attempt to carry it and no automated path out.
        """
        start = self.source.index("_is_contradictory")
        # The branch runs to its own `return`.
        branch = self.source[start:self.source.index("return", start)]
        self.assertIn(
            "_close_work_unit", branch,
            "the contradictory branch flags the attempt but never disposes "
            "of its work unit, so a unit whose only attempt died pre-record "
            "strands in `submitted` until a human intervenes")

    def test_every_terminal_branch_reaches_the_disposition_helper(self):
        """The class: no terminal branch may forget its unit.

        Each `if classification == CLASS_*` block must mention
        `_close_work_unit`. This is deliberately structural — asserting on
        source rather than on behaviour — because the failure it guards is
        an ABSENCE, and an absent call has no behaviour to observe: the unit
        simply sits in `submitted` and nothing raises, logs or fails.
        """
        for name in TERMINAL_CLASSIFICATIONS:
            self.assertTrue(hasattr(service, name),
                            "%s is no longer a reconciler constant; update "
                            "this test's list with the rename" % name)
            marker = "classification == %s" % name
            self.assertIn(marker, self.source,
                          "_transition no longer dispatches on %s" % name)
            start = self.source.index(marker)
            branch = self.source[start:self.source.index("return", start)]
            self.assertIn(
                "_close_work_unit", branch,
                "the %s branch reaches a terminal attempt state without "
                "disposing of its work unit — the work unit 352 defect, in "
                "a different branch" % name)

    def test_no_terminal_branch_returns_before_disposing(self):
        """A guard against the fix being undone by an early return.

        Catches the shape the defect actually had: a branch that does its
        attempt-level work, returns, and leaves the unit behind. Every
        `return` inside a terminal branch must come after the disposition
        call, which is what asserting on the branch text up to its FIRST
        return achieves in the two tests above — this one pins the count so
        an added early return cannot slip a path past them.
        """
        for marker in ("_is_contradictory",
                       "classification == CLASS_NEVER_STARTED",
                       "classification == CLASS_ABRUPT_LOSS"):
            start = self.source.index(marker)
            branch = self.source[start:self.source.index("return", start)]
            disposals = len(re.findall(r"_close_work_unit\(", branch))
            self.assertEqual(
                1, disposals,
                "the branch at %r should dispose of its unit exactly once "
                "before returning; found %d" % (marker, disposals))


if __name__ == "__main__":
    unittest.main()
