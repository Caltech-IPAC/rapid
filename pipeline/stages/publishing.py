"""Uploading a stage's products, and recording what was published.

One helper, used by every job type's upload stage, because "what is a published
product" must not be answered differently in three places (review findings #6
and #18).

What it enforces, and why each was a defect:

**Run- and attempt-scoped keys.** Object keys are built from
`StageContext.product_prefix` and nowhere else. The reference path built its
own from `job_type/unit` — and its helpers from a legacy `job_type/jid…` — so a
retry or a reprocessing run overwrote the previous attempt's objects, leaving
earlier records citing keys whose bytes had changed.

**Failures are raised, not counted.** `util.upload_files_to_s3_bucket` returns
a boolean nobody read: an upload could fail while the stage recorded a
successful publication, and the attempt closed `published` with objects that
were never written. An upload that fails here fails the stage.

**Every published object is recorded by URI and checksum.** The terminal record
used to serialize `context.products` — the stage-to-stage channel, holding
downloaded inputs and scratch paths — as local paths and scalars. A registrar
reading that could not tell which canonical S3 objects were final products, nor
verify their bytes. `publish` writes an entry per object with the immutable URI
and the checksum of exactly the bytes uploaded.

**A published key is written once, never over** (review finding #9). The three
rules above were enforced; this one was only stated. The upload was
`context.s3.upload_file(path, bucket, key)` — three positional arguments, no
condition — so whatever was at the key lost. Attempt-scoped keys made that
mostly harmless, but not harmless enough in two live shapes. A same-attempt
replay (the stage re-runs after a crash between upload and record) rewrote its
own objects, which is benign only as long as the bytes are identical and
nothing proves they are. Worse, `StageContext.product_prefix` falls back to
`job_type/unit/unidentified-attempt` when run or attempt identity is missing —
and EVERY context that has lost its identity collides on that one prefix, so
two units publishing under it silently overwrote each other and left both
attempts' records citing keys holding one attempt's bytes. That is precisely
the immutable-keys violation the prefix was introduced to close, reappearing
one layer down.

`IfNoneMatch="*"` closes it the same way `submission.submit.S3ManifestStore`
and `pipeline.runtime.boundaries.S3ObjectStore` close it, and for the same
reason: S3 refuses the second writer server-side rather than us racing a
head-then-put. An identical replay is NOT an error — the object already there
is the one this stage meant to write, and the attempt continues. Different
bytes under a used key raise, because that is two writers holding one identity
and no correct outcome exists.

**The reading half of the same contract.** `verify_downloaded_input` is here
rather than in a module of its own because it answers the mirror question:
this module decides what it means to publish an object by URI and checksum,
and something has to decide what it means to CONSUME one. Both reference paths
import this module already, and splitting the two halves of one rule across two
files is how they drift.
"""

import hashlib
import os

from pipeline.runtime.boundaries import checksum  # noqa: F401 - re-exported
from pipeline.runtime.errors import InputError, StorageError
from submission.submit import is_precondition_failed

#: How much of a product file is held in memory at once. Products here are
#: FITS mosaics and catalogues — hundreds of megabytes for a reference image —
#: so nothing below ever does `handle.read()` on one.
_CHUNK = 1024 * 1024


def _digest(path: str) -> str:
    """The SHA-256 of a product file, hex, read in chunks.

    Same digest as `boundaries.checksum` produces for a bytes body — one hash
    function across the tree, so the registrar validating a product and the
    stage that published it are comparing comparable strings. This spelling
    exists only so a multi-hundred-megabyte mosaic is never materialized whole
    to be hashed; the previous `checksum(handle.read())` did exactly that.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _b64_of_hex(hex_digest: str) -> str:
    """S3 wants the digest base64-encoded; the pipeline works in hex."""
    import base64

    return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")


def _stored_digest(client, bucket: str, key: str) -> str:
    """The SHA-256 of the object already at `key`, hex, read in chunks.

    For the objects S3 cannot answer for: one written before this pipeline
    sent `ChecksumSHA256`, which therefore has no stored digest to compare
    against. The bytes are the only evidence, so they are fetched and hashed.

    STREAMED, not `read()`. `get_object` returns a `StreamingBody`, and
    reading it whole would put a multi-hundred-megabyte mosaic in memory —
    the same reason `_digest` chunks the local file. `iter_chunks` is boto3's
    own chunked reader; the fallback covers the stubs and file-like bodies
    that stand in for it in tests.

    A failure to read is NOT a match. It propagates, and the caller defers
    rather than citing bytes it could not verify.
    """
    body = client.get_object(Bucket=bucket, Key=key)["Body"]
    digest = hashlib.sha256()
    try:
        chunks = body.iter_chunks(chunk_size=_CHUNK)
    except AttributeError:
        chunks = iter(lambda: body.read(_CHUNK), b"")
    for block in chunks:
        digest.update(block)
    return digest.hexdigest()


def _put_file_if_absent(client, path: str, bucket: str, key: str,
                        digest: str) -> bool:
    """Create `key` from the bytes of `path`. Return whether it was created.

    **Why not `upload_file`.** The obvious fix — pass `IfNoneMatch` through
    `upload_file(..., ExtraArgs=...)` — does not exist. boto3 validates
    `ExtraArgs` against `s3transfer.manager.TransferManager.ALLOWED_UPLOAD_ARGS`
    and raises `ValueError("Invalid extra_args key ...")` for anything outside
    it; that list carries the checksum arguments and the content/encryption
    ones, and no conditional header at all — no `IfNoneMatch`, no `IfMatch`.
    Nor could it sensibly: above `multipart_threshold` `upload_file` becomes
    CreateMultipartUpload/UploadPart/CompleteMultipartUpload, and a condition
    belongs on the completing call rather than on any part, which is a
    different argument split from the single-PUT path. So conditional create
    for a file means calling `put_object` ourselves.

    **The cost, and why it is acceptable.** `put_object` is a single PUT, so it
    gives up the multipart parallelism and the 5 GiB single-object ceiling that
    `upload_file` handles transparently. RAPID's products are comfortably under
    that — reference mosaics and catalogues, not archives — and correctness of
    the write-once contract is worth more here than upload throughput on
    objects this size. Should a product ever approach 5 GiB this needs to
    become an explicit multipart with `IfNoneMatch` on the completion.

    **Memory.** `Body` is the open file handle, not its contents: boto3 accepts
    a seekable file-like object and streams it, so a 400 MB mosaic is never
    resident. The digest is computed in a separate chunked pass rather than by
    reading the file into a buffer, for the same reason.
    """
    with open(path, "rb") as handle:
        try:
            client.put_object(Bucket=bucket, Key=key, Body=handle,
                              ChecksumSHA256=_b64_of_hex(digest),
                              IfNoneMatch="*")
            return True
        except Exception as exc:  # noqa: BLE001 - narrowed immediately below
            if not is_precondition_failed(exc):
                raise

    # The key is taken. Whether that is this attempt replaying itself or two
    # writers under one identity is decided by the bytes, and only by the
    # bytes: compare what is there against what we were about to write.
    existing = client.head_object(Bucket=bucket, Key=key,
                                  ChecksumMode="ENABLED")
    encoded = existing.get("ChecksumSHA256")
    if encoded is not None:
        import base64

        if base64.b64decode(encoded).hex() == digest:
            return False
    elif _stored_digest(client, bucket, key) == digest:
        # An object written before checksums were sent carries no
        # ChecksumSHA256 to compare, so its digest is computed from the bytes
        # themselves.
        #
        # SIZE IS NOT EVIDENCE (round-4 finding #4). This used to accept a
        # length match as a replay, which is the one thing the invariant
        # directly above forbids: a legacy object of the same length but
        # different content was left in S3 while `publish_products` recorded
        # the LOCAL digest, so the terminal record cited a checksum for bytes
        # that were never stored — and the registrar, which validates a
        # product by fetching the key and hashing what it gets, would refuse
        # it. Reading the object costs one GET on a path taken only when a key
        # is already occupied, which is rare and is exactly when being right
        # matters more than being quick.
        return False

    raise StorageError(
        f"a product already exists at s3://{bucket}/{key} with different "
        f"content. Two attempts have claimed one product identity — most "
        f"likely both lost their run/attempt id and fell back to the shared "
        f"'unidentified-attempt' prefix — and the records citing this key no "
        f"longer describe its bytes. Republish under a distinct attempt "
        f"identity rather than replacing this object.",
        key=key, bucket=bucket)


def publish_products(context, bucket, entries, product_type=None):
    """Upload `entries` under this attempt's prefix and record each one.

    `entries` is an iterable of (name, local_path) pairs — normally
    `context.publishable()`. Returns the list of published entries.

    Raises
    ------
    StorageError
        If any upload fails. The bytes are the product; a stage that could not
        write them has not produced one, and reporting success would close the
        attempt `published` over objects that do not exist.
    StorageError
        If a key is already occupied by DIFFERENT bytes. Identical bytes are a
        replay and pass through as an ordinary publication.
    """
    entries = list(entries)
    if not entries:
        raise InputError(
            "no product files exist to upload; every stage either skipped or "
            "produced nothing on disk")

    prefix = context.product_prefix()
    published = []

    for name, path in entries:
        key = f"{prefix}/{os.path.basename(path)}"
        # Hashed before the write, not after: the checksum is what makes a
        # replay distinguishable from a collision, so it has to exist before we
        # can ask S3 to create the key conditionally.
        digest = _digest(path)
        try:
            created = _put_file_if_absent(context.s3, path, bucket, key,
                                          digest)
        except StorageError:
            # Already the right shape and already says which defect it is.
            raise
        except Exception as exc:  # noqa: BLE001 - translated, never swallowed
            raise StorageError(
                f"could not upload the product {name!r} from {path} to "
                f"s3://{bucket}/{key}: {exc}. The attempt cannot be closed as "
                f"having published products it did not write.") from exc

        entry = context.publish(
            name, uri=f"s3://{bucket}/{key}", checksum=digest,
            size=os.path.getsize(path), product_type=product_type)
        published.append({"name": name, **entry})
        if created:
            context.logger.info("published %s -> s3://%s/%s", name, bucket, key)
        else:
            # Worth saying out loud. The publication is honest — the bytes at
            # the key are this attempt's — but a stage reaching an occupied
            # key means it ran twice, and that is the signal a retry loop or a
            # lost attempt identity leaves behind.
            context.logger.info(
                "published %s -> s3://%s/%s (already present with identical "
                "content; this attempt is replaying its own upload)",
                name, bucket, key)

    return published


def split_s3_uri(uri: str) -> tuple[str, str]:
    """`s3://bucket/key/with/slashes` -> `("bucket", "key/with/slashes")`.

    A URI names its own bucket, so the bucket is READ FROM IT rather than
    assumed. The stages used to take the bucket from the `s3/inputs-bucket`
    parameter and derive the key by splitting that name off the URI, which
    silently did the wrong thing whenever the object was somewhere else:
    `"s3://other/k".split("inputs-bucket/", 1)[-1]` is the whole URI back
    again, so the download was attempted with `s3://other/k` as the KEY,
    against the inputs bucket, and failed as a missing key rather than as
    the disagreement it was.

    Raises `InputError` rather than returning a partial parse: a malformed
    URI is bad input to this invocation, and the caller has no better
    answer than the manifest it was handed.
    """
    if not uri.startswith("s3://"):
        raise InputError(
            f"{uri!r} is not an s3:// URI, so its bucket cannot be read "
            "from it", uri=uri)
    remainder = uri[len("s3://"):]
    bucket, slash, key = remainder.partition("/")
    if not bucket or not slash or not key:
        raise InputError(
            f"{uri!r} names no bucket-and-key pair", uri=uri)
    return bucket, key


def verify_downloaded_input(context, name: str, path: str,
                            expected: str | None) -> None:
    """Check a downloaded input against the checksum its manifest cited.

    `expected` absent is not a failure. Manifests written before the citing
    fact existed carry a URI and nothing else, and those units are ordinary
    work that must keep running — a stage that refused them would strand every
    unit gathered by an older submitter. Absence is logged, not raised, so the
    gap is visible without being fatal.

    `expected` present and NOT matching is a hard failure, and specifically an
    `InputError`: the manifest is this invocation's input, and bytes that
    disagree with it mean the object under the cited key changed after the unit
    was described. For the coadd-input CSV that is not hypothetical — the
    overlap query returns more frames as the survey advances, so a second
    gathering pass under one run identity writes a genuinely different list.
    Before this check a reference job in that state coadded whatever list it
    found and reported success, producing a reference image built from frames
    its own submission never named and which nothing downstream could
    reconstruct.
    """
    if expected is None:
        context.logger.info(
            "%s carries no checksum in the manifest; it was gathered before "
            "the citing fact existed, so its bytes cannot be verified", name)
        return

    actual = _digest(path)
    if actual == expected:
        return

    raise InputError(
        f"the {name} downloaded to {path} does not match the checksum the "
        f"manifest cited for it (expected {expected[:12]}, got "
        f"{actual[:12]}). The object under the cited key has been replaced "
        f"since this unit was gathered, so processing it would produce a "
        f"product built from inputs this unit's submission never described.",
        unit=context.unit.key, expected=expected, actual=actual)
