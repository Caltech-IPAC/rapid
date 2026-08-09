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

import importlib.util
import sys
import types
import unittest


def _stub(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def _module_or_stub(name):
    """The module under `name` — the stub if one was installed, else the
    real package, imported on demand. Written this way because the helper
    no longer shadows installed packages (W8): indexing sys.modules would
    raise KeyError for a real module nobody has imported yet."""
    if name in sys.modules:
        return sys.modules[name]
    import importlib
    return importlib.import_module(name)

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
    # than by `sys.modules` membership (W8, 2026-08-06). In the image every
    # name here except injectionLightCurveModels is real, and shadowing a
    # real package with a bare ModuleType broke collection outright:
    # `from astropy.wcs import WCS` found a stub with no WCS, and a stub at
    # "numpy.ma" beneath the real numpy sent numpy 2.x's lazy __getattr__
    # into unbounded recursion. Off a laptop with none of these installed
    # the old form was fine, which is why it survived to be found here.
    for name in names:
        if name in sys.modules:
            continue
        try:
            if importlib.util.find_spec(name) is not None:
                continue
        except (ImportError, ValueError):
            pass
        _stub(name)

    numpy = _module_or_stub("numpy")
    if not hasattr(numpy, "ma"):
        numpy.ma = _module_or_stub("numpy.ma")
        numpy.array = lambda *a, **k: None
        numpy.nanmedian = lambda *a, **k: 0.0
        numpy.nanmin = lambda *a, **k: 0.0
        numpy.nanmax = lambda *a, **k: 0.0
        numpy.isnan = lambda *a, **k: False

    astropy_io = _module_or_stub("astropy.io")
    if not hasattr(astropy_io, "fits"):
        astropy_io.fits = _module_or_stub("astropy.io.fits")
        astropy_io.ascii = _module_or_stub("astropy.io.ascii")

    astropy_table = _module_or_stub("astropy.table")
    if not hasattr(astropy_table, "QTable"):
        astropy_table.QTable = object
        astropy_table.join = lambda *a, **k: None
        astropy_table.Table = object

    astropy_wcs = _module_or_stub("astropy.wcs")
    if not hasattr(astropy_wcs, "WCS"):
        astropy_wcs.WCS = object

    astropy_coordinates = _module_or_stub("astropy.coordinates")
    if not hasattr(astropy_coordinates, "SkyCoord"):
        astropy_coordinates.SkyCoord = object

    scipy_ndimage = _module_or_stub("scipy.ndimage")
    if not hasattr(scipy_ndimage, "zoom"):
        scipy_ndimage.zoom = lambda *a, **k: None
        scipy_ndimage.gaussian_filter = lambda *a, **k: None

    botocore_exceptions = _module_or_stub("botocore.exceptions")
    if not hasattr(botocore_exceptions, "ClientError"):
        botocore_exceptions.ClientError = Exception

    psycopg2 = _module_or_stub("psycopg2")
    if not hasattr(psycopg2, "sql"):
        psycopg2.sql = _module_or_stub("psycopg2.sql")

    galsim = _module_or_stub("galsim")
    if not hasattr(galsim, "wcs"):
        galsim.wcs = _module_or_stub("galsim.wcs")
        galsim.roman = _module_or_stub("galsim.roman")

    photutils_background = _module_or_stub("photutils.background")
    if not hasattr(photutils_background, "Background2D"):
        photutils_background.Background2D = object
        photutils_background.MedianBackground = object

    photutils_segmentation = _module_or_stub("photutils.segmentation")
    if not hasattr(photutils_segmentation, "detect_threshold"):
        photutils_segmentation.detect_threshold = lambda *a, **k: None
        photutils_segmentation.detect_sources = lambda *a, **k: None
        photutils_segmentation.deblend_sources = lambda *a, **k: None
        photutils_segmentation.SourceCatalog = object

    injection_models = _module_or_stub("injectionLightCurveModels")
    if not hasattr(injection_models, "SinusoidalLightCurve"):
        injection_models.SinusoidalLightCurve = object
        injection_models.GaussianLightCurve = object

    dateutil_tz = _module_or_stub("dateutil.tz")
    if not hasattr(dateutil_tz, "gettz"):
        dateutil_tz.gettz = lambda *a, **k: None
    dateutil = _module_or_stub("dateutil")
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


class ProductPrefixTests(unittest.TestCase):
    """Product keys carry run and attempt identity (implementation review #18).

    The prefix was `job_type/exposure/sca`, which carries neither — so
    reprocessing or retrying the same exposure/SCA OVERWROTE the earlier
    attempt's objects, and every old record and checksum then referred to keys
    whose bytes had changed. That is what the storage design's immutable-keys
    rule forbids: a key, once written, names those bytes forever.
    """

    def test_the_prefix_carries_run_and_attempt_identity(self):
        context = make_context(run_id="run-1", attempt_id=4242)
        prefix = context.product_prefix()

        self.assertIn("run-1", prefix)
        self.assertIn("attempt-0000004242", prefix)
        self.assertIn(context.unit.key, prefix)
        self.assertTrue(prefix.startswith(context.job_type))

    def test_two_attempts_at_one_unit_do_not_share_a_prefix(self):
        first = make_context(run_id="run-1", attempt_id=1).product_prefix()
        retry = make_context(run_id="run-1", attempt_id=2).product_prefix()

        self.assertNotEqual(first, retry)

    def test_two_runs_over_one_unit_do_not_share_a_prefix(self):
        first = make_context(run_id="run-1", attempt_id=1).product_prefix()
        reprocess = make_context(run_id="run-2", attempt_id=9).product_prefix()

        self.assertNotEqual(first, reprocess)

    def test_a_context_with_no_identity_says_so_rather_than_colliding(self):
        # A production path that lost its identity must fail visibly, not
        # silently produce the old colliding shape.
        prefix = make_context().product_prefix()

        self.assertIn("unidentified-attempt", prefix)

    def test_the_prefix_is_stable_for_one_attempt(self):
        context = make_context(run_id="run-1", attempt_id=7)
        self.assertEqual(context.product_prefix(), context.product_prefix())

    def test_the_attempt_component_is_zero_padded_to_ten_digits(self):
        # storage.md § Key schema, component law: attempt is 10 digits.
        context = make_context(run_id="run-1", attempt_id=7)
        self.assertIn("attempt-0000000007", context.product_prefix())

    def test_the_attempt_component_does_not_truncate_a_wide_value(self):
        context = make_context(run_id="run-1", attempt_id=12345678901)
        self.assertIn("attempt-12345678901", context.product_prefix())

    def test_database_effect_job_type_mints_no_product_key(self):
        # Co-design ruling 2: "database-effect job types declare empty
        # product sets and mint no product keys." A statistics unit's `.key`
        # is `{field:06d}/00` — a synthetic array-layer carrier, not a real
        # storage path (`_per_field_units`, submission/gathering.py) — so
        # `product_prefix()` must refuse rather than build a misleading S3
        # key from it.
        from pipeline.runtime.errors import ConfigError

        context = make_context(job_type="statistics", run_id="run-1",
                               attempt_id=1)
        with self.assertRaises(ConfigError):
            context.product_prefix()

    def test_product_producing_job_types_are_unaffected(self):
        # The refusal is scoped to database-effect types; science and
        # reference-image keep building product keys exactly as before.
        for job_type in ("science", "reference-image"):
            context = make_context(job_type=job_type, run_id="run-1",
                                   attempt_id=1)
            self.assertTrue(context.product_prefix())

    def test_a_job_type_outside_the_registry_still_mints_a_key(self):
        # post-process is deliberately unregistered (ruling 9); it must keep
        # building product keys exactly as every job type did before this
        # ruling, not be refused for lacking a declaration.
        context = make_context(job_type="post-process", run_id="run-1",
                               attempt_id=1)
        self.assertTrue(context.product_prefix())


# ---------------------------------------------------------------------------
# The database-effect job types' disposition record (post-DB chain conversion)
# ---------------------------------------------------------------------------

class DispositionRecordTests(unittest.TestCase):
    """What a job type that writes rows instead of products records.

    Co-design ruling 2, and the operations design's § Post-DB science chain:
    "each declares an empty product set, its terminal record is a pure
    disposition record that promotes nothing, and its effect — rows written,
    rows removed — is recorded in the attempt record's own fields".
    """

    def test_effect_counts_land_in_provenance(self):
        context = make_context(job_type="catalog-load")
        context.record_effect(rows_written=4096, rows_removed=0)

        self.assertEqual(context.provenance["rows_written"], 4096)
        self.assertEqual(context.provenance["rows_removed"], 0)

    def test_a_zero_effect_is_recorded_rather_than_omitted(self):
        # The should-find-nothing dedup check's whole output is a zero. An
        # omitted count and a count of zero are different statements: the
        # first says nobody looked.
        context = make_context(job_type="merge-dedup")
        context.record_effect(rows_written=0, rows_removed=0)

        self.assertIn("rows_written", context.provenance)
        self.assertIn("rows_removed", context.provenance)

    def test_counts_accumulate_across_stages_of_one_unit(self):
        # A sequence may write through more than one stage, and the unit's
        # effect is their sum — not the last stage's figure.
        context = make_context(job_type="crossmatch")
        context.record_effect(rows_written=10)
        context.record_effect(rows_written=5, rows_removed=2)

        self.assertEqual(context.provenance["rows_written"], 15)
        self.assertEqual(context.provenance["rows_removed"], 2)

    def test_extra_facts_ride_alongside_the_counts(self):
        context = make_context(job_type="catalog-load")
        context.record_effect(rows_written=1, load_rate_rows_per_second=250.0)

        self.assertEqual(context.provenance["load_rate_rows_per_second"], 250.0)

    def test_a_database_effect_job_publishes_no_products(self):
        # THE EMPTY PRODUCT SET. `_execute` derives ProductDisposition.NONE
        # from an empty `published_products`, and `decide` SKIPs `none` — so
        # these attempts close successfully and never become registration
        # candidates. Recording an effect must not create a product.
        context = make_context(job_type="statistics")
        context.record_effect(rows_written=99)

        self.assertEqual(context.published_products, {})

    def test_a_borrowed_connection_is_required_not_invented(self):
        # A database-effect job type with no connection is a wiring fault in
        # the image, and says so, rather than failing with an
        # AttributeError inside a query.
        context = make_context(job_type="catalog-load")

        with self.assertRaises(ConfigError) as caught:
            context.require_connection()

        self.assertIn("catalog-load", str(caught.exception))

    def test_the_lent_connection_is_handed_back_unchanged(self):
        sentinel = object()
        context = make_context(job_type="catalog-load", connection=sentinel)

        self.assertIs(context.require_connection(), sentinel)
