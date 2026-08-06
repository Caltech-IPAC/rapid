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
import importlib.util
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

from pipeline.entrypoints import job  # noqa: E402
from pipeline.runtime.errors import ConfigError, RecordsError  # noqa: E402
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
    """W6 replaced the refusal with the record-consuming implementation.

    Until W6 this function raised: wiring it to the legacy path would have
    re-entered the log-grep chain on the wrong side of the cutover fence. It
    now consumes reconciled outcomes, so what these tests pin is the contract
    it consumes them under.
    """

    def _context(self):
        context = mock.Mock(job_type="registration")
        context.logger = mock.Mock()
        context.provenance = {}
        return context

    def test_consumes_reconciled_attempts_and_records_the_pass(self):
        context = self._context()
        rows = [{"attempt_id": 1, "lifecycle_state": "terminal_after_start",
                 "rapid_outcome": "success", "product_disposition": "published",
                 "scheduler_state": "SUCCEEDED", "exposure_id": 1, "sca": 1,
                 "sky_tile": None, "error_category": None,
                 "application_intended_exit": 0, "scheduler_observed_exit": 0}]

        with mock.patch("database.modules.utils.rapid_db_connect.connection"), \
                mock.patch("pipeline.registration.candidates",
                           return_value=rows):
            job.dispatch_registration(context)

        context.record.assert_called_once()
        recorded = context.record.call_args.kwargs["registration"]
        # AMENDED by FixA (review finding #5). No registrar is installed, so
        # this is a DECISION pass: the attempt is approved and counted into
        # `would_register`, never into `registered`. Reporting approvals as
        # registrations is what let the job return registered=N while writing
        # no operation-table rows.
        self.assertEqual(1, recorded["would_register"])
        self.assertEqual(0, recorded["registered"])
        self.assertEqual(0, recorded["exit_code"])

    def test_a_failing_registration_raises_rather_than_exiting_zero(self):
        # The defect in what this replaced: four scripts hardcoded exit 0, so
        # a pass where every registration failed looked like a clean one.
        context = self._context()
        rows = [{"attempt_id": 1, "lifecycle_state": "terminal_after_start",
                 "rapid_outcome": "success", "product_disposition": "published",
                 "scheduler_state": "SUCCEEDED", "exposure_id": 1, "sca": 1,
                 "sky_tile": None, "error_category": None,
                 "application_intended_exit": 0, "scheduler_observed_exit": 0}]

        def failing(conn, candidate_rows, register=None, run=None,
                    dry_run=False):
            from pipeline.registration import RegistrationRun
            failed = RegistrationRun()
            failed.failed = 1
            return failed

        with mock.patch("database.modules.utils.rapid_db_connect.connection"), \
                mock.patch("pipeline.registration.candidates",
                           return_value=rows), \
                mock.patch("pipeline.registration.register_batch",
                           side_effect=failing):
            with self.assertRaises(RecordsError):
                job.dispatch_registration(context)


# ---------------------------------------------------------------------------
# _execute
# ---------------------------------------------------------------------------

class ExecuteOutcomeTests(unittest.TestCase):
    """The success path, which no canary had ever reached."""

    def _recorder(self):
        from pipeline.runtime.stages import StageRecorder
        return StageRecorder()

    def test_a_clean_run_reports_success(self):
        # `recorder.failed` is a PROPERTY. Calling it — `recorder.failed()` —
        # raised TypeError on every successful non-registration job, and was
        # unreached only because the sole canaried job type raises earlier.
        context = mock.Mock(job_type="science")
        recorder = self._recorder()

        with mock.patch.object(job, "run_sequence"):
            outcome, disposition, error = job._execute(
                context, "science", recorder, mock.Mock())

        self.assertEqual("success", outcome)
        self.assertEqual("published", disposition)
        self.assertIsNone(error)

    def test_a_recorded_stage_failure_reports_partial(self):
        from pipeline.runtime.stages import StageRecord

        context = mock.Mock(job_type="science")
        recorder = self._recorder()
        recorder.record(StageRecord(
            stage_name="one", started_at=None, duration_ms=1,
            outcome="failure", error_category="tool_failure"))

        with mock.patch.object(job, "run_sequence"):
            outcome, _disposition, _error = job._execute(
                context, "science", recorder, mock.Mock())

        self.assertEqual("partial", outcome)


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
