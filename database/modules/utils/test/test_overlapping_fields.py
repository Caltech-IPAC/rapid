"""Tests for the exact sky-tile footprint (`overlapping_fields`).

Three kinds of check, deliberately separated:

  1. **Invariants**, asserted on every run and cheap enough to stay a unit
     test. The load-bearing one is `sampled tiles are a SUBSET of the
     computed footprint`: every sample point lies inside the image, so
     the tile containing it genuinely overlaps, so a correct footprint
     must contain it. That is exactly the direction grid sampling fails
     in, and it is what this module exists to fix.

     THAT INVARIANT IS STATED AT `min_overlap_pixels=0.0`, and the tests
     pass it explicitly rather than relying on the default. With the
     production default of 25 px the outer margin of the detector is
     DELIBERATELY outside the tested rectangle, so a pixel sampled there
     lands in a tile the footprint may legitimately omit. Asserting the
     subset property against an inset footprint would be asserting that
     the threshold does not work.

  2. **Regressions**, pinned by their exact WCS. The sliver case below is
     the one that made the 2026-09-08 sweep look like a false positive
     until it was measured: a real 0.038 arcsec overlap, 0.35 of a pixel
     width, so no pixel centre lands in it and no amount of sampling can
     confirm it.

  3. **The measured sweep**, opt-in behind `RAPID_FOOTPRINT_SWEEP=1`
     because every-pixel ground truth over hundreds of pointings takes
     minutes. Skipped LOUDLY, never silently: a skip here is not a pass,
     it means nothing re-measured the numbers `overlapping_fields`'s own
     docstring quotes.

WHY THE SWEEP IS WORTH ITS RUNTIME. It is what caught the defect. Grid
sampling at step 500 — the method that was going to be shipped as the
backfill — missed at least one genuinely overlapped tile in 221 of 1,200
pointings (18%), 246 tiles in total. Nothing in a fast unit test would
have found that; it needed ground truth at a resolution the method
itself could not reach.

WHAT IS NOT ASSERTED, AND WHY. `exact footprint is a subset of the
bounding-box method` held in 200/200 spot checks but is NOT guaranteed:
an image edge is a great circle and can bulge in declination past its own
corners, so a tile touched only by that bulge can fall outside the
corner-derived box. The sweep MEASURES the bbox over-report rather than
asserting a containment that geometry does not promise.
"""

import math
import os
import unittest

import numpy as np

import modules.utils.rapid_pipeline_subs as util
from database.modules.utils import overlapping_fields as of
from database.modules.utils import roman_tessellation as tess
from database.modules.utils import roman_tessellation_db as access


NAXIS = 4088                       # naxis1_sciimage / naxis2_sciimage
SCALE = 0.11 / 3600.0              # deg/px

SWEEP_ENABLED = os.getenv("RAPID_FOOTPRINT_SWEEP") == "1"
SWEEP_POINTINGS = int(os.getenv("RAPID_FOOTPRINT_SWEEP_POINTINGS", "300"))
SWEEP_STEP = int(os.getenv("RAPID_FOOTPRINT_SWEEP_STEP", "4"))


def wcs(crval1, crval2, rot_deg, naxis=NAXIS, scale=SCALE):
    """A square-detector WCS at a given sky position and rotation."""
    t = math.radians(rot_deg)
    return dict(crpix1=(naxis + 1) / 2.0, crpix2=(naxis + 1) / 2.0,
                crval1=crval1, crval2=crval2,
                cd11=-scale * math.cos(t), cd12=scale * math.sin(t),
                cd21=scale * math.sin(t), cd22=scale * math.cos(t),
                naxis1=naxis, naxis2=naxis)


def footprint(w, **kw):
    return of.overlapping_fields(
        w["crval1"], w["crval2"], w["crpix1"], w["crpix2"],
        w["cd11"], w["cd12"], w["cd21"], w["cd22"],
        w["naxis1"], w["naxis2"], **kw)


def project_scalar(w, x, y):
    """One pixel through the pipeline's own projection."""
    return util.tan_proj2(x, y, w["crpix1"] - 1.0, w["crpix2"] - 1.0,
                          w["crval1"], w["crval2"],
                          w["cd11"], w["cd12"], w["cd21"], w["cd22"])


def sampled_tiles(w, step):
    """Tiles hit by a pixel grid, via the scalar projection.

    This is grid sampling — the approximation `overlapping_fields`
    replaces — used here only as a source of points KNOWN to be inside
    the image, which is what makes the subset assertion meaningful.
    """
    seen = set()
    xs = list(range(0, w["naxis1"], step)) + [w["naxis1"] - 1]
    ys = list(range(0, w["naxis2"], step)) + [w["naxis2"] - 1]
    for y in ys:
        for x in xs:
            ra, dec = project_scalar(w, float(x), float(y))
            seen.add(int(tess.rtid_of(ra, dec)))
    return seen


def _project_arrays(w, x, y):
    """Vectorized transcription of `tan_proj2`, for the sweep only.

    Ground truth at every pixel is 16.7 million projections per image;
    the scalar form cannot deliver that in a usable time. `VectorParity`
    below asserts this reproduces `util.tan_proj2` exactly, so the sweep
    never rests on an unverified transcription.
    """
    dtr, rtd = util.dtr, util.rtd
    fsamp = x - (w["crpix1"] - 1.0)
    fline = y - (w["crpix2"] - 1.0)
    xx = -(w["cd11"] * fsamp + w["cd12"] * fline) * dtr
    yy = -(w["cd21"] * fsamp + w["cd22"] * fline) * dtr
    delta = np.arctan(np.sqrt(xx * xx + yy * yy))
    yy = np.where((xx == 0.0) & (yy == 0.0), 1.0, yy)
    beta = np.arctan2(-xx, yy)
    glatr = w["crval2"] * dtr
    glongr = w["crval1"] * dtr
    lat = np.arcsin(-np.sin(delta) * np.cos(beta) * np.cos(glatr)
                    + np.cos(delta) * np.sin(glatr))
    xxx = (np.sin(glatr) * np.sin(delta) * np.cos(beta)
           + np.cos(glatr) * np.cos(delta))
    yyy = np.sin(delta) * np.sin(beta)
    lon = glongr + np.arctan2(yyy, xxx)
    return np.mod(lon * rtd, 360.0), lat * rtd


def ground_truth(w, step=1, chunk=64):
    """Tiles containing at least one pixel centre. Chunked for memory."""
    seen = set()
    xs = np.arange(0, w["naxis1"], step, dtype=float)
    for y0 in range(0, w["naxis2"], chunk * step):
        ys = np.arange(y0, min(y0 + chunk * step, w["naxis2"]), step,
                       dtype=float)
        X, Y = np.meshgrid(xs, ys)
        ra, dec = _project_arrays(w, X.ravel(), Y.ravel())
        seen |= set(int(v) for v in tess.rtid_of_arrays(ra, dec))
    return seen


def random_pointings(n, seed):
    """`n` (ra, dec, rotation) triples spread over the whole sky."""
    rng = np.random.default_rng(seed)
    for _ in range(n):
        yield (float(rng.uniform(0.0, 360.0)),
               float(np.degrees(math.asin(rng.uniform(-1.0, 1.0)))),
               float(rng.uniform(0.0, 360.0)))


class Shape(unittest.TestCase):
    """The contract the `overlapfields` column depends on."""

    def test_sorted_unique_and_in_range(self):
        for ra, dec, rot in random_pointings(40, 20260908):
            fp = footprint(wcs(ra, dec, rot))
            self.assertEqual(fp, sorted(fp), "must be ascending")
            self.assertEqual(len(fp), len(set(fp)), "must be deduplicated")
            self.assertTrue(all(1 <= t <= tess.NROWS for t in fp),
                            "every entry must be a valid rtid")

    def test_never_empty(self):
        # 104's `cardinality(overlapfields) >= 1` check depends on this:
        # an image always covers at least the tile holding its centre,
        # and the inset shrinks the rectangle about that same centre, so
        # no threshold below half the detector can empty the result.
        for ra, dec, rot in random_pointings(40, 1):
            w = wcs(ra, dec, rot)
            for inset in (0.0, of.DEFAULT_MIN_OVERLAP_PIXELS, 1024.0):
                self.assertGreaterEqual(
                    len(footprint(w, min_overlap_pixels=inset)), 1)

    def test_field_is_unioned_in(self):
        w = wcs(268.0, -28.5, 37.0)
        self.assertIn(999999, footprint(w, field=999999))

    def test_inset_beyond_half_the_detector_is_refused(self):
        w = wcs(268.0, -28.5, 37.0)
        with self.assertRaises(ValueError):
            footprint(w, min_overlap_pixels=NAXIS / 2.0 + 1.0)


class Invariants(unittest.TestCase):
    """Properties that must hold for any correct footprint."""

    def test_sampled_tiles_are_a_subset(self):
        """THE load-bearing test — the direction sampling gets wrong.

        Every sample point lies inside the image, so the tile containing
        it genuinely overlaps and MUST appear. A footprint that omits one
        is under-reporting, which is how an image goes silently missing
        from a field's stack.

        Asserted at `min_overlap_pixels=0.0`, explicitly: this is a
        property of the exact geometry. See the module docstring for why
        it is not asserted against an inset footprint.
        """
        for ra, dec, rot in random_pointings(30, 2):
            w = wcs(ra, dec, rot)
            missing = (sampled_tiles(w, 128)
                       - set(footprint(w, min_overlap_pixels=0.0)))
            self.assertEqual(
                missing, set(),
                "footprint omits tiles containing real image pixels at "
                "ra=%r dec=%r rot=%r" % (ra, dec, rot))

    def test_sampled_tiles_inside_the_inset_are_a_subset_at_the_default(self):
        """The same guarantee, restated for the production threshold.

        The inset does not weaken the promise, it moves the boundary: a
        pixel at least `min_overlap_pixels` in from every edge is inside
        the tested rectangle, so its tile must still appear. This is what
        the default actually guarantees, and it is worth an assertion of
        its own rather than being left as a consequence.
        """
        inset = int(math.ceil(of.DEFAULT_MIN_OVERLAP_PIXELS))
        for ra, dec, rot in random_pointings(20, 22):
            w = wcs(ra, dec, rot)
            fp = set(footprint(w))
            interior = set()
            for y in range(inset, NAXIS - inset, 128):
                for x in range(inset, NAXIS - inset, 128):
                    ra_s, dec_s = project_scalar(w, float(x), float(y))
                    interior.add(int(tess.rtid_of(ra_s, dec_s)))
            self.assertEqual(interior - fp, set(),
                             "ra=%r dec=%r rot=%r" % (ra, dec, rot))

    def test_centre_tile_is_present(self):
        """What the backfill's own preflight asserts against `field`."""
        for ra, dec, rot in random_pointings(30, 3):
            w = wcs(ra, dec, rot)
            cra, cdec = project_scalar(w, (NAXIS - 1) / 2.0, (NAXIS - 1) / 2.0)
            self.assertIn(int(tess.rtid_of(cra, cdec)), footprint(w))

    def test_quarter_turn_of_a_square_detector_changes_nothing(self):
        """A square rotated 90 degrees about its centre maps onto itself.

        So it covers the SAME sky and must give the SAME tiles. This is
        the cheapest test that is sensitive to rotation being handled at
        all: a bounding-box method passes it (a box is also symmetric),
        but a method that mishandled the corner ordering or dropped an
        edge would not.
        """
        for ra, dec, rot in random_pointings(25, 4):
            w0, w90 = wcs(ra, dec, rot), wcs(ra, dec, rot + 90.0)
            self.assertEqual(footprint(w0), footprint(w90),
                             "ra=%r dec=%r rot=%r" % (ra, dec, rot))

    def test_min_overlap_pixels_is_monotonic(self):
        """Insetting the rectangle can only ever remove tiles."""
        for ra, dec, rot in random_pointings(25, 5):
            w = wcs(ra, dec, rot)
            wide = set(footprint(w, min_overlap_pixels=0.0))
            for inset in (of.DEFAULT_MIN_OVERLAP_PIXELS, 128.0, 512.0):
                narrow = set(footprint(w, min_overlap_pixels=inset))
                self.assertTrue(narrow <= wide,
                                "inset %r added tiles at ra=%r dec=%r rot=%r"
                                % (inset, ra, dec, rot))
                wide = narrow


class Wraparound(unittest.TestCase):
    """RA 0/360 and the poles, where interval arithmetic breaks first."""

    def test_ra_zero_straddle(self):
        for crval1 in (0.0, 0.02, 359.98, 360.0 - 1e-9):
            w = wcs(crval1, 5.0, 30.0)
            fp = footprint(w, min_overlap_pixels=0.0)
            self.assertTrue(fp, "empty footprint straddling RA=0")
            self.assertEqual(sampled_tiles(w, 128) - set(fp), set())

    def test_high_declination(self):
        for dec in (85.0, 89.0, 89.9, -89.9):
            w = wcs(120.0, dec, 47.0)
            self.assertEqual(
                sampled_tiles(w, 128)
                - set(footprint(w, min_overlap_pixels=0.0)), set())

    def test_over_the_pole(self):
        # The polar cap tiles (rtid 1 and NROWS) have full-RA boxes and
        # are the special case the closed form deliberately does not
        # special-case; assert they are reachable rather than assumed.
        w = wcs(0.0, 89.98, 12.0)
        self.assertIn(1, footprint(w, min_overlap_pixels=0.0))
        w = wcs(0.0, -89.98, 12.0)
        self.assertIn(tess.NROWS, footprint(w, min_overlap_pixels=0.0))


class Regressions(unittest.TestCase):
    """Cases pinned by exact WCS because they were nearly mis-diagnosed."""

    #: 2026-09-08 sweep. Read as a method-5 false positive against step-4
    #: sampling; measured instead as a REAL overlap of 0.038 arcsec —
    #: 0.35 of a pixel width — so no pixel centre falls inside it and no
    #: sampling resolution can confirm it. Tile 1241885 is the sliver.
    SLIVER = dict(crpix1=2044.5, crpix2=2044.5,
                  crval1=249.81474451461165, crval2=37.378667653139566,
                  cd11=-2.45697750195376e-05, cd12=-1.816502493248903e-05,
                  cd21=-1.816502493248903e-05, cd22=2.45697750195376e-05,
                  naxis1=NAXIS, naxis2=NAXIS)

    SLIVER_FOOTPRINT = [1233691, 1233692, 1233693,
                        1237787, 1237788, 1237789, 1237790,
                        1241884, 1241885]

    def test_sub_pixel_sliver_is_reported_by_the_exact_geometry(self):
        self.assertEqual(footprint(self.SLIVER, min_overlap_pixels=0.0),
                         self.SLIVER_FOOTPRINT)

    def test_the_production_default_drops_the_sliver(self):
        """The threshold exists for exactly this tile.

        0.038 arcsec of overlap is real geometry and no science, so the
        25 px default is what keeps it out of `overlapfields`. If this
        ever fails, either the default moved or the geometry did.
        """
        self.assertNotIn(1241885, footprint(self.SLIVER))
        self.assertEqual(footprint(self.SLIVER),
                         self.SLIVER_FOOTPRINT[:-1])

    def test_even_one_pixel_removes_the_sliver(self):
        """It is a THIRD of a pixel wide — the default is not load-bearing
        for this case, only for how much more it also excludes."""
        self.assertNotIn(1241885,
                         footprint(self.SLIVER, min_overlap_pixels=1.0))

    def test_sampling_cannot_see_the_sliver(self):
        """Documents WHY the sweep's 'extra' tiles are not errors."""
        self.assertNotIn(1241885, sampled_tiles(self.SLIVER, 4))


class Defaults(unittest.TestCase):
    """The production threshold, pinned where it is easy to read."""

    def test_default_is_twentyfive_pixels(self):
        # laher, 2026-09-08. 25 px x 0.11 arcsec/px = 2.75 arcsec, about
        # 1% of a ~250 arcsec tile edge. Changing this changes what the
        # `overlapfields` column MEANS, so it fails a test rather than
        # sliding through as a diff nobody reads.
        self.assertEqual(of.DEFAULT_MIN_OVERLAP_PIXELS, 25.0)

    def test_the_signature_default_is_the_constant(self):
        # One home for the number: the backfill's argparse default reads
        # this same constant, so the backfill and any future registration
        # path cannot drift into two definitions of one column.
        import inspect
        got = inspect.signature(
            of.overlapping_fields).parameters["min_overlap_pixels"].default
        self.assertIs(got, of.DEFAULT_MIN_OVERLAP_PIXELS)

    def test_default_is_a_subset_of_the_exact_geometry(self):
        for ra, dec, rot in random_pointings(25, 33):
            w = wcs(ra, dec, rot)
            self.assertTrue(
                set(footprint(w))
                <= set(footprint(w, min_overlap_pixels=0.0)),
                "the default added tiles at ra=%r dec=%r rot=%r"
                % (ra, dec, rot))


class VectorParity(unittest.TestCase):
    """The sweep's numpy projection must equal the pipeline's own."""

    def test_matches_the_scalar_tan_proj2(self):
        worst_ra = worst_dec = 0.0
        for ra, dec, rot in random_pointings(6, 6):
            w = wcs(ra, dec, rot)
            xs = np.linspace(0, NAXIS - 1, 11)
            X, Y = np.meshgrid(xs, xs)
            X, Y = X.ravel(), Y.ravel()
            rv, dv = _project_arrays(w, X, Y)
            for i in range(X.size):
                rs, ds = project_scalar(w, float(X[i]), float(Y[i]))
                worst_ra = max(worst_ra,
                               abs((rv[i] - rs + 180.0) % 360.0 - 180.0))
                worst_dec = max(worst_dec, abs(dv[i] - ds))
        self.assertLess(worst_ra, 1e-9)
        self.assertLess(worst_dec, 1e-9)


class MeasuredSweep(unittest.TestCase):
    """Ground-truth sweep. Opt-in: minutes, not seconds."""

    def test_no_real_tile_is_ever_missed(self):
        if not SWEEP_ENABLED:
            self.skipTest(
                "the ground-truth sweep did not run (set "
                "RAPID_FOOTPRINT_SWEEP=1). This is NOT a pass: nothing "
                "here re-measured the figures overlapping_fields.py's "
                "docstring quotes, and the sweep is what caught grid "
                "sampling missing a real tile in 18% of pointings.")

        bbox = access.RomanTessellationClosedForm()
        missed = missed_images = 0
        extra = 0
        n_exact = n_truth = n_bbox = n_production = 0
        dropped_by_default = 0
        sampling_missed = sampling_bad_images = 0

        for ra, dec, rot in random_pointings(SWEEP_POINTINGS, 20260908):
            w = wcs(ra, dec, rot)
            # Ground truth samples EVERY pixel, including the outer
            # margin the production default excludes, so the exactness
            # assertion is made against the un-inset geometry. What the
            # default costs is measured separately, below.
            exact = set(footprint(w, min_overlap_pixels=0.0))
            production = set(footprint(w))
            truth = ground_truth(w, step=SWEEP_STEP)

            # The bounding-box method, called for real rather than
            # reimplemented, so this measures what callers actually get.
            cra, cdec = [], []
            for x, y in ((0, 0), (NAXIS - 1, 0),
                         (NAXIS - 1, NAXIS - 1), (0, NAXIS - 1)):
                r, d = project_scalar(w, float(x), float(y))
                cra.append(r); cdec.append(d)
            c0 = project_scalar(w, (NAXIS - 1) / 2.0, (NAXIS - 1) / 2.0)
            box = set(t[0] for t in bbox.get_overlapping_rtids(
                c0[0], c0[1], cra[0], cdec[0], cra[1], cdec[1],
                cra[2], cdec[2], cra[3], cdec[3]))

            sampled = sampled_tiles(w, 500)

            if truth - exact:
                missed += len(truth - exact); missed_images += 1
            extra += len(exact - truth)
            if truth - sampled:
                sampling_missed += len(truth - sampled)
                sampling_bad_images += 1
            n_exact += len(exact); n_truth += len(truth); n_bbox += len(box)
            n_production += len(production)
            dropped_by_default += len(exact - production)

        bbox.close()
        n = SWEEP_POINTINGS
        print("\n  sweep: %d pointings, ground truth every %d px" % (n, SWEEP_STEP))
        print("    exact          : %d missed in %d image(s); "
              "%d tile(s) beyond truth (slivers thinner than the sample step)"
              % (missed, missed_images, extra))
        print("    bounding box   : %+.0f%% tiles vs truth"
              % (100.0 * (n_bbox - n_truth) / n_truth))
        print("    grid sampling  : %d tile(s) missed in %d of %d image(s) (%.0f%%)"
              % (sampling_missed, sampling_bad_images, n,
                 100.0 * sampling_bad_images / n))
        print("    default %g px  : drops %d tile(s), %.1f%% of the exact "
              "footprint (%.2f -> %.2f tiles per image)"
              % (of.DEFAULT_MIN_OVERLAP_PIXELS, dropped_by_default,
                 100.0 * dropped_by_default / n_exact,
                 n_exact / float(n), n_production / float(n)))

        self.assertEqual(
            missed, 0,
            "the exact footprint missed %d tile(s) containing real image "
            "pixels across %d image(s) — this is the under-reporting "
            "failure the module exists to prevent" % (missed, missed_images))


if __name__ == "__main__":
    unittest.main()
