"""Tests for the sign- and filter-aware aggregation.

These exercise the two things that are easy to get silently wrong when results
are split by filter: the FP/img denominator (which must count only that filter's
images) and the chance-match floor (which must be measured within the filter and
population being scored, not over the whole sample).
"""
import io
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from analyses.rimtimsim_detection import aggregate, config


TOML = """
[run]
proc_date = "20260813"
database = "testdb"
product_bucket = "b"
log_bucket = "l"
science_jids = [1, 3]
reference_jids = [0]

[survey]
field = 1
sca = 2
filters = ["Z087", "K213"]
[survey.filter_alias]
Z087 = "F087"
K213 = "F213"

[catalogs]
archive = "a.zip"
catalog = "c.txt"
lightcurves = "lc_{trexs_filter}.pqt"
time_column = "T"
jd_to_mjd = 2400000.5
rapid_id_min = 5000000
mag_sentinel = 99.99

[truth]
static_max_dflux = 0.5
match_px = 0.5
branches = ["positive", "negative"]

[sweep]
diffs = ["sfft"]
thresholds = [3.0]
aperture_r = 3.0
[sweep.fwhm_px]
Z087 = 0.0
K213 = 0.0

[paths]
work = "WORK"
cache = "cache/img"
truth = "truth"
sweep = "sweep_out"
"""

VARIANT = "v1"


def write_job(outdir, jid, filt, dflux, is_rapid, matched, n_det, n_fp):
    """Write one per-job npz in the layout `sweep.process` produces."""
    os.makedirs(outdir, exist_ok=True)
    out = dict(jid=jid, filt=filt, dflux=np.asarray(dflux, float),
               is_rapid=np.asarray(is_rapid, bool),
               variants=np.array([VARIANT]))
    out[VARIANT + "|matched"] = np.asarray(matched, bool)
    out[VARIANT + "|scalars"] = np.array([n_det, n_fp], np.int64)
    with open(os.path.join(outdir, "%d.npz" % jid), "wb") as fh:
        np.savez_compressed(fh, **out)


class FilterAwareAggregation(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cfgpath = os.path.join(self.tmp, "t.toml")
        with open(cfgpath, "w") as fh:
            fh.write(TOML.replace("WORK", self.tmp))
        self.env = os.environ.pop("RTS_WORK", None)
        self.cfg = config.load(cfgpath)
        aggregate._CACHE.clear()

        # Two Z087 images and one K213 image.  Each carries one bright brightening
        # (detected), one bright fader (not detected on the positive branch) and
        # two static controls, one of which is a chance match.
        self.out = os.path.join(self.tmp, "sweep_out", "sfft_positive")
        for jid, filt, nfp in ((1, "Z087", 100), (2, "Z087", 300), (3, "K213", 40)):
            write_job(self.out, jid, filt,
                      dflux=[500.0, -500.0, 0.1, 0.2],
                      is_rapid=[True, False, False, True],
                      matched=[True, False, True, False],
                      n_det=1000, n_fp=nfp)

    def tearDown(self):
        shutil.rmtree(self.tmp)
        aggregate._CACHE.clear()
        if self.env is not None:
            os.environ["RTS_WORK"] = self.env

    # -- load --------------------------------------------------------------

    def test_load_tags_every_source_with_its_filter(self):
        R = aggregate.load(self.cfg, "sfft", "positive")
        self.assertEqual(len(R["filt"]), 12)
        self.assertEqual((R["filt"] == "Z087").sum(), 8)
        self.assertEqual((R["filt"] == "K213").sum(), 4)

    def test_load_counts_images_and_scalars_per_filter(self):
        R = aggregate.load(self.cfg, "sfft", "positive")
        self.assertEqual(R["nimg"], {"Z087": 2, "K213": 1})
        self.assertEqual(R["filters"], ["K213", "Z087"])
        self.assertEqual(R["scalars"][(VARIANT, "Z087")][1], 400)
        self.assertEqual(R["scalars"][(VARIANT, "K213")][1], 40)

    def test_cache_is_keyed_on_the_sweep_directory(self):
        first = aggregate.load(self.cfg, "sfft", "positive")
        self.assertIs(aggregate.load(self.cfg, "sfft", "positive"), first)
        # a second config pointing somewhere else must not be served the first
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        cfgpath = os.path.join(other, "t.toml")
        with open(cfgpath, "w") as fh:
            fh.write(TOML.replace("WORK", other))
        write_job(os.path.join(other, "sweep_out", "sfft_positive"), 1, "K213",
                  [500.0], [True], [True], 10, 7)
        R2 = aggregate.load(config.load(cfgpath), "sfft", "positive")
        self.assertEqual(R2["nimg"], {"K213": 1})

    # -- FP/img denominator ------------------------------------------------

    def _table(self, **kw):
        buf, keep = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            aggregate.report(self.cfg, "sfft", "positive", **kw)
        finally:
            sys.stdout = keep
        return buf.getvalue()

    def _fp(self, text):
        for line in text.splitlines():
            if line.startswith(VARIANT):
                return float(line.split("|")[0].split()[1])
        raise AssertionError("no %s row in:\n%s" % (VARIANT, text))

    def test_fp_per_image_uses_only_that_filters_images(self):
        # Z087: 400 false positives over 2 images; K213: 40 over 1.
        self.assertAlmostEqual(self._fp(self._table(filt="Z087")), 200.0)
        self.assertAlmostEqual(self._fp(self._table(filt="K213")), 40.0)

    def test_pooled_fp_per_image_spans_every_image(self):
        # 440 false positives over 3 images.
        self.assertAlmostEqual(self._fp(self._table()), 147.0, places=0)

    def test_unknown_filter_is_rejected(self):
        with self.assertRaises(SystemExit):
            self._table(filt="H158")

    # -- sign awareness ----------------------------------------------------

    def test_only_the_recoverable_sign_is_scored(self):
        # Each image carries a +500 brightening (matched) and a -500 fader
        # (unmatched), plus two positive-side controls of which one is a chance
        # match -- so the floor is 0.5.  In the 300-1000 bin the positive branch
        # must see the brightening ALONE: c = 1.0, floor-corrected to 1.000.  A
        # sign-blind implementation would fold |dflux| and put the fader in the
        # same bin, giving c = 0.5 and a corrected 0.000.
        row = [l for l in self._table().splitlines() if l.startswith(VARIANT)][0]
        cells = row.split("|")[1].split()
        self.assertEqual(cells[5], "1.000", row)     # 300-1000 bin
        self.assertTrue(row.rstrip().endswith("0.500"), row)

    def test_scored_and_control_counts(self):
        # 3 images x (one +500 brightening + two positive controls) = 9 scored;
        # the -500 fader is excluded on this branch.  Controls are the 6 sources
        # with |dflux| < 0.5.
        self.assertIn("9 sources scored (recoverable sign), 6 static controls",
                      self._table())

    def test_controls_are_restricted_to_the_scored_population(self):
        # is_rapid marks one control per image (dflux 0.2, unmatched) as RAPID and
        # the other (0.1, matched) as TRExS, so the floor is 0 for the RAPID
        # population and 1 for the TRExS one.
        self.assertIn("floor", self._table(population="rapid"))
        rapid = [l for l in self._table(population="rapid").splitlines()
                 if l.startswith(VARIANT)][0]
        trexs = [l for l in self._table(population="trexs").splitlines()
                 if l.startswith(VARIANT)][0]
        self.assertTrue(rapid.rstrip().endswith("0.000"), rapid)
        self.assertTrue(trexs.rstrip().endswith("1.000"), trexs)


if __name__ == "__main__":
    unittest.main()


class DuplicateDetection(unittest.TestCase):
    """A variant pair that agrees on every source is a config error, not a result."""

    def _R(self, ma, mb):
        return dict(labels=["a", "b"], M={"a": np.array(ma, bool), "b": np.array(mb, bool)})

    def test_identical_variants_are_reported(self):
        self.assertEqual(aggregate.duplicate_variants(
            self._R([1, 0, 1, 1], [1, 0, 1, 1])), [["a", "b"]])

    def test_variants_differing_anywhere_are_not(self):
        self.assertEqual(aggregate.duplicate_variants(
            self._R([1, 0, 1, 1], [1, 0, 1, 0])), [])

    def test_equal_counts_but_different_sources_are_not_duplicates(self):
        self.assertEqual(aggregate.duplicate_variants(
            self._R([1, 1, 0, 0], [0, 0, 1, 1])), [])

    def test_three_way_duplicate_is_one_group(self):
        R = dict(labels=["a", "b", "c"],
                 M={"a": np.array([1, 0, 1], bool), "b": np.array([1, 0, 1], bool),
                    "c": np.array([1, 0, 1], bool)})
        self.assertEqual(aggregate.duplicate_variants(R), [["a", "b", "c"]])

    def test_two_separate_pairs_are_two_groups(self):
        R = dict(labels=["a", "b", "c", "d"],
                 M={"a": np.array([1, 1, 0, 0], bool), "b": np.array([1, 1, 0, 0], bool),
                    "c": np.array([0, 0, 1, 1], bool), "d": np.array([0, 0, 1, 1], bool)})
        self.assertEqual(aggregate.duplicate_variants(R), [["a", "b"], ["c", "d"]])

    def test_same_count_different_pattern_is_not_grouped(self):
        # These land in the same cheap bucket (equal shape and sum) and must be
        # separated by the exact comparison, not merged by it.
        R = dict(labels=["a", "b"],
                 M={"a": np.array([1, 1, 0, 0], bool), "b": np.array([0, 0, 1, 1], bool)})
        self.assertEqual(aggregate.duplicate_variants(R), [])
