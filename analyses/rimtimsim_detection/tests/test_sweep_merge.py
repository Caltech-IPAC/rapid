"""Tests for variant-level resume in the sweep.

The property under test is that a result file is not all-or-nothing. Adding one
variant, or correcting one whose kernel changed, must cost that variant alone and
must not disturb the thirty-six others already computed -- otherwise a single bad
entry forces a ten-hour rerun of the whole matrix.
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from analyses.rimtimsim_detection import sweep


class VariantSignature(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def _kernel(self, name, text):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as fh:
            fh.write(text)
        return p

    def test_same_parameters_give_the_same_signature(self):
        p = dict(thresh=3.0, minarea=1, filt="Y", kernel=None)
        self.assertEqual(sweep.signature("sex", p), sweep.signature("sex", dict(p)))

    def test_changing_a_parameter_changes_the_signature(self):
        a = sweep.signature("sex", dict(thresh=3.0, minarea=1, filt="Y", kernel=None))
        b = sweep.signature("sex", dict(thresh=4.0, minarea=1, filt="Y", kernel=None))
        self.assertNotEqual(a, b)

    def test_kernel_content_not_path_determines_the_signature(self):
        # The bug this guards: two variants pointed at different paths holding the
        # same kernel are the same computation, and two pointing at one path whose
        # content changed are not.
        k1 = self._kernel("one.conv", "CONV NORM\n# g\n1 2 1\n")
        k2 = self._kernel("two.conv", "CONV NORM\n# g\n1 2 1\n")
        k3 = self._kernel("three.conv", "CONV NONORM\n# d\n-1 2 -1\n")
        base = dict(thresh=3.0, minarea=1, filt="Y")
        s1 = sweep.signature("sex", dict(base, kernel=k1))
        s2 = sweep.signature("sex", dict(base, kernel=k2))
        s3 = sweep.signature("sex", dict(base, kernel=k3))
        self.assertEqual(s1, s2)
        self.assertNotEqual(s1, s3)

    def test_kind_is_part_of_the_signature(self):
        p = dict(nsig=3.0, fwhm=1.4)
        self.assertNotEqual(sweep.signature("dao", p), sweep.signature("pu", p))


class GaussAndDaoKernelsDiffer(unittest.TestCase):
    """The two SExtractor filter profiles must be genuinely different files."""

    def setUp(self):
        import tempfile
        self.kdir = tempfile.mkdtemp()
        self.ks = sweep.build_kernels(self.kdir, 1.3231, 1.7275)

    def test_build_kernels_emits_both_profiles(self):
        for tag, (g, d, w) in self.ks.items():
            self.assertTrue(os.path.exists(g), tag)
            self.assertTrue(os.path.exists(d), tag)
            self.assertNotEqual(os.path.realpath(g), os.path.realpath(d))

    def test_gaussian_is_unit_sum_and_dao_is_zero_sum(self):
        for tag, (g, d, w) in self.ks.items():
            gk = np.loadtxt(g, skiprows=2)
            dk = np.loadtxt(d, skiprows=2)
            self.assertAlmostEqual(gk.sum(), 1.0, places=4, msg=tag)
            self.assertAlmostEqual(dk.sum(), 0.0, places=4, msg=tag)

    def test_zero_sum_kernel_is_declared_nonorm(self):
        # SExtractor divides a NORM filter by its sum; for a zero-sum kernel that
        # is a division by ~0.
        for tag, (g, d, w) in self.ks.items():
            self.assertEqual(open(g).readline().strip(), "CONV NORM", tag)
            self.assertEqual(open(d).readline().strip(), "CONV NONORM", tag)

    def test_the_two_families_get_different_kernels(self):
        v = dict((label, p) for label, kind, p in
                 sweep.variants(self.ks, [3.0]) if label.startswith("SE-"))
        for tag in ("fN", "fW"):
            g = v["SE-gauss-%s@3" % tag]["kernel"]
            d = v["SE-dao-%s@3" % tag]["kernel"]
            self.assertNotEqual(g, d, tag)
            self.assertNotEqual(sweep.signature("sex", v["SE-gauss-%s@3" % tag]),
                                sweep.signature("sex", v["SE-dao-%s@3" % tag]), tag)


if __name__ == "__main__":
    unittest.main()


class ResumeDecisions(unittest.TestCase):
    """Which variants a resumed sweep decides to recompute.

    Reimplements `process`'s decision rule against synthetic state, so the policy
    can be tested without fetching a 400 MB difference image.
    """

    @staticmethod
    def decide(want_sigs, prev, refresh=False):
        todo = []
        for label, sig in want_sigs.items():
            have_sig = prev.get(label + "|sig")
            if label + "|matched" not in prev:
                todo.append(label)
            elif refresh:
                todo.append(label)
            elif have_sig is not None and have_sig != sig:
                todo.append(label)
        return sorted(todo)

    def test_absent_variants_are_computed(self):
        self.assertEqual(self.decide({"a": "s1"}, {}), ["a"])

    def test_current_variants_are_skipped(self):
        prev = {"a|matched": 1, "a|sig": "s1"}
        self.assertEqual(self.decide({"a": "s1"}, prev), [])

    def test_changed_signature_is_recomputed(self):
        prev = {"a|matched": 1, "a|sig": "OLD"}
        self.assertEqual(self.decide({"a": "s1"}, prev), ["a"])

    def test_results_predating_signatures_are_grandfathered(self):
        # The migration case: treating these as stale would make the first run
        # under the new format recompute the whole matrix.
        prev = {"a|matched": 1}
        self.assertEqual(self.decide({"a": "s1"}, prev), [])

    def test_refresh_overrides_grandfathering(self):
        prev = {"a|matched": 1}
        self.assertEqual(self.decide({"a": "s1"}, prev, refresh=True), ["a"])

    def test_a_new_variant_alongside_grandfathered_ones_costs_only_itself(self):
        prev = {"a|matched": 1, "b|matched": 1}
        self.assertEqual(self.decide({"a": "s1", "b": "s2", "c": "s3"}, prev), ["c"])
