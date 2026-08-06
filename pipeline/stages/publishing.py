"""Uploading a stage's products, and recording what was published.

One helper, used by every job type's upload stage, because "what is a published
product" must not be answered differently in three places (review findings #6
and #18).

What it enforces, and why each was a defect:

**Run- and attempt-scoped keys.** Product keys are built from
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
"""

import os

from pipeline.runtime.boundaries import checksum
from pipeline.runtime.errors import InputError, StorageError


def _digest(path: str) -> str:
    with open(path, "rb") as handle:
        return checksum(handle.read())


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
        try:
            context.s3.upload_file(path, bucket, key)
        except Exception as exc:  # noqa: BLE001 - translated, never swallowed
            raise StorageError(
                f"could not upload the product {name!r} from {path} to "
                f"s3://{bucket}/{key}: {exc}. The attempt cannot be closed as "
                f"having published products it did not write.") from exc

        entry = context.publish(
            name, uri=f"s3://{bucket}/{key}", checksum=_digest(path),
            size=os.path.getsize(path), product_type=product_type)
        published.append({"name": name, **entry})
        context.logger.info("published %s -> s3://%s/%s", name, bucket, key)

    return published
