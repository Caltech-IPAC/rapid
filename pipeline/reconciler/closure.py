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

#: A store fault, as distinct from a rejection. `read_predecessor` used to
#: collapse the two: any exception from HEAD or GET became REJECTED_UNREADABLE,
#: so an AccessDenied, a throttle or a timeout produced a reconciler-first
#: record that terminalized the attempt *while the real sequence-0 record sat
#: intact in the bucket* (review finding #16). The record is authoritative and
#: complete-snapshot, so publishing one without facts that exist is
#: unrecoverable — nothing revisits a terminal row. Transient faults now DEFER.
#: Only a record that was genuinely read and genuinely failed validation is
#: rejected.
DEFERRED_STORE_FAULT = "store_fault"


@dataclasses.dataclass(frozen=True)
class ClosureRecord:
    """A built closure record and the facts about how it was built."""

    body: dict
    sequence: int
    reconciler_first: bool
    predecessor_key: str | None = None
    rejected_reason: str | None = None

    def to_bytes(self):
        # `default=` is not optional here, and its absence was defect 8. The
        # body is built straight from database row values, so it carries
        # whatever psycopg2 hands back — and `attempt_stages.duration_ms` is
        # `numeric NOT NULL` (migration 011), which arrives as a `Decimal`
        # that bare `json.dumps` refuses. Every closure record for an attempt
        # with stages then failed to publish, the row stayed open, and the
        # next poll retried it to fail identically.
        #
        # This is the FOURTH defect of one shape (a numpy repr bound into
        # SQL, a numpy scalar in the terminal record, a caught query error
        # leaving its transaction aborted, and now this) — which is the
        # argument for the proposed boundary audit rather than a fifth fix.
        # Found live only after defect 7's SAVEPOINT let `read_attempt_stages`
        # succeed for the first time: until that read worked, `stages` was
        # always None and no Decimal ever reached this encoder.
        #
        # `termination._json_default` rather than a second local copy: it is
        # already the project's one answer to "what does `json` do with a
        # value it cannot encode", it is already imported here, and it now
        # coerces `Decimal` too. Two coercion policies drifting apart is the
        # failure mode this avoids.
        return json.dumps(self.body, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False,
                          default=termination._json_default).encode("utf-8")


@dataclasses.dataclass(frozen=True)
class Predecessor:
    """The outcome of trying to read the application's sequence-0 record.

    Exactly one of three shapes, and the caller must distinguish all three:

    * **usable** — `body` is set. `key` and `checksum` are the record's own
      identity, computed from the bytes just read.
    * **rejected** — `reason` is a REJECTED_* constant. The record was read (or
      definitively absent) and cannot be trusted; a reconciler-first record
      cites it and the attempt closes.
    * **deferred** — `reason` is `DEFERRED_STORE_FAULT`. Nothing is known: the
      store could not answer. The attempt must NOT be closed on this evidence.
    """

    body: dict | None = None
    reason: str | None = None
    key: str | None = None
    checksum: str | None = None

    @property
    def usable(self):
        return self.body is not None

    @property
    def deferred(self):
        return self.reason == DEFERRED_STORE_FAULT


def read_predecessor(store, key, attempt_id):
    """Fetch and validate the application's sequence-0 record.

    Returns a `Predecessor`. Validation is by identity and checksum, never by
    mere presence — a record that exists but describes a different attempt, or
    whose stored checksum disagrees with its bytes, is *rejected*, and the
    caller writes a reconciler-first record citing it.

    A store that cannot answer is neither of those (review finding #16): it is
    a deferral, because "the bucket refused the read" is not evidence about the
    attempt. See `DEFERRED_STORE_FAULT`.

    The returned `key` and `checksum` matter beyond validation. In the
    record-written/row-not-closed crash state the application died between
    writing sequence 0 and `mark_application_closed`, so the ROW's
    `terminal_record_key` and `terminal_record_checksum` are both NULL — and
    the body does not carry them either, since they only exist once the object
    is written. PostgreSQL requires a non-null key for `application_closed`, so
    materialization was attempting an illegal transition on every pass and the
    attempt stayed `started` forever (review finding #14). This function has
    just read and checksummed those exact bytes: it knows both, and hands them
    to the caller so materialization can supply them.
    """
    try:
        head = store.head(key)
    except Exception as exc:  # noqa: BLE001 - a store fault is NOT a rejection
        logger.warning("could not head predecessor %s: %s; deferring", key, exc)
        return Predecessor(reason=DEFERRED_STORE_FAULT, key=key)
    if head is None:
        return Predecessor(reason=REJECTED_ABSENT, key=key)

    try:
        raw = store.get(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not read predecessor %s: %s; deferring", key, exc)
        return Predecessor(reason=DEFERRED_STORE_FAULT, key=key)

    computed = body_checksum(raw)
    stored = head.get("checksum")
    if stored and stored != computed:
        logger.warning("predecessor %s failed checksum: stored %s computed %s",
                       key, stored, computed)
        return Predecessor(reason=REJECTED_CHECKSUM, key=key)

    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("predecessor %s is not readable json: %s", key, exc)
        return Predecessor(reason=REJECTED_UNREADABLE, key=key)

    if str(body.get("attempt_id")) != str(attempt_id):
        logger.warning("predecessor %s belongs to attempt %s, not %s",
                       key, body.get("attempt_id"), attempt_id)
        return Predecessor(reason=REJECTED_IDENTITY, key=key)

    # The checksum is the one just computed over the bytes, deliberately not
    # `stored`: a HEAD that omits the checksum metadata must still yield a
    # usable identity, and where both exist they have just been proven equal.
    return Predecessor(body=body, key=key, checksum=computed)


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


def read_attempt_stages(conn, attempt_id):
    """The attempt's own stage rows, for a reconciler-first record.

    A started attempt that died before writing sequence 0 still left
    `attempt_stages` rows behind — the runtime writes each one as the stage
    finishes, precisely so the boundaries survive a crash. The reconstruction
    read only the attempt row and the scheduler observation, so those rows were
    on the other side of the same transaction and never folded in: the
    published record claimed to be the complete terminal account while the
    stage detail sat in a table nobody asked (review finding #16).

    Returns a list in stage order. A query failure returns None — DISTINCT from
    an empty list, which means the attempt genuinely recorded no stages — so
    the caller can say which of the two the record reflects.
    """
    if conn is None or attempt_id is None:
        return None
    # `attempt_stages` has SIX columns — stage_record_id, attempt_id,
    # stage_name, started_at, duration_ms, outcome (migration 011). It has
    # never had `error_category`: that column lives on `attempts` and on
    # `attempt_error_categories`, and selecting it here made every
    # reconciliation of a started attempt fail. Found live by the W9 ramp,
    # whose 36 attempts were the first started-but-unclosed rows the
    # reconciler had ever been asked to reconstruct — before them the query
    # was never reached with real work, so the wrong column sat undetected.
    sql = ("SELECT stage_name, outcome, started_at, duration_ms"
           "  FROM attempt_stages WHERE attempt_id = %s"
           " ORDER BY started_at, stage_name")
    try:
        # SAVEPOINT, because the `except` below cannot undo an aborted
        # transaction. PostgreSQL puts the whole transaction into a failed
        # state on ANY statement error, so a caught-and-warned failure here
        # still left every later statement in the cycle raising
        # `InFailedSqlTransaction` — the reconciler logged one honest warning
        # and then 36 misleading ones, and closed nothing. Exactly the
        # cascade shape the numpy-repr defect had in gathering: the error is
        # handled locally and the transaction is not.
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT read_attempt_stages")
            try:
                cur.execute(sql, (attempt_id,))
                names = [description[0] for description in cur.description]
                rows = [dict(zip(names, row)) for row in cur.fetchall()]
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT read_attempt_stages")
                raise
            else:
                cur.execute("RELEASE SAVEPOINT read_attempt_stages")
    except Exception as exc:  # noqa: BLE001 - absence and failure differ
        logger.warning("could not read attempt_stages for %s: %s",
                       attempt_id, exc)
        return None
    for row in rows:
        row["started_at"] = _iso(row.get("started_at"))
    return rows


def read_log_stream(logs_client, log_group, log_stream, limit=200):
    """The tail of the attempt's CloudWatch stream, as a safety net.

    The record used to CLAIM the stream as a source while never fetching a byte
    of it, which is worse than omitting it — a consumer trusting the claim
    would believe the stage boundaries had been recovered (that half was fixed
    in FixA by demoting it to a pointer). This reads it for real, so the claim
    and the content agree.

    Deliberately the TAIL and deliberately bounded: what a reconstruction needs
    is how the attempt ended, and a record is not a log archive. Returns None
    if it cannot be read — a missing log is not a reason to fail a closure.
    """
    if logs_client is None or not log_stream or not log_group:
        return None
    try:
        response = logs_client.get_log_events(
            logGroupName=log_group, logStreamName=log_stream,
            limit=limit, startFromHead=False)
    except Exception as exc:  # noqa: BLE001 - a safety net, not a dependency
        logger.warning("could not read log stream %s/%s: %s",
                       log_group, log_stream, exc)
        return None
    return [{"timestamp": event.get("timestamp"),
             "message": event.get("message")}
            for event in response.get("events", ())]


def build_closure_record(attempt_row, observation, sequence,
                         predecessor=None, rejected_key=None,
                         rejected_reason=None, classification=None,
                         error_category=None, bundle=None,
                         binding_drift=None, stages=None, log_tail=None,
                         now=None):
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
             ("scheduler" if observation is not None else None),
             # Named only where they were actually read, which is the whole
             # rule: `stages` is None when the query failed and a list (even
             # an empty one) when it answered.
             ("attempt_stages" if stages is not None else None),
             ("log_stream" if log_tail else None))
            if source)

        # The attempt's own stage rows (review finding #16). A started attempt
        # that died before writing sequence 0 still left these behind — the
        # runtime writes each as the stage finishes, precisely so the
        # boundaries survive a crash — and the reconstruction never asked.
        if stages is not None:
            body["stages"] = stages
        if observation is not None and observation.log_stream:
            body["safety_stream"] = {
                "log_stream": observation.log_stream,
                "read": bool(log_tail),
            }
            if log_tail:
                body["safety_stream"]["events"] = log_tail
        if rejected_key:
            body["rejected_predecessor"] = {
                "key": rejected_key,
                "reason": rejected_reason,
            }
    else:
        body["reconstructed"] = False

    if binding_drift is not None:
        # The scheduler ran this attempt under a definition that disagrees
        # with its submission-time binding (#11). Recorded in the terminal
        # account rather than only logged: it says the products were made by
        # something other than what the submission says made them.
        body["execution_binding_drift"] = {
            "observed_job_definition": binding_drift,
            "recorded_job_definition_arn":
                (attempt_row or {}).get("binding_job_definition_arn"),
            "recorded_job_definition_rev":
                (attempt_row or {}).get("binding_job_definition_rev"),
        }
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
