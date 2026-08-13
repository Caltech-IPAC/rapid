"""
File:    artifacts.py

Artifact rows for an attempt's published files — rule 10's missing concept.

**The defect this closes.** No artifact concept existed anywhere in the
repository. `StageContext.publish` recorded an untyped
`{uri, checksum, size?, product_type?}` dict entry
(`pipeline/stages/context.py:325-338`) which flowed into the terminal
record's `products` list, and registration then copied the URI and checksum
onto the product row itself. So "these bytes exist, are durable and are
checksummed" and "this is the scientific product with this identity" were
the same row, and nothing could tell them apart. Rule 10: "Products and
artifacts are distinct records."

**Every published file becomes an artifact — not only the products.** This
is the product/output map brief D fixes, and it is worth stating plainly
because it is the part that looks like an omission if you only read the
product tables: an attempt publishes three difference images (the SFFT
masked one the release binds to the `difference_image` role, plus the ZOGY
and naive variants) and its catalogues. Exactly ONE of those becomes a
product; ALL of them become artifacts. The release doc records that design
already — "The ZOGY and naive variants stay published, checksummed record
products with no identity-table rows" (`cdf/science/pipeline.toml:73-75`) —
and artifacts are what "published, checksummed, no identity-table row"
means once artifacts exist to say it. Companion files are the same case.

**Database-effect job types produce effects, not products.** Crossmatch,
catalog-load, the merge and currency sweeps and alert production write
their outcomes through `context.record_effect`, publish no files, and are
not in `REGISTRABLE_JOB_TYPES` at all. They get no product rows and no
artifact rows: an artifact records bytes at an address, and these job types
produce none. (Both readings recorded for the merge gate, per brief D.)

**Attempt-scoped, and why that is not a bug.** A re-attempt at the same
science writes NEW artifact rows even when the bytes are identical, because
an artifact is a record of a publication event — this attempt put these
bytes at this address — and two attempts are two events. What must produce
no new rows is a REPLAY of the same attempt at the same record sequence,
and that is enforced by `artifacts_replay_uq` in the database rather than
by a find-or-insert here.

**Build provenance lands here, deliberately.** The image digest and source
revision identify the build that produced the bytes. They are excluded from
the product key — which tracks the specified science process, not the build
that executed it — and recorded on the artifact instead, so the exclusion
loses nothing.
"""

import logging

logger = logging.getLogger("rapid.registration.artifacts")

#: The checksum algorithm the publishing stage uses
#: (`pipeline/stages/publishing.py:_digest` — `hashlib.sha256`, hex). Recorded
#: explicitly on every artifact row rather than assumed, because a checksum
#: whose algorithm is implied is a checksum that cannot be re-verified after
#: the algorithm changes.
CHECKSUM_ALGORITHM = "sha256"

#: How long a full hex SHA-256 is. The legacy `refimages.checksum` and
#: `diffimages.checksum` columns were `varchar(32)` and truncated exactly
#: this (`006-core-tables.sql:393,448`; the casts at `rapid_db.py:1936,2182`)
#: — a latent defect flagged by brief D, NOT reproduced here, and fixed by
#: CR-8 (`rapid_systems` migration 054, which widened both columns to
#: `varchar(64)`). `l2files.checksum` (`006-core-tables.sql:259`) is a
#: separate column CR-8 left untouched and still truncates.
SHA256_HEX_LENGTH = 64


class ArtifactRecordingError(ValueError):
    """A published entry could not be recorded as an artifact.

    Raised rather than skipped: a published file with no artifact row is
    exactly the state rule 10 exists to prevent — bytes in the object store
    that the database has no durable record of.
    """


def _entry_checksum(entry, name):
    """The checksum of one published entry, validated for shape.

    The length is checked HERE rather than left to the database's CHECK
    constraint, so the failure names the published product rather than
    surfacing as a constraint violation on a row the reader cannot map back
    to a file. Both checks exist: this one for the message, the constraint
    for the guarantee.
    """
    checksum = (entry.get("checksum") or "").strip().lower()
    if not checksum:
        raise ArtifactRecordingError(
            f"the published entry {name!r} carries no checksum; an artifact "
            f"row asserts these bytes are durable AND verifiable, and one "
            f"without a digest asserts only the first")
    if len(checksum) != SHA256_HEX_LENGTH:
        raise ArtifactRecordingError(
            f"the published entry {name!r} carries a {len(checksum)}-"
            f"character checksum; a {CHECKSUM_ALGORITHM} digest is "
            f"{SHA256_HEX_LENGTH} hex characters. A short value here is a "
            f"legacy varchar(32) truncation reaching a column that does not "
            f"have it, and storing it would make a later comparison succeed "
            f"against the wrong bytes")
    return checksum


def _build_identity(record):
    """`(image_digest, source_revision)` from the record's own provenance.

    Absent rather than invented where the record carries no provenance
    block: pre-provenance records exist and refusing them would block replay
    of exactly the attempts most in need of it. The columns are nullable for
    this reason.
    """
    provenance = record.get("provenance") or {}
    return (provenance.get("container_digest"),
            provenance.get("source_sha"))


def record_artifacts(repository, record, attempt_id, record_sequence,
                     entries=None):
    """Write one artifact row per published file of this attempt.

    Runs inside the registration consumer's per-attempt transaction, on the
    connection that transaction owns, so artifact rows commit with the
    product rows, the legacy version rows and the watermark — or roll back
    with them.

    Parameters
    ----------
    repository : ProductRepository
        Built over the borrowed registration connection.
    record : dict
        The attempt's terminal record. `products` is its list of published
        entries (`termination._product_entries`).
    attempt_id, record_sequence : int
        The replay key's first two components. `record_sequence` is the
        attempt's `terminal_record_sequence`, read off the candidate ROW by
        the registrar — the same number the watermark advances to, so the
        artifact rows and the watermark describe the same registration by
        construction.
    entries : list, optional
        The published entries to record. Defaults to every entry in the
        record — which is the intended call: ALL published files become
        artifacts, not only the role-selected product.

    Returns
    -------
    dict
        Published name -> `Artifact`, so the caller can bind the one that
        filled the product role without re-querying.
    """
    if entries is None:
        entries = record.get("products") or []

    image_digest, source_revision = _build_identity(record)
    artifacts = {}

    for entry in entries:
        name = entry.get("name")
        if not name:
            # A nameless entry cannot be bound to anything or found again.
            raise ArtifactRecordingError(
                f"attempt {attempt_id} published an entry with no name: "
                f"{entry!r}. The name is the artifact's replay key component "
                f"and the handle every binding uses.")
        uri = entry.get("uri")
        if not uri:
            # `_product_entries` carries non-file entries through as
            # `{"name": ..., "value": ...}` — infobits, counts, resolved
            # identities. Those are record facts, not published bytes, and
            # an artifact row for one would assert an object exists that
            # never did.
            logger.debug(
                "attempt %s published %r as a value rather than a file; "
                "no artifact row", attempt_id, name)
            continue

        artifacts[name] = repository.upsert_artifact(
            attempt_id=attempt_id,
            record_sequence=record_sequence,
            published_name=name,
            uri=uri,
            checksum=_entry_checksum(entry, name),
            checksum_algorithm=CHECKSUM_ALGORITHM,
            size_bytes=entry.get("size"),
            content_type=entry.get("product_type"),
            image_digest=image_digest,
            source_revision=source_revision)

    logger.info("attempt %s recorded %d artifact(s) at record sequence %s",
                attempt_id, len(artifacts), record_sequence)
    return artifacts
