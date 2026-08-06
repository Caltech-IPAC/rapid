"""
File:    test_job.py

Tests for the dispatching entrypoint: argument parsing, manifest loading
(checksum and URI validation), route validation, provenance, and the two
things the entrypoint refuses outright (registration's payload, a missing
required parameter).

**Why `sys.modules` is stubbed before import.** `pipeline.entrypoints.job`
imports `pipeline.stages.context`, and importing anything under
`pipeline.stages` runs `pipeline/stages/__init__.py`, which eagerly imports
`sequences` and, through it, `science`, `reference_image`, and
`post_process` — which import numpy, astropy (several submodules), scipy,
psycopg2, boto3, dateutil, galsim, romanisim, and photutils at module scope.
None of those are installed in this environment. The functions under test
here (`parse_arguments`, `load_manifest`, `validate_route`,
`build_provenance`, `dispatch_registration`, `_required`) touch none of
that heavy machinery, so lightweight stand-ins satisfy the import without
pretending to implement the science. This mirrors the existing pattern in
`pipeline.runtime.test` of substituting the smallest fake that makes an
import boundary crossable rather than mocking call-by-call.
"""

import io
import sys
import types
import unittest
from unittest import mock


def _stub(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def _install_third_party_stubs() -> None:
    """Install no-op stand-ins for every third-party module the stage
    package's eager imports touch, so `pipeline.entrypoints.job` (which
    imports `pipeline.stages.context`, which runs `pipeline/stages/__init__`,
    which imports every stage module) can be imported without numpy,
    astropy, scipy, psycopg2, boto3, dateutil, galsim, romanisim, or
    photutils actually being installed.

    Idempotent: skips a module already present, so running this more than
    once (multiple test modules in one process) does not clobber a real
    installation if one is ever present.
    """
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

from pipeline.entrypoints import job  # noqa: E402
from pipeline.runtime.errors import ConfigError  # noqa: E402
from pipeline.runtime.test.stubs import make_job_environment  # noqa: E402
from submission.manifest import (  # noqa: E402
    Manifest,
    ProcessingUnit,
    UnitFacts,
)
from submission.routes import (  # noqa: E402
    CLASS_BULK,
    CLASS_PROMPT,
    JOB_TYPE_SCIENCE,
    RouteError,
)


QUEUE_NAMES = {
    "batch/queue-prompt": "rapid-queue-prompt",
    "batch/queue-bulk": "rapid-queue-bulk",
}


def _science_manifest(**overrides) -> Manifest:
    unit = ProcessingUnit(exposure=1, sca=2, facts=UnitFacts(
        science_image_uri="s3://rapid-bucket/sci.fits"))
    fields = {"units": [unit], "job_type": JOB_TYPE_SCIENCE}
    fields.update(overrides)
    return Manifest(fields.pop("units"), **fields)


class FakeS3Body:
    """Mimics boto3's StreamingBody: a `.read()` that returns bytes."""

    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeS3Client:
    """A minimal `get_object` double: returns the body handed to it, or
    raises what it was told to raise."""

    def __init__(self, body: bytes | None = None, error: Exception | None = None):
        self._body = body
        self._error = error
        self.calls: list = []

    def get_object(self, Bucket, Key):  # noqa: N803 - matches boto3's signature
        self.calls.append((Bucket, Key))
        if self._error is not None:
            raise self._error
        return {"Body": FakeS3Body(self._body)}


# ---------------------------------------------------------------------------
# parse_arguments
# ---------------------------------------------------------------------------

class ParseArgumentsTests(unittest.TestCase):

    def test_class_prompt_is_accepted(self):
        args = job.parse_arguments(["--class", "prompt"])
        self.assertEqual(args.workload_class, "prompt")

    def test_class_bulk_is_accepted(self):
        args = job.parse_arguments(["--class", "bulk"])
        self.assertEqual(args.workload_class, "bulk")

    def test_missing_class_exits_nonzero(self):
        with self.assertRaises(SystemExit) as ctx:
            job.parse_arguments([])
        self.assertNotEqual(ctx.exception.code, 0)

    def test_unknown_class_value_is_rejected(self):
        with self.assertRaises(SystemExit) as ctx:
            job.parse_arguments(["--class", "leisurely"])
        self.assertNotEqual(ctx.exception.code, 0)


# ---------------------------------------------------------------------------
# load_manifest
# ---------------------------------------------------------------------------

class LoadManifestTests(unittest.TestCase):

    def test_checksum_mismatch_raises_configerror(self):
        manifest = _science_manifest()
        body = manifest.to_json().encode("utf-8")
        job_env = make_job_environment(
            manifest_uri="s3://rapid-bucket/manifests/m.json",
            manifest_checksum="not-the-real-checksum")
        client = FakeS3Client(body=body)

        with self.assertRaises(ConfigError):
            job.load_manifest(job_env, client)

    def test_non_s3_uri_raises_configerror(self):
        job_env = make_job_environment(
            manifest_uri="https://example.com/manifest.json")
        with self.assertRaises(ConfigError):
            job.load_manifest(job_env, FakeS3Client())

    def test_uri_with_no_key_raises_configerror(self):
        # "s3://bucket-only" has a bucket but no key: partition leaves the
        # key empty.
        job_env = make_job_environment(manifest_uri="s3://rapid-bucket")
        with self.assertRaises(ConfigError):
            job.load_manifest(job_env, FakeS3Client())

    def test_matching_checksum_returns_the_manifest(self):
        manifest = _science_manifest()
        body = manifest.to_json().encode("utf-8")
        job_env = make_job_environment(
            manifest_uri="s3://rapid-bucket/manifests/m.json",
            manifest_checksum=manifest.checksum())
        client = FakeS3Client(body=body)

        loaded = job.load_manifest(job_env, client)
        self.assertEqual(loaded.job_type, JOB_TYPE_SCIENCE)
        self.assertEqual(client.calls, [("rapid-bucket", "manifests/m.json")])


# ---------------------------------------------------------------------------
# validate_route
# ---------------------------------------------------------------------------

class ValidateRouteTests(unittest.TestCase):

    def test_science_manifest_against_bulk_class_is_rejected(self):
        # science is a prompt-class job type.
        manifest = _science_manifest()
        job_env = make_job_environment(queue_name="rapid-queue-bulk")
        with self.assertRaises(RouteError):
            job.validate_route(manifest, CLASS_BULK, job_env, QUEUE_NAMES)

    def test_science_manifest_against_prompt_with_right_queue_returns_route(self):
        manifest = _science_manifest()
        job_env = make_job_environment(queue_name="rapid-queue-prompt")
        route = job.validate_route(manifest, CLASS_PROMPT, job_env, QUEUE_NAMES)
        self.assertEqual(route.job_type, JOB_TYPE_SCIENCE)

    def test_right_class_wrong_queue_is_rejected(self):
        manifest = _science_manifest()
        job_env = make_job_environment(queue_name="rapid-queue-bulk")
        with self.assertRaises(RouteError):
            job.validate_route(manifest, CLASS_PROMPT, job_env, QUEUE_NAMES)


# ---------------------------------------------------------------------------
# build_provenance
# ---------------------------------------------------------------------------

class BuildProvenanceTests(unittest.TestCase):

    def test_missing_variables_are_named_in_the_message(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                job.build_provenance("digest123", {})
        message = str(ctx.exception)
        self.assertIn("RAPID_SOURCE_SHA", message)
        self.assertIn("RAPID_IMAGE_DIGEST", message)
        self.assertIn("RAPID_JOB_DEFINITION_REV", message)

    def test_all_present_returns_provenance_with_config_digest(self):
        env = {
            "RAPID_SOURCE_SHA": "a" * 40,
            "RAPID_IMAGE_DIGEST": "sha256:" + "b" * 64,
            "RAPID_JOB_DEFINITION_REV": "3",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            provenance = job.build_provenance("digest123", {})
        self.assertEqual(provenance.config_digest, "digest123")
        self.assertEqual(provenance.source_sha, "a" * 40)
        self.assertEqual(provenance.job_definition_rev, "3")

    def test_partial_absence_is_still_named(self):
        env = {"RAPID_SOURCE_SHA": "a" * 40}
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                job.build_provenance("digest123", {})
        message = str(ctx.exception)
        self.assertNotIn("RAPID_SOURCE_SHA,", message.split(":")[0])
        self.assertIn("RAPID_IMAGE_DIGEST", message)
        self.assertIn("RAPID_JOB_DEFINITION_REV", message)


# ---------------------------------------------------------------------------
# dispatch_registration
# ---------------------------------------------------------------------------

class DispatchRegistrationTests(unittest.TestCase):

    def test_raises_configerror(self):
        context = mock.Mock(job_type="registration")
        with self.assertRaises(ConfigError):
            job.dispatch_registration(context)


# ---------------------------------------------------------------------------
# _required
# ---------------------------------------------------------------------------

class RequiredParameterTests(unittest.TestCase):

    def test_present_parameter_is_returned(self):
        self.assertEqual(job._required({"s3/records-bucket": "b"},
                                       "s3/records-bucket"), "b")

    def test_absent_parameter_raises_configerror_naming_it(self):
        with self.assertRaises(ConfigError) as ctx:
            job._required({}, "s3/records-bucket")
        self.assertIn("s3/records-bucket", str(ctx.exception))

    def test_empty_string_parameter_is_treated_as_absent(self):
        with self.assertRaises(ConfigError):
            job._required({"s3/records-bucket": ""}, "s3/records-bucket")


if __name__ == "__main__":
    unittest.main()
