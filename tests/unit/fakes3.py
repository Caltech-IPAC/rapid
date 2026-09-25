"""A minimal in-memory stand-in for a boto3 S3 client, for tests that must
run whether or not boto3 is installed.

Only the operations ``rapidpipe.products.storage`` and ``rapidpipe run
inputs`` call are implemented: ``put_object`` (test setup), ``get_object``,
``list_objects_v2`` (with ``Prefix`` and ``ContinuationToken``/``IsTruncated``
pagination), ``upload_file``, ``download_file``, ``head_object`` (a missing
key raises a ``404`` client error) and ``copy_object``. ``calls`` records each operation's
name and its key argument, in order, so a test can assert on upload order
(e.g. "manifest.json last").
"""

from __future__ import annotations

from typing import Any


class FakeClientError(Exception):
    """Stands in for ``botocore.exceptions.ClientError``.

    Carries a ``response`` dict shaped like the real exception's
    (``{"Error": {"Code": ...}}``), which is what
    ``rapidpipe.stages.contract._client_error_code`` matches on -- by
    shape, not by ``isinstance`` against botocore -- so this stand-in is
    recognised identically to the real thing.
    """

    def __init__(self, code: str, message: str = "not found"):
        super().__init__(f"{code}: {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


class FakeEndpointConnectionError(Exception):
    """Stands in for ``botocore.exceptions.EndpointConnectionError``.

    Named exactly that so ``_TRANSIENT_EXCEPTION_NAMES`` (matched by class
    name, not ``isinstance``) recognises it as network-shaped.
    """


# Alias matching the real botocore name, for tests that want to raise
# "the network-shaped exception" without caring this is a stand-in.
EndpointConnectionError = FakeEndpointConnectionError


class _Body:
    """Stands in for the streaming ``Body`` on a ``get_object`` response."""

    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeS3:
    """An in-memory bucket store, keyed by ``(bucket, key)``."""

    def __init__(self, page_size: int | None = None):
        self._objects: dict[tuple[str, str], bytes] = {}
        self.calls: list[tuple[str, str]] = []
        #: When set, list_objects_v2 returns at most this many keys per
        #: page, forcing pagination even for a small fixture.
        self.page_size = page_size

    def seed(self, bucket: str, key: str, data: bytes) -> None:
        self._objects[(bucket, key)] = data

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **_: Any) -> dict:
        self.calls.append(("put_object", Key))
        data = Body.read() if hasattr(Body, "read") else Body
        self._objects[(Bucket, Key)] = data
        return {}

    def get_object(self, *, Bucket: str, Key: str, **_: Any) -> dict:
        self.calls.append(("get_object", Key))
        try:
            data = self._objects[(Bucket, Key)]
        except KeyError:
            raise FakeClientError("NoSuchKey") from None
        return {"Body": _Body(data)}

    def list_objects_v2(
        self, *, Bucket: str, Prefix: str = "", ContinuationToken: str | None = None,
        **_: Any,
    ) -> dict:
        self.calls.append(("list_objects_v2", Prefix))
        matching = sorted(
            key for (bucket, key) in self._objects if bucket == Bucket
            and key.startswith(Prefix)
        )
        if self.page_size is None:
            page = matching
            start = 0
        else:
            start = int(ContinuationToken) if ContinuationToken else 0
            page = matching[start:start + self.page_size]

        contents = [
            {"Key": key, "Size": len(self._objects[(Bucket, key)])}
            for key in page
        ]
        response: dict[str, Any] = {"Contents": contents}
        if self.page_size is not None and start + self.page_size < len(matching):
            response["IsTruncated"] = True
            response["NextContinuationToken"] = str(start + self.page_size)
        else:
            response["IsTruncated"] = False
        return response

    def upload_file(self, filename: str, bucket: str, key: str, **_: Any) -> None:
        self.calls.append(("upload_file", key))
        with open(filename, "rb") as fh:
            self._objects[(bucket, key)] = fh.read()

    def download_file(self, bucket: str, key: str, filename: str, **_: Any) -> None:
        self.calls.append(("download_file", key))
        try:
            data = self._objects[(bucket, key)]
        except KeyError:
            raise FakeClientError("NoSuchKey") from None
        with open(filename, "wb") as fh:
            fh.write(data)

    def head_object(self, *, Bucket: str, Key: str, **_: Any) -> dict:
        self.calls.append(("head_object", Key))
        try:
            data = self._objects[(Bucket, Key)]
        except KeyError:
            raise FakeClientError("404", "Not Found") from None
        return {"ContentLength": len(data)}

    def copy_object(
        self, *, Bucket: str, Key: str, CopySource: dict[str, str], **_: Any,
    ) -> dict:
        self.calls.append(("copy_object", Key))
        try:
            data = self._objects[(CopySource["Bucket"], CopySource["Key"])]
        except KeyError:
            raise FakeClientError("NoSuchKey") from None
        self._objects[(Bucket, Key)] = data
        return {}


class FakeVersionedS3:
    """An in-memory versioned bucket for ``rapidpipe.runs.cleanup``.

    Implements only ``list_object_versions`` (``Prefix``, ``KeyMarker`` /
    ``VersionIdMarker`` pagination at ``page_size`` entries, versions and
    delete markers both) and ``delete_objects`` (``Delete={"Objects":
    [{"Key", "VersionId"}], "Quiet": True}``, removing exactly those
    versions). ``calls`` records ``(operation, kwargs)`` in order.
    ``fail_keys`` makes ``delete_objects`` report an error for those keys
    instead of deleting them.
    """

    def __init__(self, page_size: int = 1000):
        self.page_size = page_size
        # (bucket, key, version_id, is_delete_marker), in insertion order.
        self.entries: list[tuple[str, str, str, bool]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_keys: set[str] = set()
        self._next_version = 0

    def seed(self, bucket: str, key: str, *, versions: int = 1, delete_marker: bool = False) -> None:
        for _ in range(versions):
            self._next_version += 1
            self.entries.append((bucket, key, f"v{self._next_version:06d}", False))
        if delete_marker:
            self._next_version += 1
            self.entries.append((bucket, key, f"v{self._next_version:06d}", True))

    def remaining(self, bucket: str, prefix: str = "") -> list[tuple[str, str]]:
        return [(k, v) for b, k, v, _m in self.entries if b == bucket and k.startswith(prefix)]

    def list_object_versions(self, **kwargs: Any) -> dict:
        self.calls.append(("list_object_versions", dict(kwargs)))
        bucket = kwargs["Bucket"]
        prefix = kwargs.get("Prefix", "")
        matching = sorted(
            (k, v, m) for b, k, v, m in self.entries if b == bucket and k.startswith(prefix))
        key_marker = kwargs.get("KeyMarker")
        version_marker = kwargs.get("VersionIdMarker")
        if key_marker is not None:
            matching = [e for e in matching
                        if (e[0], e[1]) > (key_marker, version_marker or "")]
        page = matching[:self.page_size]
        truncated = len(matching) > self.page_size
        response: dict[str, Any] = {
            "Versions": [{"Key": k, "VersionId": v} for k, v, m in page if not m],
            "DeleteMarkers": [{"Key": k, "VersionId": v} for k, v, m in page if m],
            "IsTruncated": truncated,
        }
        if truncated:
            response["NextKeyMarker"] = page[-1][0]
            response["NextVersionIdMarker"] = page[-1][1]
        return response

    def delete_objects(self, **kwargs: Any) -> dict:
        self.calls.append(("delete_objects", dict(kwargs)))
        bucket = kwargs["Bucket"]
        objects = kwargs["Delete"]["Objects"]
        assert len(objects) <= 1000, "delete_objects takes at most 1000 keys"
        errors = []
        for obj in objects:
            if obj["Key"] in self.fail_keys:
                errors.append({"Key": obj["Key"], "VersionId": obj["VersionId"],
                               "Code": "AccessDenied", "Message": "denied"})
                continue
            self.entries = [e for e in self.entries
                            if not (e[0] == bucket and e[1] == obj["Key"]
                                    and e[2] == obj["VersionId"])]
        return {"Errors": errors} if errors else {}
