"""Closure records: every classification, as a complete canonical snapshot.

The rule this module implements, in one sentence: *the highest-sequence record
alone is always the full terminal account.* Consumers never chain-fold, never
read sequence 0 and a correction and merge them. That is why every record built
here folds in its predecessor's application facts verbatim rather than citing
them.

Two forms, and the difference is what the predecessor was:

**Agreed closure** — the application wrote a sequence-0 record and it validates
by key and checksum. Its facts are folded in as authored; the reconciler adds
the scheduler's observations and the classification. This is the ordinary case,
and it is still a closure record: *every* classification gets one, agreed
included, or the record store would silently mean "nothing disagreed" for both
the agreed case and the never-checked one.

**Reconciler-first** — there is no usable predecessor. That covers three
situations the design names together because they produce the same artifact:
sequence 0 absent because the attempt never started; absent because the attempt
died before writing it; or *present but failing validation*, in which case the
rejected key and the reason it was rejected are recorded in the new record. A
reconciler-first record is built from the row, the scheduler and the log
stream, and is marked reconstructed — never presented as though the application
had authored it.
"""

import dataclasses
import json
import logging
from typing import Any

from pipeline.runtime import termination
from pipeline.runtime.boundaries import checksum as body_checksum
from pipeline.runtime.errors import RecordsError

logger = logging.getLogger("rapid.reconciler.closure")

RECORD_AUTHOR = "reconciler"

# Why a predecessor was not usable. Recorded in the record itself, so a
# reconciler-first record always says what it could not use and why.
REJECTED_ABSENT = "absent"
REJECTED_CHECKSUM = "checksum_invalid"
REJECTED_IDENTITY = "identity_mismatch"
REJECTED_UNREADABLE = "unreadable"


@dataclasses.dataclass(frozen=True)
class ClosureRecord:
    """A built closure record and the facts about how it was built."""

    body: dict
    sequence: int
    reconciler_first: bool
    predecessor_key: str | None = None
    rejected_reason: str | None = None

    def to_bytes(self):
        return json.dumps(self.body, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")


def read_predecessor(store, key, attempt_id):
    """Fetch and validate the application's sequence-0 record.

    Returns (body, None) when it is usable, or (None, reason) when it is not.
    Validation is by identity and checksum, never by mere presence — a record
    that exists but describes a different attempt, or whose stored checksum
    disagrees with its bytes, is *rejected*, and the caller writes a
    reconciler-first record citing it.
    """
    try:
        head = store.head(key)
    except Exception as exc:  # noqa: BLE001 - a store fault is a rejection
        logger.warning("could not head predecessor %s: %s", key, exc)
        return None, REJECTED_UNREADABLE
    if head is None:
        return None, REJECTED_ABSENT

    try:
        raw = store.get(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not read predecessor %s: %s", key, exc)
        return None, REJECTED_UNREADABLE

    stored = head.get("checksum")
    computed = body_checksum(raw)
    if stored and stored != computed:
        logger.warning("predecessor %s failed checksum: stored %s computed %s",
                       key, stored, computed)
        return None, REJECTED_CHECKSUM

    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("predecessor %s is not readable json: %s", key, exc)
        return None, REJECTED_UNREADABLE

    if str(body.get("attempt_id")) != str(attempt_id):
        logger.warning("predecessor %s belongs to attempt %s, not %s",
                       key, body.get("attempt_id"), attempt_id)
        return None, REJECTED_IDENTITY

    return body, None


def _scheduler_facts(observation):
    if observation is None:
        return {}
    return {
        "scheduler_state": observation.state,
        "scheduler_observed_exit": observation.exit_code,
        "scheduler_created_at": _iso(observation.created_at),
        "scheduler_started_at": _iso(observation.started_at),
        "scheduler_stopped_at": _iso(observation.stopped_at),
        "scheduler_attempt_index": observation.attempt_index,
        "scheduler_status_reason": observation.status_reason,
        "scheduler_log_stream": observation.log_stream,
    }


def _iso(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


def build_closure_record(attempt_row, observation, sequence,
                         predecessor=None, rejected_key=None,
                         rejected_reason=None, classification=None,
                         error_category=None, bundle=None, now=None):
    """Build the complete canonical snapshot for one classification.

    `attempt_row` is a mapping of the attempt's database columns — the fallback
    source for every application fact when there is no predecessor to fold in.
    `predecessor` is the validated sequence-0 body, or None for the
    reconciler-first form.
    """
    # `now` is accepted for signature stability with the rest of the package
    # and is deliberately unused: see the determinism note below.
    del now
    reconciler_first = predecessor is None

    if predecessor is not None:
        # Fold the predecessor in whole: every application-authored fact
        # carries forward verbatim. Starting from a copy rather than
        # re-deriving is deliberate — a field the application authored that
        # this module does not know about must still survive into the
        # canonical snapshot.
        body = dict(predecessor)
    else:
        body = _from_row(attempt_row)

    # NOTE the absent `reconciled_at`. A closure record must be BYTE-IDENTICAL
    # for a given classification, because the record store is create-once: a
    # replayed lease, a retried cycle, or a second reconciler must re-derive
    # exactly the same object or the conditional put fails as "already exists
    # with different content" — which is the store correctly refusing two
    # writers under one identity. Stamping wall-clock time here made every
    # replay a different object and turned idempotence into a hard error
    # (found live, 2026-08-06, on the first cycle over an existing record).
    # Nothing is lost: *when* the attempt happened is carried by the
    # scheduler's own timestamps below, which are facts about the attempt
    # rather than about when someone looked at it.
    body.update({
        "schema_version": termination.RECORD_SCHEMA_VERSION,
        "record_sequence": sequence,
        "record_author": RECORD_AUTHOR,
        "reconciler_first": reconciler_first,
        "reconciliation_classification": classification,
    })
    body.update(_scheduler_facts(observation))

    if reconciler_first:
        # The adopted marked-reconstruction rule: say what was reconstructed
        # and from what, in the record itself.
        #
        # `reconstructed_from` names the sources actually READ (review finding
        # #14). It used to list "log_stream" whenever the observation carried a
        # `logStreamName`, while no CloudWatch content was ever fetched — so
        # the record asserted evidence nobody had looked at, which is worse
        # than omitting it: a consumer trusting the claim would believe the
        # stage boundaries had been recovered. The stream's NAME is recorded
        # separately below, as a pointer for a human, which is what it is.
        body["reconstructed"] = True
        body["reconstructed_from"] = sorted(
            source for source in
            (("attempt_row" if attempt_row else None),
             ("scheduler" if observation is not None else None))
            if source)
        if observation is not None and observation.log_stream:
            # A pointer, explicitly not a source: nothing here read it.
            body["safety_stream"] = {
                "log_stream": observation.log_stream,
                "read": False,
            }
        if rejected_key:
            body["rejected_predecessor"] = {
                "key": rejected_key,
                "reason": rejected_reason,
            }
    else:
        body["reconstructed"] = False

    if error_category is not None:
        body["error_category"] = error_category
    if bundle is not None:
        body["bundle_key"] = bundle.get("key")
        body["bundle_checksum"] = bundle.get("checksum")

    return ClosureRecord(
        body=body,
        sequence=sequence,
        reconciler_first=reconciler_first,
        predecessor_key=(rejected_key if reconciler_first else None),
        rejected_reason=rejected_reason,
    )


def _from_row(attempt_row):
    """Every application fact the ROW carries, for a reconciler-first record.

    A complete canonical snapshot means complete (review finding #14): this
    used to serialize identity and the submission binding only, so a
    reconciler-first record for a STARTED attempt that died before writing its
    own record dropped the runtime provenance the row demonstrably held — the
    source sha, the container digest, the configuration digest and the
    snapshot key that names the configuration it ran against, plus whatever
    application outcome it had already recorded. Those are exactly the facts
    such a record exists to preserve, and the row is the only place left
    holding them.

    Runtime-selected provenance stays ABSENT rather than sentinel-valued where
    the row does not carry it: an attempt that never started never selected
    any, and NULL says that (the adopted absent-not-sentinel rule). The filter
    at the end is what implements it — a key whose value is None is dropped,
    so a never-started attempt's record is exactly as thin as it should be
    and a started one's is as complete as the row allows.
    """
    row = attempt_row or {}
    runtime_provenance = {
        "source_sha": row.get("source_sha"),
        "container_digest": row.get("container_digest"),
        "job_definition_rev": row.get("job_definition_rev"),
        "config_digest": row.get("config_digest"),
        "config_snapshot_key": row.get("config_snapshot_key"),
    }
    runtime_provenance = {key: value
                          for key, value in runtime_provenance.items()
                          if value is not None}

    body = {
        "attempt_id": row.get("attempt_id"),
        "run_id": row.get("run_id"),
        "logical_job_id": row.get("logical_job_id"),
        "scheduler_job_id": row.get("scheduler_job_id"),
        "application_attempt_index": row.get("application_attempt_index"),
        "exposure_id": row.get("exposure_id"),
        "sca": row.get("sca"),
        "sky_tile": row.get("sky_tile"),
        "submitted_at": _iso(row.get("submitted_at")),
        "started_at": _iso(row.get("started_at")),
        "ended_at": _iso(row.get("ended_at")),
        # The application's own account, where it got as far as writing one.
        "rapid_outcome": row.get("rapid_outcome"),
        "product_disposition": row.get("product_disposition"),
        "application_intended_exit": row.get("application_intended_exit"),
        "error_category": row.get("error_category"),
        "terminal_record_key": row.get("terminal_record_key"),
        "terminal_record_sequence": row.get("terminal_record_sequence"),
        "terminal_record_checksum": row.get("terminal_record_checksum"),
        "provenance": {
            "job_definition_arn": row.get("binding_job_definition_arn"),
            "job_definition_rev": row.get("binding_job_definition_rev"),
            "image_digest": row.get("binding_image_digest"),
            "release_identity": row.get("binding_release_identity"),
            "manifest_checksum": row.get("binding_manifest_checksum"),
        },
    }
    if runtime_provenance:
        body["runtime_provenance"] = runtime_provenance
    return {key: value for key, value in body.items() if value is not None}


MAX_SUPERSESSION_CLIMB = 8


def publish_closure_record(store, prefix, attempt_row, record):
    """Write the closure record at its sequence's key. Create-once.

    Returns the store's put result. A key that already exists holding the
    *same* content is not an error — the reconciler is idempotent under
    replay, and an identical record at the same sequence is the expected
    outcome of a retried lease.

    A key that already exists holding *different* content is the supersession
    case. Records are immutable, so the answer is never to overwrite: the new
    account is published at the next free sequence, and the highest sequence
    remains the full terminal account (every record here is a complete
    canonical snapshot, so a consumer reading only the highest still sees
    everything). The climb is bounded — a store that answers "different
    content" at every sequence is a fault to surface, not to loop on.
    """
    attempt_id = attempt_row.get("attempt_id")
    sequence = record.sequence

    for offset in range(MAX_SUPERSESSION_CLIMB):
        actual_sequence = sequence + offset
        # THE BODY IS RE-SERIALIZED AT EACH SEQUENCE (review finding #15).
        # It used to be serialized ONCE, before the loop, so a climb wrote the
        # new account at the sequence-2 key while its `record_sequence` field
        # still declared 1 — and the DB then stored the stale sequence too. A
        # consumer selecting "the highest sequence" would read a record that
        # says it is a lower one, which is exactly the ambiguity the monotonic
        # sequence exists to remove.
        published = _at_sequence(record, actual_sequence)
        key = termination.terminal_record_key(
            prefix, attempt_row.get("run_id"),
            attempt_row.get("logical_job_id"), attempt_id, actual_sequence)
        try:
            result = store.put_if_absent(key, published.to_bytes(),
                                         content_type="application/json")
        except Exception as exc:  # noqa: BLE001 - only the divergence case
            if not _is_content_divergence(exc):
                raise
            logger.info(
                "sequence %d for attempt %s already holds a different "
                "account; superseding at %d",
                actual_sequence, attempt_id, actual_sequence + 1)
            continue

        logger.info("closure record %s (%s) sequence %d for attempt %s",
                    key, "written" if result.created else "already present",
                    actual_sequence, attempt_id)
        return PublishedRecord(result=result, key=result.key or key,
                               sequence=actual_sequence,
                               record=published)

    raise RecordsError(
        f"could not publish a closure record for attempt {attempt_id}: "
        f"sequences {sequence}..{sequence + MAX_SUPERSESSION_CLIMB - 1} all "
        f"hold different accounts")


def _at_sequence(record, sequence):
    """The same record, declaring the sequence it is actually written at."""
    if sequence == record.sequence:
        return record
    body = dict(record.body)
    body["record_sequence"] = sequence
    return dataclasses.replace(record, body=body, sequence=sequence)


@dataclasses.dataclass(frozen=True)
class PublishedRecord:
    """What was actually written, and where.

    Carries the sequence the record LANDED at rather than the one it was built
    for: after a supersession climb those differ, and the caller writes the
    landed one onto the row (review finding #15 — the DB stored the stale
    sequence for the same reason the body did).
    """

    result: Any
    key: str
    sequence: int
    record: "ClosureRecord"

    @property
    def created(self):
        return getattr(self.result, "created", None)

    @property
    def checksum(self):
        return getattr(self.result, "checksum", None)

    def __getattr__(self, name):
        """Forward anything else to the store's own result.

        This wrapper exists only to add the LANDED sequence (review finding
        #15 — a supersession climb makes it differ from the one the record was
        built for). Everything a caller previously read off the store result
        must keep working, so unknown attributes pass through rather than
        making every call site learn about the wrapper.
        """
        try:
            return getattr(object.__getattribute__(self, "result"), name)
        except AttributeError:
            raise AttributeError(
                f"{type(self).__name__!s} has no attribute {name!r}, and "
                f"neither does the store result it wraps") from None


_DIVERGENCE_PHRASE = "already exists with different content"


def _is_content_divergence(exc):
    """Is this the store refusing to overwrite an object holding other content?

    Both stores raise `StorageError` for several conditions; only this one is
    recoverable by climbing a sequence.

    Two signals, because the two stores report it differently — a difference
    that first showed up as this check passing in the unit suite and doing
    nothing in production. `InMemoryObjectStore` attaches structured
    `existing_checksum`/`new_checksum` details; `S3ObjectStore` attaches only
    key and bucket and says it in the message. Checking both means neither
    store can drift out of this predicate silently.
    """
    details = getattr(exc, "details", None) or {}
    existing = details.get("existing_checksum")
    new = details.get("new_checksum")
    if existing and new and existing != new:
        return True
    return _DIVERGENCE_PHRASE in str(exc)
