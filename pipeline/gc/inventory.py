"""Step 1 — the pinned, timestamped S3 inventory.

**WHY AN INVENTORY AND NOT A LIVE LISTING.** §4.11 step 1 says "obtain an S3
inventory". The candidate set must come from a PINNED SNAPSHOT, never from a
live `ListObjects` interleaved with the anti-join: an object created during
such a listing is neither reliably present nor reliably absent, and this
process acts on ABSENCE. A listing that missed a newly-published object would
report it as garbage.

**PAGINATION COMPLETENESS IS PROVEN, AND A TRUNCATED LISTING IS FATAL.** This
is the sharpest failure mode in the whole design and it fails silently by
default: a short listing makes objects look absent, absence is what the
anti-join acts on, and nothing about a truncated page announces itself. So
`read_inventory` refuses a source that cannot demonstrate completeness rather
than proceeding with what it got. "Never silently short" is the requirement;
an exception is the only way to meet it.

**AN INVENTORY OLDER THAN THE FRESHNESS BOUND IS REFUSED.** A stale snapshot
describes a bucket that no longer exists. The bound is configuration, not a
constant here, and — like the horizon — there is no default that permits
deletion.

**OBJECTS ARE CAPTURED WITH THEIR `VersionId`.** The products bucket has
versioning Enabled, and deletion is by exact version. An inventory row without
a version identifier cannot be acted on, so it is refused at read time rather
than discovered at delete time.
"""

import datetime
import typing


class InventoryError(Exception):
    """The inventory cannot be trusted, so nothing may be deleted from it."""

    error_category = "gc_inventory_error"


class InventoryTruncated(InventoryError):
    """The listing was short and the shortfall was detectable.

    FATAL, NEVER TOLERATED. A truncated listing makes objects look absent and
    absence is what the anti-join acts on, so proceeding would manufacture
    candidates out of a paging failure.
    """

    error_category = "gc_inventory_truncated"


class InventoryStale(InventoryError):
    """The snapshot is older than the configured freshness bound."""

    error_category = "gc_inventory_stale"


class InventoryObject(typing.NamedTuple):
    """One object as the inventory reports it."""

    bucket: str
    key: str
    version_id: str
    size: int
    last_modified: object

    @property
    def uri(self):
        return "s3://%s/%s" % (self.bucket, self.key)


class Inventory(typing.NamedTuple):
    """A pinned, timestamped, PROVEN-COMPLETE snapshot."""

    inventory_id: str
    taken_at: object
    objects: tuple
    complete: bool

    def __len__(self):
        return len(self.objects)


def read_inventory(source, *, inventory_id, taken_at, freshness_seconds,
                   now=None, declared_buckets=(), declared_prefixes=()):
    """Read a pinned inventory, or refuse it.

    `source` is any iterable of page dicts, each
    ``{"objects": [...], "truncated": bool}``. The page shape is deliberately
    primitive so an S3 Inventory report reader, a recorded listing and a test
    double all present the same thing — and so a DOUBLE CAN REFUSE, which is
    the property that makes testing this meaningful: a stub that cannot return
    a partial page, a missing object or a stale snapshot proves nothing.

    `declared_buckets` / `declared_prefixes` filter to the declared scope at
    READ time. Scope is fixed before the reference set: an object outside the
    declared scope is never a candidate, so it never enters the pipeline at
    all rather than being filtered out later where a missed filter would be a
    deletion.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    taken = _as_datetime(taken_at)

    if freshness_seconds is None:
        raise InventoryStale(
            "no inventory freshness bound is configured, so the snapshot's "
            "age cannot be judged. There is deliberately no default that "
            "permits deletion: an unbounded staleness check is not a check.")
    age = (now - taken).total_seconds()
    if age > freshness_seconds:
        raise InventoryStale(
            "inventory %s was taken %.0fs ago, beyond the configured "
            "freshness bound of %ss. A stale snapshot describes a bucket "
            "that no longer exists, and every absence in it is a claim about "
            "the past." % (inventory_id, age, freshness_seconds))

    objects = []
    pages = 0
    for page in source:
        pages += 1
        if page.get("truncated"):
            raise InventoryTruncated(
                "inventory %s reported a truncated page at page %d. A "
                "truncated listing is FATAL rather than short: the objects "
                "it omitted would be indistinguishable from objects that do "
                "not exist, and absence is exactly what the anti-join acts "
                "on." % (inventory_id, pages))
        for row in page.get("objects", ()):
            entry = _as_object(row)
            if declared_buckets and entry.bucket not in declared_buckets:
                continue
            if declared_prefixes and not any(
                    entry.key.startswith(prefix)
                    for prefix in declared_prefixes):
                continue
            objects.append(entry)

    if pages == 0:
        raise InventoryTruncated(
            "inventory %s yielded no pages at all — not even an empty one. "
            "An empty iterable is indistinguishable from a reader that "
            "failed before it started, so it is refused rather than read as "
            "'the bucket is empty'." % (inventory_id,))

    return Inventory(inventory_id, taken, tuple(objects), True)


def _as_object(row):
    """One inventory row, refusing anything that cannot be acted on."""
    if isinstance(row, InventoryObject):
        return row
    bucket = row.get("bucket")
    key = row.get("key")
    version = row.get("version_id")
    if not bucket or not key:
        raise InventoryError(
            "an inventory row is missing its bucket or key: %r" % (row,))
    if not version:
        # REFUSED AT READ TIME, NOT AT DELETE TIME. Deletion is by exact
        # version; a row without one could only be deleted by key, which on a
        # versioned bucket installs a delete marker over whatever is current.
        raise InventoryError(
            "inventory row s3://%s/%s carries no VersionId. Deletion is by "
            "EXACT VERSION — a key-only delete on a versioning-enabled "
            "bucket installs a delete marker over whatever is current, "
            "including a version written after the plan was computed. This "
            "row cannot be acted on safely and is refused here rather than "
            "at delete time." % (bucket, key))
    return InventoryObject(
        bucket=bucket, key=key, version_id=str(version),
        size=int(row.get("size") or 0),
        last_modified=_as_datetime(row.get("last_modified")))


def _as_datetime(value):
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return (value.replace(tzinfo=datetime.timezone.utc)
                if value.tzinfo is None else value)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.datetime.fromisoformat(text)
    return (parsed.replace(tzinfo=datetime.timezone.utc)
            if parsed.tzinfo is None else parsed)
