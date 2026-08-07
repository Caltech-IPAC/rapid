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

    A missing scheduler state is NOT agreement. `None` means the scheduler said
    nothing — the never-resolved path passes it when there is no observation at
    all — which is the opposite of unambiguous, and it used to read as consent
    alongside "SUCCEEDED". An attempt claiming success whose scheduler identity
    never resolved is the contradictory case the reconciler flags for a human,
    and it was filing that attempt's diagnostics under the SHORTER expiry.
    Agreement must be stated: only "SUCCEEDED" agrees.
    """
    if rapid_outcome == "success" and scheduler_state == "SUCCEEDED":
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


class TagsUnreadable(RuntimeError):
    """The object's tags could not be read, as distinct from being absent.

    The distinction is the whole point (review finding #16). `read_retention_class`
    used to convert EVERY exception — a throttle, a permission error, a
    transient network fault — into "no retention tag", and `stamp_retention`
    reads that as "nothing to protect" and writes whatever class it was given.
    So a transient failure reading an existing FAILURE tag permitted it to be
    replaced with the shorter SUCCESS expiry, silently, and the monotonic rule
    that is supposed to make correction safe was defeated by the one condition
    it most needs to survive.

    Absence still returns None. Only "I could not find out" raises.
    """


#: The S3 error codes that mean the object genuinely has no tag set, as
#: opposed to the call having failed. NoSuchTagSet is the documented answer
#: for an object with no tags; NoSuchKey is an absent object, which the caller
#: (not this function) decides how to treat.
_ABSENCE_CODES = frozenset({"NoSuchTagSet", "NoSuchKey", "404"})


def _error_code(exc):
    """The S3 error code from a botocore ClientError, if this is one."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        code = error.get("Code")
        if code is not None:
            return str(code)
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if status is not None:
            return str(status)
    return None


def read_retention_class(client, bucket, key):
    """The retention class currently on an object, or None if it has no tags.

    A missing object or a missing tag set is not an error here: the caller
    decides whether an absent bundle is a fault, and this function only
    reports what tagging says.

    **An unreadable tag set IS an error** (review finding #16). Absence and
    failure are different facts and only one of them means "nothing to
    protect" — see `TagsUnreadable`.
    """
    try:
        response = client.get_object_tagging(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001 - classified, not swallowed
        code = _error_code(exc)
        if code in _ABSENCE_CODES:
            logger.debug("no tag set on %s/%s (%s)", bucket, key, code)
            return None
        raise TagsUnreadable(
            f"could not read the retention tag on {bucket}/{key}: {exc}. "
            f"Absence and failure are different facts: treating this as "
            f"'no retention tag' would permit a longer-retention class to be "
            f"replaced with a shorter one."
        ) from exc
    return _from_tag_set(response.get("TagSet")).get(TAG_RETENTION)


def stamp_retention(client, bucket, key, attempt_row, retention_class,
                    extra=None):
    """Write the canonical full tag set, refusing a shortening correction.

    Returns the tag set written, or None when nothing was written — either
    because the object already carries a longer-retention class (the monotonic
    rule holding, a normal outcome under replay) or because there is no bundle
    to tag (an attempt that died before uploading one has nothing to stamp,
    which is a recorded fact rather than a failure).

    RAISES on anything else (review finding #16): a tag set that could not be
    READ, or a tagging call that failed. The caller defers the attempt and
    retries on the next poll rather than terminalizing a row whose bundle was
    never stamped — a terminal row is outside the open set, so an unstamped
    bundle would then expire under the wrong lifecycle rule with nothing left
    to notice.
    """
    try:
        current = read_retention_class(client, bucket, key)
    except TagsUnreadable:
        raise
    except Exception as exc:  # noqa: BLE001 - absent object, nothing to tag
        code = _error_code(exc)
        if code in _ABSENCE_CODES:
            logger.info("no bundle at %s/%s to stamp; nothing to retain",
                        bucket, key)
            return None
        raise

    if not is_monotonic(current, retention_class):
        logger.info(
            "retention for %s/%s stays %s; refusing to shorten it to %s",
            bucket, key, current, retention_class)
        return None

    tags = canonical_tag_set(attempt_row, retention_class, extra=extra)
    try:
        client.put_object_tagging(Bucket=bucket, Key=key,
                                  Tagging={"TagSet": _to_tag_set(tags)})
    except Exception as exc:  # noqa: BLE001 - classified
        if _error_code(exc) in _ABSENCE_CODES:
            logger.info("no bundle at %s/%s to stamp; nothing to retain",
                        bucket, key)
            return None
        raise
    logger.info("stamped %s/%s retention=%s (%d tags)",
                bucket, key, retention_class, len(tags))
    return tags
