"""
File:    test_boundaries.py

The object-store boundary: conditional create, checksum-on-read, and the
in-memory implementation's fidelity to those semantics.

The in-memory store is what every crash-recovery test writes through, so its
faithfulness is load-bearing: a test that passes against a store which
silently overwrites would prove nothing about a protocol whose correctness
rests on create-once.
"""

import unittest

from pipeline.runtime.boundaries import (
    InMemoryObjectStore,
    PutResult,
    S3ObjectStore,
    checksum,
)
from pipeline.runtime.errors import StorageError


class TestChecksum(unittest.TestCase):

    def test_is_sha256_hex(self):
        self.assertEqual(
            checksum(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")

    def test_is_stable(self):
        """One function everywhere, so writer and validator agree. A hash
        mismatch between them would make every record look corrupt."""
        self.assertEqual(checksum(b"abc"), checksum(b"abc"))

    def test_differs_on_different_content(self):
        self.assertNotEqual(checksum(b"a"), checksum(b"b"))


class TestInMemoryObjectStore(unittest.TestCase):

    def setUp(self):
        self.store = InMemoryObjectStore()

    def test_put_creates_and_reports_created(self):
        result = self.store.put_if_absent("k", b"body")
        self.assertIsInstance(result, PutResult)
        self.assertTrue(result.created)
        self.assertEqual(result.checksum, checksum(b"body"))
        self.assertEqual(result.size, 4)

    def test_identical_content_is_a_replay_not_a_create(self):
        self.store.put_if_absent("k", b"body")
        result = self.store.put_if_absent("k", b"body")
        self.assertFalse(result.created)
        self.assertEqual(result.checksum, checksum(b"body"))

    def test_different_content_at_one_key_raises(self):
        """The store genuinely refuses to overwrite. If it did not, every
        crash-recovery test that passes through it would be vacuous."""
        self.store.put_if_absent("k", b"one")
        with self.assertRaises(StorageError):
            self.store.put_if_absent("k", b"two")
        self.assertEqual(self.store.get("k"), b"one")

    def test_get_returns_what_was_written(self):
        self.store.put_if_absent("k", b"body")
        self.assertEqual(self.store.get("k"), b"body")

    def test_get_on_a_missing_key_raises(self):
        with self.assertRaises(StorageError):
            self.store.get("nope")

    def test_head_reports_checksum_and_size(self):
        self.store.put_if_absent("k", b"body")
        head = self.store.head("k")
        self.assertEqual(head["checksum"], checksum(b"body"))
        self.assertEqual(head["size"], 4)

    def test_head_on_a_missing_key_is_none(self):
        self.assertIsNone(self.store.head("nope"))

    def test_the_failure_hooks_simulate_an_unreachable_store(self):
        self.store.fail_on_put.add("k")
        with self.assertRaises(StorageError):
            self.store.put_if_absent("k", b"body")

        self.store.put_if_absent("j", b"body")
        self.store.fail_on_get.add("j")
        with self.assertRaises(StorageError):
            self.store.get("j")

    def test_put_calls_are_recorded_in_order(self):
        self.store.put_if_absent("a", b"1")
        self.store.put_if_absent("b", b"2")
        self.assertEqual(self.store.put_calls, ["a", "b"])


class _FakeS3Client:
    """Just enough boto3 surface to exercise the adapter's translation."""

    def __init__(self):
        self.objects = {}
        self.put_kwargs = []
        # Keys whose HEAD omits ChecksumSHA256 — what real S3 returns for an
        # object stored without a checksum, with a different algorithm, or via
        # multipart upload. The fake used to always supply one, so the
        # adapter's absent-digest branch was unreachable from any test.
        self.head_without_checksum = set()

    def put_object(self, **kwargs):
        self.put_kwargs.append(kwargs)
        key = kwargs["Key"]
        if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
            raise _ClientError("PreconditionFailed")
        self.objects[key] = kwargs["Body"]

    def get_object(self, **kwargs):
        key = kwargs["Key"]
        if key not in self.objects:
            raise _ClientError("NoSuchKey")
        return {"Body": _Body(self.objects[key])}

    def head_object(self, **kwargs):
        import base64
        import hashlib

        key = kwargs["Key"]
        if key not in self.objects:
            raise _ClientError("404")
        body = self.objects[key]
        if key in self.head_without_checksum:
            return {"ContentLength": len(body)}
        return {
            "ChecksumSHA256": base64.b64encode(
                hashlib.sha256(body).digest()).decode("ascii"),
            "ContentLength": len(body),
        }


class _ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class TestS3ObjectStoreAdapter(unittest.TestCase):
    """The adapter's translation only. No live bucket is touched — W8 owns
    the live proof; these assert that the adapter asks for the right thing."""

    def setUp(self):
        self.client = _FakeS3Client()
        self.store = S3ObjectStore("bucket", client=self.client)

    def test_create_is_conditional_server_side(self):
        """`IfNoneMatch="*"` is a server-side create-once, not a
        read-then-write race."""
        self.store.put_if_absent("k", b"body")
        self.assertEqual(self.client.put_kwargs[0]["IfNoneMatch"], "*")

    def test_a_checksum_is_sent_with_every_put(self):
        self.store.put_if_absent("k", b"body")
        self.assertIn("ChecksumSHA256", self.client.put_kwargs[0])

    def test_precondition_failure_with_matching_content_is_a_replay(self):
        self.store.put_if_absent("k", b"body")
        result = self.store.put_if_absent("k", b"body")
        self.assertFalse(result.created)

    def test_precondition_failure_with_different_content_raises(self):
        self.store.put_if_absent("k", b"one")
        with self.assertRaises(StorageError):
            self.store.put_if_absent("k", b"two")

    def test_head_converts_the_base64_checksum_to_hex(self):
        """S3 returns base64; the protocol works in hex. Two conventions
        would mean a validator comparing incomparable strings."""
        self.store.put_if_absent("k", b"body")
        self.assertEqual(self.store.head("k")["checksum"], checksum(b"body"))

    def test_head_on_a_missing_key_is_none_not_an_error(self):
        self.assertIsNone(self.store.head("nope"))

    def test_get_translates_a_client_error(self):
        with self.assertRaises(StorageError):
            self.store.get("nope")


class TestChecksumlessHead(unittest.TestCase):
    """A HEAD with no stored digest must not decide the question either way.

    Real S3 omits `ChecksumSHA256` for any object written without one, written
    with a different algorithm, or uploaded multipart. Deciding "different
    content" from that absence is the same mistake as deciding "same content"
    from a matching length — reading a missing fact as an answer.
    """

    def setUp(self):
        self.client = _FakeS3Client()
        self.store = S3ObjectStore("bucket", client=self.client)

    def test_head_reports_the_absence_rather_than_inventing_a_digest(self):
        self.store.put_if_absent("k", b"body")
        self.client.head_without_checksum.add("k")

        self.assertIsNone(self.store.head("k")["checksum"])

    def test_an_identical_replay_is_a_replay_even_with_no_stored_digest(self):
        # The crash-recovery path the whole create-once protocol rests on.
        # This used to raise StorageError("already exists with different
        # content") for byte-identical content, permanently: the attempt could
        # never re-run to completion.
        self.store.put_if_absent("k", b"body")
        self.client.head_without_checksum.add("k")

        result = self.store.put_if_absent("k", b"body")

        self.assertFalse(result.created)
        self.assertEqual(checksum(b"body"), result.checksum)

    def test_genuinely_different_content_still_collides(self):
        # The fix must not turn a real two-writer collision into a silent
        # replay: absence sends us to the bytes, and the bytes still disagree.
        self.store.put_if_absent("k", b"one")
        self.client.head_without_checksum.add("k")

        with self.assertRaises(StorageError):
            self.store.put_if_absent("k", b"two")


if __name__ == "__main__":
    unittest.main()
