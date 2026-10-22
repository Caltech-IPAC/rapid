"""Files under a URL: ``file://`` on this host, or ``s3://`` (Part 1 statement 6).

A job writes its attempt under ``<outputs root>/<run>/<stage>/<job>/<attempt>/``
and the publisher reads it back from there; a stage reads its named inputs
by the URL their rows record. Two schemes, one function each way:

- :func:`get` gives a local path for a file URL: the file itself for
  ``file://``, a download into this process's cache for ``s3://``. Objects
  under a published prefix never change, so a cached download is reused.
  The cache is one ``mkdtemp`` directory under ``TMPDIR``, removed when the
  process exits normally; a killed process leaves it behind.
- :func:`put` makes a local directory's files appear under a prefix: a
  no-op for ``file://`` (the stage wrote there), an upload of every file
  for ``s3://`` with ``manifest.json`` last.

``boto3`` is imported only through :func:`rapidpipe.products.storage.s3_client`,
the one indirection a test replaces.
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from rapidpipe.products import storage

_cache: Path | None = None


def local_path(url: str) -> Path:
    """The directory or file a ``file://`` URL names on this host."""
    parsed = urlparse(url)
    if parsed.scheme != "file":
        raise ValueError(f"{url!r} is not a file:// URL")
    return Path(unquote(parsed.path))


def _bucket_key(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"{url!r} is neither a file:// nor an s3://bucket/ URL")
    return parsed.netloc, parsed.path.lstrip("/")


def get(url: str, *, client: Any = None, unsigned: bool = False) -> Path:
    """A local path holding the file ``url`` names; ``FileNotFoundError`` when an S3 object is absent.

    ``unsigned`` reads a public bucket anonymously (botocore's ``UNSIGNED``
    signature), as the intake stage reads its source root.
    """
    global _cache
    if urlparse(url).scheme == "file":
        return local_path(url)
    bucket, key = _bucket_key(url)
    if _cache is None:
        _cache = Path(tempfile.mkdtemp(prefix="rapidpipe-get-"))
        atexit.register(shutil.rmtree, _cache, ignore_errors=True)
    path = _cache / bucket / key
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            s3 = client or (storage.s3_client(unsigned=True) if unsigned else storage.s3_client())
            s3.download_file(bucket, key, str(path))
        except Exception as exc:  # noqa: BLE001 - ClientError-shaped
            if storage.is_not_found(exc):
                raise FileNotFoundError(url) from exc
            raise
    return path


def put(directory: Path, prefix: str, *, client: Any = None) -> None:
    """Upload every file under ``directory`` to the ``s3://`` ``prefix``, ``manifest.json`` last.

    For a ``file://`` prefix, ``directory`` is the prefix itself and nothing moves.
    """
    if urlparse(prefix).scheme == "file":
        if local_path(prefix) != directory:
            raise ValueError(f"{directory} is not the file:// prefix {prefix}")
        return
    bucket, key = _bucket_key(prefix)
    storage.publish_dir(directory, storage.Location(kind="s3", bucket=bucket, prefix=key.strip("/")),
                        client=client)


def attempts(job_prefix: str, *, client: Any = None) -> set[int]:
    """The attempt numbers already present under an ``s3://`` job prefix."""
    bucket, key = _bucket_key(job_prefix.rstrip("/") + "/")
    s3 = client or storage.s3_client()
    found, token = set(), None
    while True:
        page = s3.list_objects_v2(Bucket=bucket, Prefix=key,
                                  **({"ContinuationToken": token} if token else {}))
        for obj in page.get("Contents", []):
            first = obj["Key"][len(key):].split("/", 1)[0]
            if first.isdigit():
                found.add(int(first))
        if not page.get("IsTruncated"):
            return found
        token = page["NextContinuationToken"]
