"""
File:    test_publishing.py

Tests for `publish_products`: the one helper every job type's upload stage
goes through, and the write-once contract it now enforces.

These drive the real helper against a fake S3 client that enforces the
CONDITION rather than recording the call. That distinction is the point.
Asserting `IfNoneMatch="*"` was passed proves we assembled the right keyword
argument; it does not prove that an occupied key refuses a second writer, nor
that identical bytes are allowed through as the replay they are. Those are the
two outcomes an operator sees, so the fake genuinely refuses and genuinely
serves the existing bytes back, and a test that passes here is a test of the
behaviour.

No stub machinery for numpy/astropy/boto3 here, unlike `test_context.py`:
`pipeline.stages.__init__` imports only `context`, and `publishing` itself
imports nothing outside the standard library and this tree.
"""

import base64
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from pipeline.runtime.errors import InputError, StorageError  # noqa: E402
from pipeline.stages.context import StageContext  # noqa: E402
from pipeline.stages.publishing import (publish_products,  # noqa: E402
                                        verify_downloaded_input)
from submission.manifest import ProcessingUnit, UnitFacts  # noqa: E402

BUCKET = "rapid-products"


class FakeLogger:
    """Records every call so a test can assert what was said, without pulling
    in the real logging machinery or asserting on formatting."""

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
            (msg % args if args else str(msg))
            for _level, msg, args in self.calls)


class PreconditionFailed(Exception):
    """A refusal identifiable only by its class name — no `response` mapping.

    What a stubbed client and moto actually raise. Kept as its own class
    because the predicate that used to live in `pipeline.runtime.boundaries`
    matched only on the error code and missed exactly this shape.
    """


class ClientError(Exception):
    """The botocore-shaped refusal: a `response` mapping carrying the code."""

    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """Enforces conditional create for real. Semantics, not call recording.

    `put_object` refuses an occupied key when `IfNoneMatch="*"` is sent, and
    `head_object` answers with the stored checksum the way S3 does — base64,
    not hex — so the adapter's conversion is exercised rather than assumed.
    """

    def __init__(self, bare_exception=False, omit_checksum=False):
        self.objects = {}
        self.put_kwargs = []
        self._bare = bare_exception
        #: Serve objects with no `ChecksumSHA256`, as one written before
        #: checksums were sent would be.
        self._omit_checksum = omit_checksum

    def put_object(self, Bucket, Key, Body, **kwargs):
        body = Body.read() if hasattr(Body, "read") else Body
        self.put_kwargs.append({"Bucket": Bucket, "Key": Key, **kwargs})
        if kwargs.get("IfNoneMatch") == "*" and Key in self.objects:
            raise self._refusal()
        self.objects[Key] = body

    def head_object(self, Bucket, Key, ChecksumMode=None):
        if Key not in self.objects:
            raise ClientError("404")
        body = self.objects[Key]
        response = {"ContentLength": len(body)}
        if not self._omit_checksum:
            response["ChecksumSHA256"] = base64.b64encode(
                hashlib.sha256(body).digest()).decode("ascii")
        return response

    def download_file(self, Bucket, Key, path):
        with open(path, "wb") as handle:
            handle.write(self.objects[Key])

    def _refusal(self):
        if self._bare:
            return PreconditionFailed("the key is taken")
        return ClientError("PreconditionFailed")


def make_context(workdir=None, **overrides) -> StageContext:
    unit = overrides.pop("unit", ProcessingUnit(
        exposure=1, sca=2,
        facts=UnitFacts(science_image_uri="s3://b/img.fits")))
    fields = {
        "workdir": workdir,
        "unit": unit,
        "job_type": "science",
        "science": {"release": {"schema_version": 1}},
        "parameters": {"s3/products-bucket": BUCKET},
        "logger": FakeLogger(),
        "s3": overrides.pop("s3", None) or FakeS3(),
        "run_id": "run-1",
        "attempt_id": 7,
    }
    fields.update(overrides)
    return StageContext(**fields)


class PublishProductsTests(unittest.TestCase):
    """The upload half of the write-once contract (review finding #9)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def write(self, name, content=b"mosaic bytes"):
        path = os.path.join(self._tmp.name, name)
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    # -- the create is conditional -------------------------------------------

    def test_the_create_is_conditional_server_side(self):
        """`IfNoneMatch="*"`, so S3 refuses a second writer rather than the
        stage racing a head-then-put."""
        context = make_context()
        path = self.write("ref_image.fits")

        publish_products(context, BUCKET, [("reference_image", path)])

        self.assertEqual(context.s3.put_kwargs[0]["IfNoneMatch"], "*")

    def test_a_checksum_is_sent_with_every_upload(self):
        """S3 validates the bytes it received against this, so a truncated
        transfer is rejected by the service rather than recorded as a
        product."""
        context = make_context()
        path = self.write("ref_image.fits")

        publish_products(context, BUCKET, [("reference_image", path)])

        self.assertIn("ChecksumSHA256", context.s3.put_kwargs[0])

    def test_the_sent_checksum_is_the_recorded_one_in_another_encoding(self):
        """One hash across the tree. A digest sent to S3 that differed from
        the digest recorded would make every product look corrupt to the
        registrar validating it."""
        context = make_context()
        path = self.write("ref_image.fits")

        published = publish_products(context, BUCKET,
                                     [("reference_image", path)])

        sent = context.s3.put_kwargs[0]["ChecksumSHA256"]
        self.assertEqual(base64.b64decode(sent).hex(),
                         published[0]["checksum"])

    def test_the_recorded_checksum_is_of_the_bytes_on_disk(self):
        context = make_context()
        path = self.write("ref_image.fits", b"exactly these bytes")

        published = publish_products(context, BUCKET,
                                     [("reference_image", path)])

        self.assertEqual(published[0]["checksum"],
                         hashlib.sha256(b"exactly these bytes").hexdigest())

    def test_the_key_carries_the_attempt_prefix(self):
        context = make_context()
        path = self.write("ref_image.fits")

        publish_products(context, BUCKET, [("reference_image", path)])

        key = context.s3.put_kwargs[0]["Key"]
        self.assertTrue(key.startswith(context.product_prefix()))
        self.assertTrue(key.endswith("ref_image.fits"))

    # -- replay vs collision -------------------------------------------------

    def test_republishing_identical_bytes_is_not_an_error(self):
        """The stage re-running after a crash between upload and record is an
        ordinary replay: the object already at the key IS the one this attempt
        meant to write, and the attempt continues."""
        s3 = FakeS3()
        path = self.write("ref_image.fits")

        first = publish_products(make_context(s3=s3), BUCKET,
                                 [("reference_image", path)])
        second = publish_products(make_context(s3=s3), BUCKET,
                                  [("reference_image", path)])

        self.assertEqual(first[0]["uri"], second[0]["uri"])
        self.assertEqual(first[0]["checksum"], second[0]["checksum"])

    def test_a_replay_still_records_the_publication(self):
        """`created=False` is success. A replay that returned nothing would
        close the attempt with no record of a product that does exist."""
        s3 = FakeS3()
        path = self.write("ref_image.fits")
        publish_products(make_context(s3=s3), BUCKET,
                         [("reference_image", path)])

        context = make_context(s3=s3)
        published = publish_products(context, BUCKET,
                                     [("reference_image", path)])

        self.assertEqual(len(published), 1)
        self.assertIn("reference_image", context.published_products)

    def test_a_replay_says_so_in_the_log(self):
        """Honest, but worth saying: reaching an occupied key means the stage
        ran twice, which is the trace a retry loop or a lost attempt identity
        leaves behind."""
        s3 = FakeS3()
        path = self.write("ref_image.fits")
        publish_products(make_context(s3=s3), BUCKET,
                         [("reference_image", path)])

        context = make_context(s3=s3)
        publish_products(context, BUCKET, [("reference_image", path)])

        self.assertIn("already present", context.logger.text())

    def test_different_bytes_under_a_used_key_raise(self):
        """Two writers holding one identity. No correct outcome exists, so
        this is refused rather than resolved."""
        s3 = FakeS3()
        first = self.write("ref_image.fits", b"attempt one")
        second = self.write("ref_image.fits.other", b"attempt two")
        publish_products(make_context(s3=s3), BUCKET,
                         [("reference_image", first)])

        # Same basename, so the same key — which is exactly how the
        # `unidentified-attempt` fallback makes two units collide.
        colliding = os.path.join(os.path.dirname(second), "ref_image.fits")
        os.replace(second, colliding)

        with self.assertRaises(StorageError) as ctx:
            publish_products(make_context(s3=s3), BUCKET,
                             [("reference_image", colliding)])
        self.assertIn("different content", str(ctx.exception))

    def test_the_refused_object_keeps_the_first_writers_bytes(self):
        """Refusing has to leave the object untouched. A version that raised
        after writing would be no better than the unconditional upload."""
        s3 = FakeS3()
        first = self.write("ref_image.fits", b"attempt one")
        publish_products(make_context(s3=s3), BUCKET,
                         [("reference_image", first)])
        key = s3.put_kwargs[0]["Key"]

        with open(first, "wb") as handle:
            handle.write(b"attempt two")
        with self.assertRaises(StorageError):
            publish_products(make_context(s3=s3), BUCKET,
                             [("reference_image", first)])

        self.assertEqual(s3.objects[key], b"attempt one")

    def test_the_collision_message_names_the_unidentified_attempt_trap(self):
        """The realistic way two attempts collide is both losing their
        identity and falling back to the shared prefix, so the message points
        at it rather than leaving an operator to rediscover it."""
        s3 = FakeS3()
        path = self.write("ref_image.fits", b"one")
        publish_products(make_context(s3=s3), BUCKET,
                         [("reference_image", path)])
        with open(path, "wb") as handle:
            handle.write(b"two")

        with self.assertRaises(StorageError) as ctx:
            publish_products(make_context(s3=s3), BUCKET,
                             [("reference_image", path)])
        self.assertIn("unidentified-attempt", str(ctx.exception))

    def test_a_conflict_reported_only_by_exception_type_is_recognized(self):
        """Stubbed clients and moto raise a bare `PreconditionFailed` with no
        `response` mapping. The stricter code-only predicate treated that as an
        unknown transport fault, so a replay was reported as a failed upload."""
        s3 = FakeS3(bare_exception=True)
        path = self.write("ref_image.fits")
        publish_products(make_context(s3=s3), BUCKET,
                         [("reference_image", path)])

        # Does not raise: recognized as the conditional refusal it is.
        published = publish_products(make_context(s3=s3), BUCKET,
                                     [("reference_image", path)])
        self.assertEqual(len(published), 1)

    def test_an_object_with_no_stored_checksum_falls_back_to_size(self):
        """Objects written before checksums were sent carry nothing to
        compare. Size is weak evidence, so it permits the replay rather than
        manufacturing a collision — but only when the sizes agree."""
        s3 = FakeS3(omit_checksum=True)
        path = self.write("ref_image.fits", b"same length!")
        publish_products(make_context(s3=s3), BUCKET,
                         [("reference_image", path)])

        published = publish_products(make_context(s3=s3), BUCKET,
                                     [("reference_image", path)])
        self.assertEqual(len(published), 1)

    def test_no_stored_checksum_and_a_different_size_still_raises(self):
        s3 = FakeS3(omit_checksum=True)
        path = self.write("ref_image.fits", b"short")
        publish_products(make_context(s3=s3), BUCKET,
                         [("reference_image", path)])
        with open(path, "wb") as handle:
            handle.write(b"considerably longer bytes")

        with self.assertRaises(StorageError):
            publish_products(make_context(s3=s3), BUCKET,
                             [("reference_image", path)])

    # -- failures are raised, not counted ------------------------------------

    def test_a_transport_failure_is_translated_not_swallowed(self):
        class Broken(FakeS3):
            def put_object(self, **_kwargs):
                raise RuntimeError("AccessDenied")

        context = make_context(s3=Broken())
        path = self.write("ref_image.fits")

        with self.assertRaises(StorageError) as ctx:
            publish_products(context, BUCKET, [("reference_image", path)])
        self.assertIn("AccessDenied", str(ctx.exception))

    def test_nothing_to_publish_is_an_input_error(self):
        with self.assertRaises(InputError):
            publish_products(make_context(), BUCKET, [])


class VerifyDownloadedInputTests(unittest.TestCase):
    """The reading half: a citation is a URI AND a checksum."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "coadd_inputs.csv")
        with open(self.path, "wb") as handle:
            handle.write(b"1,a.fits\n2,b.fits\n")
        self.digest = hashlib.sha256(b"1,a.fits\n2,b.fits\n").hexdigest()

    def test_a_matching_checksum_passes_quietly(self):
        context = make_context()
        verify_downloaded_input(context, "coadd-input list", self.path,
                                self.digest)

    def test_a_differing_checksum_raises_before_the_science_runs(self):
        """The object under the cited key changed after the unit was
        gathered. Coadding it would build a reference from frames this unit's
        own submission never named."""
        context = make_context()

        with self.assertRaises(InputError) as ctx:
            verify_downloaded_input(context, "coadd-input list", self.path,
                                    "0" * 64)
        self.assertIn("coadd-input list", str(ctx.exception))

    def test_an_absent_checksum_is_legacy_not_a_failure(self):
        """A manifest written before the citing fact existed still describes
        ordinary work. Refusing it would strand every unit an older submitter
        gathered."""
        context = make_context()

        verify_downloaded_input(context, "coadd-input list", self.path, None)

        self.assertIn("no checksum", context.logger.text())


if __name__ == "__main__":
    unittest.main()
