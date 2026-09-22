"""Locations a stage reads from or writes to: a local directory or S3 prefix.

The stage contract's "Invocation" section: "`--inputs` names a local
directory or S3 prefix containing `manifest.json`... `--outputs` names the
attempt's exclusive output location... The same program accepts a local
directory or an S3 location." This module gives that a small type
(:class:`Location`) and the four operations ``rapidpipe.stages.contract``
needs to fetch inputs down to local disk before a stage body runs, and
publish outputs back up after it returns.

This module imports nothing from ``rapidpipe`` outside ``rapidpipe.products``.
``boto3`` is imported lazily, inside the functions that need it, so importing
this module -- and running a stage entirely against local directories --
never requires it to be installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class LocationError(ValueError):
    """A location string is malformed."""


@dataclass(frozen=True)
class Location:
    """A parsed ``--inputs``/``--outputs`` argument.

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


def parse_location(text: str) -> Location:
    """Parse a ``--inputs``/``--outputs`` argument into a :class:`Location`.

    ``s3://bucket`` and ``s3://bucket/`` are the bucket root, an empty
    prefix. Anything starting ``s3://`` with no bucket (``s3://`` or
    ``s3:///foo``) is malformed. Everything else is a local path.
    """
    if text.startswith("s3://"):
        rest = text[len("s3://"):]
        bucket, _, prefix = rest.partition("/")
        if not bucket:
            raise LocationError(f"malformed S3 location {text!r}: no bucket")
        prefix = prefix.strip("/")
        return Location(kind="s3", bucket=bucket, prefix=prefix)
    return Location(kind="local", path=Path(text))


def join(location: Location, relative: str) -> str:
    """Return the string a manifest or database row records for ``relative``
    beneath ``location``."""
    if location.is_s3():
        prefix = f"{location.prefix}/{relative}" if location.prefix else relative
        return f"s3://{location.bucket}/{prefix}"
    assert location.path is not None
    return str(location.path / relative)


def _key(location: Location, relative: str = "") -> str:
    assert location.bucket is not None
    if not relative:
        return location.prefix or ""
    return f"{location.prefix}/{relative}" if location.prefix else relative


def s3_client() -> Any:
    """Return a fresh ``boto3`` S3 client.

    A module-level indirection point: ``rapidpipe.stages.contract`` calls
    this exactly once per :func:`~rapidpipe.stages.contract.run_stage`
    invocation that needs one, so tests can monkeypatch
    ``rapidpipe.products.storage.s3_client`` to return a fake.
    """
    import boto3

    return boto3.client("s3")


def fetch_prefix(location: Location, dest_dir: Path, *, client: Any = None) -> Path:
    """Materialize ``location`` beneath ``dest_dir`` on local disk.

    For a local location, returns ``location.path`` unchanged -- no copy is
    made. For an S3 location, lists every object under ``prefix/`` and
    downloads each beneath ``dest_dir``, preserving the key's path relative
    to the prefix, then returns ``dest_dir``. Refuses to overwrite a
    ``dest_dir`` that already exists and is non-empty.
    """
    if not location.is_s3():
        assert location.path is not None
        return location.path

    if dest_dir.exists() and any(dest_dir.iterdir()):
        raise LocationError(
            f"refusing to fetch into non-empty destination: {dest_dir}")
    dest_dir.mkdir(parents=True, exist_ok=True)

    s3 = client if client is not None else s3_client()
    prefix = location.prefix
    list_prefix = f"{prefix}/" if prefix else ""

    continuation_token = None
    found_any = False
    while True:
        kwargs: dict[str, Any] = {"Bucket": location.bucket, "Prefix": list_prefix}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        response = s3.list_objects_v2(**kwargs)
        for obj in response.get("Contents", []):
            found_any = True
            key = obj["Key"]
            relative = key[len(list_prefix):] if list_prefix else key
            if not relative:
                # The "directory marker" object some tools write at the
                # prefix itself, with nothing after it.
                continue
            dest_path = dest_dir / relative
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(location.bucket, key, str(dest_path))
        if response.get("IsTruncated"):
            continuation_token = response.get("NextContinuationToken")
            continue
        break

    if not found_any:
        raise LocationError(
            f"no objects found under s3://{location.bucket}/{list_prefix}")

    return dest_dir


def fetch_object(
    location: Location, relative: str, dest_path: Path, *, client: Any = None,
) -> Path:
    """Fetch one object, ``relative`` to ``location``'s prefix, to ``dest_path``.

    For a local location, returns the local path unchanged (no copy) --
    ``dest_path`` is ignored, matching :func:`fetch_prefix`'s no-copy rule
    for local locations. For S3, downloads exactly one object; used by
    ``--dry-run`` to validate an S3 input manifest without fetching the
    whole prefix.
    """
    if not location.is_s3():
        assert location.path is not None
        return location.path / relative

    s3 = client if client is not None else s3_client()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    key = _key(location, relative)
    s3.download_file(location.bucket, key, str(dest_path))
    return dest_path


def publish_dir(
    src_dir: Path,
    location: Location,
    *,
    client: Any = None,
    last: str = "manifest.json",
) -> None:
    """Publish everything beneath ``src_dir`` to ``location``.

    For a local location: a no-op when ``location.path`` is already
    ``src_dir`` (the runner passes the same directory as both the body's
    working output directory and its declared ``--outputs``); any other
    local location is an error, since the runner never asks for a local
    copy.

    For an S3 location: uploads every file beneath ``src_dir``, preserving
    relative paths as keys under the prefix, uploading the file named
    ``last`` (the manifest) after every other file -- so a reader that
    lists the prefix and finds the manifest can trust every other object
    is already there (stage contract, "The manifest": "a stage publishes
    its manifest only after its outputs are complete").
    """
    if not location.is_s3():
        assert location.path is not None
        if location.path == src_dir:
            return
        raise LocationError(
            f"publish_dir: local destination {location.path} is not the "
            f"stage's own output directory {src_dir}; a local copy is not "
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
