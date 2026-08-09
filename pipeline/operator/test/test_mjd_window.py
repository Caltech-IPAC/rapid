"""`pipeline.operator.gathering.mjd_window`: the readiness query's bounds.

PORTED FROM `pipeline/test/test_vpo_phases.py` (`MjdWindowTests`) when the
monolith was retired (IR-2). `mjd_window` moved to this module in IR-1a;
these tests now import it directly rather than through
`virtualPipelineOperator`. The registry itself is covered separately in
`test_gathering_registry.py`.
"""

import unittest

from pipeline.operator.gathering import mjd_window


class MjdWindowTests(unittest.TestCase):
    """The window's two values are bound into the readiness query."""

    def test_the_window_is_built_in_floats_not_numpy_scalars(self):
        """astropy returns `numpy.float64`, and psycopg2 has no adapter for
        it — so it reprs the value, which under NumPy 2 is
        `np.float64(61679.0)`. Pasted into SQL that reads as a
        schema-qualified name and Postgres fails with `schema "np" does not
        exist`, aborting the transaction. Gathering then reports zero ready
        pairs, which looks exactly like a night with no data. Asserting the
        exact type, not just the value: `np.float64` compares equal to a
        float, so an equality check would pass while the bug was live."""
        start, end = mjd_window("2027-10-01 00:00:00", "2027-10-08 00:00:00")

        self.assertIs(type(start), float)
        self.assertIs(type(end), float)
        self.assertAlmostEqual(61679.0, start, places=6)
        self.assertAlmostEqual(61686.0, end, places=6)

    def test_the_window_survives_being_formatted_into_a_repr(self):
        """The failure mode was a repr, so pin the repr itself: a bare
        number, with no `np.` qualifier anywhere in it."""
        for value in mjd_window("2027-10-01 00:00:00", "2027-10-08 00:00:00"):
            self.assertNotIn("np.", repr(value))


if __name__ == "__main__":
    unittest.main()
