"""Delivery discovery for the processing-date loop (supervisor step 4, R1-R3, R13).

A schedule whose spec names an ``inbox`` (``s3://bucket/prefix``) finds its
deliveries there, one per ``<inbox>/<YYYY-MM-DD>/<name>/manifest.json``: the
date directory is the delivery's processing date, ``<name>`` the delivery
prefix ``admit`` reads. Keys of any other shape are ignored (counted).

:func:`discover` lists the inbox, keeps every delivery whose location is not
yet in ``loop_deliveries`` for the schedule (identity by location: a
recorded location is never read again, whatever its state), reads each
manifest in key order and classifies it against the schedule's ``batched``
deliveries plus the ones already classified in this firing:

- not a delivery manifest (the shape ``admit`` accepts: stage ``delivery``,
  one ``l2-image`` entry of format ``delivered``, string ``exposure``,
  ``detector`` and ``version`` key fields, a primary member with a
  sha256) -> ``quarantined``, ``malformed``;
- same (exposure, detector, version, sha256) as a batched delivery ->
  ``refused``, ``identical re-delivery``;
- same (exposure, detector, version), another sha256 -> ``quarantined``,
  ``checksum conflict``;
- same (exposure, detector), another version, while a version is batched
  -> ``deferred``, ``corrected delivery awaits a correction run``;
- otherwise ``batched``.

A manifest that fails to read or fails validation with ``ValueError``,
``TypeError``, ``KeyError``, ``AttributeError`` or ``IndexError`` is
``malformed`` (quarantined), not a fatal error: one bad object never blocks
the rest of the firing. Anything else (a transient storage error) propagates
so the delivery stays unrecorded and is retried next firing.

It reads and never writes; :func:`insert_deliveries` records the outcome
(the caller commits). :func:`resolve_unit_collisions` reclassifies, per
processing date and in key order, a batched delivery whose derived unit id
repeats an earlier batched delivery's as ``quarantined``, reason
:data:`COLLISION` (two delivery names can derive the same unit id), before
``rapidpipe.launch.loop`` forms the batches.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Sequence

from rapidpipe.exitcodes import ExitCode
from rapidpipe.products.manifest import Manifest
from rapidpipe.products.storage import parse_location

BATCHED, REFUSED, QUARANTINED, DEFERRED = "batched", "refused", "quarantined", "deferred"
MALFORMED = "malformed"
IDENTICAL = "identical re-delivery"
CONFLICT = "checksum conflict"
CORRECTED = "corrected delivery awaits a correction run"
COLLISION = "unit id collision"


@dataclass(frozen=True)
class Identity:
    """A delivery's l2-image identity and its primary member's checksum."""

    exposure: str
    detector: str
    version: str
    checksum: str
    instance: str | None = None


@dataclass(frozen=True)
class Delivery:
    """One discovered delivery and its classification (a ``loop_deliveries`` row)."""

    location: str
    processing_date: _dt.date
    unit: str
    identity: Identity | None
    state: str
    reason: str | None = None
    batch: int | None = None

    @property
    def label(self) -> str:
        i = self.identity
        return "-/-/v-" if i is None else f"{i.exposure}/{i.detector}/v{i.version}"


@dataclass(frozen=True)
class Discovery:
    """A firing's discovery: the new deliveries in key order, the keys ignored
    (not ``<date>/<name>/manifest.json``) and the locations already recorded."""

    deliveries: tuple[Delivery, ...]
    ignored: int
    recorded: int

    def with_state(self, state: str) -> list[Delivery]:
        return [d for d in self.deliveries if d.state == state]

    def summary(self) -> str:
        counts = ", ".join(f"{len(self.with_state(s))} {s}"
                           for s in (BATCHED, REFUSED, QUARANTINED, DEFERRED))
        return (f"{len(self.deliveries)} new deliveries ({counts}); {self.recorded} already "
                f"recorded, {self.ignored} other keys ignored")


def manifest_pattern(prefix: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(prefix)}/(\d{{4}}-\d{{2}}-\d{{2}})/([^/]+)/manifest\.json$")


def list_inbox(client: Any, inbox: str) -> tuple[list[tuple[str, _dt.date, str]], int]:
    """``([(delivery location, processing date, name)], ignored)`` under ``inbox``,
    sorted by key, through ``list_objects_v2``'s paginator (one listing: the
    firing's membership is what this call returns)."""
    loc = parse_location(inbox)
    pattern = manifest_pattern(loc.prefix)
    found: list[tuple[str, _dt.date, str]] = []
    ignored = 0
    keys: list[str] = []
    for page in client.get_paginator("list_objects_v2").paginate(
            Bucket=loc.bucket, Prefix=f"{loc.prefix}/"):
        keys.extend(obj["Key"] for obj in page.get("Contents", []) or [])
    for key in sorted(keys):
        match = pattern.fullmatch(key)
        if match is None:
            ignored += 1
            continue
        try:
            date = _dt.date.fromisoformat(match.group(1))
        except ValueError:
            ignored += 1
            continue
        name = match.group(2)
        found.append((f"s3://{loc.bucket}/{loc.prefix}/{match.group(1)}/{name}", date, name))
    return found, ignored


def identify(manifest: Manifest) -> Identity | None:
    """The delivery's identity, or ``None`` when the manifest is not a
    delivery manifest ``admit`` would accept (``admit._validate_delivery_manifest``)."""
    if manifest.stage != "delivery" or manifest.unit.kind != "detector-image":
        return None
    if len(manifest.outputs) != 1:
        return None
    entry = manifest.outputs[0]
    if entry.kind != "l2-image" or entry.format_version != "delivered":
        return None
    key = [entry.key.get(f) for f in ("exposure", "detector", "version")]
    if not all(isinstance(v, str) and v for v in key):
        return None
    primary = [m for m in entry.members if m.path == entry.primary]
    if len(primary) != 1 or not primary[0].sha256:
        return None
    return Identity(exposure=key[0], detector=key[1], version=key[2],
                    checksum=primary[0].sha256, instance=entry.instance)


def classify(identity: Identity | None, history: Sequence[Identity]) -> tuple[str, str | None]:
    """``(state, reason)`` of one delivery against the batched ``history`` (R3)."""
    if identity is None:
        return QUARANTINED, MALFORMED
    same = [h for h in history if (h.exposure, h.detector, h.version) ==
            (identity.exposure, identity.detector, identity.version)]
    if any(h.checksum == identity.checksum for h in same):
        return REFUSED, IDENTICAL
    if same:
        return QUARANTINED, CONFLICT
    if any((h.exposure, h.detector) == (identity.exposure, identity.detector)
           for h in history):
        return DEFERRED, CORRECTED
    return BATCHED, None


def _read(storage: Any, location: str) -> Manifest | None:
    """The manifest at ``location``, or ``None`` when it is not a readable manifest
    (``_Storage.read_manifest`` exits 64 on an invalid one; a fake raises
    ``ValueError``, ``TypeError``, ``KeyError``, ``AttributeError`` or
    ``IndexError``, any of which manifest validation can raise on a malformed
    object). Anything else (a transient storage error) propagates, so the
    delivery stays unrecorded and is read again by the next firing."""
    try:
        return storage.read_manifest(location)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        return None
    except Exception as exc:
        if getattr(exc, "code", None) == ExitCode.USAGE:
            return None
        raise


def discover(conn, schedule: str, inbox: str, storage: Any, client: Any,
             unit_of: Callable[[str], str]) -> Discovery:
    """List ``inbox``, read and classify every delivery ``schedule`` has not
    recorded (R3). Reads only."""
    listed, ignored = list_inbox(client, inbox)
    recorded = recorded_locations(conn, schedule)
    history = batched_identities(conn, schedule)
    deliveries: list[Delivery] = []
    already = 0
    for location, date, _name in listed:
        if location in recorded:
            already += 1
            continue
        manifest = _read(storage, location)
        identity = None if manifest is None else identify(manifest)
        state, reason = classify(identity, history)
        if state == BATCHED:
            history.append(identity)
        deliveries.append(Delivery(location=location, processing_date=date,
                                   unit=unit_of(location), identity=identity,
                                   state=state, reason=reason))
    return Discovery(deliveries=tuple(deliveries), ignored=ignored, recorded=already)


def resolve_unit_collisions(deliveries: Iterable[Delivery],
                            unit_of: Callable[[str], str]) -> list[Delivery]:
    """Walk ``deliveries`` in the given (key) order; a ``batched`` delivery
    whose derived unit id repeats an earlier ``batched`` delivery's of the
    same processing date is reclassified ``quarantined``, reason
    :data:`COLLISION` naming the earlier location, and dropped from the
    batch (two delivery names can derive the same unit id). Every other
    delivery is returned unchanged. Pure: no reads, no writes."""
    earliest: dict[tuple[_dt.date, str], str] = {}
    out: list[Delivery] = []
    for d in deliveries:
        if d.state != BATCHED:
            out.append(d)
            continue
        key = (d.processing_date, unit_of(d.location))
        earlier = earliest.get(key)
        if earlier is None:
            earliest[key] = d.location
            out.append(d)
        else:
            out.append(replace(d, state=QUARANTINED, reason=f"{COLLISION} with {earlier}"))
    return out


# ======================================================================
# loop_deliveries (migration 20260926-01)
# ======================================================================

def recorded_locations(conn, schedule: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT location FROM loop_deliveries WHERE schedule = %s", (schedule,))
        return {row[0] for row in cur.fetchall()}


def batched_identities(conn, schedule: str) -> list[Identity]:
    with conn.cursor() as cur:
        cur.execute("SELECT exposure, detector, version, checksum, delivery_instance "
                    "FROM loop_deliveries WHERE schedule = %s AND state = 'batched' "
                    "ORDER BY discovered_at, location", (schedule,))
        return [Identity(*row) for row in cur.fetchall()]


def insert_deliveries(conn, schedule: str, deliveries: Iterable[Delivery],
                      batch: int | None) -> None:
    """One ``loop_deliveries`` row per delivery (``batch`` for batched rows,
    ``NULL`` otherwise). Does not commit."""
    with conn.cursor() as cur:
        for d in deliveries:
            i = d.identity
            cur.execute(
                "INSERT INTO loop_deliveries (schedule, location, processing_date, exposure, "
                "detector, version, checksum, delivery_instance, unit, state, reason, batch) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (schedule, d.location, d.processing_date,
                 None if i is None else i.exposure, None if i is None else i.detector,
                 None if i is None else i.version, None if i is None else i.checksum,
                 None if i is None else i.instance, d.unit, d.state, d.reason,
                 batch if d.state == BATCHED else None))


def batch_locations(conn, schedule: str, processing_date: _dt.date, batch: int) -> list[str]:
    """The ``batched`` delivery locations of one batch, in location order."""
    with conn.cursor() as cur:
        cur.execute("SELECT location FROM loop_deliveries WHERE schedule = %s "
                    "AND processing_date = %s AND batch = %s AND state = 'batched' "
                    "ORDER BY location", (schedule, processing_date, batch))
        return [row[0] for row in cur.fetchall()]


@dataclass(frozen=True)
class DeliveryRow:
    processing_date: _dt.date
    state: str
    location: str
    exposure: str | None
    detector: str | None
    version: str | None
    reason: str | None
    batch: int | None


def delivery_rows(conn, schedule: str) -> list[DeliveryRow]:
    """Every ``loop_deliveries`` row of ``schedule`` (``loop show``)."""
    with conn.cursor() as cur:
        cur.execute("SELECT processing_date, state, location, exposure, detector, version, "
                    "reason, batch FROM loop_deliveries WHERE schedule = %s "
                    "ORDER BY processing_date, discovered_at, location", (schedule,))
        return [DeliveryRow(*row) for row in cur.fetchall()]
