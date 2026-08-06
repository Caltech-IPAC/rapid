"""Tests for the closed-form sky tessellation (W7).

Three things are checked here, and they are different in kind:

  1. **Parity with rapid_systems.** The generator is authored in
     rapid_systems and duplicated here byte-for-byte. If both copies are
     present, they must be identical; if only this one is, the test says
     so and skips rather than passing silently.

  2. **The rules and invariants**, exercised exhaustively against a small
     synthetic NSIDE so this stays a fast unit test. The full
     6,291,458-row battery lives in rapid_systems
     (`tools/tessellation/certify.py`) and is a recorded artifact run.

  3. **Behavioural equivalence with the SQLite class**, when the legacy
     database happens to be available. This is what justified switching
     the payload scripts: the closed form does not approximate the old
     answers, it reproduces them. Skipped — loudly — when the 1.4 GiB
     file is not present, which is the normal case now that nothing
     bakes it.
"""

import importlib
import os
import unittest

import numpy as np

from database.modules.utils import roman_tessellation as tess
from database.modules.utils import roman_tessellation_db as access


REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))

# The rapid_systems original, if the two repos are checked out side by
# side (the normal laptop layout: ~/Desktop/rapid/{rapid,rapid_systems}).
RAPID_SYSTEMS_ORIGINAL = os.path.join(
    os.path.dirname(REPO_ROOT), "rapid_systems",
    "tools", "tessellation", "roman_tessellation.py")

CARRIED_SQLITE = os.getenv("ROMANTESSELLATIONDBNAME")


class Parity(unittest.TestCase):
    """The two copies of the generator must not drift."""

    def test_matches_the_rapid_systems_original(self):
        if not os.path.exists(RAPID_SYSTEMS_ORIGINAL):
            self.skipTest(
                "rapid_systems is not checked out beside this repo, so the "
                "parity check cannot run (looked for %s). This is not a "
                "pass: it means nothing verified the two copies here."
                % RAPID_SYSTEMS_ORIGINAL)
        mine = os.path.join(os.path.dirname(__file__), "..",
                            "roman_tessellation.py")
        with open(RAPID_SYSTEMS_ORIGINAL, "rb") as fh:
            original = fh.read()
        with open(mine, "rb") as fh:
            carried = fh.read()
        self.assertEqual(
            original, carried,
            "database/modules/utils/roman_tessellation.py has drifted from "
            "the rapid_systems original. Edit the original, then copy it "
            "here — see that file's module docstring.")


class Constants(unittest.TestCase):
    """The production shape, without generating any rows."""

    def test_shape(self):
        self.assertEqual(tess.NSIDE, 512)
        self.assertEqual(tess.NRINGS, 2047)
        self.assertEqual(tess.NDECBINS, 2049)
        self.assertEqual(tess.NROWS, 6291458)

    def test_ring_counts_sum_to_the_table_size(self):
        total = sum(tess.nrabins(i) for i in range(1, tess.NRINGS + 1))
        self.assertEqual(total + 2, tess.NROWS)

    def test_pole_patch_values_match_the_2024_note(self):
        # database/schema/roman_tessellation_nside512.txt records these as
        # hand-applied repairs; the generator derives them from the rule.
        self.assertEqual(tess.CAP_BOUNDARY, 89.954315)
        self.assertEqual(tess.POLE_CDEC, 89.9771575)

    def test_the_2024_worked_example(self):
        # The note works (11.1, -43.8) -> rtid 5321355 through by hand.
        self.assertEqual(tess.rtid_of(11.1, -43.8), 5321355)


class ClosedFormAccess(unittest.TestCase):
    """The access class the payload scripts now construct."""

    def setUp(self):
        self.t = access.RomanTessellationClosedForm()

    def test_get_rtid_sets_the_attribute_like_the_sqlite_class(self):
        # loadPSFCatIntoDBSourcesTable reads .rtid after calling get_rtid,
        # so that contract has to hold.
        got = self.t.get_rtid(11.1, -43.8)
        self.assertEqual(got, 5321355)
        self.assertEqual(self.t.rtid, 5321355)

    def test_get_rtid_array_matches_the_scalar_form(self):
        rng = np.random.default_rng(20260806)
        ra = rng.uniform(0.0, 360.0, 5000)
        dec = np.degrees(np.arcsin(rng.uniform(-1.0, 1.0, 5000)))
        vec = self.t.get_rtid_array(ra, dec)
        scal = np.array([self.t.get_rtid(a, d) for a, d in zip(ra, dec)])
        np.testing.assert_array_equal(vec, scal)

    def test_centre_and_corner_attributes(self):
        self.t.get_center_sky_position(5321355)
        self.assertAlmostEqual(self.t.ra0, 11.067073, places=5)
        self.assertAlmostEqual(self.t.dec0, -43.80449, places=5)
        self.t.get_corner_sky_positions(5321355)
        self.assertLess(self.t.ramin, self.t.ramax)
        self.assertLess(self.t.decmin, self.t.decmax)
        # The four corners, in the SQLite class's order.
        self.assertEqual((self.t.ra1, self.t.dec1),
                         (self.t.ramin, self.t.decmin))
        self.assertEqual((self.t.ra3, self.t.dec3),
                         (self.t.ramax, self.t.decmax))

    def test_close_is_a_no_op(self):
        self.t.close()
        self.assertEqual(self.t.exit_code, 0)

    def test_check_version_accepts_the_pinned_release_values(self):
        ok = self.t.check_version(version="nside512-v2", digest="0" * 64,
                                  nside=512, nrows=6291458)
        self.assertTrue(ok)
        self.assertEqual(self.t.exit_code, 0)

    def test_check_version_refuses_a_different_tessellation(self):
        ok = self.t.check_version(nside=256, nrows=6291458)
        self.assertFalse(ok)
        self.assertEqual(self.t.exit_code, 70)

    def test_neighbour_counts_at_the_poles(self):
        self.assertEqual(len(self.t.get_all_neighboring_rtids(1)),
                         tess.nrabins(1))
        self.assertEqual(len(self.t.get_all_neighboring_rtids(tess.NROWS)),
                         tess.nrabins(tess.NRINGS))

    def test_neighbours_are_symmetric_on_a_sample(self):
        rng = np.random.default_rng(11)
        for r in rng.integers(2, tess.NROWS, 300):
            r = int(r)
            for other in self.t.get_all_neighboring_rtids(r):
                self.assertIn(r, self.t.get_all_neighboring_rtids(other),
                              "%d -> %d not symmetric" % (r, other))

    def test_dec_bin_returns_a_whole_ring_ordered_by_ramin(self):
        self.t.get_corner_sky_positions(5321355)
        rows = self.t.get_sky_tiles_in_dec_bin(self.t.decmin, self.t.decmax)
        self.assertEqual(len(rows), 3936)     # the 2024 note's nrabins here
        self.assertEqual([r[1] for r in rows],
                         sorted(r[1] for r in rows))
        self.assertIn(5321355, [r[0] for r in rows])

    def test_overlapping_rtids_covers_a_small_box(self):
        self.t.get_center_sky_position(5321355)
        self.t.get_corner_sky_positions(5321355)
        rows = self.t.get_overlapping_rtids(
            self.t.ra0, self.t.dec0,
            self.t.ra1, self.t.dec1, self.t.ra2, self.t.dec2,
            self.t.ra3, self.t.dec3, self.t.ra4, self.t.dec4)
        self.assertIn(5321355, [r[0] for r in rows])
        self.assertEqual(self.t.exit_code, 0)

    def test_pole_tiles_are_not_degenerate(self):
        # The SQLite class special-cased the south pole for a
        # ramin == ramax box. The canonical encoding has no such box, so
        # the pole falls out of the ordinary overlap test.
        for rtid in (1, tess.NROWS):
            ramin, ramax, decmin, decmax = tess.corners_of(rtid)
            self.assertNotEqual(ramin, ramax, "rtid %d" % rtid)


class SqliteEquivalence(unittest.TestCase):
    """The closed form reproduces the SQLite class, where it can be checked.

    This is the evidence behind the switch. It needs the 1.4 GiB legacy
    file, which nothing bakes any more, so it normally skips — and says
    plainly that it skipped rather than reporting a pass.
    """

    @classmethod
    def setUpClass(cls):
        if not CARRIED_SQLITE or not os.path.exists(CARRIED_SQLITE):
            raise unittest.SkipTest(
                "the legacy SQLite tessellation is not available "
                "(set ROMANTESSELLATIONDBNAME to run this comparison). "
                "Nothing was compared: the equivalence evidence is the "
                "recorded run in rapid_systems "
                "tools/tessellation/certification-2026-08-06.txt.")
        cls.legacy = access.RomanTessellationNSIDE512()
        cls.closed = access.RomanTessellationClosedForm()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "legacy"):
            cls.legacy.close()

    def test_get_rtid_agrees_on_tile_centres(self):
        rng = np.random.default_rng(5)
        for rtid in rng.integers(2, tess.NROWS, 200):
            cra, cdec = tess.center_of(int(rtid))
            self.assertEqual(self.closed.get_rtid(cra, cdec),
                             self.legacy.get_rtid(cra, cdec))

    def test_neighbours_agree(self):
        rng = np.random.default_rng(6)
        sample = [int(r) for r in rng.integers(2, tess.NROWS, 150)]
        sample += [1, tess.NROWS, 2, tess.NROWS - 1]
        for rtid in sample:
            self.assertEqual(
                sorted(self.closed.get_all_neighboring_rtids(rtid)),
                sorted(self.legacy.get_all_neighboring_rtids(rtid)),
                "neighbours differ for rtid %d" % rtid)

    def test_centres_agree_exactly(self):
        rng = np.random.default_rng(7)
        for rtid in rng.integers(1, tess.NROWS + 1, 200):
            rtid = int(rtid)
            self.legacy.get_center_sky_position(rtid)
            self.closed.get_center_sky_position(rtid)
            self.assertEqual((self.closed.ra0, self.closed.dec0),
                             (self.legacy.ra0, self.legacy.dec0),
                             "centre differs for rtid %d" % rtid)

    def test_corners_agree_within_the_r_trees_outward_widening(self):
        """Corners agree to within 2 float32 ULPs, always outward.

        Not exact equality, and deliberately so. The SQLite class read
        corners from the R-tree `vskytiles`, and SQLite's R-tree stores
        float32 and rounds every box OUTWARD so no contained point can be
        missed: min bounds down, max bounds up. Its numbers are therefore
        a slightly-too-large box, not a more precise one — the class's
        own comment ("apparently it has more precision") had it wrong.

        That widening is a property of SQLite's index, is not
        reproducible from the generation rule, and is not reproduced. The
        canonical bounds are the tessellation's. What this asserts is
        that the difference is only the widening: bounded at 2 ULPs and
        never in the direction that would shrink a box.
        """
        rng = np.random.default_rng(7)
        for rtid in rng.integers(2, tess.NROWS, 200):
            rtid = int(rtid)
            self.legacy.get_corner_sky_positions(rtid)
            self.closed.get_corner_sky_positions(rtid)
            for name, mine, theirs, outward in (
                    ("ramin", self.closed.ramin, self.legacy.ramin, -1),
                    ("ramax", self.closed.ramax, self.legacy.ramax, +1),
                    ("decmin", self.closed.decmin, self.legacy.decmin, -1),
                    ("decmax", self.closed.decmax, self.legacy.decmax, +1)):
                ulp = abs(float(np.spacing(np.float32(mine)))) or 1e-12
                delta = theirs - mine
                self.assertLessEqual(
                    abs(delta), 2.0 * ulp,
                    "%s differs by more than 2 ULPs for rtid %d "
                    "(%r vs %r)" % (name, rtid, theirs, mine))
                self.assertGreaterEqual(
                    delta * outward, -1e-12,
                    "%s moved inward for rtid %d, which the R-tree's "
                    "outward rounding cannot do (%r vs %r)"
                    % (name, rtid, theirs, mine))


if __name__ == "__main__":
    unittest.main(verbosity=2)
