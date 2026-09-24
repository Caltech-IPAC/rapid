"""Alert assembly and the Avro container, ported from `dev`'s ``alerts/produce.py``.

`origin/dev`'s ``alerts/produce.py`` (Emily Everetts, 07/26), as pure
functions over plain records instead of methods over a live provider:

- the registry check run at import (``_validate_registry``) and the
  registry-driven builders (``build_record`` and ``build_*``), unchanged;
- :func:`assemble_alert`: `dev`'s ``assemble_alert_for_source``, given the
  object, history, matches and cutouts the provider used to fetch;
- :func:`load_schema`: the packaged ``schema/<major>/<minor>/*.avsc``,
  checked against the registry first (:func:`schema_problems`, standing in
  for `dev`'s unported ``gen_schema.schema_problems``);
- :func:`serialize_alert` and :class:`BatchStats`, unchanged apart from the
  flagged-source tally below;
- :class:`AlertContainer`: `dev`'s ``open_alert_archive`` (a fastavro
  object container, codec deflate, compression level 1, fastavro's default
  block size), with a caller-given sync marker so the bytes are
  reproducible; :func:`locate_records` reads a closed container back and
  gives every record's block (see below);
- :func:`batch_produce`: `dev`'s ``batch_produce`` plus the provider's
  per-chip prefetch lookups (``_prefetch_chip``, ``get_object_for_source``,
  ``get_prv_detections``, ``get_cutouts``), over rows the stage read.

Locating a record. fastavro writes a container as a header followed by
sync-marked blocks, packing records into a block until it reaches the sync
interval (16 000 bytes, `dev`'s default, kept); a block decodes on its own
given the header. :func:`locate_records` reads the closed container with
``fastavro.block_reader`` and returns, per record in container order, its
block's byte offset and size (sync marker included) and its index within
the block, with the decoded ``diaSourceId`` so the caller can check it. An
alert with a 129x129 float32 cutout (about 67 kB) fills a block alone;
smaller alerts share one.

Reproducible bytes. Given the same records in the same order, the same
schema, codec, level and sync marker, fastavro writes the same bytes: the
header carries only the schema (key-sorted by :func:`load_schema`, since
fastavro's own key order follows the process's hash seed) and the codec,
deflate is deterministic, and the FITS cutouts carry no date. The one clock value in an alert is
``timeProcessedMjd``; :func:`batch_produce` stamps one value, given by the
caller, on every alert of the container (`dev` stamps each alert with its
own ``Time.now()``), so a rerun that reuses it reproduces the container.

Flagged sources. `dev` counts the image's ``flags <> 0`` sources and logs
them, never builds an alert (``ALERTABLE_FLAGS``). :class:`BatchStats` keeps
`dev`'s fields and adds ``n_flagged`` and ``flagged`` (one entry per flagged
sid) and ``dropped`` (every source that produced no alert, flagged or
unassociated, with a reason), so the summary names every dropped source.

Kafka. `dev`'s ``publish_alert`` is not ported: publication is off in this
build (``[publish] kafka``), and nothing here imports a Kafka client.

No database access; files only through the paths and handles callers give.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import logging
from pathlib import Path
from typing import Any, BinaryIO, Callable, Sequence

import fastavro
import fastavro.schema
import fastavro.write
from astropy.time import Time

from rapidpipe.science.alerts.crossmatch import associate_ss
from rapidpipe.science.alerts.cutouts import STAMP_HALF_WIDTH, extract_stamp
from rapidpipe.science.alerts.param_registry import (
    ALERT_PARAMS, DIA_FORCED_SOURCE_PARAMS, DIA_OBJECT_PARAMS, DIA_SOURCE_PARAMS,
    NED_MATCH_PARAMS, NOT_USED, RECORDS, REF_MATCH_PARAMS, SS_MATCH_PARAMS, VERSION,
    Param, Status, is_nullable)
from rapidpipe.science.alerts.records import (
    AssociationError, Cutouts, ForcedPhot, NedMatch, ObjectRecord, RefMatch, Source,
    SSMatch)

logger = logging.getLogger(__name__)

#: The packaged ``.avsc`` files: ``schema/<major>/<minor>/rapid.v<major>_<minor>.<record>.avsc``.
SCHEMA_ROOT = Path(__file__).resolve().parent / "schema"

#: The alert schema version this port produces (`dev`'s ``param_registry.VERSION``).
SCHEMA_VERSION = VERSION

#: `dev`'s archive defaults (``open_alert_archive``): "part of the MAST delivery contract".
DEFAULT_CODEC = "deflate"
DEFAULT_COMPRESSION_LEVEL = 1

# Which normalized record each schema record is built from. The top-level
# alert record is not listed because assemble_alert() fills it directly.
BUILDER_DATA_CLASSES = {
    "diaSource": Source,
    "diaForcedSource": ForcedPhot,
    "diaObject": ObjectRecord,
    "ssMatch": SSMatch,
    "refMatch": RefMatch,
    "nedMatch": NedMatch,
}

# Every match record must open with this envelope after its leading string
# identifier. Enforced by _validate_registry().
MATCH_ENVELOPE = ("ra", "dec", "sep", "pa")


# ---------------------------------------------------------------------------
# Registry validation -- runs at import, as in dev
# ---------------------------------------------------------------------------

def _available_attributes(data_cls: type) -> set[str]:
    field_names = {f.name for f in dataclasses.fields(data_cls)}
    property_names = {name for name, value in vars(data_cls).items()
                      if isinstance(value, property)}
    return field_names | property_names


def _validate_registry() -> None:
    """Check every IMPLEMENTED param against its builder data class (`dev`, unchanged)."""
    problems = []
    for record in RECORDS:
        if record.name == "alert":
            continue  # filled directly by assemble_alert(), checked there
        data_cls = BUILDER_DATA_CLASSES.get(record.name)
        for p in record.params:
            if p.status is not Status.IMPLEMENTED:
                continue  # stubs are inactive; nothing to check
            if data_cls is None:
                problems.append(
                    f"{record.name}.{p.name} is IMPLEMENTED, but the "
                    f"{record.name} record has no builder data class")
            elif p.getter is None and (p.attr or p.name) not in _available_attributes(data_cls):
                problems.append(
                    f"{record.name}.{p.name} reads {data_cls.__name__}."
                    f"{p.attr or p.name}, which does not exist")
        if record.name.endswith("Match"):
            names = tuple(p.name for p in record.params)
            if (len(names) < 5 or names[1:5] != MATCH_ENVELOPE
                    or record.params[0].avro != "string"):
                problems.append(
                    f"{record.name} must open with the match-record "
                    f"envelope: a string identifier, then "
                    f"{'/'.join(MATCH_ENVELOPE)} (got {names[:5]})")
    if problems:
        raise ValueError("param_registry.py is inconsistent:\n  "
                         + "\n  ".join(problems))


_validate_registry()


# ---------------------------------------------------------------------------
# Record builders (registry-driven, dev unchanged)
# ---------------------------------------------------------------------------

def build_record(param_list: Sequence[Param], data: Any) -> dict[str, Any]:
    """Build a schema-conforming dict, enforcing each param's status."""
    out = {}
    for p in param_list:
        if p.status is Status.NOT_USED:
            continue
        if p.status is not Status.IMPLEMENTED:
            out[p.name] = None  # stubs stay null even if attr/getter is staged
            continue
        try:
            if p.getter is not None:
                value = p.getter(data)
            else:
                value = getattr(data, p.attr or p.name)
        except Exception as exc:
            raise RuntimeError(
                f"getting param {p.name!r} from {type(data).__name__} "
                f"failed: {exc}") from exc
        if value is None and not is_nullable(p.avro):
            raise ValueError(
                f"param {p.name!r} is IMPLEMENTED and non-nullable but its "
                f"value is None (was the {type(data).__name__} populated by "
                f"the provider?)")
        out[p.name] = value
    return out


def build_dia_source(source: Source) -> dict[str, Any]:
    return build_record(DIA_SOURCE_PARAMS, source)


def build_dia_object(obj: ObjectRecord) -> dict[str, Any]:
    return build_record(DIA_OBJECT_PARAMS, obj)


def build_dia_forced_source(forced_phot: ForcedPhot) -> dict[str, Any]:
    return build_record(DIA_FORCED_SOURCE_PARAMS, forced_phot)


def build_ss_match(match: SSMatch) -> dict[str, Any]:
    return build_record(SS_MATCH_PARAMS, match)


def build_ref_match(match: RefMatch) -> dict[str, Any]:
    return build_record(REF_MATCH_PARAMS, match)


def build_ned_match(match: NedMatch) -> dict[str, Any]:
    return build_record(NED_MATCH_PARAMS, match)


# ---------------------------------------------------------------------------
# Alert assembly
# ---------------------------------------------------------------------------

def assemble_alert(source: Source, obj: ObjectRecord, prv: list[Source], *,
                   ss_matches: list[SSMatch] | None,
                   ref_matches: tuple[list[RefMatch], list[RefMatch]] | None,
                   ned_matches: list[NedMatch] | None,
                   cutouts: Cutouts,
                   forced: list[ForcedPhot] | None = None,
                   time_proc: float | None = None) -> dict[str, Any]:
    """`dev`'s ``assemble_alert_for_source``, with the provider's answers passed in.

    ``ss_matches`` must come from :func:`~rapidpipe.science.alerts.crossmatch.associate_ss`
    (it sets ``source.is_ss_candidate``, which the diaSource record reads).
    ``time_proc`` defaults to now, as `dev` stamps it.
    """
    source.aid = obj.aid
    if time_proc is None:
        time_proc = float(Time.now().mjd)  # pyright: ignore[reportArgumentType]
    source.time_proc = float(time_proc)

    prv_dia_sources = [build_dia_source(p) for p in prv] if prv else None

    mjds = [source.mjdobs] + [p.mjdobs for p in prv]
    obj.first_mjd = min(mjds)
    obj.last_mjd = max(mjds)
    obj.validity_mjd = source.mjdobs
    dia_object = build_dia_object(obj)

    prv_dia_forced_sources = ([build_dia_forced_source(fp) for fp in forced]
                              if forced else None)

    ref_star_matches = ref_galaxy_matches = None
    if ref_matches is not None:
        stars, galaxies = ref_matches
        ref_star_matches = [build_ref_match(m) for m in stars]
        ref_galaxy_matches = [build_ref_match(m) for m in galaxies]

    alert = {
        "schemaVersion": VERSION,
        "pipelineVersion": None,
        "diaSourceId": source.sid,
        "diaSource": build_dia_source(source),
        "prvDiaSources": prv_dia_sources,
        "diaObject": dia_object,
        "prvDiaForcedSources": prv_dia_forced_sources,
        "ssMatches": (None if ss_matches is None
                      else [build_ss_match(m) for m in ss_matches]),
        "refStarMatches": ref_star_matches,
        "refGalaxyMatches": ref_galaxy_matches,
        "nedMatches": (None if ned_matches is None
                       else [build_ned_match(m) for m in ned_matches]),
        "cutoutDifference": cutouts.difference,
        "cutoutScience": cutouts.science,
        "cutoutReference": cutouts.template,
        "observation_reason": None,
        "target_name": None,
    }

    expected = {p.name for p in ALERT_PARAMS if p.status is not NOT_USED}
    if set(alert) != expected:
        raise RuntimeError(
            "assemble_alert() and ALERT_PARAMS in param_registry.py disagree: "
            f"missing keys {sorted(expected - set(alert))}, "
            f"unexpected keys {sorted(set(alert) - expected)}")
    return alert


# ---------------------------------------------------------------------------
# Schema and serialization
# ---------------------------------------------------------------------------

def schema_paths(version: str = SCHEMA_VERSION, schema_root: str | Path = SCHEMA_ROOT) -> list[Path]:
    """The ``.avsc`` paths for a schema version, in load order (`dev`, unchanged)."""
    major, minor = version.split(".")
    namespace = f"rapid.v{major}_{minor}"
    schema_dir = Path(schema_root) / major / minor
    return [schema_dir / f"{namespace}.{record.name}.avsc" for record in RECORDS]


def schema_problems(schema_root: str | Path = SCHEMA_ROOT) -> list[str]:
    """Where the packaged ``.avsc`` files and the registry disagree; empty when they agree.

    Checks what `dev`'s generator writes from the registry: each record's
    file exists, carries this version and namespace, and lists exactly the
    registry's non-NOT_USED params in order, nullable ones defaulting to null.
    """
    problems = []
    major, minor = SCHEMA_VERSION.split(".")
    for record, path in zip(RECORDS, schema_paths(SCHEMA_VERSION, schema_root)):
        if not path.exists():
            problems.append(f"{path.name}: missing")
            continue
        schema = json.loads(path.read_text())
        if schema.get("namespace") != f"rapid.v{major}_{minor}" or schema.get("name") != record.name:
            problems.append(f"{path.name}: namespace/name {schema.get('namespace')}.{schema.get('name')}")
        if schema.get("version") != SCHEMA_VERSION:
            problems.append(f"{path.name}: version {schema.get('version')!r}")
        want = [p for p in record.params if p.status is not NOT_USED]
        got = schema.get("fields", [])
        if [f["name"] for f in got] != [p.name for p in want]:
            problems.append(f"{path.name}: field names differ from the registry")
            continue
        for p, f in zip(want, got):
            if is_nullable(p.avro) and ("default" not in f or f["default"] is not None):
                problems.append(f"{path.name}: {p.name} is nullable but has no null default")
    return problems


def load_schema(version: str = SCHEMA_VERSION, schema_root: str | Path = SCHEMA_ROOT):
    """The parsed alert schema, checked against the registry for the production version."""
    if version == SCHEMA_VERSION:
        problems = schema_problems(schema_root)
        if problems:
            raise RuntimeError("Avro schema files are out of sync with param_registry.py:\n  "
                               + "\n  ".join(problems))
    paths = [str(p) for p in schema_paths(version, schema_root)]
    return _key_sorted(fastavro.schema.load_schema_ordered(paths))


def _key_sorted(value: Any) -> Any:
    """``value`` with every dict's keys sorted; list order (field order) untouched.

    fastavro's parsed schema orders each field's keys by set iteration, which
    follows the process's string-hash seed, and a container's header embeds
    that schema as JSON text. Sorting the keys makes the header -- and so the
    container bytes -- the same in every process; the schema is unchanged.
    """
    if isinstance(value, dict):
        return {key: _key_sorted(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_key_sorted(item) for item in value]
    return value


def serialize_alert(alert_dict: dict[str, Any], schema: Any = None) -> bytes:
    """Schemaless Avro bytes for one alert, as `dev` counts and would publish them."""
    if schema is None:
        schema = load_schema()
    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, schema, alert_dict)
    return buf.getvalue()


def sync_marker_for(seed: str) -> bytes:
    """A 16-byte Avro sync marker derived from ``seed`` (the attempt id), so bytes repeat."""
    return hashlib.sha256(seed.encode()).digest()[:16]


class AlertContainer:
    """`dev`'s ``open_alert_archive``: one Avro object container, `dev`'s block packing.

    :meth:`write` appends one alert and returns its 0-based ordinal in the
    container. :meth:`flush` writes out the last block (call it before
    closing the file; `dev` flushes on exit, also after an error).
    """

    def __init__(self, fo: BinaryIO, schema: Any = None, *, codec: str = DEFAULT_CODEC,
                 compression_level: int = DEFAULT_COMPRESSION_LEVEL,
                 sync_marker: bytes | None = None) -> None:
        if schema is None:
            schema = load_schema()
        self._writer = fastavro.write.Writer(fo, schema, codec=codec,
                                             compression_level=compression_level,
                                             sync_marker=sync_marker)
        self.count = 0

    def write(self, alert: dict[str, Any]) -> int:
        self._writer.write(alert)
        self.count += 1
        return self.count - 1

    def flush(self) -> None:
        self._writer.flush()


@dataclasses.dataclass(frozen=True)
class Locator:
    """Where one record sits in a closed container."""

    record_ordinal: int      # 0-based within the container
    block_offset: int        # byte offset of its block
    block_length: int        # the block's size in bytes, sync marker included
    record_index: int        # 0-based within the block
    dia_source_id: int       # the decoded record's diaSourceId


def locate_records(fo: BinaryIO) -> list[Locator]:
    """Every record's block and position, read back with ``fastavro.block_reader``."""
    locators: list[Locator] = []
    for block in fastavro.block_reader(fo):
        for index, record in enumerate(block):
            locators.append(Locator(record_ordinal=len(locators), block_offset=int(block.offset),
                                    block_length=int(block.size), record_index=index,
                                    dia_source_id=int(record["diaSourceId"])))
    return locators


# The alert-level lists whose three states (null = could not run, empty =
# ran and found nothing, populated) BatchStats tallies, and the cutouts it
# counts as present.
MATCH_FIELDS = ("ssMatches", "refStarMatches", "refGalaxyMatches", "nedMatches")
CUTOUT_FIELDS = ("cutoutDifference", "cutoutScience", "cutoutReference")


@dataclasses.dataclass
class BatchStats:
    """What went into one image's container, plus what was dropped (`dev`, extended).

    `dev`'s fields unchanged; ``n_flagged``/``flagged`` and ``dropped`` are
    the port's (module docstring, "Flagged sources").
    """

    pid: int | None = None
    n_alerts: int = 0
    n_failed: int = 0
    n_bytes: int = 0
    n_with_prv: int = 0          # alerts carrying >= 1 prior detection
    n_prv_total: int = 0         # prior detections summed over alerts
    max_prv: int = 0
    n_with_forced: int = 0       # alerts carrying forced photometry
    match_states: dict[str, dict[str, int]] = dataclasses.field(
        default_factory=lambda: {f: {"null": 0, "empty": 0, "matched": 0}
                                 for f in MATCH_FIELDS})
    cutouts_present: dict[str, int] = dataclasses.field(
        default_factory=lambda: {f: 0 for f in CUTOUT_FIELDS})
    failures: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    n_flagged: int = 0
    flagged: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    dropped: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    def record(self, alert: dict[str, Any], n_bytes: int) -> None:
        self.n_alerts += 1
        self.n_bytes += n_bytes
        prv = alert.get("prvDiaSources")
        if prv:
            self.n_with_prv += 1
            self.n_prv_total += len(prv)
            self.max_prv = max(self.max_prv, len(prv))
        if alert.get("prvDiaForcedSources"):
            self.n_with_forced += 1
        for field in MATCH_FIELDS:
            value = alert.get(field)
            state = ("null" if value is None
                     else "empty" if len(value) == 0 else "matched")
            self.match_states[field][state] += 1
        for field in CUTOUT_FIELDS:
            if alert.get(field) is not None:
                self.cutouts_present[field] += 1

    def record_failure(self, sid: int, exc: BaseException, reason: str) -> None:
        """Note one alertable source that produced no alert (`dev`'s, plus a reason code)."""
        self.n_failed += 1
        entry = {"sid": sid, "error": type(exc).__name__, "message": str(exc)}
        self.failures.append(entry)
        self.dropped.append({**entry, "reason": reason})

    def record_flagged(self, sid: int, flags: int) -> None:
        self.n_flagged += 1
        entry = {"sid": sid, "error": "FlaggedSource",
                 "message": f"sid={sid} has PSF-fit flags={flags}: the cross-match associates "
                            "only flags = 0 sources, so no alert is produced for it"}
        self.flagged.append(entry)
        self.dropped.append({**entry, "reason": "flagged"})

    @property
    def dropped_count(self) -> int:
        return self.n_failed + self.n_flagged

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def summary_lines(self) -> list[str]:
        mean_prv = (self.n_prv_total / self.n_with_prv if self.n_with_prv else 0.0)
        lines = [
            f"pid={self.pid}: {self.n_alerts} alerts archived, "
            f"{self.n_failed} sources dropped, {self.n_flagged} flagged, {self.n_bytes} bytes",
            f"  history: {self.n_with_prv} alerts with prior detections "
            f"(mean {mean_prv:.1f}, max {self.max_prv}); "
            f"{self.n_with_forced} with forced photometry",
        ]
        for field in MATCH_FIELDS:
            s = self.match_states[field]
            lines.append(f"  {field}: {s['matched']} matched, "
                         f"{s['empty']} empty, {s['null']} null")
        lines.append("  cutouts present: " + ", ".join(
            f"{f}={n}" for f, n in self.cutouts_present.items()))
        return lines

    def log(self, level: int = logging.INFO) -> None:
        for line in self.summary_lines():
            logger.log(level, "%s", line)


# ---------------------------------------------------------------------------
# The per-image batch: dev's batch_produce over the provider's prefetch
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Associations:
    """`dev`'s per-chip prefetch (``_prefetch_chip``): objects, orphans and histories."""

    objects_by_sid: dict[int, dict[str, Any]]      # sid -> object row (lowest aid)
    orphans_by_sid: dict[int, list[int]]           # sid -> merges aids with no object row
    history: dict[tuple[str, int], list[Source]]   # (association set, aid) -> sources, oldest first


def index_associations(object_rows: Sequence[dict[str, Any]],
                       history_rows: Sequence[dict[str, Any]]) -> Associations:
    """Index the association and history rows the way `dev`'s prefetch does.

    ``object_rows``: one per (sid, merges aid, association set), ordered by
    sid then aid, with ``sid``, ``merges_aid``, ``association_set`` and the
    object's ``aid``/``ra0``/``dec0``/``stdevra``/``stdevdec``/``nsources``
    (``aid`` None for an orphan). An image may span several fields, hence
    several association sets; an object is identified by (set, aid).
    ``history_rows``: ``sources`` rows (with ``band``, ``exptime``) plus
    ``object_set`` and ``object_aid``, ordered by mjdobs.
    """
    objects_by_sid: dict[int, dict[str, Any]] = {}
    orphans_by_sid: dict[int, list[int]] = {}
    duplicated = set()
    for row in object_rows:
        sid = row["sid"]
        if row["aid"] is None:
            orphans_by_sid.setdefault(sid, []).append(row["merges_aid"])
            continue
        if sid in objects_by_sid:
            duplicated.add(sid)      # keep the first (lowest aid)
            continue
        objects_by_sid[sid] = row
    if duplicated:
        logger.warning("%d sources have more than one merges row (several aids each); "
                       "the lowest aid is used", len(duplicated))
    history: dict[tuple[str, int], list[Source]] = {}
    for row in history_rows:
        row = dict(row)
        row["aid"] = row["object_aid"]
        history.setdefault((row["object_set"], row["aid"]), []).append(
            Source.from_row(row, strict=True))
    return Associations(objects_by_sid, orphans_by_sid, history)


def association_failure(detection: Source, orphan_aids: list[int]) -> tuple[str, str]:
    """`dev`'s ``_association_failure`` text, and the port's reason code."""
    if orphan_aids:
        aids = ", ".join(str(a) for a in orphan_aids)
        return "orphan", (f"sid={detection.sid}: merges row points at aid={aids}, which has no "
                          f"astroobjects row in the association set: the object was deleted "
                          f"after cross-matching")
    return "unassociated", (f"sid={detection.sid} has no merges row in the association set: "
                            f"source cross-matching missed it")


@dataclasses.dataclass(frozen=True)
class Written:
    """One alert written to the container: what its outbox row records."""

    sid: int
    aid: int
    pid: int
    first_seen_mjd: float
    ra: float
    dec: float
    record_ordinal: int


def batch_produce(sources: Sequence[Source], associations: Associations, *,
                  container: AlertContainer, stats: BatchStats, schema: Any,
                  window_days: float,
                  difference_image: tuple[Any, Any],
                  stamp_half_width: int = STAMP_HALF_WIDTH,
                  ss_lookup: Callable[[int], Any] | None = None,
                  ref_matches_by_sid: dict[int, Any] | None = None,
                  ned_matches_by_sid: dict[int, Any] | None = None,
                  time_proc: float) -> list[Written]:
    """`dev`'s ``batch_produce`` for one image, answering the provider's calls from memory.

    ``sources`` are the image's alertable (``flags = 0``) detections in sid
    order. ``difference_image`` is ``(pixels, header)`` of the difference
    image; the science and reference stamps are None (their files are not
    input-set members in this port). ``ss_lookup`` is ``expid -> predictions
    or None`` (None: association not run). ``ref_matches_by_sid``/
    ``ned_matches_by_sid`` are None when that cross-match is off; a sid
    missing from a given dict means "not run" for that source, as in `dev`.
    ``time_proc`` is the one ``timeProcessedMjd`` of every alert (module
    docstring, "Reproducible bytes").
    """
    pixels, header = difference_image
    written: list[Written] = []

    for source in sources:
        row = associations.objects_by_sid.get(source.sid)
        if row is None:
            reason, message = association_failure(
                source, associations.orphans_by_sid.get(source.sid, []))
            exc = AssociationError(message)
            stats.record_failure(source.sid, exc, reason)
            logger.warning("pid=%s sid=%s: no alert produced (%s: %s)",
                           source.pid, source.sid, type(exc).__name__, exc)
            continue
        obj = ObjectRecord.from_row(row, strict=True)
        cutoff = source.mjdobs - window_days
        prv = [s for s in associations.history.get((row["association_set"], obj.aid), [])
               if s.sid != source.sid and s.mjdobs >= cutoff]
        ss_matches = associate_ss(source, ss_lookup(source.expid) if ss_lookup else None)
        ref_matches = (None if ref_matches_by_sid is None
                       else ref_matches_by_sid.get(source.sid))
        ned_matches = (None if ned_matches_by_sid is None
                       else ned_matches_by_sid.get(source.sid))
        cutouts = Cutouts(
            difference=extract_stamp(pixels, source.xfit + 1.0, source.yfit + 1.0,
                                     header=header, half_width=stamp_half_width),
            science=None, template=None)
        alert = assemble_alert(source, obj, prv, ss_matches=ss_matches,
                               ref_matches=ref_matches, ned_matches=ned_matches,
                               cutouts=cutouts, time_proc=time_proc)
        n_bytes = len(serialize_alert(alert, schema=schema))
        ordinal = container.write(alert)
        stats.record(alert, n_bytes)
        written.append(Written(
            sid=source.sid, aid=obj.aid, pid=source.pid,
            first_seen_mjd=float(obj.first_mjd), ra=float(source.ra), dec=float(source.dec),
            record_ordinal=ordinal))
    container.flush()
    stats.log()
    return written
