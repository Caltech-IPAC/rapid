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
- R4 (amended A1): one production run per date, created by ``run create
  --release``'s code path, walking admit -> register -> difference ->
  finalize -> register(finalize output) -> load(finalize output) per detector
  image (the raw difference is never registered), maintain per
  ``<yyyymmdd>/SCA<nn>``, crossmatch -> statistics -> prune per field, then
  alerts per detector image, every attempt through ``submit_unit`` +
  ``reconcile``. An alerts input set names, per field the image touches,
  the association set, its statistics set and the field's prune output
  (supervisor step 9, R5: the history leaves out the pruned pairs).
- R5 (amended A3): a field's base catalog is the association set crossmatch
  produced for that field in the most recent earlier ``complete`` row of the
  same schedule that has one (walking back over dates; promoted or not, with
  ``base_promoted`` recorded); none on the first date. The set must be
  readable by the date's run (supervisor step 9 R2: complete, retained, a
  selected production output); one that is not is skipped for the next
  earlier date and named in ``record.bases_skipped``. The source sets the
  loop reads fields from pass the same rule. Input-set manifests
  live under ``<scratch root>/runs/<run>/inputs/<stage>/<unit>/``.
- A2: an attempt left running without a Batch job goes to step 6's
  ``resolve_jobless`` once and the walk is retried once; still job-less, the
  date fails (row ``failed``, ``record.jobless_attempt``, exit 1).
- Step 9 R4: a unit whose input manifest the launcher refuses
  (``InputsRefused``, ``run start``'s exit 65: absent, unreadable, or naming
  an input of a deleting run) fails the date with the message (row
  ``failed``, ``record.failure``, exit 1); nothing was written for it.
- A4: one loop per schedule (``pg_try_advisory_lock``, exit 75 when held); a
  ``failed`` row stops the loop unless ``--retry-failed`` reopens it: a new
  run seeded from the row's run through step 6's ``run create --seed <run>
  --only-failed`` path (the row repointed to it, the old run id appended to
  ``record.previous_runs``), then resumed; the row's completion commits with
  ``finish_run``. A ``failed`` row whose run is open with no failed or
  cancelled unit (a refusal, not a unit's failure) is reopened on the same
  run by any ``loop run`` (``record.reopened`` gets the time) and resumed.
- Codex 7-2: within a date every unit a phase can run is walked before the
  date fails (the detector-image chains, then maintain, then the field
  chains, then alerts), so a seeded re-run, whose stage list starts at its
  earliest non-complete position, holds every unit still to run; each
  field's crossmatch input set carries every source set of the date (the
  neighbour pass needs neighbouring fields' rows); a date whose run is no
  longer open completes only when every unit its plan requires is
  ``complete`` (in the run, or inherited from the runs it was seeded from).
- R6: once every unit is complete the policy's checks are run and recorded
  and the run is promoted (``promote_run``, ``who="scheduler"``,
  ``check_policy`` = the spec's, else the run's, else the default); a refusal
  is recorded on the row, not a failure of the date; the run is finished
  either way.
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

from rapidpipe.db import objects as _objects
from rapidpipe.db.ids import new_ulid
from rapidpipe.launch import batch as launch_batch
from rapidpipe.products.manifest import Inputs, Manifest, OutputEntry, Unit
from rapidpipe.products.storage import join, parse_location
from rapidpipe.runs import repository
from rapidpipe.runs.inputs import InputsRefused

#: The run's selected stages (R4), and the positions in it each part of
#: the walk takes.
SELECTED_STAGES = (
    "admit", "register", "difference", "finalize", "register", "load",
    "maintain", "crossmatch", "statistics", "prune", "alerts")
IMAGE_CHAIN = list(range(0, 6))
MAINTAIN, CROSSMATCH, STATISTICS, PRUNE, ALERTS = 6, 7, 8, 9, 10

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
    ``<scratch root>/runs/<run>/inputs``; ``out`` prints a line;
    ``create_seeded_run(conn, seed) -> run id`` is ``run create --seed <run>
    --only-failed`` (``rapidpipe.cli.main.create_only_failed_run``, no
    commit; a refusal is a ``RunModelError``)."""

    walk: Callable[..., int]
    create_run: Callable[..., str]
    storage: Any
    inputs_root: Callable[[str], str]
    out: Callable[[str], None] = field(default=lambda line: print(line, flush=True))
    create_seeded_run: Callable[..., str] | None = None


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


def previous_complete_rows(conn, schedule: str, processing_date: _dt.date) -> list[LoopRow]:
    """``schedule``'s ``complete`` rows before ``processing_date``, newest first (R5/A3)."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_ROW_COLUMNS} FROM loop_dates WHERE schedule = %s "
                    "AND processing_date < %s AND state = 'complete' "
                    "ORDER BY processing_date DESC", (schedule, processing_date))
        return [LoopRow(*row) for row in cur.fetchall()]


def run_promotion(conn, run_id: str) -> str | None:
    """The most recent promotions row whose request names ``run_id``, if any."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM promotions WHERE request_context->>'run' = %s "
                    "ORDER BY happened_at DESC, id DESC LIMIT 1", (run_id,))
        row = cur.fetchone()
    return row[0] if row else None


def run_state(conn, run_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
    return row[0] if row else None


def jobless_attempts(conn, run_id: str) -> list[str]:
    """The run's attempts still running with no scheduler job (A2)."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM attempts WHERE run = %s AND disposition IS NULL "
                    "AND scheduler_job_id IS NULL ORDER BY id", (run_id,))
        return [row[0] for row in cur.fetchall()]


def try_lock(conn, schedule: str) -> bool:
    """A session advisory lock on the schedule, so one loop runs per schedule (A4)."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext('rapidpipe.loop:' || %s))",
                    (schedule,))
        (locked,) = cur.fetchone()
    conn.commit()
    return bool(locked)


def unlock(conn, schedule: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(hashtext('rapidpipe.loop:' || %s))",
                    (schedule,))
    conn.commit()


def repoint_row(conn, schedule: str, processing_date: _dt.date, run_id: str,
                record: dict[str, Any]) -> None:
    """``--retry-failed``: a failed row back to ``open`` on its seeded re-run
    ``run_id`` (Codex 7-2). Does not commit."""
    with conn.cursor() as cur:
        cur.execute("UPDATE loop_dates SET run = %s, state = 'open', ended_at = NULL, "
                    "promotion = NULL, record = %s "
                    "WHERE schedule = %s AND processing_date = %s AND state = 'failed'",
                    (run_id, json.dumps(record, default=str), schedule, processing_date))


def run_lineage(conn, run_id: str) -> tuple[tuple[str, ...], list[str]]:
    """``((run, its seed, the seed's seed, ...), run's selected stages)``."""
    chain: list[str] = []
    stages: list[str] | None = None
    current: str | None = run_id
    while current is not None and current not in chain:
        with conn.cursor() as cur:
            cur.execute("SELECT seed_run, selected_stages FROM runs WHERE id = %s", (current,))
            row = cur.fetchone()
        if row is None:
            raise LoopError(f"run {current} does not exist")
        chain.append(current)
        if stages is None:
            stages = list(row[1] or [])
        current = row[0]
    return tuple(chain), stages or []


def unit_state(conn, run_id: str, stage: str, unit_id: str) -> tuple[str, bool] | None:
    """``(state, seeded)`` of the run's (stage, unit_id), or ``None``."""
    with conn.cursor() as cur:
        cur.execute("SELECT state, seeded_from_unit IS NOT NULL FROM units "
                    "WHERE run = %s AND stage = %s AND unit_id = %s",
                    (run_id, stage, unit_id))
        row = cur.fetchone()
    return None if row is None else (row[0], bool(row[1]))


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


def readable_result_set(conn, instance: str, run_id: str, kind: str) -> None:
    """Refuse (:class:`ValueError`) a result set run ``run_id`` may not read.

    ``rapidpipe.db.objects.assert_readable_result_set`` (supervisor step 9
    ruling R2; Codex 9-2): complete and retained, and ``run_id``'s own or a
    production run's (custody ``candidate``/``current``) selected output,
    of ``kind``. The loop applies it to the source sets it discovers fields
    from and to the base it chooses, with the date's run as the reader.
    """
    with conn.cursor() as cur:
        _objects.assert_readable_result_set(cur, instance, run_id, kind=kind)


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

@dataclass(frozen=True)
class Base:
    entry: OutputEntry
    run: str
    processing_date: _dt.date
    promoted: bool


def base_for_field(conn, storage: Any, previous: Sequence[LoopRow], field_id: int,
                   run_id: str, skipped: list[dict[str, str]] | None = None
                   ) -> Base | None:
    """The base for ``field_id`` (R5/A3): the newest of ``previous`` (complete
    rows, newest first) whose run crossmatched the field and whose
    association set run ``run_id`` may read (:func:`readable_result_set`,
    ruling R2), or ``None``. A set it may not read is not eligible: the
    search goes on to the next earlier date, and ``skipped`` (when given)
    gets ``{run, processing_date, instance, reason}`` for it."""
    for row in previous:
        entry = base_entry(conn, storage, row, field_id)
        if entry is None:
            continue
        try:
            readable_result_set(conn, entry.instance, run_id, "association-set")
        except ValueError as exc:
            if skipped is not None:
                skipped.append({"run": row.run, "processing_date": str(row.processing_date),
                                "instance": entry.instance, "reason": str(exc)})
            continue
        return Base(entry=entry, run=row.run, processing_date=row.processing_date,
                    promoted=run_promotion(conn, row.run) is not None)
    return None


def base_entry(conn, storage: Any, previous: LoopRow | None, field_id: int) -> OutputEntry | None:
    """One complete row's ``association-set`` entry for ``field_id``, or ``None``.

    The instance is found through that date's run's units, attempts and
    product_instances; the entry itself (key, registration) is that
    attempt's manifest's, whose ``key.field`` must be the field
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
    """Crossmatch's input set for one field: every source set of the date
    (crossmatch filters by field itself, and its neighbour pass reads
    neighbouring fields' rows from the supplied sets; Codex 7-2), plus the
    field's base."""
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
    def __init__(self, code: int, reason: str, *, jobless: str | None = None):
        super().__init__(reason)
        self.code = code
        self.jobless = jobless


def alert_source_set(entries: Sequence[OutputEntry], difference: str) -> OutputEntry:
    """The image's one source set loaded from the finalized ``difference``
    instance (alerts refuses any other number, or another difference)."""
    matching = [e for e in entries if e.key.get("difference") == difference]
    if len(matching) != 1:
        raise LoopError(f"{len(matching)} source sets were loaded from difference "
                        f"{difference}; alerts needs exactly one")
    return matching[0]


def alert_result_sets(source: OutputEntry, associations: Sequence[str],
                      statistics: Sequence[str], pruned: Sequence[str] = ()) -> list[str]:
    """alerts' ``inputs.result_sets``: the source set, one or more association
    sets, at most one statistics set and at most one pruned set per
    association set (each field's prune output, supervisor step 9 R5)."""
    if not associations:
        raise LoopError(f"source set {source.instance} has no association sets")
    if len(statistics) > len(associations) or len(set(statistics)) != len(statistics):
        raise LoopError("more statistics sets than association sets")
    if len(pruned) > len(associations) or len(set(pruned)) != len(pruned):
        raise LoopError("more pruned sets than association sets")
    return [source.instance, *associations, *statistics, *pruned]


def field_pruned_set(entries: Sequence[OutputEntry], location: str, association: str) -> str:
    """The one ``pruned-set`` a field's prune manifest (at ``location``) lists,
    which must prune ``association``, the field's association set (R5)."""
    pruned = [e for e in entries if e.kind == "pruned-set"]
    if len(pruned) != 1:
        raise LoopError(f"{location}/manifest.json has {len(pruned)} pruned sets")
    base = pruned[0].key.get("base")
    if base != association:
        raise LoopError(f"{location}/manifest.json's pruned set {pruned[0].instance} prunes "
                        f"{base}, not the field's association set {association}")
    return pruned[0].instance


def _promote(conn, run_id: str, spec: LoopSpec, processing_date: _dt.date
             ) -> tuple[str | None, str, str, list[dict[str, Any]]]:
    """(promotion id or None, the record's ``promotion`` text, ``promotion_gate``,
    the checks run) (R6).

    With step 6's gate (``promote_run`` takes ``check_policy``): resolve the
    policy (the spec's, else the run's, else the default), run its checks
    over the run's candidates as ``scheduler`` through
    ``rapidpipe.checks.runner`` (recorded, committed), then promote under it.
    Without it: promote under the released-image rule only. A run already
    promoted (a resumed date) reuses that promotion. A refusal is returned,
    not raised."""
    existing = run_promotion(conn, run_id)
    kwargs: dict[str, Any] = {}
    checks: list[dict[str, Any]] = []
    if "check_policy" not in inspect.signature(repository.promote_run).parameters:
        gate = "released-image only"
        if existing is not None:
            return existing, existing, gate, checks
    else:
        from rapidpipe.checks.registry import CheckError
        from rapidpipe.checks.runner import (
            CheckUsageError,
            resolve_run_policy,
            run_policy_checks,
        )

        try:
            policy = resolve_run_policy(conn, run_id, spec.check_policy)
        except CheckError as exc:
            conn.rollback()
            return None, f"refused: {exc}", "check policy (unloadable)", checks
        gate = f"check policy {policy.ref}"
        if existing is not None:
            # A resumed date whose run was promoted before the loop stopped.
            return existing, existing, gate, checks
        try:
            recorded = run_policy_checks(conn, run_id, policy, who="scheduler")
        except (CheckError, CheckUsageError) as exc:
            conn.rollback()
            return None, f"refused: {exc}", gate, checks
        conn.commit()
        checks = [{"id": c.id, "check": f"{c.check_name}@{c.version}",
                   "instance": c.instance, "required": c.required, "outcome": c.outcome}
                  for c in recorded]
        kwargs["check_policy"] = policy
    try:
        promotion = repository.promote_run(
            conn, run_id, "scheduler", f"processing date {processing_date}", **kwargs)
    except (repository.RunNotFound, repository.RunDeletingOrDeleted):
        raise
    except repository.RunModelError as exc:
        conn.rollback()
        return None, f"refused: {exc}", gate, checks
    conn.commit()
    return promotion, promotion, gate, checks


def _finish_row(conn, spec: LoopSpec, date: _dt.date, run_id: str, record: dict[str, Any],
                out: Callable[[str], None]) -> int:
    """(f)+(g): promote (or reuse the run's promotion, or record a refusal),
    then ``finish_run`` and the row's completion in one transaction (A4)."""
    promotion_id, promotion_text, gate, checks = _promote(conn, run_id, spec, date)
    if run_state(conn, run_id) == "open":
        repository.finish_run(conn, run_id)
    record.update(units=unit_records(conn, run_id), promotion=promotion_text,
                  promotion_gate=gate, checks=checks)
    _update_row(conn, spec.schedule, date, state="complete", promotion=promotion_id,
                record=record)
    conn.commit()
    out(f"date={date} run={run_id} state=complete promotion={promotion_text}")
    return EXIT_OK


def _fail_row(conn, spec: LoopSpec, date: _dt.date, run_id: str, record: dict[str, Any],
              out: Callable[[str], None], **fields: Any) -> int:
    record.update(units=unit_records(conn, run_id), **fields)
    _update_row(conn, spec.schedule, date, state="failed", promotion=None, record=record)
    conn.commit()
    reason = fields.get("failure") or fields.get("reason")
    out(f"date={date} run={run_id} state=failed reason={reason}")
    return EXIT_FAILED


# ----------------------------------------------------------------------
# The date's run, seeded or not (Codex 7-2)
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class RunView:
    """The date's run as the walk sees it. ``chain`` is the run, then the run
    it was seeded from by ``--retry-failed``, and so on; ``offset`` is the
    position in :data:`SELECTED_STAGES` the run's own stage list starts at (a
    production seed's re-run selects the suffix from its earliest
    non-complete position, step 6 R7). A unit the run has no row for is
    inherited when the nearest run of the chain holding it has it
    ``complete``; its output is read there (a production run's outputs are
    project custody)."""

    run: str
    chain: tuple[str, ...]
    offset: int

    @property
    def seeded(self) -> bool:
        return len(self.chain) > 1


def run_view(conn, run_id: str) -> RunView:
    chain, stages = run_lineage(conn, run_id)
    offset = len(SELECTED_STAGES) - len(stages)
    if offset < 0 or tuple(stages) != SELECTED_STAGES[offset:]:
        raise LoopError(f"run {run_id} selects {', '.join(stages) or 'no stages'}; the loop "
                        f"walks {', '.join(SELECTED_STAGES)} or a suffix of it")
    return RunView(run=run_id, chain=chain, offset=offset)


def _holder(conn, view: RunView, stage: str, unit_id: str) -> tuple[str, str] | None:
    """``(run, state)`` of the nearest run of the chain with the unit, or ``None``."""
    for run_id in view.chain:
        found = unit_state(conn, run_id, stage, unit_id)
        if found is not None:
            return run_id, found[0]
    return None


def _inherited(conn, view: RunView, stage: str, unit_id: str) -> bool:
    """The unit completed in a run this one was seeded from and has no row here."""
    if not view.seeded or unit_state(conn, view.run, stage, unit_id) is not None:
        return False
    found = _holder(conn, view, stage, unit_id)
    return found is not None and found[1] == "complete"


def _output(conn, view: RunView, stage: str, unit_id: str) -> str:
    """The unit's selected output, from the run of the chain holding it."""
    run_id = view.run
    if view.seeded:
        found = _holder(conn, view, stage, unit_id)
        if found is not None:
            run_id = found[0]
    return selected_output(conn, run_id, stage, unit_id)


def _producer_position(position: int) -> int:
    return next(p for p in reversed(range(position)) if SELECTED_STAGES[p] != "register")


def image_units(unit: str) -> list[tuple[int, str, str]]:
    """``(position, stage, unit id)`` of one detector image's chain; a
    ``register`` unit is ``<producing stage>/<unit>``."""
    units = []
    for position in IMAGE_CHAIN:
        stage = SELECTED_STAGES[position]
        if stage == "register":
            units.append((position, stage,
                          f"{SELECTED_STAGES[_producer_position(position)]}/{unit}"))
        else:
            units.append((position, stage, unit))
    return units


def _loads(conn, storage: Any, view: RunView, day: LoopDate
           ) -> tuple[dict[str, tuple[str, list[OutputEntry]]],
                      dict[str, list[tuple[str, OutputEntry]]]]:
    """Per detector image its load output and source sets; per maintain unit
    ``<yyyymmdd>/SCA<nn>`` the (load output, source set) pairs it maintains."""
    loads: dict[str, tuple[str, list[OutputEntry]]] = {}
    for image in day.detector_images:
        load_loc = _output(conn, view, "load", image.unit)
        entries = [o for o in storage.read_manifest(load_loc).outputs if o.kind == "source-set"]
        if not entries:
            raise LoopError(f"{load_loc}/manifest.json has no source-set entry")
        loads[image.unit] = (load_loc, entries)
    by_maintain: dict[str, list[tuple[str, OutputEntry]]] = {}
    for load_loc, entries in loads.values():
        for entry in entries:
            mu = maintain_unit_id(str(entry.registration.get("table", "")))
            by_maintain.setdefault(mu, []).append((load_loc, entry))
    return loads, by_maintain


def _fields(conn, loads: dict[str, tuple[str, list[OutputEntry]]], date: _dt.date,
            run_id: str) -> tuple[list[int], list[OutputEntry], dict[str, set[int]]]:
    """The date's fields, every source set of the date (image order, once
    each), and per image the fields its source sets have rows in. Each
    source set must be readable by run ``run_id`` (:func:`readable_result_set`,
    ruling R2: its own, or an inherited production seed's selected output)
    before its rows are read; one that is not is a :class:`LoopError`."""
    fields: set[int] = set()
    sources: dict[str, OutputEntry] = {}
    image_fields: dict[str, set[int]] = {}
    for unit, (_, entries) in loads.items():
        for entry in entries:
            if entry.instance not in sources:
                try:
                    readable_result_set(conn, entry.instance, run_id, "source-set")
                except ValueError as exc:
                    raise LoopError(f"date {date}: run {run_id} may not read the source set "
                                    f"{entry.instance} of {unit}: {exc}") from None
            sources.setdefault(entry.instance, entry)
            for f in source_set_fields(conn, str(entry.registration["table"]), entry.instance):
                fields.add(f)
                image_fields.setdefault(unit, set()).add(f)
    if not fields:
        raise LoopError(f"date {date}: the loaded source sets have no fields")
    return sorted(fields), list(sources.values()), image_fields


def incomplete_units(conn, storage: Any, view: RunView, day: LoopDate) -> list[str]:
    """Every unit the date's plan requires that is not ``complete`` in the
    run (or inherited complete from its seeds), as ``"<stage> <unit>
    (<state>|absent)"``: per image admit, register, difference, finalize,
    register, load; maintain; per field crossmatch, statistics, prune; per
    image alerts (Codex 7-2). Without every load, maintain's units and the
    fields cannot be known, and that is said instead."""
    missing: list[str] = []

    def check(stage: str, unit_id: str) -> None:
        found = _holder(conn, view, stage, unit_id)
        if found is None:
            missing.append(f"{stage} {unit_id} (absent)")
        elif found[1] != "complete":
            missing.append(f"{stage} {unit_id} ({found[1]})")

    for image in day.detector_images:
        for _, stage, unit_id in image_units(image.unit):
            check(stage, unit_id)
    if missing:
        missing.append("maintain, crossmatch, statistics and prune units (unknown until "
                       "every load is complete)")
    else:
        loads, by_maintain = _loads(conn, storage, view, day)
        for mu in by_maintain:
            check("maintain", mu)
        fields, _, _ = _fields(conn, loads, day.processing_date, view.run)
        for f in fields:
            for stage in ("crossmatch", "statistics", "prune"):
                check(stage, str(f))
    for image in day.detector_images:
        check("alerts", image.unit)
    return missing


def _phase(items: Sequence[Any], body: Callable[[Any], None]) -> None:
    """Walk every item of one phase; a failed item does not stop the others,
    but the phase then fails the date with every item's reason (Codex 7-2:
    a seeded re-run then holds every unit still to run). A timeout or any
    other error propagates at once."""
    failures: list[_Stop] = []
    for item in items:
        try:
            body(item)
        except _Stop as stop:
            if stop.code != EXIT_FAILED:
                raise
            failures.append(stop)
    if failures:
        raise _Stop(EXIT_FAILED, "; ".join(str(f) for f in failures),
                    jobless=next((f.jobless for f in failures if f.jobless), None))


# ----------------------------------------------------------------------
# One date
# ----------------------------------------------------------------------

def process_date(conn, spec: LoopSpec, day: LoopDate, tools: LoopTools, *,
                 interval: float, timeout: float) -> int:
    """Walk one date to complete or failed; return its exit code (0 or 1);
    :class:`_Stop` 75 on a timeout, the row left ``open``."""
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
        view = RunView(run=run_id, chain=(run_id,), offset=0)
    else:
        run_id = row.run
        record = dict(row.record)
        out(f"date={date} run={run_id} resumed")
        view = run_view(conn, run_id)
        state = run_state(conn, run_id)
        if state != "open":
            # A4: the run was finished before the row was. Codex 7-2: complete
            # the row only when every unit the date's plan requires is.
            missing = incomplete_units(conn, storage, view, day)
            if missing:
                return _fail_row(conn, spec, date, run_id, record, out, reason=(
                    f"run {run_id} is {state} but the date's units are not all complete: "
                    + ", ".join(missing)))
            record.setdefault("resumed_after_finish", True)
            return _finish_row(conn, spec, date, run_id, record, out)

    hint = f"rapidpipe loop run --spec {spec.location} --date {date}"

    def walk(unit_id: str, positions: list[int], *, inputs: Sequence[str] = (),
             settings: Sequence[str] = (), templates: Sequence[str] = ()) -> None:
        resolved = False
        while True:
            try:
                rc = tools.walk(conn, run_id=run_id, unit_id=unit_id, positions=positions,
                                inputs=list(inputs), settings=list(settings),
                                templates=list(templates), interval=interval,
                                timeout=timeout, continue_hint=hint)
                break
            except InputsRefused as exc:
                # R4: the launcher refused the unit's input manifest before
                # writing anything (``run start`` exits 65). The unit cannot
                # run, so it fails the date with the message, as a unit that
                # cannot be walked does, rather than ending the loop.
                conn.rollback()
                stages = ",".join(SELECTED_STAGES[p + view.offset] for p in positions)
                raise _Stop(EXIT_FAILED, f"{stages} {unit_id}: inputs refused: {exc}") from exc
            except Exception as exc:
                if getattr(exc, "code", None) != EXIT_USAGE:
                    raise
                conn.rollback()
                jobless = jobless_attempts(conn, run_id)
                if not jobless:
                    raise
                if resolved:
                    # A2: resolved once and still job-less; never wait on it.
                    raise _Stop(EXIT_FAILED, f"attempt {jobless[0]} is running with no "
                                             f"scheduler job: {exc}", jobless=jobless[0])
                # A2: step 6's resolver (run reconcile --resolve-jobless),
                # then one more walk: a found job is attached, a lost attempt
                # leaves its unit ready for the next one.
                results = launch_batch.resolve_jobless(
                    conn, run_id=run_id,
                    older_than_seconds=launch_batch.DEFAULT_JOBLESS_AFTER_SECONDS)
                record.setdefault("jobless_resolved", []).extend(
                    {"attempt": r.attempt_id, "status": r.batch_status, "job": r.job_id}
                    for r in results)
                out(f"date={date} run={run_id} resolved job-less attempts: "
                    + (", ".join(f"{r.attempt_id}={r.batch_status}" for r in results)
                       or "none resolvable yet"))
                resolved = True
        if rc != 0:
            stages = ",".join(SELECTED_STAGES[p + view.offset] for p in positions)
            raise _Stop(EXIT_FAILED, f"{stages} {unit_id} did not complete (exit {rc})")

    def position(absolute: int, stage: str, unit_id: str) -> int:
        """``absolute`` in the run's own stage list; a unit before the run's
        first stage that its seeds did not complete cannot run here."""
        if absolute < view.offset:
            raise _Stop(EXIT_FAILED, f"{stage} {unit_id} is not complete in the runs "
                                     f"{view.run} was seeded from ({', '.join(view.chain[1:])})"
                                     f", and {view.run} does not select it")
        return absolute - view.offset

    def inherited(stage: str, unit_id: str) -> bool:
        return _inherited(conn, view, stage, unit_id)

    def image_chain(image: DetectorImage) -> None:
        settings = [s for s in (image.admit_settings,) if s]
        difference_settings = ([f"difference={image.difference_settings}"]
                               if image.difference_settings else [])
        template = [f"difference={image.difference_template}"]
        if not view.seeded:
            walk(image.unit, IMAGE_CHAIN, inputs=[image.delivery],
                 settings=settings + difference_settings, templates=template)
            return
        # A seeded re-run: one position at a time, skipping what the seeds
        # completed; a seeded unit takes its seed attempt's inputs and
        # settings (step 6 R8), any other unit its producer's output.
        for absolute, stage, unit_id in image_units(image.unit):
            if inherited(stage, unit_id):
                continue
            here = [position(absolute, stage, unit_id)]
            found = unit_state(conn, view.run, stage, unit_id)
            if found is not None and found[1]:
                walk(image.unit, here)
            elif stage == "admit":
                walk(image.unit, here, inputs=[image.delivery], settings=settings)
            elif stage == "difference":
                # run start composes the template against admit's output in
                # this run or, inherited, in its (production) seed; no further.
                admit = _holder(conn, view, "admit", image.unit)
                if admit is None or admit[0] not in view.chain[:2]:
                    raise _Stop(EXIT_FAILED, f"difference {image.unit} reads an input set "
                                             "composed from admit's output, which is not in "
                                             f"{view.run} or its seed {view.chain[1]}")
                walk(image.unit, here, settings=difference_settings, templates=template)
            else:
                producer = _producer_position(absolute)
                producer_unit = next(u for p, _, u in image_units(image.unit) if p == producer)
                walk(image.unit, here, inputs=[_output(conn, view, SELECTED_STAGES[producer],
                                                       producer_unit)])

    try:
        # (b) the detector-image chain, admit..load, per image (A1: register
        # follows finalize only; load reads finalize's output).
        _phase(day.detector_images, image_chain)
        loads, by_maintain = _loads(conn, storage, view, day)

        # (c) maintain per <yyyymmdd>/SCA<nn>.
        def maintain(item: tuple[str, list[tuple[str, OutputEntry]]]) -> None:
            mu, items = item
            if inherited("maintain", mu):
                return
            here = [position(MAINTAIN, "maintain", mu)]
            if len({loc for loc, _ in items}) == 1:
                inputs = items[0][0]
            else:
                inputs = _bind_and_write(
                    conn, storage, run_id=run_id, stage="maintain", unit_id=mu,
                    dest=f"{tools.inputs_root(run_id)}/maintain/{mu}",
                    manifest=input_set_manifest(
                        run_id, Unit(kind="detector-date", id=mu), [e for _, e in items],
                        [e.instance for _, e in items]))
            walk(mu, here, inputs=[inputs])

        _phase(list(by_maintain.items()), maintain)

        # (d) per field: crossmatch (every source set of the date + the
        # field's base) -> statistics -> prune.
        fields, sources, image_fields = _fields(conn, loads, date, run_id)
        previous = previous_complete_rows(conn, schedule, date)
        association: dict[int, str] = {}
        statistics: dict[int, str] = {}
        pruned: dict[int, str] = {}
        bases: dict[str, str | None] = {}
        base_detail: dict[str, Any] = {}
        bases_skipped: dict[str, list[dict[str, str]]] = {}

        def field_chain(f: int) -> None:
            unit = str(f)
            skipped: list[dict[str, str]] = []
            base = base_for_field(conn, storage, previous, f, run_id, skipped)
            if skipped:
                bases_skipped[unit] = skipped
            bases[unit] = base.entry.instance if base is not None else None
            base_detail[unit] = None if base is None else {
                "run": base.run, "processing_date": str(base.processing_date),
                "base_promoted": base.promoted}
            if not inherited("crossmatch", unit):
                here = [position(CROSSMATCH, "crossmatch", unit)]
                xm_inputs = _bind_and_write(
                    conn, storage, run_id=run_id, stage="crossmatch", unit_id=unit,
                    dest=f"{tools.inputs_root(run_id)}/crossmatch/{f}",
                    manifest=crossmatch_inputs(run_id, f, sources,
                                               base.entry if base is not None else None))
                walk(unit, here, inputs=[xm_inputs])
            xm_out = _output(conn, view, "crossmatch", unit)
            sets = [o.instance for o in storage.read_manifest(xm_out).outputs
                    if o.kind == "association-set"]
            if len(sets) != 1:
                raise LoopError(f"{xm_out}/manifest.json has {len(sets)} association sets")
            association[f] = sets[0]
            if not inherited("statistics", unit):
                walk(unit, [position(STATISTICS, "statistics", unit)], inputs=[xm_out])
            st_out = _output(conn, view, "statistics", unit)
            st_sets = [o.instance for o in storage.read_manifest(st_out).outputs
                       if o.kind == "statistics-set"]
            if len(st_sets) > 1:
                raise LoopError(f"{st_out}/manifest.json has {len(st_sets)} statistics sets")
            if st_sets:
                statistics[f] = st_sets[0]
            if not inherited("prune", unit):
                walk(unit, [position(PRUNE, "prune", unit)], inputs=[xm_out])
            # R5: the field's prune output (the selected attempt's pruned set)
            # binds to every alerts input set naming this field.
            pr_out = _output(conn, view, "prune", unit)
            pruned[f] = field_pruned_set(storage.read_manifest(pr_out).outputs, pr_out,
                                         association[f])

        _phase(fields, field_chain)

        # (e) alerts per detector image (A5, stages/alerts.py's input rules).
        alerts: dict[str, dict[str, Any]] = {}

        def image_alerts(image: DetectorImage) -> None:
            if not inherited("alerts", image.unit):
                here = [position(ALERTS, "alerts", image.unit)]
                fin_loc = _output(conn, view, "finalize", image.unit)
                fin = storage.read_manifest(fin_loc)
                diffs = [o for o in fin.outputs if o.kind == "difference-image"]
                if len(diffs) != 1:
                    raise LoopError(f"{fin_loc}/manifest.json has {len(diffs)} "
                                    "difference-image entries; expected one")
                own_sources = alert_source_set(loads[image.unit][1], diffs[0].instance)
                template = storage.read_manifest(image.difference_template)
                refcats = [o for o in template.outputs if o.kind == "reference-catalog"]
                if len(refcats) > 1:
                    raise LoopError(f"{image.difference_template}/manifest.json has "
                                    f"{len(refcats)} reference-catalog entries")
                dest = f"{tools.inputs_root(run_id)}/alerts/{image.unit}"
                own_fields = sorted(image_fields.get(image.unit, ()))
                result_sets = alert_result_sets(
                    own_sources, [association[f] for f in own_fields],
                    [statistics[f] for f in own_fields if f in statistics],
                    [pruned[f] for f in own_fields])
                manifest = None
                if not storage.exists(parse_location(dest), "manifest.json"):
                    _copy_members(storage, fin_loc, diffs[0], dest)
                    for refcat in refcats:
                        _copy_members(storage, image.difference_template, refcat, dest)
                    manifest = input_set_manifest(run_id, fin.unit, [diffs[0], *refcats],
                                                  result_sets)
                _bind_and_write(conn, storage, run_id=run_id, stage="alerts",
                                unit_id=image.unit, dest=dest, manifest=manifest)
                walk(image.unit, here, inputs=[dest])
            al_out = _output(conn, view, "alerts", image.unit)
            containers = [o.instance for o in storage.read_manifest(al_out).outputs
                          if o.kind == "alert-container"]
            alerts[image.unit] = {"instance": containers[0] if containers else None,
                                  "location": al_out}

        _phase(day.detector_images, image_alerts)
    except _Stop as stop:
        if stop.code == EXIT_TIMEOUT:
            raise
        fields_failed: dict[str, Any] = {"failure": str(stop)}
        if stop.jobless:
            fields_failed["jobless_attempt"] = stop.jobless
        return _fail_row(conn, spec, date, run_id, record, out, **fields_failed)
    except Exception as exc:
        if getattr(exc, "code", None) == EXIT_TIMEOUT:
            conn.rollback()
            out(f"date={date} run={run_id} state=timeout")
            raise _Stop(EXIT_TIMEOUT, str(exc)) from exc
        raise

    record.update(fields=fields, base_sets=bases, bases=base_detail,
                  association_sets={str(f): i for f, i in association.items()},
                  statistics_sets={str(f): i for f, i in statistics.items()},
                  pruned_sets={str(f): i for f, i in pruned.items()},
                  alerts=alerts)
    if bases_skipped:
        record["bases_skipped"] = bases_skipped
    return _finish_row(conn, spec, date, run_id, record, out)


#: Keys of a failed row's record that describe the failed run, moved to
#: ``previous_failures`` when ``--retry-failed`` repoints the row.
_FAILURE_KEYS = ("failure", "reason", "jobless_attempt")


def reopenable(conn, row: LoopRow) -> bool:
    """A ``failed`` row whose run can simply be resumed (step 9, R4
    follow-up): the run is still ``open`` and has no ``failed`` or
    ``cancelled`` unit -- the date failed on a refusal (an input manifest
    refused before submission, or a later stage's composition refused with
    every existing unit complete), not on a unit. A row whose run has a
    failed unit takes ``--retry-failed``'s seeded path instead; a finished
    run cannot take new units, so its row is not reopened either."""
    if row.state != "failed" or run_state(conn, row.run) != "open":
        return False
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM units WHERE run = %s AND state IN ('failed', 'cancelled') "
                    "LIMIT 1", (row.run,))
        return cur.fetchone() is None


def reopen_date(conn, spec: LoopSpec, row: LoopRow, tools: LoopTools) -> None:
    """Reopen a :func:`reopenable` row on the same run: ``open`` again, its
    failure moved to ``previous_failures`` and the reopen's time appended to
    ``record.reopened``, committed; the caller then resumes the date."""
    record = dict(row.record)
    failed = {k: record.pop(k) for k in _FAILURE_KEYS if k in record}
    record.pop("units", None)
    record.setdefault("previous_failures", []).append({"run": row.run, **failed})
    record["reopened"] = [*record.get("reopened", []),
                          _dt.datetime.now(_dt.timezone.utc).isoformat()]
    repoint_row(conn, spec.schedule, row.processing_date, row.run, record)
    conn.commit()
    tools.out(f"date={row.processing_date} run={row.run} reopened (failed with no failed "
              "units; resuming the same run)")


def retry_date(conn, spec: LoopSpec, row: LoopRow, tools: LoopTools) -> int:
    """``--retry-failed`` on a ``failed`` row (A4, Codex 7-2): create a run
    seeded from the row's run through step 6's ``run create --seed <run>
    --only-failed`` path (``tools.create_seeded_run``), repoint the row to it
    (``open``), append the old run to ``record.previous_runs``, and commit
    both together; the caller then resumes the date as usual. A refusal
    (nothing to re-run, a deleting seed) leaves the row ``failed`` with the
    message as ``record.reason`` and returns 1."""
    date, out = row.processing_date, tools.out
    record = dict(row.record)
    if tools.create_seeded_run is None:
        raise LoopError("--retry-failed needs run create's --only-failed path")
    try:
        new_run = tools.create_seeded_run(conn, row.run)
    except repository.RunModelError as exc:
        conn.rollback()
        record["reason"] = f"--retry-failed: {exc}"
        _update_row(conn, spec.schedule, date, state="failed", promotion=None, record=record)
        conn.commit()
        out(f"date={date} run={row.run} state=failed reason={record['reason']}")
        return EXIT_FAILED
    failed = {k: record.pop(k) for k in _FAILURE_KEYS if k in record}
    record.pop("units", None)  # the old run's; the new run's are recorded at the end
    record.setdefault("previous_failures", []).append({"run": row.run, **failed})
    record["previous_runs"] = [*record.get("previous_runs", []), row.run]
    record["run"] = new_run
    repoint_row(conn, spec.schedule, date, new_run, record)
    conn.commit()
    out(f"date={date} run={new_run} reopened (--retry-failed, seeded from {row.run})")
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
             dry_run: bool = False, interval: float = 30.0, timeout: float = 14400.0,
             retry_failed: bool = False) -> int:
    """``loop run`` (R3): every selected date whose row is absent or ``open``
    (or ``failed``, with ``retry_failed``), in spec order, under the
    schedule's advisory lock. 0 when all are complete, 1 at the first failed
    date, 75 on a timeout or when another loop holds the schedule."""
    chosen = _selected_dates(spec, dates)
    if dry_run:
        plan(conn, spec, tools, dates=[d.processing_date for d in chosen])
        return EXIT_OK
    if not try_lock(conn, spec.schedule):
        tools.out(f"another loop holds schedule {spec.schedule}")
        return EXIT_TIMEOUT
    try:
        for day in chosen:
            row = loop_row(conn, spec.schedule, day.processing_date)
            if row is not None and reopenable(conn, row):
                reopen_date(conn, spec, row, tools)
            elif row is not None and row.state == "failed" and retry_failed:
                code = retry_date(conn, spec, row, tools)
                if code != EXIT_OK:
                    return code
            elif row is not None and row.state != "open":
                tools.out(f"date={day.processing_date} run={row.run} state={row.state} "
                          "(skipped)")
                if row.state == "failed":
                    tools.out(f"date={day.processing_date}: failed; stopping before later "
                              "dates (--retry-failed re-runs its failed units)")
                    return EXIT_FAILED
                continue
            try:
                code = process_date(conn, spec, day, tools, interval=interval,
                                    timeout=timeout)
            except _Stop as stop:
                tools.out(f"timeout: {stop}")
                return stop.code
            if code != EXIT_OK:
                return code
        return EXIT_OK
    finally:
        try:
            conn.rollback()
            unlock(conn, spec.schedule)
        except Exception:  # noqa: BLE001 - the session ends with the connection anyway
            pass


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
        elif reopenable(conn, row):
            action, run = "reopen", row.run
        else:
            action, run = f"skip ({row.state})", row.run
        previous_rows = previous_complete_rows(conn, spec.schedule, day.processing_date)
        previous = previous_rows[0] if previous_rows else None
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
