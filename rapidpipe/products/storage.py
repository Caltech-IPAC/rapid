"""S3 locations, the S3 client and S3 error classification.

:class:`Location` names a local directory or an S3 prefix;
:func:`publish_dir` uploads a directory to an S3 prefix with its manifest
last (``rapidpipe.foundation.prefix``). :func:`s3_client` is the one place
``boto3`` makes an S3 client, a module-level indirection point tests
monkeypatch. :func:`client_error_code`, :func:`is_not_found` and
:func:`is_transient` classify a ClientError-shaped exception by shape.

This module imports nothing from ``rapidpipe``. ``boto3`` is imported lazily, inside :func:`s3_client`, so importing this
module never requires it to be installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class LocationError(ValueError):
    """A location :func:`publish_dir` cannot publish to."""


@dataclass(frozen=True)
class Location:
    """A local directory or an S3 prefix.

    ``kind`` is ``"local"`` or ``"s3"``. A local location carries ``path``;
    an S3 location carries ``bucket`` and ``prefix`` (no leading or
    trailing slash; ``""`` is a valid, bucket-root prefix).
    """

    kind: str
    path: Path | None = None
    bucket: str | None = None
    prefix: str | None = None

    def is_s3(self) -> bool:
        return self.kind == "s3"


def _key(location: Location, relative: str = "") -> str:
    assert location.bucket is not None
    if not relative:
        return location.prefix or ""
    return f"{location.prefix}/{relative}" if location.prefix else relative


def s3_client(unsigned: bool = False) -> Any:
    """Return a fresh ``boto3`` S3 client; ``unsigned`` reads a public bucket anonymously.

    A module-level indirection point, so tests can monkeypatch
    ``rapidpipe.products.storage.s3_client`` to return a fake.
    """
    import boto3

    if unsigned:
        from botocore import UNSIGNED
        from botocore.config import Config

        return boto3.client("s3", config=Config(signature_version=UNSIGNED))
    return boto3.client("s3")


def publish_dir(
    src_dir: Path,
    location: Location,
    *,
    client: Any = None,
    last: str = "manifest.json",
) -> None:
    """Publish everything beneath ``src_dir`` to ``location``.

    For a local location: a no-op when ``location.path`` is already
    ``src_dir``; any other local location is an error, since a local copy
    is not supported.

    For an S3 location: uploads every file beneath ``src_dir``, preserving
    relative paths as keys under the prefix, uploading the file named
    ``last`` (the manifest) after every other file -- so a reader that
    lists the prefix and finds the manifest can trust every other object
    is already there.
    """
    if not location.is_s3():
        assert location.path is not None
        if location.path == src_dir:
            return
        raise LocationError(
            f"publish_dir: local destination {location.path} is not the "
            f"directory being published, {src_dir}; a local copy is not "
            "supported")

    s3 = client if client is not None else s3_client()

    last_path: Path | None = None
    other_paths: list[Path] = []
    for path in sorted(src_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(src_dir).as_posix()
        if relative == last:
            last_path = path
        else:
            other_paths.append(path)

    for path in other_paths:
        relative = path.relative_to(src_dir).as_posix()
        key = _key(location, relative)
        s3.upload_file(str(path), location.bucket, key)

    if last_path is not None:
        key = _key(location, last)
        s3.upload_file(str(last_path), location.bucket, key)


#: Recognised as network-shaped: a fetch or publish is retryable when the
#: exception's class name ends with one of these, checked by name (suffix,
#: not exact match, so a test's stand-in class such as
#: ``FakeEndpointConnectionError`` is recognised the same way as
#: ``botocore.exceptions.EndpointConnectionError``) rather than by
#: ``isinstance``, since this module never imports botocore.
TRANSIENT_EXCEPTION_NAMES = (
    "EndpointConnectionError",
    "ConnectionError",
    "ConnectTimeoutError",
    "ReadTimeoutError",
    "ThrottlingException",
    "RequestTimeout",
    "RequestTimeoutException",
)

#: A ClientError-shaped exception's ``response["Error"]["Code"]`` values
#: that mean "the object is not there": ``404`` from ``head_object``,
#: ``NoSuchKey`` from ``get_object``, ``NotFound`` from some resources.
NOT_FOUND_ERROR_CODES = ("404", "NoSuchKey", "NotFound")

#: A ClientError-shaped exception's ``Error.Code`` values that mean "slow
#: down and repeat": AWS's throttling codes (SSM's ``ThrottlingException``
#: failed 1,448 of 3,456 intakes on 2026-10-23) and S3's ``SlowDown``.
TRANSIENT_ERROR_CODES = ("Throttling", "ThrottlingException", "TooManyRequestsException", "SlowDown",
                         "RequestLimitExceeded")


def client_error_code(exc: BaseException) -> str | None:
    """The ``Error.Code`` of a ClientError-shaped exception, or ``None``.

    Matches by shape (a ``response`` attribute holding that structure), not
    by ``isinstance``, so a test's stand-in exception is recognised the
    same way as a real ``botocore.exceptions.ClientError`` without this
    module importing botocore.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    if not isinstance(error, dict):
        return None
    code = error.get("Code")
    return code if isinstance(code, str) else None


def is_not_found(exc: BaseException) -> bool:
    """Whether ``exc`` is a ClientError-shaped "the object is not there"."""
    return client_error_code(exc) in NOT_FOUND_ERROR_CODES


def is_transient(exc: BaseException) -> bool:
    """Whether ``exc`` is worth repeating: its class name is network-shaped
    (:data:`TRANSIENT_EXCEPTION_NAMES`), or it is ClientError-shaped with a
    throttling code (:data:`TRANSIENT_ERROR_CODES`) or HTTP status 503."""
    class_name = type(exc).__name__
    if any(class_name.endswith(name) for name in TRANSIENT_EXCEPTION_NAMES):
        return True
    if client_error_code(exc) in TRANSIENT_ERROR_CODES:
        return True
    response = getattr(exc, "response", None)
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode") if isinstance(response, dict) else None
    return status == 503
