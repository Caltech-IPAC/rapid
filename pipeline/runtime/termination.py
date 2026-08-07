"""
File:    termination.py

The startup and termination protocols: the ordered sequences that make an
attempt's account complete, and crash-anywhere recoverable.

The proposal states the termination order outright: "write product
disposition; upload the diagnostics bundle (one tar.gz per attempt,
classification-neutral key derived from attempt identity, create-once); write
the immutable S3 terminal record (same keying, citing the bundle checksum);
close the attempt row's application-authored fields by compare-and-set from
its current lifecycle state; exit." And the startup half: persist the
canonical configuration snapshot as a content-addressed create-once object
*before any work begins*, then bind its digest inside the same
compare-and-set that marks the attempt started.

**The order is the design, not an implementation detail.** Each adjacency
encodes a recovery state, and this module's tests assert them one by one:

    snapshot persisted ─┬─ crash → terminal-without-start
                        │         (no work occurred, by construction)
    started CAS ────────┼─ crash → started row, fully reconstructible
                        │
    work / stages ──────┤
                        │
    bundle uploaded ────┼─ crash → started row, bundle present, no record
                        │         → reconciler writes a reconciler-first record
    terminal record ────┼─ crash → started row beside a VALID record
                        │         → reconciler materializes application-closed
    application-closed ─┴─ crash → application-closed row, record present
      CAS                        → reconciler adds scheduler facts

The one adjacency worth stating twice: the S3 terminal record is written
BEFORE the application-closed database transition, deliberately. A crash
between them leaves a started row beside a valid record, and the record is the
authoritative application account — so the reconciler materializes the
transition from it, values verbatim, marked reconciler-materialized. Reversing
the order would produce the unrecoverable case instead: a row claiming to cite
a record that was never written.

**No step is idempotent by accident.** The snapshot is content-addressed
(identical configuration dedupes to one object), the bundle and record keys
derive from immutable attempt identity, and every write is a conditional
create. Replaying the whole protocol after a crash re-derives the same keys
and either creates or validates — never overwrites.

**A failure in any step exits nonzero.** That is the fail-loud posture's
narrow exception: a caught *application* failure records its outcome and exits
0, but a failure in the records path itself has nowhere to record an outcome,
so it must not exit 0 and let Batch report SUCCEEDED over a job whose account
does not exist.
"""

import dataclasses
import datetime
import io
import json
import os
import re
import tarfile
from typing import Any

from pipeline.runtime.boundaries import checksum
from pipeline.runtime.errors import (
    RecordsError,
    StorageError,
    serialize_error,
)
from pipeline.runtime.logging_setup import get_logger
from pipeline.runtime.process import redact

_logger = get_logger("termination")

# The record schema version this module produces. Tied to the attempt record's
# own version (migration 013's amended shape) because the terminal record is
# the S3 projection of the same facts; a consumer that understands one
# understands the other.
RECORD_SCHEMA_VERSION = 2

# The application always writes sequence 0 where it ran. Only the reconciler
# writes higher — that is what makes the highest-sequence record the
# authoritative account, and what a consumer selects on.
APPLICATION_RECORD_SEQUENCE = 0

# Under the fail-loud posture, a CLASSIFIED application failure still intends
# exit 0: scheduler-SUCCEEDED with application-failure is the representable
# combination the schema was built for. Nonzero is reserved for the
# unrecordable.
EXIT_RECORDED = 0
EXIT_UNRECORDABLE = 70


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

def snapshot_key(prefix: str, digest: str) -> str:
    """The key of a canonical configuration snapshot: content-addressed.

    Keyed by the digest, not by the attempt: identical configurations dedupe
    to one object, which is the point of content-addressing here — a thousand
    array children of one submission persist one snapshot between them, and
    the attempt rows all bind the same key.
    """
    if not digest:
        raise ValueError("a configuration snapshot needs its digest")
    return f"{prefix.rstrip('/')}/config-snapshots/sha256/{digest}.json"


def bundle_key(prefix: str, run_id: str, logical_job_id: str,
               attempt_id: Any) -> str:
    """The diagnostics bundle key: classification-neutral, from identity.

    NO success/ or failure/ prefix. The retention class is a reconciler-
    stamped object TAG applied at classification time and the lifecycle rules
    act on tags (the third ratification amendment) — so a reclassification
    retags rather than stranding a bundle under a key that says the wrong
    thing. Deriving the key from attempt identity alone is also what lets the
    reconciler compute it from the row.
    """
    return (f"{prefix.rstrip('/')}/bundles/{_safe(run_id)}/"
            f"{_safe(logical_job_id)}/attempt-{_safe(attempt_id)}.tar.gz")


def terminal_record_key(prefix: str, run_id: str, logical_job_id: str,
                        attempt_id: Any,
                        sequence: int = APPLICATION_RECORD_SEQUENCE) -> str:
    """The terminal record key, carrying its monotonic per-attempt sequence.

    Zero-padded so lexical order is numeric order: a consumer listing the
    prefix gets the records in sequence order without parsing, and "the
    highest sequence" is the last key rather than a computation.
    """
    if sequence < 0:
        raise ValueError(
            f"terminal record sequence is monotonic from 0; got {sequence}")
    return (f"{prefix.rstrip('/')}/records/{_safe(run_id)}/"
            f"{_safe(logical_job_id)}/attempt-{_safe(attempt_id)}"
            f"/seq-{sequence:04d}.json")


def _safe(value: Any) -> str:
    """Reduce an identity component to safe key characters.

    Dots are kept — a run id or a job id legitimately contains them — but a
    component that is ONLY dots becomes `_`, so no segment of a derived key
    can ever be `.` or `..`. An S3 key is not a filesystem path and `..` in
    one does not traverse anywhere, but a key carrying it still confuses every
    tool that syncs a prefix to local disk, which is exactly what someone does
    when fetching diagnostics.
    """
    text = "".join(c if (c.isalnum() or c in "._-") else "_" for c in str(value))
    # Collapse any run of two or more dots. A single dot is ordinary in an
    # identifier ("v1.2", a hostname); two in a row are only ever a traversal
    # segment, and after the substitution above a component like "a/../b" has
    # already become "a_.._b" — the dots survive because dots are allowed, so
    # they are removed here rather than by the character filter.
    text = re.sub(r"\.{2,}", "_", text)
    if text.strip("._-") == "":
        return "_" * max(len(text), 1)
    return text


# ---------------------------------------------------------------------------
# Startup: the configuration snapshot and the started CAS
# ---------------------------------------------------------------------------

def canonical_snapshot_bytes(resolved_config: dict) -> tuple:
    """Serialize a resolved configuration canonically; return (bytes, digest).

    Canonical means: sorted keys, no insignificant whitespace, UTF-8. Two
    processes that resolved the same configuration must produce byte-identical
    output, or content-addressing degenerates into one object per process and
    the digest stops identifying the configuration.

    The digest covers the bytes actually persisted, so a reader can verify the
    object it fetched is the configuration the attempt claims to have used —
    "the digest alone is not reconstructible" is precisely why the snapshot is
    persisted at all: the parameter tree is mutable and its reads paginate.
    """
    body = json.dumps(resolved_config, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return body, checksum(body)


@dataclasses.dataclass(frozen=True)
class StartupResult:
    """What the startup half of the protocol established."""

    config_digest: str
    snapshot_key: str
    snapshot_created: bool
    started_at: datetime.datetime


def persist_configuration_snapshot(store: Any, prefix: str,
                                   resolved_config: dict) -> tuple:
    """Persist the resolved configuration, content-addressed and create-once.

    Returns `(digest, key, created)`. `created=False` means an identical
    snapshot was already there — the dedupe case, and success.

    Raises `RecordsError` on failure, not `StorageError`: this object is
    classified under the records/provenance retention rule, and an attempt
    that cannot persist the configuration it is about to run against cannot
    later produce complete provenance. Failing here, before any work, is the
    cheap place to fail.
    """
    body, digest = canonical_snapshot_bytes(resolved_config)
    key = snapshot_key(prefix, digest)
    try:
        result = store.put_if_absent(key, body, content_type="application/json")
    except StorageError as exc:
        raise RecordsError(
            f"could not persist the configuration snapshot at {key}: {exc}. "
            f"Provenance for this attempt could not be completed, so no work "
            f"is started.", key=key, config_digest=digest) from exc

    _logger.info("configuration snapshot %s (%s) digest %s",
                 key, "created" if result.created else "already present",
                 digest[:12])
    return digest, key, result.created


def start_attempt(writer: Any, attempt_id: Any, provenance: Any,
                  config_digest: str, snapshot_key_value: str,
                  scheduler_job_id: str | None = None,
                  application_attempt_index: int | None = None,
                  now: datetime.datetime | None = None) -> StartupResult:
    """Mark the attempt started, binding the configuration digest.

    "The attempt→snapshot binding is a single database write: the same
    compare-and-set that marks the attempt started carries the digest and
    snapshot key, so there is no bound-but-unpersisted or worked-but-unbound
    state." One call, one transition — the binding cannot be half-done.

    Called only AFTER `persist_configuration_snapshot` returns. A crash
    between the two is the terminal-without-start case: the snapshot exists
    unreferenced (harmless, content-addressed, and reused by the retry that
    resolves the same configuration), the row never left `submitted`, and no
    work occurred by construction.
    """
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    if provenance.config_digest != config_digest:
        raise RecordsError(
            f"provenance carries config digest {provenance.config_digest!r} "
            f"but the persisted snapshot is {config_digest!r}; the row would "
            f"bind a digest whose object is not the one this attempt read",
            config_digest=config_digest)

    if not snapshot_key_value:
        raise RecordsError(
            f"attempt {attempt_id} cannot be marked started without the "
            f"configuration snapshot's key: the binding is one write carrying "
            f"both the digest and the key, and a row bound to a digest whose "
            f"object cannot be named is the half-bound state this transition "
            f"exists to prevent",
            attempt_id=attempt_id, config_digest=config_digest)

    try:
        writer.mark_started(
            attempt_id, started_at=moment, provenance=provenance,
            scheduler_job_id=scheduler_job_id,
            application_attempt_index=application_attempt_index,
            config_snapshot_key=snapshot_key_value)
    except Exception as exc:  # noqa: BLE001 - translated
        raise RecordsError(
            f"could not mark attempt {attempt_id} started: {exc}",
            attempt_id=attempt_id) from exc

    _logger.info("attempt %s started, bound to config %s (%s)",
                 attempt_id, config_digest[:12], snapshot_key_value)
    return StartupResult(config_digest=config_digest,
                         snapshot_key=snapshot_key_value,
                         snapshot_created=True, started_at=moment)


# ---------------------------------------------------------------------------
# The diagnostics bundle
# ---------------------------------------------------------------------------

def build_bundle(bundle_dir: str) -> bytes:
    """Tar and gzip a bundle staging directory into bytes.

    Built in memory rather than to a file: the bundle is uploaded immediately
    and never read locally, and a temp file would be one more thing to clean
    up on a path whose whole job is to work when things are going wrong.

    Deterministic within a run — members are added in sorted order — so two
    builds of an unchanged directory produce the same archive and the
    conditional create sees a replay rather than a collision. mtimes are
    zeroed for the same reason: a rebuild after a crash must not differ from
    the first build merely because the clock moved.
    """
    if not os.path.isdir(bundle_dir):
        raise StorageError(
            f"bundle staging directory {bundle_dir!r} does not exist",
            path=bundle_dir)

    buffer = io.BytesIO()
    # mtime=0 in the gzip header, for the same determinism reason as the
    # member mtimes below.
    with tarfile.open(fileobj=buffer, mode="w:gz",
                      compresslevel=6, format=tarfile.PAX_FORMAT) as tar:
        for root, dirs, files in os.walk(bundle_dir):
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(root, name)
                arcname = os.path.relpath(full, bundle_dir)
                info = tar.gettarinfo(full, arcname=arcname)
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                with open(full, "rb") as handle:
                    tar.addfile(info, handle)
    return buffer.getvalue()


def upload_bundle(store: Any, key: str, body: bytes) -> dict:
    """Upload the bundle, create-once. Returns `{"key", "checksum", "created"}`.

    A bundle already present at this attempt's key is this attempt's own
    earlier upload, replayed after a crash. It is KEPT, not replaced, and the
    checksum returned is the stored object's — the same immutability rule the
    terminal record follows, and for the same reason: exactly one bundle
    exists per attempt, "whatever it is later called", and a record already
    written may already cite the stored bundle's checksum.

    A replayed build is not necessarily byte-identical to the first — a retry
    that got further before dying has more stage logs to stage — so comparing
    content and raising would misdiagnose the ordinary crash-and-retry case.
    What the key guarantees is identity: only this attempt derives it.

    NO retention-class prefix and no retention tag is set here: the class is
    reconciled classification, stamped by the reconciler as an object tag at
    classification time. The application does not know yet whether its own
    attempt will be classified as a success.
    """
    existing = store.head(key)
    if existing is not None:
        _logger.info(
            "diagnostics bundle %s already uploaded by an earlier run of this "
            "attempt (sha256 %s, %s bytes); kept — exactly one bundle exists "
            "per attempt", key, str(existing.get("checksum"))[:12],
            existing.get("size"))
        return {"key": key, "checksum": existing.get("checksum"),
                "created": False, "size": existing.get("size", 0)}

    try:
        result = store.put_if_absent(key, body,
                                     content_type="application/gzip")
    except StorageError as exc:
        raise RecordsError(
            f"could not upload the diagnostics bundle to {key}: {exc}",
            key=key) from exc

    _logger.info("diagnostics bundle %s (%s), %d bytes, sha256 %s",
                 key, "uploaded" if result.created else "already present",
                 result.size, result.checksum[:12])
    return {"key": key, "checksum": result.checksum,
            "created": result.created, "size": result.size}


# ---------------------------------------------------------------------------
# The terminal record
# ---------------------------------------------------------------------------

def _ppid_of(job_type: Any) -> Any:
    """The pipeline id for a job type, or None where it has none.

    Read from the route matrix, the single home W4 gave the ppid map. None
    rather than a raise for an unknown type: a record that cannot name its
    pipeline is still a valid account of what the attempt did, and the
    registrar treats the absence as the finding it is.
    """
    if not job_type:
        return None
    try:
        from submission.routes import ppid_for
        return ppid_for(job_type)
    except Exception:  # noqa: BLE001 - an unroutable type is not a record fault
        return None


def build_terminal_record(ownership: Any, job_env: Any, outcome: str,
                          product_disposition: str,
                          intended_exit: int,
                          started_at: Any, ended_at: Any,
                          config_digest: str, snapshot_key_value: str,
                          bundle: dict,
                          stages: list,
                          provenance: Any = None,
                          error: Any = None,
                          sequence: int = APPLICATION_RECORD_SEQUENCE,
                          science_provenance: dict | None = None,
                          products: dict | None = None,
                          job_type: str | None = None) -> dict:
    """Assemble the application's terminal record (sequence 0).

    A complete, self-contained account: identity, the submission-time
    execution binding's runtime-observed counterpart (provenance), the
    configuration snapshot it is bound to, the bundle it cites by checksum,
    every stage span, and the outcome taxonomy's five fields as distinct
    entries. A consumer reading this one object needs nothing else to know
    what the application did.

    `error` is a `SerializedError` or None. Its category is the record's
    `error_category`; a successful attempt has none, absent rather than
    null-valued in spirit (the key is simply not present).

    `science_provenance` and `products` are the STAGES' own accumulations —
    checksums, source counts, product facts, resolved input and reference
    identities (review finding #6). They were absent: stages recorded them
    into `StageContext` and the entrypoint passed only the runtime
    `Provenance`, so files were uploaded but sequence 0 carried no
    authoritative product list, no URIs, no checksums and no input or
    reference identities. A registration callback cannot register from a
    record that lacks them — it would have to guess from mutable external
    state, which is what the record exists to replace.
    """
    record = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "record_sequence": sequence,
        "record_author": "application",
        "run_id": ownership.run_id,
        "logical_job_id": ownership.logical_job_id,
        "attempt_id": ownership.attempt_id,
        "scheduler_job_id": ownership.scheduler_job_id,
        "application_attempt_index": ownership.attempt_index,
        # WHAT KIND OF WORK THIS WAS, and which pipeline it belongs to.
        #
        # Found porting the registrar (round 2): registration dispatches on the
        # job type — a reference-image attempt registers a reference and its
        # catalogues, a science attempt registers a difference image and its
        # measurements — and every operations-table insert takes a ppid. The
        # legacy bodies read both from the per-job `.ini` they were handed.
        # Neither was in the record, so a registrar reading records alone could
        # not have told the two job types apart, nor named the pipeline any row
        # belonged to.
        #
        # The job type comes from the MANIFEST — it is what the submitter said
        # this unit is — and the ppid from the route matrix keyed by it, which
        # is the single home migration W4 gave that number. Derived once, here,
        # at the moment the record is authored, so the value in the record is
        # the one the attempt actually ran as.
        "job_type": job_type,
        "ppid": _ppid_of(job_type),
        "queue_name": job_env.queue_name,
        "batch_id": job_env.batch_id,
        "manifest_uri": job_env.manifest_uri,
        "manifest_checksum": job_env.manifest_checksum,
        "started_at": _iso(started_at),
        "ended_at": _iso(ended_at),
        "rapid_outcome": outcome,
        "product_disposition": product_disposition,
        "application_intended_exit": intended_exit,
        "config_digest": config_digest,
        "config_snapshot_key": snapshot_key_value,
        "bundle_key": bundle["key"],
        "bundle_checksum": bundle["checksum"],
        "stages": stages,
    }
    if job_env.array_index is not None:
        record["array_index"] = job_env.array_index
    if provenance is not None:
        record["provenance"] = {
            "source_sha": provenance.source_sha,
            "container_digest": provenance.container_digest,
            "job_definition_rev": provenance.job_definition_rev,
            "config_digest": provenance.config_digest,
        }
    # The stages' own account (review finding #6). Absent rather than
    # empty-valued where a job produced none: a registration job has no
    # science products, and {} would claim it looked and found nothing where
    # the truth is that the question does not apply.
    if science_provenance:
        record["science_provenance"] = dict(science_provenance)
    if products:
        record["products"] = _product_entries(products)
    if error is not None:
        record["error"] = error.as_dict()
        record["error_category"] = error.error_category
    return record


def _product_entries(products: dict) -> list:
    """The authoritative product list: one entry per named product.

    A LIST of named entries rather than the raw mapping, because that is what
    a consumer registers from — it iterates products, and each needs its name
    beside its facts. Whatever a stage recorded (URI, checksum, size, type)
    carries through verbatim; this shape is about making the list iterable,
    not about deciding what a product is.
    """
    entries = []
    for name in sorted(products):
        value = products[name]
        if isinstance(value, dict):
            entries.append({"name": name, **value})
        else:
            entries.append({"name": name, "value": value})
    return entries


def _json_default(value: Any) -> Any:
    """Coerce what `json` cannot encode, for the terminal record only.

    The stage bodies are extracted scientific code and they compute in numpy:
    `coverage_and_uncertainty_statistics` hands `context.record` a
    `numpy.float32` for `reference_cov5percent`, and `json.dumps` raises
    `TypeError: Object of type float32 is not JSON serializable` on it. That
    exception lands in `write_terminal_record`, which is called on the failure
    path as well as the success path — so an attempt that failed for an
    unrelated reason could not write the record SAYING it failed. The
    entrypoint reported "unrecordable failure (internal_error)" and the row
    stayed non-terminal with no terminal record at all, which is precisely the
    state the attempt-record contract exists to make impossible. Found live by
    the W9 ramp, on all 18 children of its first step.

    Coercing here rather than at `context.record` is deliberate. `record()`
    takes what the science computed and should not be in the business of
    typing it, and a stage that hands over a numpy scalar is not doing
    anything wrong. This is the one place the values must become JSON, so
    this is where the conversion belongs.

    `.item()` rather than `float()`/`int()`: it is numpy's own "give me the
    equivalent Python scalar", so an integer type stays an integer and a bool
    stays a bool, where `float()` would flatten all three. NOT `default=str`
    (the shape `science_config` uses): stringifying `0.87` to `"0.87"` keeps
    the record writable while silently changing a numeric field's type under
    every consumer that reads it.
    """
    # numpy is not imported at module scope: the runtime is deliberately free
    # of the science stack, and a duck-typed check keeps it that way. Every
    # numpy scalar carries `.item()`; so do Python's own numbers, but those
    # never reach here because json encodes them natively.
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except (ValueError, TypeError):
            pass
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    raise TypeError(
        f"the terminal record carries a {type(value).__name__} at "
        f"{value!r}, which is not JSON-serializable and has no scalar "
        f"equivalent; a stage recorded a value the record cannot carry")


def write_terminal_record(store: Any, key: str, record: dict) -> dict:
    """Write the terminal record, create-once. Returns key/checksum/created.

    Written BEFORE the application-closed database transition — the two-store
    boundary the design makes explicit. A crash between them leaves a started
    row beside a valid record, which the reconciler materializes; the reverse
    order would leave a row citing a record that does not exist, which nothing
    can recover.

    **A record already at this key is this attempt's own earlier write.** The
    key derives from immutable attempt identity, so nothing else can be there.
    The protocol therefore does NOT overwrite and does not treat a difference
    as a collision: the existing record is the one that was already published,
    possibly already consumed, and the design's rule is that a terminal record
    is never mutated — "a correction appends an explicitly superseding
    record", and only the reconciler writes above sequence 0.

    This matters because a replay is not byte-identical. The re-run's
    `ended_at` is a later moment, so its serialized record differs from the
    first. Comparing content and raising would misdiagnose the ordinary
    crash-and-retry case as two writers under one identity; overwriting would
    mutate an immutable record. Validating the existing object and keeping it
    is the only reading consistent with both rules — and the returned checksum
    is the EXISTING record's, so the attempt row cites what is actually
    stored.
    """
    body = json.dumps(record, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False,
                      default=_json_default).encode("utf-8")

    existing = _validated_existing_record(store, key, record)
    if existing is not None:
        return existing

    try:
        result = store.put_if_absent(key, body,
                                     content_type="application/json")
    except StorageError as exc:
        raise RecordsError(
            f"could not write the terminal record to {key}: {exc}. The "
            f"application's account of this attempt does not exist, so this "
            f"process must not exit 0.", key=key) from exc

    _logger.info("terminal record %s (%s), sha256 %s", key,
                 "written" if result.created else "already present",
                 result.checksum[:12])
    return {"key": key, "checksum": result.checksum,
            "created": result.created}


def _validated_existing_record(store: Any, key: str, record: dict) -> Any:
    """Validate a record already at this key; return its result, or None.

    Validation, not mere presence — the design's rule for every recovery
    decision in this chain. An object at the attempt's own record key is
    accepted only if it is a well-formed record for THIS attempt: parseable
    JSON, carrying the same attempt id and record sequence. Anything else is a
    genuine identity collision and raises, because it means two different
    attempts derived one key.

    Returns None when nothing is there, which sends the caller to the
    conditional create.
    """
    head = store.head(key)
    if head is None:
        return None

    try:
        body = store.get(key)
        existing = json.loads(body.decode("utf-8"))
    except StorageError as exc:
        raise RecordsError(
            f"a terminal record exists at {key} but could not be read to "
            f"validate it: {exc}", key=key) from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise RecordsError(
            f"the object at {key} is not a readable terminal record "
            f"({exc}); this attempt's record key is occupied by something "
            f"that is not its record", key=key) from exc

    same_attempt = (existing.get("attempt_id") == record.get("attempt_id")
                    and existing.get("record_sequence")
                    == record.get("record_sequence"))
    if not same_attempt:
        raise RecordsError(
            f"the terminal record at {key} belongs to attempt "
            f"{existing.get('attempt_id')!r} sequence "
            f"{existing.get('record_sequence')!r}, not to attempt "
            f"{record.get('attempt_id')!r} sequence "
            f"{record.get('record_sequence')!r}: two attempts derived one "
            f"record key", key=key)

    _logger.info(
        "terminal record %s already written by an earlier run of this "
        "attempt (sha256 %s); it is immutable and is kept — a correction is "
        "a superseding record, which only the reconciler writes",
        key, checksum(body)[:12])
    return {"key": key, "checksum": checksum(body), "created": False}


# ---------------------------------------------------------------------------
# The protocol
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class TerminationResult:
    """What the termination protocol produced. Returned for the exit path."""

    intended_exit: int
    outcome: str
    product_disposition: str
    error_category: str | None
    bundle_key: str
    bundle_checksum: str
    record_key: str
    record_checksum: str
    record_sequence: int


def terminate(writer: Any, store: Any, ownership: Any, job_env: Any,
              workdir: Any, records_prefix: str,
              outcome: str, product_disposition: str,
              started_at: Any, config_digest: str, snapshot_key_value: str,
              stages: list, provenance: Any = None,
              error: Any = None, now: datetime.datetime | None = None,
              on_step: Any = None, record_store: Any = None,
              science_provenance: dict | None = None,
              products: dict | None = None,
              job_type: str | None = None) -> TerminationResult:
    """Run the ordered closing sequence. Raises `RecordsError` if any step fails.

    `store` receives the diagnostics bundle; `record_store` receives the
    terminal record. **They are different buckets in production** — the bundle
    expires on a reconciled retention class, the record is provenance kept at
    least product lifetime, and one bucket would force one lifecycle policy
    over both (design/storage.md; the third ratification amendment).

    `record_store` defaults to `store` so a caller with one store — every unit
    test, which uses one `InMemoryObjectStore` and asserts on keys rather than
    buckets — keeps working. The default is a convenience, not the contract:
    the first live canary wrote its terminal record into the diagnostics
    bucket because the entrypoint passed only `store`, and the key looked
    right in the log while the bucket was wrong.

    The order, and what each adjacency means on a crash, is the module
    docstring's table. `on_step(name)` is a test hook invoked BEFORE each
    step: a test raises from it to simulate death at exactly that boundary and
    then asserts the resulting state is one of the design's legal recovery
    states. Nothing in production passes it.

    The product disposition is written as part of the terminal record and the
    application-closed transition rather than as a separate earlier write:
    the design's "write product disposition" step is a fact about the products
    this attempt produced, and both stores receive it in the two writes that
    follow. Splitting it into a third write would create a state — disposition
    recorded, nothing else — that no recovery rule covers.
    """
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    records = record_store if record_store is not None else store
    serialized = None
    if error is not None:
        serialized = (error if hasattr(error, "error_category")
                      else serialize_error(error, redactor=redact))

    # Under the fail-loud posture a classified application failure still
    # intends exit 0. The unrecordable cases never reach this function — they
    # raise out of it.
    intended_exit = EXIT_RECORDED

    _step(on_step, "build_bundle")
    body = build_bundle(workdir.bundle_dir)

    _step(on_step, "upload_bundle")
    bundle = upload_bundle(
        store,
        bundle_key(records_prefix, ownership.run_id, ownership.logical_job_id,
                   ownership.attempt_id),
        body)

    _step(on_step, "write_terminal_record")
    record = build_terminal_record(
        ownership, job_env, outcome=outcome,
        product_disposition=product_disposition,
        intended_exit=intended_exit, started_at=started_at, ended_at=moment,
        config_digest=config_digest, snapshot_key_value=snapshot_key_value,
        bundle=bundle, stages=stages, provenance=provenance,
        error=serialized,
        science_provenance=science_provenance, products=products,
        job_type=job_type)
    record_key = terminal_record_key(
        records_prefix, ownership.run_id, ownership.logical_job_id,
        ownership.attempt_id, APPLICATION_RECORD_SEQUENCE)
    written = write_terminal_record(records, record_key, record)

    _step(on_step, "mark_application_closed")
    from observability.attempts import ProductDisposition, RapidOutcome

    try:
        writer.mark_application_closed(
            ownership.attempt_id,
            ended_at=moment,
            application_intended_exit=intended_exit,
            rapid_outcome=RapidOutcome(outcome),
            product_disposition=ProductDisposition(product_disposition),
            terminal_record_key=written["key"],
            terminal_record_sequence=APPLICATION_RECORD_SEQUENCE,
            terminal_record_checksum=written["checksum"],
            error_category=(serialized.error_category if serialized else None),
        )
    except Exception as exc:  # noqa: BLE001 - translated
        # The record is already durable and valid; only the row transition
        # failed. The reconciler will materialize it from the record — which
        # is exactly the recovery this ordering was chosen for — but this
        # process still exits nonzero, because it cannot itself confirm the
        # account is complete.
        raise RecordsError(
            f"the terminal record for attempt {ownership.attempt_id} is "
            f"written and valid at {written['key']}, but the "
            f"application-closed transition failed: {exc}. The reconciler "
            f"materializes the transition from the record; this process exits "
            f"nonzero because it could not confirm closure itself.",
            attempt_id=ownership.attempt_id,
            terminal_record_key=written["key"]) from exc

    _logger.info("attempt %s closed: outcome=%s disposition=%s intended_exit=%s",
                 ownership.attempt_id, outcome, product_disposition,
                 intended_exit)

    return TerminationResult(
        intended_exit=intended_exit,
        outcome=outcome,
        product_disposition=product_disposition,
        error_category=(serialized.error_category if serialized else None),
        bundle_key=bundle["key"],
        bundle_checksum=bundle["checksum"],
        record_key=written["key"],
        record_checksum=written["checksum"],
        record_sequence=APPLICATION_RECORD_SEQUENCE,
    )


def _step(on_step: Any, name: str) -> None:
    if on_step is not None:
        on_step(name)


def _iso(value: Any) -> Any:
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    return value
