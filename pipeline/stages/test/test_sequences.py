"""
File:    test_sequences.py

Tests for the stage-sequence table: what each job type runs, and the
invariants the entrypoint depends on — a sequence exists for every routable
stage-pipeline job type, registration is dispatched elsewhere, and no
sequence repeats a stage name (stage names key the per-stage bundle log
files, so a duplicate would silently overwrite an earlier stage's log).

**Why `sys.modules` is stubbed before import.** Importing `pipeline.stages.
sequences` (directly, or transitively through `pipeline/stages/__init__.py`)
pulls in `science`, `reference_image`, and `post_process`, which import
numpy, astropy, scipy, psycopg2, boto3, dateutil, galsim, romanisim, and
photutils at module scope. None of those are installed in this environment.
`sequence_for` and the `SEQUENCES` table itself need none of that machinery
— they are a lookup over `(name, callable)` tuples — so lightweight
stand-ins satisfy the import chain without exercising the science.
"""

import sys
import types
import unittest


def _stub(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def _install_third_party_stubs() -> None:
    """See the identical helper in `pipeline.entrypoints.test.test_job` for
    the full rationale; duplicated here rather than imported so each test
    tree stays independently discoverable and self-contained."""
    names = [
        "numpy", "numpy.ma",
        "scipy", "scipy.ndimage",
        "astropy", "astropy.io", "astropy.io.fits", "astropy.io.ascii",
        "astropy.table", "astropy.wcs", "astropy.coordinates",
        "astropy.units",
        "boto3", "botocore", "botocore.exceptions",
        "psycopg2", "psycopg2.extensions", "psycopg2.sql",
        "dateutil", "dateutil.tz",
        "galsim", "galsim.wcs", "galsim.roman",
        "romanisim", "romanisim.bandpass", "romanisim.catalog",
        "romanisim.image", "romanisim.psf",
        "photutils", "photutils.background", "photutils.segmentation",
        "injectionLightCurveModels",
    ]
    for name in names:
        if name not in sys.modules:
            _stub(name)

    numpy = sys.modules["numpy"]
    if not hasattr(numpy, "ma"):
        numpy.ma = sys.modules["numpy.ma"]
        numpy.array = lambda *a, **k: None
        numpy.nanmedian = lambda *a, **k: 0.0
        numpy.nanmin = lambda *a, **k: 0.0
        numpy.nanmax = lambda *a, **k: 0.0
        numpy.isnan = lambda *a, **k: False

    astropy_io = sys.modules["astropy.io"]
    if not hasattr(astropy_io, "fits"):
        astropy_io.fits = sys.modules["astropy.io.fits"]
        astropy_io.ascii = sys.modules["astropy.io.ascii"]

    astropy_table = sys.modules["astropy.table"]
    if not hasattr(astropy_table, "QTable"):
        astropy_table.QTable = object
        astropy_table.join = lambda *a, **k: None
        astropy_table.Table = object

    astropy_wcs = sys.modules["astropy.wcs"]
    if not hasattr(astropy_wcs, "WCS"):
        astropy_wcs.WCS = object

    astropy_coordinates = sys.modules["astropy.coordinates"]
    if not hasattr(astropy_coordinates, "SkyCoord"):
        astropy_coordinates.SkyCoord = object

    scipy_ndimage = sys.modules["scipy.ndimage"]
    if not hasattr(scipy_ndimage, "zoom"):
        scipy_ndimage.zoom = lambda *a, **k: None
        scipy_ndimage.gaussian_filter = lambda *a, **k: None

    botocore_exceptions = sys.modules["botocore.exceptions"]
    if not hasattr(botocore_exceptions, "ClientError"):
        botocore_exceptions.ClientError = Exception

    psycopg2 = sys.modules["psycopg2"]
    if not hasattr(psycopg2, "sql"):
        psycopg2.sql = sys.modules["psycopg2.sql"]

    galsim = sys.modules["galsim"]
    if not hasattr(galsim, "wcs"):
        galsim.wcs = sys.modules["galsim.wcs"]
        galsim.roman = sys.modules["galsim.roman"]

    photutils_background = sys.modules["photutils.background"]
    if not hasattr(photutils_background, "Background2D"):
        photutils_background.Background2D = object
        photutils_background.MedianBackground = object

    photutils_segmentation = sys.modules["photutils.segmentation"]
    if not hasattr(photutils_segmentation, "detect_threshold"):
        photutils_segmentation.detect_threshold = lambda *a, **k: None
        photutils_segmentation.detect_sources = lambda *a, **k: None
        photutils_segmentation.deblend_sources = lambda *a, **k: None
        photutils_segmentation.SourceCatalog = object

    injection_models = sys.modules["injectionLightCurveModels"]
    if not hasattr(injection_models, "SinusoidalLightCurve"):
        injection_models.SinusoidalLightCurve = object
        injection_models.GaussianLightCurve = object

    dateutil_tz = sys.modules["dateutil.tz"]
    if not hasattr(dateutil_tz, "gettz"):
        dateutil_tz.gettz = lambda *a, **k: None
    dateutil = sys.modules["dateutil"]
    if not hasattr(dateutil, "tz"):
        dateutil.tz = dateutil_tz


_install_third_party_stubs()

from pipeline.stages.sequences import SEQUENCES, sequence_for  # noqa: E402
from submission.routes import JOB_TYPES, JOB_TYPE_REGISTRATION, RouteError  # noqa: E402


# ---------------------------------------------------------------------------
# Each stage-pipeline job type returns a non-empty tuple of (str, callable)
# ---------------------------------------------------------------------------

class SequenceForTests(unittest.TestCase):

    def test_science_returns_a_non_empty_tuple_of_name_callable_pairs(self):
        sequence = sequence_for("science")
        self.assertIsInstance(sequence, tuple)
        self.assertGreater(len(sequence), 0)
        for name, fn in sequence:
            self.assertIsInstance(name, str)
            self.assertTrue(callable(fn))

    def test_reference_image_returns_a_non_empty_tuple_of_name_callable_pairs(self):
        sequence = sequence_for("reference-image")
        self.assertIsInstance(sequence, tuple)
        self.assertGreater(len(sequence), 0)
        for name, fn in sequence:
            self.assertIsInstance(name, str)
            self.assertTrue(callable(fn))

    def test_post_process_returns_a_non_empty_tuple_of_name_callable_pairs(self):
        sequence = sequence_for("post-process")
        self.assertIsInstance(sequence, tuple)
        self.assertGreater(len(sequence), 0)
        for name, fn in sequence:
            self.assertIsInstance(name, str)
            self.assertTrue(callable(fn))

    def test_registration_raises_routeerror(self):
        # Registration is not a staged pipeline; the entrypoint dispatches it
        # directly rather than through a stage sequence.
        with self.assertRaises(RouteError):
            sequence_for("registration")

    def test_nonsense_job_type_raises_routeerror(self):
        with self.assertRaises(RouteError):
            sequence_for("nonsense")

    def test_registration_error_names_available_sequences(self):
        with self.assertRaises(RouteError) as ctx:
            sequence_for(JOB_TYPE_REGISTRATION)
        message = str(ctx.exception)
        for job_type in SEQUENCES:
            self.assertIn(job_type, message)


# ---------------------------------------------------------------------------
# Cross-checks against the route matrix
# ---------------------------------------------------------------------------

class SequenceVocabularyTests(unittest.TestCase):

    def test_every_sequence_job_type_is_a_real_route_job_type(self):
        for job_type in SEQUENCES:
            self.assertIn(job_type, JOB_TYPES,
                          f"{job_type!r} has a stage sequence but is not in "
                          f"submission.routes.JOB_TYPES")


# ---------------------------------------------------------------------------
# Uniqueness: stage names key the per-stage bundle log files
# ---------------------------------------------------------------------------

class StageNameUniquenessTests(unittest.TestCase):

    def test_every_sequence_has_unique_stage_names(self):
        for job_type, sequence in SEQUENCES.items():
            names = [name for name, _fn in sequence]
            with self.subTest(job_type=job_type):
                self.assertEqual(
                    len(names), len(set(names)),
                    f"job type {job_type!r} has duplicate stage names in its "
                    f"sequence: {names}; duplicates would key the same "
                    f"per-stage bundle log file twice and the second stage's "
                    f"log would overwrite the first's")


if __name__ == "__main__":
    unittest.main()
