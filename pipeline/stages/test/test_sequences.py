"""
File:    test_sequences.py

Tests for the stage-sequence table: what each job type runs, and the
invariants the entrypoint depends on — a sequence exists for every routable
stage-pipeline job type, registration is dispatched elsewhere, and no
sequence repeats a stage name (stage names key the per-stage bundle log
files, so a duplicate would silently overwrite an earlier stage's log).

**Why `sys.modules` is stubbed before import.** Importing `pipeline.stages.
sequences` (directly, or transitively through `pipeline/stages/__init__.py`)
pulls in `science` and `reference_image`, which import numpy, astropy,
scipy, psycopg2, boto3, dateutil, galsim, romanisim, and photutils at
module scope. None of those are installed in this environment.
`sequence_for` and the `SEQUENCES` table itself need none of that machinery
— they are a lookup over `(name, callable)` tuples — so lightweight
stand-ins satisfy the import chain without exercising the science.
"""

import importlib.util
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
    # Stub only what is genuinely MISSING, judged by importability rather
    # than by `sys.modules` membership. The distinction is the whole bug
    # (W8, 2026-08-06): not-yet-imported is not the same as not-installed,
    # and in the image every name here except injectionLightCurveModels is
    # real. Shadowing a real package with a bare ModuleType broke imports
    # two ways — `from astropy.wcs import WCS` found a stub with no WCS,
    # and a stub at "numpy.ma" beneath the real numpy sent numpy 2.x's
    # lazy __getattr__ into unbounded recursion. Both surfaced at
    # COLLECTION time, so the suites errored out whole rather than failing
    # a test. Off a laptop with none of these installed the old code was
    # fine, which is exactly why it survived to be found here.
    stubbed: set[str] = set()
    for name in names:
        if name in sys.modules:
            continue
        try:
            if importlib.util.find_spec(name) is not None:
                continue        # real and importable — leave it alone
        except (ImportError, ValueError):
            pass                # unimportable: a stub is what it needs
        _stub(name)
        stubbed.add(name)

    def _decorate(name, **attributes):
        """Attach stub attributes, but only to a module we stubbed."""
        if name not in stubbed:
            return
        module = sys.modules[name]
        for attribute, value in attributes.items():
            setattr(module, attribute, value)

    _decorate("numpy",
              ma=sys.modules["numpy.ma"] if "numpy.ma" in stubbed else None,
              array=lambda *a, **k: None,
              nanmedian=lambda *a, **k: 0.0,
              nanmin=lambda *a, **k: 0.0,
              nanmax=lambda *a, **k: 0.0,
              isnan=lambda *a, **k: False)
    if "astropy.io" in stubbed:
        _decorate("astropy.io",
                  fits=sys.modules["astropy.io.fits"],
                  ascii=sys.modules["astropy.io.ascii"])
    _decorate("astropy.table", QTable=object,
              join=lambda *a, **k: None, Table=object)
    _decorate("astropy.wcs", WCS=object)
    _decorate("astropy.coordinates", SkyCoord=object)

    _decorate("scipy.ndimage",
              zoom=lambda *a, **k: None,
              gaussian_filter=lambda *a, **k: None)
    _decorate("botocore.exceptions", ClientError=Exception)
    if "psycopg2.sql" in stubbed:
        _decorate("psycopg2", sql=sys.modules["psycopg2.sql"])
    if "galsim.wcs" in stubbed:
        _decorate("galsim", wcs=sys.modules["galsim.wcs"],
                  roman=sys.modules["galsim.roman"])
    _decorate("photutils.background",
              Background2D=object, MedianBackground=object)
    _decorate("photutils.segmentation",
              detect_threshold=lambda *a, **k: None,
              detect_sources=lambda *a, **k: None,
              deblend_sources=lambda *a, **k: None,
              SourceCatalog=object)
    _decorate("injectionLightCurveModels",
              SinusoidalLightCurve=object, GaussianLightCurve=object)
    _decorate("dateutil.tz", gettz=lambda *a, **k: None)
    if "dateutil.tz" in stubbed:
        _decorate("dateutil", tz=sys.modules["dateutil.tz"])


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
