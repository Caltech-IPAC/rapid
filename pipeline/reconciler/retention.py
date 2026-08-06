"""Bundle retention as a tag, stamped at classification time.

The bundle key is classification-neutral — it is derived from attempt identity
alone, because the key is chosen before anyone knows whether the attempt
succeeded. Retention is therefore carried by an object *tag*, and the lifecycle
rules act on tags rather than on key prefixes. Exactly one bundle per attempt,
whatever it is later called.

Two properties this module exists to guarantee:

**The retag is a canonical full-set rewrite.** S3's `PutObjectTagging` replaces
an object's entire tag set; there is no merge and no compare-and-set. So the
reconciler reconstructs the *whole* set — the producing-release tag included,
per the storage design — with the retention class updated. Anything that reads
the existing set and writes back a delta would silently drop tags it did not
know about.

**Correction is monotonic toward retention.** A superseding classification may
retag only to the *longer*-retention class. A failure reclassified later can
never inherit the success expiry, because that would shorten the life of
evidence about a failure. This is what makes replay and concurrent correction
safe in the absence of a compare-and-set: the operation is not merely
idempotent, it is order-independent.
"""

import logging

logger = logging.getLogger("rapid.reconciler.retention")

TAG_RETENTION = "retention-class"
TAG_RELEASE = "producing-release"
TAG_ATTEMPT = "attempt-id"

CLASS_SUCCESS = "success"
CLASS_FAILURE = "failure"

# Ordered by retention length, shortest first. The ordering *is* the monotonic
# rule: a class may only ever be replaced by one at a higher rank.
RETENTION_CLASSES = (CLASS_SUCCESS, CLASS_FAILURE)
_RANK = {name: rank for rank, name in enumerate(RETENTION_CLASSES)}


class RetentionError(RuntimeError):
    """A retention rule was violated — never silently corrected."""


def retention_class_for(rapid_outcome, scheduler_state=None):
    """Which retention class a classified attempt's bundle belongs to.

    Anything that is not an unambiguous success retains longer. An attempt with
    no application outcome at all — never started, or dead before classifying
    itself — is failure-class: its diagnostics are the only evidence there is.
    """
    if rapid_outcome == "success" and scheduler_state in (None, "SUCCEEDED"):
        return CLASS_SUCCESS
    return CLASS_FAILURE


def rank(retention_class):
    if retention_class not in _RANK:
        raise RetentionError(
            f"unknown retention class {retention_class!r}; "
            f"known classes are {RETENTION_CLASSES}")
    return _RANK[retention_class]


def is_monotonic(current, proposed):
    """May `current` be replaced by `proposed`?

    True when the proposal retains at least as long. Equal is allowed — replay
    of the same classification must succeed, not raise.
    """
    if current is None:
        return True
    return rank(proposed) >= rank(current)


def canonical_tag_set(attempt_row, retention_class, extra=None):
    """The complete tag set for a bundle, reconstructed from authoritative facts.

    Built from the attempt row rather than from the object's existing tags, so
    that a tag lost or corrupted by an earlier partial write is *repaired* by
    the next classification rather than carried forward.
    """
    rank(retention_class)  # reject an unknown class before writing anything

    tags = {TAG_RETENTION: retention_class}

    attempt_id = attempt_row.get("attempt_id")
    if attempt_id is not None:
        tags[TAG_ATTEMPT] = str(attempt_id)

    release = attempt_row.get("binding_release_identity")
    if release:
        tags[TAG_RELEASE] = str(release)

    if extra:
        tags.update({str(k): str(v) for k, v in extra.items()})
    return tags


def _to_tag_set(mapping):
    return [{"Key": key, "Value": value} for key, value in sorted(mapping.items())]


def _from_tag_set(tag_set):
    return {tag["Key"]: tag["Value"] for tag in tag_set or ()}


def read_retention_class(client, bucket, key):
    """The retention class currently on an object, or None if it has no tags.

    A missing object is not an error here: the caller decides whether an absent
    bundle is a fault, and this function only reports what tagging says.
    """
    try:
        response = client.get_object_tagging(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001 - absent or unreadable, same answer
        logger.debug("no readable tags on %s/%s: %s", bucket, key, exc)
        return None
    return _from_tag_set(response.get("TagSet")).get(TAG_RETENTION)


def stamp_retention(client, bucket, key, attempt_row, retention_class,
                    extra=None):
    """Write the canonical full tag set, refusing a shortening correction.

    Returns the tag set written, or None when the write was skipped because the
    object already carries a longer-retention class — the monotonic rule
    holding, which is a normal outcome under replay and not an error.
    """
    current = read_retention_class(client, bucket, key)
    if not is_monotonic(current, retention_class):
        logger.info(
            "retention for %s/%s stays %s; refusing to shorten it to %s",
            bucket, key, current, retention_class)
        return None

    tags = canonical_tag_set(attempt_row, retention_class, extra=extra)
    client.put_object_tagging(Bucket=bucket, Key=key,
                              Tagging={"TagSet": _to_tag_set(tags)})
    logger.info("stamped %s/%s retention=%s (%d tags)",
                bucket, key, retention_class, len(tags))
    return tags
