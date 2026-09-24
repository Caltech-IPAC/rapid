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
``inputs.result_sets`` names the source set, association set and
statistics set by instance id. The stage tells them apart by
``product_instances.kind`` and reads the parent tables ``sources``,
``merges``, ``astroobjects`` and ``astroobjectsmeta`` by ``result_set``.
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
outbox rows because the rows reference both instances. A rerun of the same
attempt after an uncertain commit finds the instances this attempt already
registered and reuses them, writing nothing.

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
from pathlib import Path
from typing import Any, Callable, Iterator

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

    def result_set_kinds(self, instances: list[str]) -> dict[str, dict[str, Any]]:
        return self._call(_alerts_db.result_set_kinds, instances)

    def difference_pid(self, instance: str) -> int:
        return self._call(_alerts_db.difference_pid, instance)

    def flagged_sources(self, source_set: str, pid: int) -> list[tuple[int, int]]:
        return self._call(_alerts_db.flagged_sources, source_set, pid)

    def alertable_sources(self, source_set: str, pid: int) -> list[dict[str, Any]]:
        return self._call(_alerts_db.alertable_sources, source_set, pid)

    def associations(self, association_set: str, statistics_set: str | None,
                     sids: list[int]) -> list[dict[str, Any]]:
        return self._call(_alerts_db.associations, association_set, statistics_set, sids)

    def history(self, association_set: str, aids: list[int], min_mjd: float):
        return self._call(_alerts_db.history, association_set, aids, min_mjd)

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


def _classify_result_sets(named: tuple[str, ...], found: dict[str, dict[str, Any]],
                          difference: str) -> dict[str, str | None]:
    """``{source-set, association-set, statistics-set: instance}`` from ``product_instances.kind``."""
    if not named:
        raise InputRejected("the input set names no result sets")
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
    for kind, instances in by_kind.items():
        if len(instances) > 1:
            raise InputRejected(f"the input set names {len(instances)} {kind} result sets")
    for kind in (SOURCE_SET, ASSOCIATION_SET):
        if not by_kind[kind]:
            raise InputRejected(f"the input set names no {kind}")
    source_set = by_kind[SOURCE_SET][0]
    key = found[source_set]["key"] or {}
    if key.get("difference") != difference:
        raise InputRejected(
            f"source set {source_set!r} was loaded from difference instance "
            f"{key.get('difference')!r}, not the input set's {difference!r}")
    return {kind: (instances[0] if instances else None) for kind, instances in by_kind.items()}


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
             sets: dict[str, str | None]) -> tuple[OutputEntry, OutputEntry]:
    container_member, summary_member = members
    container = OutputEntry(
        kind="alert-container", format_version="1", instance=container_instance,
        key=_key(difference), members=(container_member, summary_member),
        primary=container_member.path,
        registration={
            "alert_count": alert_count, "dropped_count": dropped_count,
            "schema_version": assemble.SCHEMA_VERSION, "difference": difference,
            "source_set": sets[SOURCE_SET], "association_set": sets[ASSOCIATION_SET],
            "statistics_set": sets[STATISTICS_SET],
        })
    alert_set = OutputEntry(
        kind="alert-set", format_version="1", instance=alert_set_instance,
        key=_key(difference), registration={"row_count": alert_count, "table": "alert_outbox"})
    return container, alert_set


def _recover(db, context: StageContext, existing: dict[str, str], difference: str,
             sets: dict[str, str | None]) -> tuple[OutputEntry, OutputEntry]:
    """Rebuild the entries of an attempt that already committed, from its rows and files."""
    if set(existing) != {"alert-container", "alert-set"}:
        raise StageError(f"attempt {context.attempt_id} registered only {sorted(existing)}; "
                         "cannot recover a partial commit")
    container_instance, alert_set_instance = existing["alert-container"], existing["alert-set"]
    by_role = {m["role"]: m for m in db.product_members(container_instance)}
    members = []
    for role in ("container", "summary"):
        row = by_role.get(role)
        if row is None:
            raise StageError(f"alert-container {container_instance} has no {role!r} member row")
        member = Member(role=role, path=row["path"], bytes=int(row["bytes"]), sha256=row["sha256"])
        path = context.outputs_dir / member.path
        if (not path.exists() or path.stat().st_size != member.bytes
                or f"sha256:{_sha256_of_file(path)}" != member.sha256):
            raise StageError(f"cannot recover attempt {context.attempt_id}: {path} is missing "
                             "or differs from the registered member")
        members.append(member)
    summary = json.loads((context.outputs_dir / members[1].path).read_text())
    alert_count = len(db.outbox_rows(container_instance))
    if alert_count != summary["n_alerts"]:
        raise StageError(f"alert-container {container_instance}: {alert_count} outbox rows, "
                         f"summary says {summary['n_alerts']}")
    return _entries(container_instance=container_instance,
                    alert_set_instance=alert_set_instance, difference=difference,
                    members=(members[0], members[1]), alert_count=alert_count,
                    dropped_count=int(summary["n_failed"]) + int(summary["n_flagged"]),
                    sets=sets)


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
            sets = _classify_result_sets(
                result_sets_read, db.result_set_kinds(list(result_sets_read)),
                difference.instance)

            existing = db.attempt_outputs(context.attempt_id)
            if existing:
                container, alert_set = _recover(db, context, existing, difference.instance, sets)
                log.warning("attempt %s already registered alert-container %s; reusing it",
                            context.attempt_id, container.instance)
                return StageResult(outputs=[container, alert_set], products_read=products_read,
                                   result_sets_read=result_sets_read,
                                   execution_notes={"recovered": container.instance})

            try:
                pid = db.difference_pid(difference.instance)
            except ValueError as exc:
                raise InputRejected(str(exc)) from exc

            stats = assemble.BatchStats(pid=pid)
            for sid, flags in db.flagged_sources(sets[SOURCE_SET], pid):
                stats.record_flagged(sid, flags)
            if stats.n_flagged:
                log.info("pid=%s: %d flagged detections (flags <> 0) skipped -- not associated "
                         "by the cross-match, not alertable", pid, stats.n_flagged)
            sources = [Source.from_row(row, strict=True)
                       for row in db.alertable_sources(sets[SOURCE_SET], pid)]
            object_rows = db.associations(sets[ASSOCIATION_SET], sets[STATISTICS_SET],
                                          [s.sid for s in sources])
            aids = sorted({row["aid"] for row in object_rows if row["aid"] is not None})
            window = float(alert_settings["prv_window_days"])
            history_rows = (db.history(sets[ASSOCIATION_SET], aids,
                                       min(s.mjdobs for s in sources) - window)
                            if aids else [])
            associations = assemble.index_associations(object_rows, history_rows)

            ref_matches = (crossmatch.chip_ref_matches(sources, refcat)
                           if alert_settings["refcat"] else None)
            ned_matches = (crossmatch.chip_ned_matches(sources, ned_reader)
                           if ned_reader is not None else None)

            context.outputs_dir.mkdir(parents=True, exist_ok=True)
            with container_path.open("wb") as fo:
                container_writer = assemble.AlertContainer(
                    fo, schema, codec=settings["archive"]["codec"],
                    compression_level=settings["archive"]["compression_level"])
                written = assemble.batch_produce(
                    sources, associations, container=container_writer, stats=stats,
                    schema=schema, window_days=window, difference_image=(pixels, header),
                    stamp_half_width=alert_settings["stamp_half_width"], ss_lookup=ss_lookup,
                    ref_matches_by_sid=ref_matches, ned_matches_by_sid=ned_matches)
            summary_path.write_text(json.dumps(stats.as_dict(), indent=2, sort_keys=True) + "\n")

            container, alert_set = _entries(
                container_instance=new_ulid(), alert_set_instance=new_ulid(),
                difference=difference.instance,
                members=(member_for_file("container", container_path,
                                         relative_to=context.outputs_dir),
                         member_for_file("summary", summary_path,
                                         relative_to=context.outputs_dir)),
                alert_count=len(written), dropped_count=stats.dropped_count, sets=sets)

            db.register_outputs({
                "run": context.run_id, "stage": "alerts", "attempt": context.attempt_id,
                "inputs": {"products": products_read, "result_sets": list(result_sets_read)},
                "outputs": [container.to_dict(),
                            {**alert_set.to_dict(), "row_count": len(written)}],
            }, context.attempt_id)
            db.insert_outbox_rows([{
                "id": new_ulid(), "run": context.run_id, "attempt": context.attempt_id,
                "instance": container.instance, "result_set": alert_set.instance,
                "alert_name": None, "candidate": w.sid, "object": w.aid, "pid": w.pid,
                "first_seen_mjd": w.first_seen_mjd, "ra": w.ra, "dec": w.dec,
                "record_index": w.record_index, "block_offset": w.block_offset,
                "block_length": w.block_length, "schema_version": assemble.SCHEMA_VERSION,
            } for w in written])
            updated = db.set_nalertpackets(difference.instance, context.run_id)
            if updated != 1:
                log.warning("diffimages row for %s in run %s: %d rows updated; nalertpackets "
                            "is set only on this run's own row", difference.instance,
                            context.run_id, updated)
                notes["nalertpackets_rows_updated"] = updated
            db.commit()
            log.info("pid=%s: %d alerts into %s, %d dropped (%d flagged); alert-container %s, "
                     "alert-set %s", pid, len(written), container_path.name,
                     stats.dropped_count, stats.n_flagged, container.instance, alert_set.instance)
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
