"""
File:    test_context.py

Tests for `StageContext`: the one object a stage is handed instead of a
global.

**Why `sys.modules` is stubbed before import.** Importing `pipeline.stages.
context` runs `pipeline/stages/__init__.py` first (Python always executes a
package's `__init__` before a submodule), which eagerly imports `sequences`
and, through it, `science`, `reference_image`, and `post_process` — pulling
in numpy, astropy, scipy, psycopg2, boto3, dateutil, galsim, romanisim, and
photutils at module scope. None of those are installed in this environment
and `StageContext` itself needs none of them, so lightweight stand-ins
satisfy the import chain without asserting anything about the science
those modules would do.
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

from pipeline.runtime.errors import ConfigError, InputError  # noqa: E402
from pipeline.stages.context import StageContext  # noqa: E402
from submission.manifest import ProcessingUnit, UnitFacts  # noqa: E402


class FakeLogger:
    """Records every call so a test can assert a rebind was logged, without
    pulling in the real logging machinery or asserting on formatting."""

    def __init__(self):
        self.calls: list = []

    def info(self, msg, *args, **kwargs):
        self.calls.append(("info", msg, args))

    def warning(self, msg, *args, **kwargs):
        self.calls.append(("warning", msg, args))

    def error(self, msg, *args, **kwargs):
        self.calls.append(("error", msg, args))

    def exception(self, msg, *args, **kwargs):
        self.calls.append(("exception", msg, args))

    def text(self) -> str:
        return "\n".join(
            (msg % args if args else str(msg)) for _level, msg, args in self.calls)


SCIENCE_CONFIG = {
    "release": {"schema_version": 1},
    "instrument": {"sca_gain": 2.0, "sca_readout_noise": 5.0},
    "zogy": {"astrometric_uncert_x": 0.1},
}


def make_context(**overrides) -> StageContext:
    unit = overrides.pop("unit", ProcessingUnit(
        exposure=1, sca=2, facts=UnitFacts(science_image_uri="s3://b/img.fits")))
    fields = {
        "workdir": None,
        "unit": unit,
        "job_type": "science",
        "science": SCIENCE_CONFIG,
        "parameters": {"s3/products-bucket": "rapid-products"},
        "logger": FakeLogger(),
    }
    fields.update(overrides)
    return StageContext(**fields)


# ---------------------------------------------------------------------------
# product()
# ---------------------------------------------------------------------------

class ProductTests(unittest.TestCase):

    def test_missing_product_raises_inputerror_naming_it(self):
        context = make_context()
        with self.assertRaises(InputError) as ctx:
            context.product("science_image")
        self.assertIn("science_image", str(ctx.exception))

    def test_missing_product_message_lists_what_has_been_produced(self):
        context = make_context()
        context.produce("science_image_gz", "a.fits.gz")
        context.produce("science_psf", "psf.fits")
        with self.assertRaises(InputError) as ctx:
            context.product("science_image")
        message = str(ctx.exception)
        self.assertIn("science_image_gz", message)
        self.assertIn("science_psf", message)

    def test_no_products_yet_is_stated_plainly(self):
        context = make_context()
        with self.assertRaises(InputError) as ctx:
            context.product("anything")
        self.assertIn("nothing yet", str(ctx.exception))

    def test_present_product_is_returned(self):
        context = make_context()
        context.produce("science_image", "reformatted.fits")
        self.assertEqual(context.product("science_image"), "reformatted.fits")


# ---------------------------------------------------------------------------
# produce()
# ---------------------------------------------------------------------------

class ProduceTests(unittest.TestCase):

    def test_returns_its_value(self):
        context = make_context()
        self.assertEqual(context.produce("science_image", "a.fits"), "a.fits")

    def test_replacing_an_existing_name_is_logged(self):
        context = make_context()
        context.produce("science_image", "a.fits")
        context.produce("science_image", "a_inject.fits")
        self.assertTrue(any("replaced" in text
                            for _level, text, _args in context.logger.calls))

    def test_replacing_an_existing_name_the_new_value_wins(self):
        context = make_context()
        context.produce("science_image", "a.fits")
        context.produce("science_image", "a_inject.fits")
        self.assertEqual(context.product("science_image"), "a_inject.fits")

    def test_producing_the_same_value_twice_is_not_logged_as_a_replacement(self):
        context = make_context()
        context.produce("science_image", "a.fits")
        context.produce("science_image", "a.fits")
        self.assertFalse(any("replaced" in text
                             for _level, text, _args in context.logger.calls))

    def test_has_product_reflects_presence_without_requiring(self):
        context = make_context()
        self.assertFalse(context.has_product("science_image"))
        context.produce("science_image", "a.fits")
        self.assertTrue(context.has_product("science_image"))


# ---------------------------------------------------------------------------
# fact() / optional_fact()
# ---------------------------------------------------------------------------

class FactTests(unittest.TestCase):

    def test_none_manifest_fact_raises_inputerror(self):
        unit = ProcessingUnit(exposure=1, sca=2, facts=UnitFacts())
        context = make_context(unit=unit)
        with self.assertRaises(InputError):
            context.fact("science_image_uri")

    def test_present_fact_is_returned(self):
        unit = ProcessingUnit(exposure=1, sca=2,
                              facts=UnitFacts(science_image_uri="s3://b/i.fits"))
        context = make_context(unit=unit)
        self.assertEqual(context.fact("science_image_uri"), "s3://b/i.fits")

    def test_optional_fact_returns_the_default_when_absent(self):
        unit = ProcessingUnit(exposure=1, sca=2, facts=UnitFacts())
        context = make_context(unit=unit)
        self.assertEqual(context.optional_fact("reference_image_id", -1), -1)
        self.assertIsNone(context.optional_fact("reference_image_id"))

    def test_optional_fact_returns_the_value_when_present(self):
        unit = ProcessingUnit(exposure=1, sca=2,
                              facts=UnitFacts(reference_image_id=42))
        context = make_context(unit=unit)
        self.assertEqual(context.optional_fact("reference_image_id", -1), 42)


# ---------------------------------------------------------------------------
# parameter()
# ---------------------------------------------------------------------------

class ParameterTests(unittest.TestCase):

    def test_absent_parameter_raises_configerror(self):
        context = make_context(parameters={})
        with self.assertRaises(ConfigError) as ctx:
            context.parameter("s3/products-bucket")
        self.assertIn("s3/products-bucket", str(ctx.exception))

    def test_present_parameter_is_returned(self):
        context = make_context(parameters={"s3/products-bucket": "rapid-products"})
        self.assertEqual(context.parameter("s3/products-bucket"), "rapid-products")

    def test_no_default_is_synthesized(self):
        # There is no fallback value for a missing parameter: it always
        # raises rather than returning None or "".
        context = make_context(parameters={})
        with self.assertRaises(ConfigError):
            context.parameter("does-not-exist")


# ---------------------------------------------------------------------------
# science_section()
# ---------------------------------------------------------------------------

class ScienceSectionTests(unittest.TestCase):

    def test_returns_a_copy_mutation_does_not_leak_to_a_second_call(self):
        """The regression test for the save/revert removal: the monolith
        mutated one live ConfigParser section across stages and manually
        reverted afterwards. `science_section` must return an independent
        copy every time, so a stage that mutates what it gets cannot affect
        the next stage — or the next call for the same section."""
        context = make_context()
        first = context.science_section("instrument")
        first["sca_gain"] = 999.0
        second = context.science_section("instrument")
        self.assertEqual(second["sca_gain"], 2.0)
        self.assertNotEqual(second["sca_gain"], first["sca_gain"])

    def test_two_copies_are_distinct_objects(self):
        context = make_context()
        first = context.science_section("instrument")
        second = context.science_section("instrument")
        self.assertIsNot(first, second)

    def test_missing_section_raises_configerror(self):
        context = make_context()
        with self.assertRaises(ConfigError):
            context.science_section("does-not-exist")


# ---------------------------------------------------------------------------
# record()
# ---------------------------------------------------------------------------

class RecordTests(unittest.TestCase):

    def test_record_accumulates_into_provenance(self):
        context = make_context()
        context.record(fwhm_ref=2.1)
        context.record(fwhm_sci=1.9)
        self.assertEqual(context.provenance, {"fwhm_ref": 2.1, "fwhm_sci": 1.9})

    def test_record_overwrites_a_key_recorded_twice(self):
        context = make_context()
        context.record(fwhm_ref=2.1)
        context.record(fwhm_ref=2.5)
        self.assertEqual(context.provenance["fwhm_ref"], 2.5)

    def test_provenance_starts_empty(self):
        context = make_context()
        self.assertEqual(context.provenance, {})


if __name__ == "__main__":
    unittest.main()
