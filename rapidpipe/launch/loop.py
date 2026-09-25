"""The processing-date loop: ``rapidpipe loop run|plan|show`` (supervisor step 7).

One scheduled invocation walks a dates spec, date by date, through one
production run each, with the machinery ``rapidpipe run start`` already
uses. The rulings this module implements, one line each:

- R3: ``loop run`` processes, in spec order and serially, every date whose
  ``loop_dates`` row is absent or ``open``; exit 0 when every processed date
  is complete, 1 on the first failed date (later dates not started), 75 on a
  timeout; an ``open`` row's run is resumed through ``run start``'s walk
  (complete units skipped, running attempts attached, ready units
  re-attempted).
- R4: one production run per date, created by ``run create --release``'s
  code path, walking admit -> register -> difference -> register -> finalize
  -> register -> load per detector image, maintain per ``<yyyymmdd>/SCA<nn>``,
  crossmatch -> statistics -> prune per field, then alerts per detector
  image, every attempt through ``submit_unit`` + ``reconcile``.
- R5: a field's base catalog is the association set crossmatch produced for
  that field in the most recent earlier ``complete`` row of the same
  schedule (its run's selected attempt); none on the first date. Input-set
  manifests live under ``<scratch root>/runs/<run>/inputs/<stage>/<unit>/``.
- R6: once every unit is complete the run is promoted (``promote_run``,
  ``who="scheduler"``, under the spec's check policy when the promotion path
  accepts one); a refusal is recorded on the row, not a failure of the date;
  the run is finished either way.
- R7: ``loop_dates`` (migration 20260924-11) holds per date the run, the
  state, the promotion and a JSON record of what ran.

The spec is a TOML document at a local path or an ``s3://`` object::

    [loop]
    schedule = "control-loop"
    release = "rebuild-v0.3"
    kind = "production"          # the only kind a scheduled loop runs
    owner = "rusholme"
    lane = "prompt"
    check_policy = "rebuild-trial@1"   # optional
    max_attempts = 3
    profile = "batch"                  # optional, the runs' resource profile

    [[dates]]
    processing_date = 2027-10-01
    [[dates.detector_images]]
    delivery = "s3://.../delivery/r0034001002001001001-sca01"
    admit_settings = "s3://.../admit-socsim.toml"
    difference_template = "s3://.../control/step3/P1/inputs"
    difference_settings = "s3://.../difference-gain1-imgnoise.toml"
    # unit = "r0034001002001001001/SCA01"  (optional; derived from delivery)

Dependency direction: ``rapidpipe.launch`` may not import ``rapidpipe.cli``,
and ``run start``'s walk, ``run create``'s release path and the storage
helper live there, so the CLI hands them in as :class:`LoopTools`; this
module never copies them.
"""

from __future__ import annotations

import datetime as _dt
import inspect
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Callable, Sequence

from rapidpipe.db.ids import new_ulid
from rapidpipe.launch import batch as launch_batch
from rapidpipe.products.manifest import Inputs, Manifest, OutputEntry, Unit
from rapidpipe.products.storage import join, parse_location
from rapidpipe.runs import repository

#: The run's selected stages (R4), and the positions in it each part of
#: the walk takes.
SELECTED_STAGES = (
    "admit", "register", "difference", "register", "finalize", "register", "load",
    "maintain", "crossmatch", "statistics", "prune", "alerts")
IMAGE_CHAIN = list(range(0, 7))
MAINTAIN, CROSSMATCH, STATISTICS, PRUNE, ALERTS = 7, 8, 9, 10, 11

#: The unit kinds of the stages whose input sets this module composes
#: (each stage's ``DECLARATION.unit``; a unit test checks they agree).
UNIT_KINDS = {"maintain": "detector-date", "crossmatch": "field", "alerts": "detector-image"}

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_TIMEOUT = 0, 1, 64, 75


class LoopSpecError(ValueError):
    """The spec is unreadable or malformed (exit 64)."""


class LoopError(RuntimeError):
    """A date cannot proceed for a reason that is not a unit's failure (exit 64)."""


# ======================================================================
# The spec
# ======================================================================

@dataclass(frozen=True)
class DetectorImage:
    delivery: str
    admit_settings: str | None
    difference_template: str
    difference_settings: str | None
    unit: str


@dataclass(frozen=True)
class LoopDate:
    processing_date: _dt.date
    detector_images: tuple[DetectorImage, ...]


@dataclass(frozen=True)
class LoopSpec:
    location: str
    schedule: str
    release: str
    kind: str
    owner: str
    lane: str
    check_policy: str | None
    max_attempts: int
    profile: str
    dates: tuple[LoopDate, ...]


_SCA_SUFFIX = re.compile(r"^(?P<stem>.+)[-_]sca(?P<sca>[0-9]{2})$", re.IGNORECASE)


def detector_unit_id(delivery: str) -> str:
    """The detector-image unit id for a delivery prefix: ``<stem>-scaNN`` ->
    ``<stem>/SCANN`` (the shape ``run start`` used in steps 3-4), else the
    prefix's last path component."""
    name = PurePosixPath(delivery.rstrip("/")).name
    match = _SCA_SUFFIX.fullmatch(name)
    if match is None:
        return name
    return f"{match.group('stem')}/SCA{match.group('sca')}"


def _required_str(table: dict[str, Any], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise LoopSpecError(f"{where}: {key!r} must be a non-empty string")
    return value


def _optional_str(table: dict[str, Any], key: str, where: str) -> str | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise LoopSpecError(f"{where}: {key!r} must be a non-empty string when given")
    return value


def parse_spec(text: str, location: str) -> LoopSpec:
    """Parse and check a loop spec (TOML); :class:`LoopSpecError` when malformed."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LoopSpecError(f"{location}: not valid TOML: {exc}") from exc
    loop = doc.get("loop")
    if not isinstance(loop, dict):
        raise LoopSpecError(f"{location}: no [loop] table")
    where = f"{location} [loop]"
    kind = loop.get("kind", "production")
    if kind != "production":
        raise LoopSpecError(f"{where}: kind must be 'production' (a scheduled loop "
                            f"promotes), got {kind!r}")
    max_attempts = loop.get("max_attempts", 1)
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise LoopSpecError(f"{where}: max_attempts must be a positive integer")

    dates_raw = doc.get("dates")
    if not isinstance(dates_raw, list) or not dates_raw:
        raise LoopSpecError(f"{location}: no [[dates]] entries")
    dates: list[LoopDate] = []
    seen: set[_dt.date] = set()
    for index, entry in enumerate(dates_raw):
        dwhere = f"{location} dates[{index}]"
        value = entry.get("processing_date") if isinstance(entry, dict) else None
        if isinstance(value, _dt.datetime) or not isinstance(value, _dt.date):
            raise LoopSpecError(f"{dwhere}: processing_date must be a TOML date (YYYY-MM-DD)")
        if value in seen:
            raise LoopSpecError(f"{dwhere}: processing_date {value} appears twice")
        seen.add(value)
        images_raw = entry.get("detector_images")
        if not isinstance(images_raw, list) or not images_raw:
            raise LoopSpecError(f"{dwhere}: no [[dates.detector_images]] entries")
        images: list[DetectorImage] = []
        for jndex, image in enumerate(images_raw):
            iwhere = f"{dwhere} detector_images[{jndex}]"
            if not isinstance(image, dict):
                raise LoopSpecError(f"{iwhere}: not a table")
            delivery = _required_str(image, "delivery", iwhere)
            unit = _optional_str(image, "unit", iwhere) or detector_unit_id(delivery)
            images.append(DetectorImage(
                delivery=delivery,
                admit_settings=_optional_str(image, "admit_settings", iwhere),
                difference_template=_required_str(image, "difference_template", iwhere),
                difference_settings=_optional_str(image, "difference_settings", iwhere),
                unit=unit))
        units = [i.unit for i in images]
        if len(set(units)) != len(units):
            raise LoopSpecError(f"{dwhere}: two detector images share a unit id ({units})")
        dates.append(LoopDate(processing_date=value, detector_images=tuple(images)))

    return LoopSpec(
        location=location,
        schedule=_required_str(loop, "schedule", where),
        release=_required_str(loop, "release", where),
        kind=kind,
        owner=_required_str(loop, "owner", where),
        lane=_required_str(loop, "lane", where),
        check_policy=_optional_str(loop, "check_policy", where),
        max_attempts=max_attempts,
        profile=_optional_str(loop, "profile", where) or "batch",
        dates=tuple(dates))


def read_spec_text(location: str, *, s3_client: Any = None) -> str:
    """The spec document at a local path or an ``s3://bucket/key`` object.

    S3 goes through ``rapidpipe.products.storage.s3_client`` (the seam tests
    replace) unless ``s3_client`` is given."""
    loc = parse_location(location)
    try:
        if not loc.is_s3():
            return loc.path.read_text()
        if s3_client is None:
            from rapidpipe.products import storage

            s3_client = storage.s3_client()
        body = s3_client.get_object(Bucket=loc.bucket, Key=loc.prefix)["Body"].read()
        return body.decode()
    except (OSError, UnicodeDecodeError) as exc:
        raise LoopSpecError(f"cannot read spec {location}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - ClientError-shaped
        code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey", "NotFound"):
            raise LoopSpecError(f"no spec at {location}") from exc
        raise


def load_spec(location: str, *, s3_client: Any = None) -> LoopSpec:
    return parse_spec(read_spec_text(location, s3_client=s3_client), location)


# ======================================================================
# What the CLI hands in
# ======================================================================

@dataclass
class LoopTools:
    """The CLI's code paths the loop runs through (never copies of them).

    ``walk(conn, *, run_id, unit_id, positions, inputs, settings, templates,
    interval, timeout, continue_hint) -> int`` is ``run start``'s walk
    (``rapidpipe.cli.runctl.walk_unit``); ``create_run(conn, **fields) ->
    run id`` is ``run create --release`` (``rapidpipe.cli.main
    .create_run_record``); ``storage`` has ``read_manifest(text)``,
    ``write_manifest(manifest, location)``, ``exists(location, rel)``,
    ``copy(src, src_rel, dst, dst_rel)`` and ``size(location, rel)``
    (``rapidpipe.cli.runctl._Storage``); ``inputs_root(run_id)`` is
    ``<scratch root>/runs/<run>/inputs``; ``out`` prints a line."""

    walk: Callable[..., int]
    create_run: Callable[..., str]
    storage: Any
    inputs_root: Callable[[str], str]
    out: Callable[[str], None] = field(default=lambda line: print(line, flush=True))


# ======================================================================
# Database reads (module-level so unit tests can replace them)
# ======================================================================

@dataclass(frozen=True)
class LoopRow:
    schedule: str
    processing_date: _dt.date
    run: str
    state: str
    started_at: Any
    ended_at: Any
    promotion: str | None
    record: dict[str, Any]


_ROW_COLUMNS = "schedule, processing_date, run, state, started_at, ended_at, promotion, record"


def loop_row(conn, schedule: str, processing_date: _dt.date) -> LoopRow | None:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_ROW_COLUMNS} FROM loop_dates "
                    "WHERE schedule = %s AND processing_date = %s",
                    (schedule, processing_date))
        row = cur.fetchone()
    return None if row is None else LoopRow(*row)


def loop_rows(conn, schedule: str) -> list[LoopRow]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_ROW_COLUMNS} FROM loop_dates WHERE schedule = %s "
                    "ORDER BY processing_date", (schedule,))
        return [LoopRow(*row) for row in cur.fetchall()]


def previous_complete_row(conn, schedule: str, processing_date: _dt.date) -> LoopRow | None:
    """The most recent ``complete`` row of ``schedule`` before ``processing_date`` (R5)."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_ROW_COLUMNS} FROM loop_dates WHERE schedule = %s "
                    "AND processing_date < %s AND state = 'complete' "
                    "ORDER BY processing_date DESC LIMIT 1", (schedule, processing_date))
        row = cur.fetchone()
    return None if row is None else LoopRow(*row)


def _insert_row(conn, schedule: str, processing_date: _dt.date, run_id: str,
                record: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO loop_dates (schedule, processing_date, run, state, record) "
                    "VALUES (%s, %s, %s, 'open', %s)",
                    (schedule, processing_date, run_id, json.dumps(record)))


def _update_row(conn, schedule: str, processing_date: _dt.date, *, state: str,
                promotion: str | None, record: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE loop_dates SET state = %s, ended_at = now(), promotion = %s, "
                    "record = %s WHERE schedule = %s AND processing_date = %s",
                    (state, promotion, json.dumps(record, default=str), schedule,
                     processing_date))


def selected_output(conn, run_id: str, stage: str, unit_id: str) -> str:
    """The selected attempt's output location (``resolve_inputs_from_stage``)."""
    return launch_batch.resolve_inputs_from_stage(
        conn, run_id=run_id, unit_id=unit_id, upstream_stage=stage)


_SOURCES_TABLE = re.compile(r"^sources_(?P<date>[0-9]{8})_(?P<sca>[0-9]{1,2})$")


def maintain_unit_id(table: str) -> str:
    """``sources_<yyyymmdd>_<sca>`` -> maintain's unit ``<yyyymmdd>/SCA<nn>``."""
    match = _SOURCES_TABLE.fullmatch(table or "")
    if match is None:
        raise LoopError(f"source-set table {table!r} is not sources_<yyyymmdd>_<sca>")
    return f"{match.group('date')}/SCA{int(match.group('sca')):02d}"


def source_set_fields(conn, table: str, instance: str) -> list[int]:
    """The distinct ``field`` values of one source set's rows in its child table."""
    maintain_unit_id(table)  # validates the name before it reaches SQL
    from psycopg2 import sql

    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT DISTINCT field FROM {} WHERE result_set = %s "
                            "ORDER BY field").format(sql.Identifier(table)), (instance,))
        return [int(row[0]) for row in cur.fetchall() if row[0] is not None]


def base_instance(conn, run_id: str, field_id: str) -> tuple[str, str] | None:
    """``(association-set instance, its manifest location)`` crossmatch's selected
    attempt produced for unit ``field_id`` in ``run_id``, or ``None``."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pi.id, a.output_location
            FROM units u
            JOIN attempts a ON a.id = u.selected_attempt
            JOIN product_instances pi
              ON pi.producing_attempt = a.id AND pi.kind = 'association-set'
            WHERE u.run = %s AND u.stage = 'crossmatch' AND u.unit_id = %s
              AND u.state = 'complete'
            ORDER BY pi.id
            """, (run_id, field_id))
        rows = cur.fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise LoopError(f"run {run_id} crossmatch {field_id}: {len(rows)} association sets "
                        "from one selected attempt; expected one")
    return rows[0][0], rows[0][1]


def _registered(conn, instance_ids: list[str]) -> list[str]:
    if not instance_ids:
        return []
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM product_instances WHERE id = ANY(%s)", (instance_ids,))
        found = {row[0] for row in cur.fetchall()}
    return [i for i in instance_ids if i in found]


def unit_records(conn, run_id: str) -> list[dict[str, Any]]:
    """Every unit of the run: stage, unit, state, selected attempt and its job."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.stage, u.unit_id, u.state, u.selected_attempt, a.scheduler_job_id
            FROM units u LEFT JOIN attempts a ON a.id = u.selected_attempt
            WHERE u.run = %s ORDER BY u.created, u.stage, u.unit_id
            """, (run_id,))
        return [{"stage": s, "unit": u, "state": st, "attempt": att, "job": job}
                for s, u, st, att, job in cur.fetchall()]


# ======================================================================
# Base-set selection and input-set composition
# ======================================================================

def base_entry(conn, storage: Any, previous: LoopRow | None, field_id: int) -> OutputEntry | None:
    """The base ``association-set`` entry for ``field_id`` (R5), or ``None``.

    The instance is found through the previous complete date's run's units,
    attempts and product_instances; the entry itself (key, registration) is
    that attempt's manifest's, whose ``key.field`` must be the field
    (crossmatch's ``_base_entry`` refuses anything else)."""
    if previous is None:
        return None
    found = base_instance(conn, previous.run, str(field_id))
    if found is None:
        return None
    instance, location = found
    manifest = storage.read_manifest(location)
    entries = [o for o in manifest.outputs if o.instance == instance]
    if len(entries) != 1:
        raise LoopError(f"{location}/manifest.json does not list association set {instance}")
    entry = entries[0]
    if str(entry.key.get("field")) != str(field_id):
        raise LoopError(f"association set {instance} is for field {entry.key.get('field')!r}, "
                        f"not {field_id}")
    return entry


def _bind_and_write(conn, storage: Any, *, run_id: str, stage: str, unit_id: str,
                    dest: str, manifest: Manifest | None) -> str:
    """Admit the unit, bind its registered inputs, commit, then write the
    manifest last (``compose_inputs``'s order: a manifest on storage means the
    bindings were committed). An existing manifest at ``dest`` is reused, so a
    resumed date re-binds (idempotent) and never rewrites it."""
    dest_loc = parse_location(dest)
    existing = storage.exists(dest_loc, "manifest.json")
    if existing:
        manifest = storage.read_manifest(dest)
    assert manifest is not None
    repository.add_unit(conn, run_id, stage, UNIT_KINDS[stage], unit_id)
    ids = [o.instance for o in manifest.outputs] + list(manifest.inputs.result_sets)
    repository.bind_unit_inputs(conn, run_id, stage, unit_id,
                                _registered(conn, list(dict.fromkeys(ids))))
    conn.commit()
    if not existing:
        storage.write_manifest(manifest, dest_loc)
    return dest


def input_set_manifest(run_id: str, unit: Unit, outputs: Sequence[OutputEntry],
                       result_sets: Sequence[str]) -> Manifest:
    return Manifest(
        run=run_id, unit=unit, stage="input-set", attempt=new_ulid(),
        execution_record="exec/input-set.json",
        inputs=Inputs(manifest="input-set", products={},
                      result_sets=tuple(dict.fromkeys(result_sets))),
        outputs=tuple(outputs))


def crossmatch_inputs(run_id: str, field_id: int, source_sets: Sequence[OutputEntry],
                      base: OutputEntry | None) -> Manifest:
    """Crossmatch's input set for one field: the source sets, plus the base."""
    outputs = list(source_sets) + ([base] if base is not None else [])
    return input_set_manifest(run_id, Unit(kind="field", id=str(field_id)), outputs,
                              [o.instance for o in outputs])


def _copy_members(storage: Any, src: str, entry: OutputEntry, dest: str) -> None:
    src_loc, dest_loc = parse_location(src), parse_location(dest)
    for member in entry.members:
        storage.copy(src_loc, member.path, dest_loc, member.path)
        actual = storage.size(dest_loc, member.path)
        if actual != member.bytes:
            raise LoopError(f"copied {join(dest_loc, member.path)} is {actual} bytes; its "
                            f"manifest says {member.bytes}")


# ======================================================================
# One date
# ======================================================================

class _Stop(Exception):
    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code


def _promote(conn, run_id: str, spec: LoopSpec, processing_date: _dt.date
             ) -> tuple[str | None, str, str]:
    """(promotion id or None, the record's ``promotion`` text, ``promotion_gate``)."""
    kwargs: dict[str, Any] = {}
    if "check_policy" in inspect.signature(repository.promote_run).parameters:
        kwargs["check_policy"] = spec.check_policy
        gate = f"check policy {spec.check_policy}" if spec.check_policy else "check policy (none)"
    else:
        gate = "released-image only"
    try:
        promotion = repository.promote_run(
            conn, run_id, "scheduler", f"processing date {processing_date}", **kwargs)
    except (repository.RunNotFound, repository.RunDeletingOrDeleted):
        raise
    except repository.RunModelError as exc:
        conn.rollback()
        return None, f"refused: {exc}", gate
    conn.commit()
    return promotion, promotion, gate


def process_date(conn, spec: LoopSpec, day: LoopDate, tools: LoopTools, *,
                 interval: float, timeout: float) -> int:
    """Walk one date to complete or failed; return its exit code (0, 1 or 75)."""
    out, storage = tools.out, tools.storage
    schedule, date = spec.schedule, day.processing_date
    row = loop_row(conn, schedule, date)
    if row is None:
        run_id = tools.create_run(
            conn, kind=spec.kind, owner=spec.owner,
            purpose=f"processing date {date} (schedule {schedule})",
            stages=list(SELECTED_STAGES), release=spec.release, lane=spec.lane,
            profile=spec.profile, db_target=None, max_attempts=spec.max_attempts,
            input_selection_ref=spec.location, check_policy_ref=spec.check_policy)
        record: dict[str, Any] = {"spec": spec.location, "release": spec.release,
                                  "run": run_id}
        _insert_row(conn, schedule, date, run_id, record)
        conn.commit()
        out(f"date={date} run={run_id} created")
    else:
        run_id = row.run
        record = dict(row.record)
        out(f"date={date} run={run_id} resumed")

    hint = f"rapidpipe loop run --spec {spec.location} --date {date}"

    def walk(unit_id: str, positions: list[int], *, inputs: Sequence[str] = (),
             settings: Sequence[str] = (), templates: Sequence[str] = ()) -> None:
        rc = tools.walk(conn, run_id=run_id, unit_id=unit_id, positions=positions,
                        inputs=list(inputs), settings=list(settings),
                        templates=list(templates), interval=interval, timeout=timeout,
                        continue_hint=hint)
        if rc != 0:
            stages = ",".join(SELECTED_STAGES[p] for p in positions)
            raise _Stop(EXIT_FAILED, f"{stages} {unit_id} did not complete (exit {rc})")

    try:
        # (b) the detector-image chain, admit..load, per image.
        loads: dict[str, tuple[str, list[OutputEntry]]] = {}
        for image in day.detector_images:
            settings = [s for s in (image.admit_settings,) if s]
            if image.difference_settings:
                settings.append(f"difference={image.difference_settings}")
            walk(image.unit, IMAGE_CHAIN, inputs=[image.delivery], settings=settings,
                 templates=[f"difference={image.difference_template}"])
            load_loc = selected_output(conn, run_id, "load", image.unit)
            entries = [o for o in storage.read_manifest(load_loc).outputs
                       if o.kind == "source-set"]
            if not entries:
                raise LoopError(f"{load_loc}/manifest.json has no source-set entry")
            loads[image.unit] = (load_loc, entries)

        # (c) maintain per <yyyymmdd>/SCA<nn>.
        by_maintain: dict[str, list[tuple[str, OutputEntry]]] = {}
        for unit, (load_loc, entries) in loads.items():
            for entry in entries:
                mu = maintain_unit_id(str(entry.registration.get("table", "")))
                by_maintain.setdefault(mu, []).append((load_loc, entry))
        for mu, items in by_maintain.items():
            if len({loc for loc, _ in items}) == 1:
                inputs = items[0][0]
            else:
                inputs = _bind_and_write(
                    conn, storage, run_id=run_id, stage="maintain", unit_id=mu,
                    dest=f"{tools.inputs_root(run_id)}/maintain/{mu}",
                    manifest=input_set_manifest(
                        run_id, Unit(kind="detector-date", id=mu), [e for _, e in items],
                        [e.instance for _, e in items]))
            walk(mu, [MAINTAIN], inputs=[inputs])

        # (d) per field: crossmatch (source sets + base) -> statistics -> prune.
        field_sets: dict[int, list[OutputEntry]] = {}
        image_fields: dict[str, set[int]] = {}
        for unit, (_, entries) in loads.items():
            for entry in entries:
                for f in source_set_fields(conn, str(entry.registration["table"]),
                                           entry.instance):
                    field_sets.setdefault(f, []).append(entry)
                    image_fields.setdefault(unit, set()).add(f)
        fields = sorted(field_sets)
        if not fields:
            raise LoopError(f"date {date}: the loaded source sets have no fields")
        previous = previous_complete_row(conn, schedule, date)
        association: dict[int, str] = {}
        statistics: dict[int, str] = {}
        bases: dict[str, str | None] = {}
        for f in fields:
            base = base_entry(conn, storage, previous, f)
            bases[str(f)] = base.instance if base is not None else None
            xm_inputs = _bind_and_write(
                conn, storage, run_id=run_id, stage="crossmatch", unit_id=str(f),
                dest=f"{tools.inputs_root(run_id)}/crossmatch/{f}",
                manifest=crossmatch_inputs(run_id, f, field_sets[f], base))
            walk(str(f), [CROSSMATCH], inputs=[xm_inputs])
            xm_out = selected_output(conn, run_id, "crossmatch", str(f))
            sets = [o.instance for o in storage.read_manifest(xm_out).outputs
                    if o.kind == "association-set"]
            if len(sets) != 1:
                raise LoopError(f"{xm_out}/manifest.json has {len(sets)} association sets")
            association[f] = sets[0]
            walk(str(f), [STATISTICS], inputs=[xm_out])
            st_out = selected_output(conn, run_id, "statistics", str(f))
            st_sets = [o.instance for o in storage.read_manifest(st_out).outputs
                       if o.kind == "statistics-set"]
            if st_sets:
                statistics[f] = st_sets[0]
            walk(str(f), [PRUNE], inputs=[xm_out])

        # (e) alerts per detector image.
        alerts: dict[str, dict[str, Any]] = {}
        for image in day.detector_images:
            fin_loc = selected_output(conn, run_id, "finalize", image.unit)
            fin = storage.read_manifest(fin_loc)
            diffs = [o for o in fin.outputs if o.kind == "difference-image"]
            if len(diffs) != 1:
                raise LoopError(f"{fin_loc}/manifest.json has {len(diffs)} difference-image "
                                "entries; expected one")
            template = storage.read_manifest(image.difference_template)
            refcats = [o for o in template.outputs if o.kind == "reference-catalog"]
            if len(refcats) > 1:
                raise LoopError(f"{image.difference_template}/manifest.json has "
                                f"{len(refcats)} reference-catalog entries")
            dest = f"{tools.inputs_root(run_id)}/alerts/{image.unit}"
            own_fields = sorted(image_fields.get(image.unit, ()))
            result_sets = ([e.instance for e in loads[image.unit][1]]
                           + [association[f] for f in own_fields]
                           + [statistics[f] for f in own_fields if f in statistics])
            manifest = None
            if not storage.exists(parse_location(dest), "manifest.json"):
                _copy_members(storage, fin_loc, diffs[0], dest)
                for refcat in refcats:
                    _copy_members(storage, image.difference_template, refcat, dest)
                manifest = input_set_manifest(run_id, fin.unit, [diffs[0], *refcats],
                                              result_sets)
            _bind_and_write(conn, storage, run_id=run_id, stage="alerts", unit_id=image.unit,
                            dest=dest, manifest=manifest)
            walk(image.unit, [ALERTS], inputs=[dest])
            al_out = selected_output(conn, run_id, "alerts", image.unit)
            containers = [o.instance for o in storage.read_manifest(al_out).outputs
                          if o.kind == "alert-container"]
            alerts[image.unit] = {"instance": containers[0] if containers else None,
                                  "location": al_out}
    except _Stop as stop:
        record.update(units=unit_records(conn, run_id), failure=str(stop))
        _update_row(conn, schedule, date, state="failed", promotion=None, record=record)
        conn.commit()
        out(f"date={date} run={run_id} state=failed reason={stop}")
        return stop.code
    except Exception as exc:
        if getattr(exc, "code", None) == EXIT_TIMEOUT:
            conn.rollback()
            out(f"date={date} run={run_id} state=timeout")
            raise _Stop(EXIT_TIMEOUT, str(exc)) from exc
        raise

    # (f) promotion under the policy, then finish.
    promotion_id, promotion_text, gate = _promote(conn, run_id, spec, date)
    repository.finish_run(conn, run_id)
    record.update(
        units=unit_records(conn, run_id), fields=fields, base_sets=bases,
        association_sets={str(f): i for f, i in association.items()},
        statistics_sets={str(f): i for f, i in statistics.items()},
        alerts=alerts, promotion=promotion_text, promotion_gate=gate)
    # (g) the row.
    _update_row(conn, schedule, date, state="complete", promotion=promotion_id, record=record)
    conn.commit()
    out(f"date={date} run={run_id} state=complete promotion={promotion_text}")
    return EXIT_OK


# ======================================================================
# The loop, the plan
# ======================================================================

def _selected_dates(spec: LoopSpec, dates: Sequence[_dt.date] | None) -> list[LoopDate]:
    if not dates:
        return list(spec.dates)
    known = {d.processing_date for d in spec.dates}
    unknown = [str(d) for d in dates if d not in known]
    if unknown:
        raise LoopSpecError(f"dates {', '.join(unknown)} are not in {spec.location}")
    wanted = set(dates)
    return [d for d in spec.dates if d.processing_date in wanted]


def run_loop(conn, spec: LoopSpec, tools: LoopTools, *, dates: Sequence[_dt.date] | None = None,
             dry_run: bool = False, interval: float = 30.0, timeout: float = 14400.0) -> int:
    """``loop run`` (R3): every selected date whose row is absent or ``open``,
    in spec order. 0 when all are complete, 1 at the first failed date, 75 on
    a timeout (the row stays ``open``; rerun to resume)."""
    chosen = _selected_dates(spec, dates)
    if dry_run:
        plan(conn, spec, tools, dates=[d.processing_date for d in chosen])
        return EXIT_OK
    for day in chosen:
        row = loop_row(conn, spec.schedule, day.processing_date)
        if row is not None and row.state != "open":
            tools.out(f"date={day.processing_date} run={row.run} state={row.state} (skipped)")
            if row.state == "failed":
                tools.out(f"date={day.processing_date}: a failed date is not retried by the "
                          "loop; stopping before later dates")
                return EXIT_FAILED
            continue
        try:
            code = process_date(conn, spec, day, tools, interval=interval, timeout=timeout)
        except _Stop as stop:
            tools.out(f"timeout: {stop}")
            return stop.code
        if code != EXIT_OK:
            return code
    return EXIT_OK


def plan(conn, spec: LoopSpec, tools: LoopTools, *,
         dates: Sequence[_dt.date] | None = None) -> list[dict[str, Any]]:
    """``loop plan``: per date, what ``loop run`` would do; prints and returns it."""
    lines: list[dict[str, Any]] = []
    for day in _selected_dates(spec, dates):
        row = loop_row(conn, spec.schedule, day.processing_date)
        if row is None:
            action, run = "create", None
        elif row.state == "open":
            action, run = "resume", row.run
        else:
            action, run = f"skip ({row.state})", row.run
        previous = previous_complete_row(conn, spec.schedule, day.processing_date)
        entry = {
            "processing_date": str(day.processing_date), "action": action, "run": run,
            "units": [i.unit for i in day.detector_images],
            "base_from": None if previous is None else {
                "processing_date": str(previous.processing_date), "run": previous.run,
                "association_sets": previous.record.get("association_sets", {})},
        }
        lines.append(entry)
        base = entry["base_from"]
        base_text = ("none (first date)" if base is None else
                     f"{base['run']} ({base['processing_date']}) "
                     + (",".join(f"{f}={i}" for f, i in base["association_sets"].items())
                        or "no association sets recorded"))
        tools.out(f"date={entry['processing_date']} action={action} run={run or '-'} "
                  f"units={','.join(entry['units'])} base={base_text}")
    return lines


def show(conn, schedule: str, *, as_json: bool, out: Callable[[str], None]) -> int:
    """``loop show``: one line per ``loop_dates`` row of ``schedule``."""
    rows = loop_rows(conn, schedule)
    for row in rows:
        out(f"{row.processing_date}\t{row.state}\trun={row.run}\t"
            f"promotion={row.promotion or row.record.get('promotion') or '-'}\t"
            f"started={row.started_at}\tended={row.ended_at or '-'}")
        if as_json:
            out(json.dumps(row.record, sort_keys=True, default=str))
    if not rows:
        out(f"schedule {schedule}: no processing dates recorded")
    return EXIT_OK
