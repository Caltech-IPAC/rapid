"""
File:    boundaries.py

The narrow injectable interfaces the termination protocol writes through.

The protocol has exactly two external stores — S3 (configuration snapshot,
diagnostics bundle, terminal record) and the database (through W1's attempt
writer) — and one read-only scheduler surface. This module defines the S3 side
as the smallest interface that supports the protocol's requirements, so unit
tests substitute an in-memory implementation and prove crash recovery without
a live bucket ever being touched. W8 owns the live proof; W2 owns the
semantics.

**Why an interface rather than calling boto3 with a mocked client.** The
protocol's correctness rests on two S3 behaviours: conditional create (so a
replayed step cannot overwrite), and checksum-on-read (so a reconciler
validates rather than trusts presence). Mocking boto3 would mean asserting on
`put_object` kwargs — testing that we passed `IfNoneMatch="*"`, not that the
create was actually conditional. An interface with `put_if_absent` as a NAMED
operation lets the in-memory implementation enforce the semantics for real,
and lets the boto3 implementation be a thin, separately-verifiable adapter.

**`ObjectExists` is not an error the protocol treats as failure.** Every write
here is keyed by immutable attempt identity, so an object already present at
the key this attempt would write is *this attempt's own earlier write*,
replayed after a crash. The protocol's response is to validate it by checksum
and continue — "idempotent by identity", principle 6. What is a failure is an
object present at the key whose content does not match: that is two attempts
believing they own one identity, and it raises.
"""

import dataclasses
import hashlib
from typing import Any, Protocol


@dataclasses.dataclass(frozen=True)
class PutResult:
    """What a conditional create did.

    `created` False with `checksum` matching means the object was already
    there with the same content — the replay case, which is success. `created`
    False with a differing checksum never returns: `put_if_absent` raises
    instead, because that is a genuine identity collision.
    """

    key: str
    checksum: str
    created: bool
    size: int


class ObjectStore(Protocol):
    """The S3 operations the termination protocol needs. All of them."""

    def put_if_absent(self, key: str, body: bytes,
                      content_type: str = "application/octet-stream",
                      ) -> PutResult:
        """Create `key` if absent; if present, validate it matches.

        Returns `created=True` on a real create, `created=False` when the
        object was already present with identical content. Raises
        `StorageError` when the object is present with DIFFERENT content —
        two writers under one identity — and on any transport failure.
        """
        ...

    def get(self, key: str) -> bytes:
        """Read an object. Raises `StorageError` if absent or unreadable."""
        ...

    def head(self, key: str) -> dict | None:
        """Return `{"checksum": ..., "size": ...}`, or None if absent."""
        ...


def checksum(body: bytes) -> str:
    """The content digest used everywhere in the protocol.

    SHA-256, hex, unprefixed. One function so the bundle checksum cited in the
    terminal record, the snapshot digest that keys the configuration object,
    and the reconciler's validation are computed identically — a mismatch in
    hash choice between writer and validator would make every record look
    corrupt.
    """
    return hashlib.sha256(body).hexdigest()


class InMemoryObjectStore:
    """An `ObjectStore` that lives in a dict. Tests only.

    It enforces the real semantics rather than recording calls: `put_if_absent`
    genuinely refuses to overwrite, `get` genuinely fails on a missing key. A
    crash-recovery test that passes against this store is a test of the
    protocol, not of what arguments it passed to boto3.
    """

    def __init__(self):
        self.objects: dict = {}
        # Test hooks: a key in `fail_on_put`/`fail_on_get` makes that
        # operation raise, which is how the crash-at-every-boundary tests
        # simulate a store that is unreachable at one specific step.
        self.fail_on_put: set = set()
        self.fail_on_get: set = set()
        self.put_calls: list = []

    def put_if_absent(self, key: str, body: bytes,
                      content_type: str = "application/octet-stream",
                      ) -> PutResult:
        from pipeline.runtime.errors import StorageError

        self.put_calls.append(key)
        if key in self.fail_on_put:
            raise StorageError(f"simulated put failure for {key}", key=key)

        digest = checksum(body)
        existing = self.objects.get(key)
        if existing is not None:
            if existing["checksum"] == digest:
                return PutResult(key=key, checksum=digest, created=False,
                                 size=len(body))
            raise StorageError(
                f"object {key} already exists with different content "
                f"(existing {existing['checksum'][:12]}, "
                f"new {digest[:12]}): two writers under one attempt identity",
                key=key, existing_checksum=existing["checksum"],
                new_checksum=digest)

        self.objects[key] = {"body": body, "checksum": digest,
                             "content_type": content_type, "size": len(body)}
        return PutResult(key=key, checksum=digest, created=True,
                         size=len(body))

    def get(self, key: str) -> bytes:
        from pipeline.runtime.errors import StorageError

        if key in self.fail_on_get:
            raise StorageError(f"simulated get failure for {key}", key=key)
        record = self.objects.get(key)
        if record is None:
            raise StorageError(f"no such object: {key}", key=key)
        return record["body"]

    def head(self, key: str) -> dict | None:
        record = self.objects.get(key)
        if record is None:
            return None
        return {"checksum": record["checksum"], "size": record["size"]}


class S3ObjectStore:
    """The boto3-backed `ObjectStore`.

    Deliberately thin: every method is one API call plus error translation.
    All the protocol's logic lives above this line, which is what lets the
    in-memory store be a faithful substitute.

    **Conditional create uses `IfNoneMatch`.** S3 added conditional writes in
    2024; `IfNoneMatch="*"` fails with `PreconditionFailed` when the key
    exists, which is a server-side create-once rather than a
    read-then-write race. On that failure the object is read back and its
    checksum compared, which is what distinguishes a replay from a collision.

    Not exercised by W2's tests — no live bucket writes are in this worker's
    scope. W8 proves it live; the unit tests prove the protocol against the
    in-memory store.
    """

    def __init__(self, bucket: str, client: Any = None):
        self.bucket = bucket
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("s3")
        return self._client

    def put_if_absent(self, key: str, body: bytes,
                      content_type: str = "application/octet-stream",
                      ) -> PutResult:
        from pipeline.runtime.errors import StorageError

        digest = checksum(body)
        try:
            self.client.put_object(
                Bucket=self.bucket, Key=key, Body=body,
                ContentType=content_type,
                ChecksumSHA256=_b64_sha256(body),
                IfNoneMatch="*",
            )
        except Exception as exc:  # noqa: BLE001 - translated below
            if _is_precondition_failed(exc):
                existing = self.head(key)
                if existing is not None and existing["checksum"] == digest:
                    return PutResult(key=key, checksum=digest, created=False,
                                     size=len(body))
                raise StorageError(
                    f"object s3://{self.bucket}/{key} already exists with "
                    f"different content: two writers under one attempt "
                    f"identity", key=key, bucket=self.bucket) from exc
            raise StorageError(
                f"could not write s3://{self.bucket}/{key}: {exc}",
                key=key, bucket=self.bucket) from exc
        return PutResult(key=key, checksum=digest, created=True,
                         size=len(body))

    def get(self, key: str) -> bytes:
        from pipeline.runtime.errors import StorageError

        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()
        except Exception as exc:  # noqa: BLE001 - translated
            raise StorageError(
                f"could not read s3://{self.bucket}/{key}: {exc}",
                key=key, bucket=self.bucket) from exc

    def head(self, key: str) -> dict | None:
        from pipeline.runtime.errors import StorageError

        try:
            response = self.client.head_object(
                Bucket=self.bucket, Key=key, ChecksumMode="ENABLED")
        except Exception as exc:  # noqa: BLE001 - translated
            if _is_not_found(exc):
                return None
            raise StorageError(
                f"could not head s3://{self.bucket}/{key}: {exc}",
                key=key, bucket=self.bucket) from exc

        # S3 returns the checksum base64-encoded; the protocol works in hex,
        # so convert rather than storing two conventions.
        encoded = response.get("ChecksumSHA256")
        digest = _hex_from_b64(encoded) if encoded else None
        return {"checksum": digest, "size": response.get("ContentLength", 0)}


def _b64_sha256(body: bytes) -> str:
    import base64

    return base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")


def _hex_from_b64(encoded: str) -> str:
    import base64

    return base64.b64decode(encoded).hex()


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code", ""))
    return ""


def _is_precondition_failed(exc: Exception) -> bool:
    """Is this S3 refusing a conditional create, or a real transport fault?

    Delegates to `submission.submit.is_precondition_failed` rather than keeping
    a second copy (review finding #9). The copy that used to live here matched
    only on `exc.response["Error"]["Code"]`, which botocore supplies and a
    stubbed client generally does not — so against the suites' fakes and moto a
    refused create looked like an unknown failure, and an ordinary replay was
    translated into `StorageError` instead of returning `created=False`. The
    shared version also matches by exception TYPE NAME, which is what those
    clients actually raise.

    Imported inside the function because `boundaries` is otherwise stdlib-only
    by design, and the import direction matters: `pipeline` depends on
    `submission`, never the reverse.
    """
    from submission.submit import is_precondition_failed

    return is_precondition_failed(exc)


def _is_not_found(exc: Exception) -> bool:
    return _error_code(exc) in ("404", "NoSuchKey", "NotFound")
