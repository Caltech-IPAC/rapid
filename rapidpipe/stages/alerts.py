"""`alerts`: one difference image's alert packets, as `dev` produces them, into an outbox.

Ported from `origin/dev`'s ``pipeline/produceAlertsForProcDate.py`` and the
``alerts/`` package it drives (``produce.batch_produce`` over an
``AlertDataProvider``), one chip's worth: `dev` loops over a processing
date's chips in a process pool; the rebuild's unit is one detector image,
so the date query, the S3 staging and the pool are the launcher's. The
science is `dev`'s, in ``rapidpipe.science.alerts``.

Inputs. ``--inputs`` is an input-set manifest (stage ``input-set``, unit
``detector-image``). Its outputs list one ``difference-image`` entry (the
finalized instance; its ``difference`` member feeds ``cutoutDifference``)
and optionally one ``reference-catalog`` entry (its primary member, a
SExtractor catalog, feeds ``refStarMatches``/``refGalaxyMatches``); its
``inputs.result_sets`` names, by instance id, exactly one source set, one or
more association sets (an image can span fields) and any statistics sets,
each describing one of the named association sets. The stage tells them
apart by ``product_instances.kind``. It reads ``sources`` through its
parent by ``result_set``, and step 1's standalone per-field tables
``merges_<f>``, ``astroobjects_<f>`` and ``astroobjectsmeta_<f>`` by name,
the field taken from the association set's logical key, by ``result_set``. Triggers come
only from the source set. An input product without a ``product_instances``
row (a `dev` reference catalog) is read but left out of ``inputs.products``
and the dependency edges, and named in ``execution_notes``
``unregistered_inputs``. An association
set's membership is its own rows plus those of the bases it extends
(crossmatch's ``logical_key.base``, recursively), so a trigger's merges
row and its object are looked up across that chain, the newest set winning
per aid, and its history is the object's merges anywhere in the chain
joined to ``sources`` in any source set. The bases read are added to the
manifest's ``inputs.result_sets``.
``cutoutScience`` and ``cutoutReference`` are null in this port: those
files are not input-set members.

What it runs, in `dev`'s order: the image's ``flags <> 0`` sources are
counted and dropped; each ``flags = 0`` source is associated through the
association set's merges (an orphan or a missing merges row drops the
source, `dev`'s ``AssociationError``), gets its previous detections within
``[alerts] prv_window_days``, the object's statistics from the statistics
set, the reference-catalog, KONA and NED matches that are on, and a
difference cutout; the alert is serialized into one Avro object container
(schema rapid.v00_04, deflate level 1) and tallied in `dev`'s summary.

Outputs, in one transaction (``load``'s pattern): the ``alert-container``
file product (members ``container`` and ``summary``) and the ``alert-set``
result set are registered, one ``alert_outbox`` row per alert is written,
and ``diffimages.nalertpackets`` is set to 1 on the run's own row for the
difference instance. Zero alertable sources still give an empty container,
an empty complete set and the manifest. Registration comes before the
outbox rows because the rows reference both instances. Each outbox row
locates its alert: the Avro block's byte offset and size and the record's
index in it, read back from the closed container with
``fastavro.block_reader``, and its ordinal in the container.

Rerun of an attempt. As in ``load``, the transaction commits before
``run_stage`` publishes the outputs, so the rows can outlive the files. A
rerun of the same attempt finds the instances and outbox rows it
committed, regenerates the container and summary -- the same records in the
same order, the sync marker derived from the attempt id, and the
``timeProcessedMjd`` stored on its outbox rows -- checks the bytes against
the registered members and the locators against the rows, and returns the
same manifest without writing (exit 70 if they differ). Deterministic: the
records, their order, cutouts, cross-matches against the same catalog,
KONA from the same file, the Avro header and blocks, the summary JSON. Not
deterministic: NED (a live service), and ``timeProcessedMjd`` of a committed
attempt with zero alerts, which appears in no byte.

Publication: ``[publish] kafka = true`` exits 64; nothing imports Kafka.

This module may import ``rapidpipe.products``, ``rapidpipe.db``,
``rapidpipe.runs`` and ``rapidpipe.science``; never another stage,
``rapidpipe.launch`` or ``rapidpipe.cli``.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from astropy.time import Time

from rapidpipe.db import alerts as _alerts_db
from rapidpipe.db import connection as _connection_module
from rapidpipe.db.connection import ConnectionUnavailable
from rapidpipe.db.ids import new_ulid
from rapidpipe.products.diffimage import DIFFERENCERS
from rapidpipe.products.manifest import Manifest, Member, OutputEntry, member_for_file
from rapidpipe.runs.repository import register_manifest
from rapidpipe.science.alerts import assemble, crossmatch, cutouts
from rapidpipe.science.alerts.records import Source
from rapidpipe.stages.contract import (
    InputRejected,
    StageContext,
    StageDeclaration,
    StageError,
    StageResult,
    TransientFailure,
    UsageError,
    run_stage,
)

_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings" / "alerts.toml"

#: The input manifest's stage: a composed input set, not one producer's manifest.
INPUT_SET_STAGE = "input-set"

SOURCE_SET, ASSOCIATION_SET, STATISTICS_SET = "source-set", "association-set", "statistics-set"
RESULT_SET_KINDS = (SOURCE_SET, ASSOCIATION_SET, STATISTICS_SET)

KAFKA_REFUSAL = "Kafka publication is not enabled in this build"

CODECS = ("deflate", "null")

DECLARATION = StageDeclaration(
    name="alerts",
    unit="detector-image",
    argument_schema={
        "description": (
            "rapidpipe stage alerts --run <run-id> --unit <unit-id> "
            "--attempt <attempt-id> --inputs <dir> --outputs <dir> "
            "[--settings <toml>] [--dry-run]. --inputs holds an input-set "
            "manifest (stage input-set) naming one difference-image entry, "
            "optionally one reference-catalog entry, and in inputs.result_sets "
            "the source, association and statistics sets by instance id."
        ),
    },
    settings_schema_path=str(_SETTINGS_PATH),
    consumes=("difference-image", "reference-catalog", "source-set", "association-set",
              "statistics-set"),
    produces=("alert-container", "alert-set"),
    database_access="read-write",
    resource_defaults={"vcpus": 1, "memory_mib": 8192},
)


# ----------------------------------------------------------------------
# The database, replaceable in tests
# ----------------------------------------------------------------------


def connect(*args, **kwargs):
    """Module-level indirection to ``rapidpipe.db.connection.connect``, for tests."""
    return _connection_module.connect(*args, **kwargs)


class PostgresAlertsDatabase:
    """The stage's database operations on one connection, in one transaction."""

    def __init__(self, conn) -> None:
        self.conn = conn

    def _call(self, fn: Callable, *args):
        with self.conn.cursor() as cur:
            return fn(cur, *args)

    def attempt_outputs(self, attempt: str) -> dict[str, str]:
        return self._call(_alerts_db.attempt_outputs, attempt)

    def product_members(self, instance: str) -> list[dict[str, Any]]:
        return self._call(_alerts_db.product_members, instance)

    def outbox_rows(self, instance: str) -> list[dict[str, Any]]:
        return self._call(_alerts_db.outbox_rows, instance)

    def result_set_kinds(self, instances: list[str], run_id: str) -> dict[str, dict[str, Any]]:
        return self._call(_alerts_db.result_set_kinds, instances, run_id)

    def difference_pid(self, instance: str) -> int:
        return self._call(_alerts_db.difference_pid, instance)

    def flagged_sources(self, source_set: str, pid: int) -> list[tuple[int, int]]:
        return self._call(_alerts_db.flagged_sources, source_set, pid)

    def alertable_sources(self, source_set: str, pid: int) -> list[dict[str, Any]]:
        return self._call(_alerts_db.alertable_sources, source_set, pid)

    def association_chain(self, instance: str, run_id: str) -> list[str]:
        return self._call(_alerts_db.association_chain, instance, run_id)

    def associations(self, lineages: dict[str, list[str]], fields: dict[str, int],
                     statistics_by_association: dict[str, str | None],
                     sids: list[int]) -> list[dict[str, Any]]:
        return self._call(_alerts_db.associations, lineages, fields,
                          statistics_by_association, sids)

    def history(self, lineages: dict[str, list[str]], fields: dict[str, int],
                objects: list[tuple[str, int]], min_mjd: float):
        return self._call(_alerts_db.history, lineages, fields, objects, min_mjd)

    def registered_instances(self, instances: list[str]) -> set[str]:
        return self._call(_alerts_db.registered_instances, instances)

    def register_outputs(self, manifest: dict[str, Any], attempt_id: str) -> None:
        register_manifest(self.conn, manifest, registering_attempt_id=attempt_id)

    def insert_outbox_rows(self, rows: list[dict[str, Any]]) -> None:
        self._call(_alerts_db.insert_outbox_rows, rows)

    def set_nalertpackets(self, instance: str, run: str) -> int:
        return self._call(_alerts_db.set_nalertpackets, instance, run)

    def commit(self) -> None:
        self.conn.commit()


#: Names a ``module:factory`` returning a context manager that yields an
#: object with :class:`PostgresAlertsDatabase`'s methods, used instead of
#: PostgreSQL. Unset in every deployment; the stage fixture
#: (``make stage-alerts``) sets it, since a subprocess cannot be monkeypatched.
DATABASE_ENV = "RAPIDPIPE_ALERTS_DATABASE"


@contextlib.contextmanager
def _postgres() -> Iterator[PostgresAlertsDatabase]:
    with connect() as conn:
        try:
            yield PostgresAlertsDatabase(conn)
        except BaseException:
            conn.rollback()
            raise


def open_database():
    """PostgreSQL, unless ``RAPIDPIPE_ALERTS_DATABASE`` names another; tests monkeypatch this."""
    override = os.environ.get(DATABASE_ENV)
    if not override:
        return _postgres()
    module_name, _, factory_name = override.partition(":")
    try:
        factory = getattr(importlib.import_module(module_name), factory_name)
    except (ImportError, AttributeError) as exc:
        raise UsageError(f"{DATABASE_ENV}={override!r} does not name a factory: {exc}") from exc
    return factory()


# ----------------------------------------------------------------------
# Settings and inputs
# ----------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_settings(settings: dict[str, Any]) -> None:
    alerts, archive, publish = settings["alerts"], settings["archive"], settings["publish"]
    if not isinstance(publish["kafka"], bool):
        raise UsageError("[publish] kafka must be true or false")
    if publish["kafka"]:
        raise UsageError(KAFKA_REFUSAL)
    if not isinstance(publish["topic"], str):
        raise UsageError("[publish] topic must be a string")
    if alerts["diff_flavor"] not in DIFFERENCERS:
        raise UsageError(f"[alerts] diff_flavor {alerts['diff_flavor']!r} is not a registered "
                         f"differencer; known: {sorted(DIFFERENCERS)}")
    half = alerts["stamp_half_width"]
    if not isinstance(half, int) or isinstance(half, bool) or half <= 0:
        raise UsageError(f"[alerts] stamp_half_width must be a positive integer, got {half!r}")
    for key in ("ned", "refcat"):
        if not isinstance(alerts[key], bool):
            raise UsageError(f"[alerts] {key} must be true or false")
    if not isinstance(alerts["kona_file"], str):
        raise UsageError("[alerts] kona_file must be a path or empty")
    window = alerts["prv_window_days"]
    if not _is_number(window) or window < 0:
        raise UsageError(f"[alerts] prv_window_days must be a non-negative number, got {window!r}")
    if archive["codec"] not in CODECS:
        raise UsageError(f"[archive] codec must be one of {list(CODECS)}, got {archive['codec']!r}")
    level = archive["compression_level"]
    if not isinstance(level, int) or isinstance(level, bool) or not 0 <= level <= 9:
        raise UsageError(f"[archive] compression_level must be an integer 0..9, got {level!r}")


def _kona_lookup(path_text: str) -> Callable[[int], Any] | None:
    """`dev`'s ``load_kona_predictions`` (alerts/cli.py): ``expid -> predictions``, or None when off."""
    if not path_text:
        return None
    path = Path(path_text)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise UsageError(f"[alerts] kona_file {path_text!r} cannot be read: {exc}") from exc
    # JSON object keys are strings; the lookup is by integer expid
    return {int(expid): predictions for expid, predictions in data.items()}.get


def _sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_member_path(inputs_dir: Path, member: Member) -> Path:
    path = inputs_dir / member.path
    if not path.exists():
        raise InputRejected(f"input member file not found: {path}")
    if path.stat().st_size != member.bytes:
        raise InputRejected(
            f"input member {member.path!r}: manifest declares {member.bytes} bytes, "
            f"file is {path.stat().st_size} bytes")
    if _sha256_of_file(path) != member.sha256.removeprefix("sha256:"):
        raise InputRejected(f"input member {member.path!r}: SHA-256 mismatch")
    return path


def _only(manifest: Manifest, kind: str, *, required: bool) -> OutputEntry | None:
    entries = [e for e in manifest.outputs if e.kind == kind]
    if len(entries) > 1 or (required and not entries):
        raise InputRejected(
            f"input set has {len(entries)} {kind} entries, expected "
            f"{'exactly one' if required else 'at most one'}")
    return entries[0] if entries else None


def _member(entry: OutputEntry, role: str) -> Member:
    members = [m for m in entry.members if m.role == role]
    if len(members) != 1:
        raise InputRejected(
            f"{entry.kind} {entry.instance!r} has {len(members)} members with role {role!r}, "
            "expected exactly one")
    return members[0]


def _primary(entry: OutputEntry) -> Member:
    members = [m for m in entry.members if m.path == entry.primary]
    if len(members) != 1:
        raise InputRejected(f"{entry.kind} {entry.instance!r} has no primary member")
    return members[0]


@dataclass(frozen=True)
class _Sets:
    """The input set's result sets, by kind (amendment: an image can span fields)."""

    source_set: str
    association_sets: tuple[str, ...]
    statistics_by_association: dict[str, str | None]

    @property
    def statistics_sets(self) -> tuple[str, ...]:
        return tuple(s for s in self.statistics_by_association.values() if s is not None)


def _classify_result_sets(named: tuple[str, ...], found: dict[str, dict[str, Any]],
                          difference: str) -> _Sets:
    """Tell the named result sets apart by ``product_instances.kind``.

    Exactly one ``source-set`` (loaded from the input set's difference
    instance), one or more ``association-set``, and any number of
    ``statistics-set``, each describing one of the named association sets
    (some value of its logical key names that set) and at most one per
    association set. Every set must be complete.
    """
    if not named:
        raise InputRejected("the input set names no result sets")
    if len(set(named)) != len(named):
        raise InputRejected("the input set names a result set twice")
    by_kind: dict[str, list[str]] = {kind: [] for kind in RESULT_SET_KINDS}
    for instance in named:
        row = found.get(instance)
        if row is None:
            raise InputRejected(f"result set {instance!r} is not registered")
        if row["kind"] not in by_kind:
            raise InputRejected(
                f"result set {instance!r} is of kind {row['kind']!r}; the alerts stage reads "
                f"only {list(RESULT_SET_KINDS)}")
        if row["complete"] is not True:
            raise InputRejected(f"{row['kind']} {instance!r} is not a complete result set")
        by_kind[row["kind"]].append(instance)
    if len(by_kind[SOURCE_SET]) != 1:
        raise InputRejected(
            f"the input set names {len(by_kind[SOURCE_SET])} source sets, expected exactly one")
    if not by_kind[ASSOCIATION_SET]:
        raise InputRejected("the input set names no association-set")
    source_set = by_kind[SOURCE_SET][0]
    key = found[source_set]["key"] or {}
    if key.get("difference") != difference:
        raise InputRejected(
            f"source set {source_set!r} was loaded from difference instance "
            f"{key.get('difference')!r}, not the input set's {difference!r}")
    associations = tuple(by_kind[ASSOCIATION_SET])
    statistics: dict[str, str | None] = {a: None for a in associations}
    for stats in by_kind[STATISTICS_SET]:
        values = set(_key_values(found[stats]["key"]))
        described = [a for a in associations if a in values]
        if len(described) != 1:
            raise InputRejected(
                f"statistics set {stats!r} must describe exactly one of the named association "
                f"sets; its logical key {found[stats]['key']!r} names {len(described)}")
        if statistics[described[0]] is not None:
            raise InputRejected(
                f"association set {described[0]!r} is described by two named statistics sets")
        statistics[described[0]] = stats
    return _Sets(source_set=source_set, association_sets=associations,
                 statistics_by_association=statistics)


def _key_values(key: Any) -> list[str]:
    """Every string in a logical key, however nested."""
    if isinstance(key, str):
        return [key]
    if isinstance(key, dict):
        return [v for value in key.values() for v in _key_values(value)]
    if isinstance(key, (list, tuple)):
        return [v for value in key for v in _key_values(value)]
    return []


# ----------------------------------------------------------------------
# Outputs
# ----------------------------------------------------------------------


def _paths(outputs_dir: Path, difference: str) -> tuple[Path, Path]:
    """`dev`'s ``alerts_jid<N>.avro`` and ``..._summary.json``, named by the difference instance."""
    return (outputs_dir / f"alerts_{difference}.avro",
            outputs_dir / f"alerts_{difference}_summary.json")


def _key(difference: str) -> dict[str, str]:
    return {"difference": difference, "schema_version": assemble.SCHEMA_VERSION}


def _entries(*, container_instance: str, alert_set_instance: str, difference: str,
             members: tuple[Member, Member], alert_count: int, dropped_count: int,
             sets: _Sets) -> tuple[OutputEntry, OutputEntry]:
    container_member, summary_member = members
    container = OutputEntry(
        kind="alert-container", format_version="1", instance=container_instance,
        key=_key(difference), members=(container_member, summary_member),
        primary=container_member.path,
        registration={
            "alert_count": alert_count, "dropped_count": dropped_count,
            "schema_version": assemble.SCHEMA_VERSION, "difference": difference,
            "source_set": sets.source_set,
            "association_sets": list(sets.association_sets),
            "statistics_sets": list(sets.statistics_sets),
            # the page's singular shape: the first of each list, or null
            "association_set": sets.association_sets[0],
            "statistics_set": (sets.statistics_sets or (None,))[0],
        })
    alert_set = OutputEntry(
        kind="alert-set", format_version="1", instance=alert_set_instance,
        key=_key(difference), registration={"row_count": alert_count, "table": "alert_outbox"})
    return container, alert_set


_LOCATOR_COLUMNS = ("candidate", "record_ordinal", "block_offset", "block_length", "record_index")


def _outbox_rows(context: StageContext, container: str, alert_set: str,
                 written: list[assemble.Written], locators: list[assemble.Locator],
                 time_proc: float) -> list[dict[str, Any]]:
    return [{
        "id": new_ulid(), "run": context.run_id, "attempt": context.attempt_id,
        "instance": container, "result_set": alert_set, "alert_name": None,
        "candidate": w.sid, "object": w.aid, "pid": w.pid, "first_seen_mjd": w.first_seen_mjd,
        "ra": w.ra, "dec": w.dec, "record_ordinal": loc.record_ordinal,
        "block_offset": loc.block_offset, "block_length": loc.block_length,
        "record_index": loc.record_index, "time_processed_mjd": time_proc,
        "schema_version": assemble.SCHEMA_VERSION,
    } for w, loc in zip(written, locators)]


def _check_recovered(db, context: StageContext, existing: dict[str, str],
                     members: tuple[Member, Member], rows: list[dict[str, Any]],
                     existing_rows: list[dict[str, Any]]) -> None:
    """A rerun must regenerate exactly what the attempt registered; StageError otherwise."""
    if set(existing) != {"alert-container", "alert-set"}:
        raise StageError(f"attempt {context.attempt_id} registered only {sorted(existing)}; "
                         "cannot recover a partial commit")
    registered = {m["role"]: m for m in db.product_members(existing["alert-container"])}
    for member in members:
        row = registered.get(member.role)
        if row is None or (row["path"], int(row["bytes"]), row["sha256"]) != (
                member.path, member.bytes, member.sha256):
            raise StageError(
                f"cannot recover attempt {context.attempt_id}: the regenerated {member.role} "
                f"{member.path} ({member.bytes} bytes, {member.sha256}) differs from the "
                f"registered member {row}")
    want = [tuple(r[c] for c in _LOCATOR_COLUMNS) for r in rows]
    have = [tuple(r[c] for c in _LOCATOR_COLUMNS) for r in existing_rows]
    if want != have:
        raise StageError(f"cannot recover attempt {context.attempt_id}: the regenerated "
                         "container's records differ from its outbox rows")


# ----------------------------------------------------------------------
# The body
# ----------------------------------------------------------------------


def _body(context: StageContext) -> StageResult:
    settings = context.settings
    _check_settings(settings)
    log = context.logger
    alert_settings = settings["alerts"]
    manifest = context.input_manifest

    if manifest.unit.kind != "detector-image":
        raise InputRejected(
            f"input manifest unit kind is {manifest.unit.kind!r}, expected 'detector-image'")
    if manifest.stage != INPUT_SET_STAGE:
        raise InputRejected(
            f"input manifest is stage {manifest.stage!r}'s, expected {INPUT_SET_STAGE!r}")

    difference = _only(manifest, "difference-image", required=True)
    assert difference is not None
    if difference.key.get("differencer") != alert_settings["diff_flavor"]:
        raise InputRejected(
            f"the input set's difference image is differencer "
            f"{difference.key.get('differencer')!r}, [alerts] diff_flavor is "
            f"{alert_settings['diff_flavor']!r}")
    difference_path = _verified_member_path(context.inputs_dir, _member(difference, "difference"))
    reference_catalog = _only(manifest, "reference-catalog", required=False)
    products_read = {"difference-image": difference.instance}
    refcat = None
    if reference_catalog is not None and alert_settings["refcat"]:
        refcat_path = _verified_member_path(context.inputs_dir, _primary(reference_catalog))
        products_read["reference-catalog"] = reference_catalog.instance
        refcat = crossmatch.load_refcat(str(refcat_path))
    result_sets_read = tuple(manifest.inputs.result_sets)

    ss_lookup = _kona_lookup(alert_settings["kona_file"])
    ned_reader = crossmatch.AstroqueryNedReader() if alert_settings["ned"] else None

    pixels, header = cutouts.load_fits_image(difference_path)
    if pixels is None:
        raise InputRejected(f"difference member {difference_path} has no readable image")
    try:
        schema = assemble.load_schema()
    except RuntimeError as exc:
        raise StageError(str(exc)) from exc
    container_path, summary_path = _paths(context.outputs_dir, difference.instance)
    notes: dict[str, Any] = {}

    try:
        with open_database() as db:
            try:
                kinds = db.result_set_kinds(list(result_sets_read), context.run_id)
            except ValueError as exc:
                raise InputRejected(str(exc)) from exc
            sets = _classify_result_sets(result_sets_read, kinds, difference.instance)
            try:
                pid = db.difference_pid(difference.instance)
                # Each named association set with the bases it extends: a new
                # detection of a known object has its merges row in the newest
                # set and its object in the set that first made it. A set's
                # rows are in its field's standalone per-field tables.
                lineages = {a: db.association_chain(a, context.run_id) for a in sets.association_sets}
                fields = {a: _alerts_db.set_field(kinds[a]["key"]) for a in sets.association_sets}
            except ValueError as exc:
                raise InputRejected(str(exc)) from exc
            # A dev product (a dev reference catalog) has no instance row to
            # depend on: it is read, but left out of the dependency edges
            # (difference.py does the same for a dev reference).
            registered = db.registered_instances(list(products_read.values()))
            unregistered = {k: v for k, v in products_read.items() if v not in registered}
            if unregistered:
                products_read = {k: v for k, v in products_read.items() if v in registered}
                notes["unregistered_inputs"] = unregistered
                log.warning("input products without instance rows, not recorded as "
                            "dependencies: %s", unregistered)
            bases = [b for chain in lineages.values() for b in chain[1:]
                     if b not in result_sets_read]
            result_sets_read = result_sets_read + tuple(dict.fromkeys(bases))

            # A rerun of this attempt after a commit whose outputs may not
            # have been published: reuse the rows, regenerate the same bytes.
            existing = db.attempt_outputs(context.attempt_id)
            existing_rows = (db.outbox_rows(existing["alert-container"])
                             if "alert-container" in existing else [])
            time_proc = (float(existing_rows[0]["time_processed_mjd"]) if existing_rows
                         else float(Time.now().mjd))

            stats = assemble.BatchStats(pid=pid)
            for sid, flags in db.flagged_sources(sets.source_set, pid):
                stats.record_flagged(sid, flags)
            if stats.n_flagged:
                log.info("pid=%s: %d flagged detections (flags <> 0) skipped -- not associated "
                         "by the cross-match, not alertable", pid, stats.n_flagged)
            sources = [Source.from_row(row, strict=True)
                       for row in db.alertable_sources(sets.source_set, pid)]
            try:
                object_rows = db.associations(lineages, fields, sets.statistics_by_association,
                                              [s.sid for s in sources])
            except ValueError as exc:
                raise InputRejected(str(exc)) from exc
            objects = sorted({(row["association_set"], row["aid"]) for row in object_rows
                              if row["aid"] is not None})
            window = float(alert_settings["prv_window_days"])
            history_rows = (db.history(lineages, fields, objects,
                                       min(s.mjdobs for s in sources) - window)
                            if objects else [])
            associations = assemble.index_associations(object_rows, history_rows)

            ref_matches = (crossmatch.chip_ref_matches(sources, refcat)
                           if alert_settings["refcat"] else None)
            ned_matches = (crossmatch.chip_ned_matches(sources, ned_reader)
                           if ned_reader is not None else None)

            context.outputs_dir.mkdir(parents=True, exist_ok=True)
            with container_path.open("wb") as fo:
                container_writer = assemble.AlertContainer(
                    fo, schema, codec=settings["archive"]["codec"],
                    compression_level=settings["archive"]["compression_level"],
                    sync_marker=assemble.sync_marker_for(context.attempt_id))
                written = assemble.batch_produce(
                    sources, associations, container=container_writer, stats=stats,
                    schema=schema, window_days=window, difference_image=(pixels, header),
                    stamp_half_width=alert_settings["stamp_half_width"], ss_lookup=ss_lookup,
                    ref_matches_by_sid=ref_matches, ned_matches_by_sid=ned_matches,
                    time_proc=time_proc)
            summary_path.write_text(json.dumps(stats.as_dict(), indent=2, sort_keys=True) + "\n")
            with container_path.open("rb") as fo:
                locators = assemble.locate_records(fo)
            if [loc.dia_source_id for loc in locators] != [w.sid for w in written]:
                raise StageError(f"{container_path.name}: the records read back "
                                 f"{[loc.dia_source_id for loc in locators]} are not the "
                                 f"alerts written {[w.sid for w in written]}")
            members = (member_for_file("container", container_path,
                                       relative_to=context.outputs_dir),
                       member_for_file("summary", summary_path,
                                       relative_to=context.outputs_dir))

            if existing:
                container, alert_set = _entries(
                    container_instance=existing.get("alert-container", ""),
                    alert_set_instance=existing.get("alert-set", ""),
                    difference=difference.instance, members=members,
                    alert_count=len(written), dropped_count=stats.dropped_count, sets=sets)
                _check_recovered(db, context, existing, members,
                                 _outbox_rows(context, container.instance, alert_set.instance,
                                              written, locators, time_proc),
                                 existing_rows)
                log.warning("attempt %s already committed alert-container %s; regenerated its "
                            "identical bytes and reused its rows", context.attempt_id,
                            container.instance)
                return StageResult(outputs=[container, alert_set], products_read=products_read,
                                   result_sets_read=result_sets_read,
                                   execution_notes={**notes, "recovered": container.instance})

            container, alert_set = _entries(
                container_instance=new_ulid(), alert_set_instance=new_ulid(),
                difference=difference.instance, members=members, alert_count=len(written),
                dropped_count=stats.dropped_count, sets=sets)
            db.register_outputs({
                "run": context.run_id, "stage": "alerts", "attempt": context.attempt_id,
                "inputs": {"products": products_read, "result_sets": list(result_sets_read)},
                "outputs": [container.to_dict(),
                            {**alert_set.to_dict(), "row_count": len(written)}],
            }, context.attempt_id)
            db.insert_outbox_rows(_outbox_rows(context, container.instance, alert_set.instance,
                                               written, locators, time_proc))
            updated = db.set_nalertpackets(difference.instance, context.run_id)
            if updated != 1:
                log.warning("diffimages row for %s in run %s: %d rows updated; nalertpackets "
                            "is set only on this run's own row", difference.instance,
                            context.run_id, updated)
                notes["nalertpackets_rows_updated"] = updated
            db.commit()
            log.info("pid=%s: %d alerts into %s (%d blocks), %d dropped (%d flagged); "
                     "alert-container %s, alert-set %s", pid, len(written), container_path.name,
                     len({loc.block_offset for loc in locators}), stats.dropped_count,
                     stats.n_flagged, container.instance, alert_set.instance)
    except ConnectionUnavailable as exc:
        raise TransientFailure(f"could not connect to the database: {exc}") from exc
    except _connection_module.psycopg2.OperationalError as exc:
        raise TransientFailure(f"database connection lost mid-transaction: {exc}") from exc

    return StageResult(outputs=[container, alert_set], products_read=products_read,
                       result_sets_read=result_sets_read, execution_notes=notes)


def main(argv: list[str]) -> int:
    return run_stage(DECLARATION, _body, argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
