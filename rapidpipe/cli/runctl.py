"""``rapidpipe run start|status|inputs|compare|expire``: run management.

The Batch-level run management the specification's "Tools" section names,
over the one-attempt primitives ``rapidpipe.cli.main`` already has
(``run submit``, ``run reconcile``):

- ``start`` -- walk a run's selected stages for one unit: skip what is
  complete, submit what is not (``rapidpipe.launch.batch.submit_unit``),
  wait for each attempt by polling ``reconcile``, and move on;
- ``status`` -- reconcile, then one line per unit;
- ``inputs`` -- compose an input set: a template input-set manifest's
  reference entries plus one producer attempt's output entry, copied into
  one prefix (manifest member paths are relative and contained, so an
  input set must live under one prefix);
- ``compare`` -- two runs side by side;
- ``expire`` -- ``rapidpipe.runs.cleanup.expire_runs``.

Database connections: every command here opens its connection through
``rapidpipe.cli.main.connect``, looked up at call time
(``from rapidpipe.cli import main as _main; _main.connect(...)``), never
through a name bound at import. A test that monkeypatches
``rapidpipe.cli.main.connect`` therefore covers these commands too, and
this module never imports ``main`` at load time (``main`` imports this
module).

The SQL each command reads is kept in small module-level functions
(:func:`_run_row`, :func:`_unit_row`, :func:`_status_rows`,
:func:`_compare_units`, :func:`_compare_instances`,
:func:`_registered_instances`) so the database-free unit tests can
monkeypatch them; ``sleep`` and ``now`` are module-level indirections for
the same reason.

Exit codes, as for the rest of ``rapidpipe run``: 0 success; 64 a usage
error or a refusal (any ``RunModelError``); 75 transient (an AWS-shaped
error, the database unavailable, or ``start``'s ``--timeout``); 1 a unit
failed. ``status`` also exits 2 when something is still running.
"""

from __future__ import annotations

import argparse
import shlex
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from rapidpipe.db.connection import ConnectionConfigError, ConnectionUnavailable
from rapidpipe.launch import batch as launch_batch
from rapidpipe.launch.batch import (
    DependencyIncomplete,
    MissingEnvironmentVariable,
    ReleaseDefinitionRefused,
)
from rapidpipe.products.manifest import Manifest, ManifestError, Member, OutputEntry, Unit
from rapidpipe.products.storage import Location, LocationError, join, parse_location
from rapidpipe.runs.repository import RunModelError
from rapidpipe.stages.contract import STAGE_NAMES, ExitCode

#: Indirections for tests: ``start --interval`` / ``status --watch`` sleep
#: through ``sleep``; ``start --timeout`` measures with ``now``.
sleep: Callable[[float], None] = time.sleep
now: Callable[[], float] = time.monotonic

COMMANDS = ("start", "status", "inputs", "compare", "expire")

_TERMINAL_UNIT_STATES = ("complete", "failed", "cancelled")
_STATUS_STILL_RUNNING = 2


class _Exit(Exception):
    """Stop the command with ``code``, printing ``message`` to stderr."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


# ======================================================================
# Parsers
# ======================================================================

def _iso8601(text: str) -> datetime:
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 timestamp: {text!r}") from exc


def _positive_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number of seconds: {text!r}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be positive: {text!r}")
    return value


def add_parsers(run_subparsers: Any) -> None:
    """Add ``start``, ``status``, ``inputs``, ``compare`` and ``expire``
    beneath ``rapidpipe run``'s subparsers action."""
    start = run_subparsers.add_parser(
        "start",
        help="Run a unit through the run's selected stages on Batch, waiting "
             "for each attempt.",
        description="Walk the run's selected stages in order (or only --stage) "
                    "for one unit: skip a complete stage, submit the next "
                    "attempt of any other, and wait for it by reconciling "
                    "every --interval seconds. A stage's inputs are, in "
                    "order: --inputs <stage>=<loc> (an unprefixed --inputs "
                    "is the first stage's), an input set composed from "
                    "--template <stage>=<loc>, else the selected output of "
                    "the nearest preceding non-register stage. A unit a "
                    "transient or lost attempt returns to ready gets another "
                    "attempt, within the run's allowance. Exit 0 when every "
                    "stage is complete, 1 when a unit is failed or cancelled, "
                    "75 on --timeout (rerun the printed command to continue).")
    start.add_argument("run_id", help="The run.")
    start.add_argument(
        "--unit", required=True, dest="unit_id",
        help="The unit id (register's own unit id is derived from the manifest "
             "it reads).")
    start.add_argument(
        "--stage", default=None,
        help="Only this stage: its first occurrence in the run's selected "
             "stages that is not yet complete.")
    start.add_argument(
        "--inputs", action="append", default=[], metavar="[STAGE=]LOC",
        help="A stage's input location; unprefixed applies to the first stage "
             "only. Repeatable.")
    start.add_argument(
        "--settings", action="append", default=[], metavar="[STAGE=]LOC",
        help="A stage's settings file; unprefixed applies to the first stage "
             "only. Repeatable.")
    start.add_argument(
        "--template", action="append", default=[], metavar="STAGE=LOC",
        help="Compose STAGE's input set from this template input-set manifest "
             "and the preceding stage's output (see 'run inputs'). Repeatable.")
    start.add_argument(
        "--no-wait", action="store_true",
        help="Submit the first stage that needs it, print the command that "
             "continues, and exit 0.")
    start.add_argument(
        "--interval", type=_positive_seconds, default=30.0,
        help="Seconds between reconcile polls (default 30).")
    start.add_argument(
        "--timeout", type=_positive_seconds, default=14400.0,
        help="Seconds to wait for one attempt before exiting 75 (default 14400).")

    status = run_subparsers.add_parser(
        "status", help="Reconcile a run, then print one line per unit.",
        description="Reconcile the run's unresolved Batch attempts, then print "
                    "stage, unit, state, selected attempt, last attempt, last "
                    "job and its disposition for every unit. Exit 0 when "
                    "every unit is complete, 1 when any failed or was "
                    "cancelled, 2 when something is still running or the run "
                    "has no units.")
    status.add_argument("run_id", help="The run.")
    status.add_argument(
        "--watch", action="store_true",
        help="Repeat every --interval seconds until every unit is terminal.")
    status.add_argument(
        "--interval", type=_positive_seconds, default=30.0,
        help="Seconds between --watch polls (default 30).")

    inputs = run_subparsers.add_parser(
        "inputs", help="Compose a unit's input set from a template and a producer's output.",
        description="Copy a template input-set manifest's entries and one "
                    "entry of the producer unit's selected output (--kind, "
                    "default l2-image, under l2/) into one prefix, verify the "
                    "copied sizes, write its manifest.json, bind the unit's "
                    "inputs, and print inputs=<dest>. Refuses to overwrite an "
                    "existing <dest>/manifest.json.")
    inputs.add_argument("run_id", help="The run.")
    inputs.add_argument("stage", choices=STAGE_NAMES, help="The stage that will read the input set.")
    inputs.add_argument("--unit", required=True, dest="unit_id", help="The unit id.")
    inputs.add_argument(
        "--from-stage", required=True, dest="from_stage", choices=STAGE_NAMES,
        help="The producing stage whose selected attempt supplies the entry.")
    inputs.add_argument(
        "--template", required=True,
        help="Location (local or s3://) of the template input set's manifest.json.")
    inputs.add_argument(
        "--dest", default=None,
        help="Where to write the input set (default: <scratch outputs root>"
             "/runs/<run>/inputs/<stage>/<unit>, for every run kind: an input "
             "set is a staged working copy, not a product). Must be under "
             "<scratch outputs root>/runs/<run>/inputs/.")
    inputs.add_argument(
        "--kind", default="l2-image",
        help="The producer output entry kind to take (default l2-image).")

    compare = run_subparsers.add_parser(
        "compare", help="Compare two runs: dispositions, products, settings.",
        description="Print, per stage and unit, the two runs' dispositions and "
                    "settings hashes; per product kind and logical key, each "
                    "run's instance ids; the runs' settings overlays; then "
                    "'same' or 'different'. Exit 0 same, 1 different, 64 a "
                    "run not found.")
    compare.add_argument("run_a", help="The first run.")
    compare.add_argument("run_b", help="The second run.")

    expire = run_subparsers.add_parser(
        "expire", help="Delete every expired, unpinned scratch run.",
        description="Delete every scratch run past its expires_at that is not "
                    "pinned, printing one deletion report per run. Honours "
                    "RAPIDPIPE_CLEANUP_ROLE_ARN.")
    expire.add_argument(
        "--now", type=_iso8601, default=None,
        help="Treat this ISO 8601 time as now (default: the database's now()).")


# ======================================================================
# Shared plumbing
# ======================================================================

def _main_module():
    from rapidpipe.cli import main as _main

    return _main


def _connect(command: str):
    """``rapidpipe.cli.main.connect`` for ``run <command>``, or :class:`_Exit`."""
    try:
        return _main_module().connect(application_name=f"rapidpipe-run-{command}")
    except ConnectionConfigError as exc:
        raise _Exit(int(ExitCode.USAGE), f"database configuration error: {exc}") from exc
    except ConnectionUnavailable as exc:
        raise _Exit(int(ExitCode.TRANSIENT_FAILURE), f"database unavailable: {exc}") from exc


def _with_connection(command: str, body: Callable[[Any], int]) -> int:
    """Connect, run ``body(conn)``, and map exceptions to exit codes.

    ``body`` commits what it wants kept; anything raised rolls back.
    """
    try:
        cm = _connect(command)
    except _Exit as exc:
        sys.stderr.write(f"rapidpipe run {command}: {exc}\n")
        return exc.code

    main = _main_module()
    with cm as conn:
        try:
            return body(conn)
        except _Exit as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run {command}: {exc}\n")
            return exc.code
        except (RunModelError, DependencyIncomplete, MissingEnvironmentVariable,
                main.RegisterUnitIdError, LocationError, ManifestError) as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run {command}: {exc}\n")
            return int(ExitCode.USAGE)
        except ReleaseDefinitionRefused as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run {command}: {exc}\n")
            return 1
        except Exception as exc:  # noqa: BLE001 - AWS/botocore-shaped errors
            if main._is_batch_error(exc):
                conn.rollback()
                sys.stderr.write(f"rapidpipe run {command}: AWS error: {exc}\n")
                return int(ExitCode.TRANSIENT_FAILURE)
            conn.rollback()
            raise


@dataclass(frozen=True)
class RunRow:
    kind: str
    selected_stages: list[str]
    state: str
    settings_overlay_ref: str | None


@dataclass(frozen=True)
class UnitRow:
    """A unit's state and its most recent attempt."""

    state: str
    selected_attempt: str | None
    last_attempt: str | None
    last_disposition: str | None
    last_job: str | None
    last_output: str | None


def _run_row(conn, run_id: str) -> RunRow | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, selected_stages, state, settings_overlay_ref "
            "FROM runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
    if row is None:
        return None
    return RunRow(row[0], list(row[1] or []), row[2], row[3])


def _unit_row(conn, run_id: str, stage: str, unit_id: str) -> UnitRow | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.state, u.selected_attempt,
                   a.id, a.disposition, a.scheduler_job_id, a.output_location
            FROM units u
            LEFT JOIN LATERAL (
                SELECT id, disposition, scheduler_job_id, output_location
                FROM attempts WHERE unit = u.id
                ORDER BY started DESC, id DESC LIMIT 1) a ON true
            WHERE u.run = %s AND u.stage = %s AND u.unit_id = %s
            """,
            (run_id, stage, unit_id))
        row = cur.fetchone()
    return None if row is None else UnitRow(*row)


def _require_run(conn, run_id: str) -> RunRow:
    row = _run_row(conn, run_id)
    if row is None:
        raise _Exit(int(ExitCode.USAGE), f"no such run: {run_id}")
    return row


def _declaration(stage: str) -> Any:
    import importlib

    module_name = f"rapidpipe.stages.{stage.replace('-', '_')}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise _Exit(int(ExitCode.USAGE),
                    f"{stage!r} is a known stage name but {module_name} is not "
                    "implemented yet") from exc
    declaration = getattr(module, "DECLARATION", None)
    if declaration is None:
        raise _Exit(int(ExitCode.USAGE), f"{module_name} has no DECLARATION")
    return declaration


def _reconcile(conn, run_id: str) -> list[Any]:
    return launch_batch.reconcile(conn, run_id=run_id)


# ======================================================================
# Storage helpers (local directories and s3:// prefixes alike)
# ======================================================================

def _s3_key(location: Location, relative: str) -> str:
    return f"{location.prefix}/{relative}" if location.prefix else relative


def _is_not_found(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    code = (response or {}).get("Error", {}).get("Code") if isinstance(response, dict) else None
    return code in ("404", "NoSuchKey", "NotFound")


class _Storage:
    """Copy, size and existence checks across local and S3 locations.

    The S3 client is made on first use only (``rapidpipe.products.storage
    .s3_client``, which tests monkeypatch), so a purely local composition
    never imports boto3.
    """

    def __init__(self, client: Any = None):
        self._client = client

    @property
    def s3(self) -> Any:
        if self._client is None:
            from rapidpipe.products import storage

            self._client = storage.s3_client()
        return self._client

    def exists(self, location: Location, relative: str) -> bool:
        if not location.is_s3():
            return (location.path / relative).exists()
        try:
            self.s3.head_object(Bucket=location.bucket, Key=_s3_key(location, relative))
        except Exception as exc:  # noqa: BLE001 - ClientError-shaped
            if _is_not_found(exc):
                return False
            raise
        return True

    def size(self, location: Location, relative: str) -> int:
        if not location.is_s3():
            return (location.path / relative).stat().st_size
        head = self.s3.head_object(Bucket=location.bucket, Key=_s3_key(location, relative))
        return int(head["ContentLength"])

    def copy(self, src: Location, src_rel: str, dst: Location, dst_rel: str) -> None:
        if src.is_s3() and dst.is_s3():
            # Server-side; copy_object handles objects up to 5 GB.
            self.s3.copy_object(
                Bucket=dst.bucket, Key=_s3_key(dst, dst_rel),
                CopySource={"Bucket": src.bucket, "Key": _s3_key(src, src_rel)})
        elif src.is_s3():
            target = dst.path / dst_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            self.s3.download_file(src.bucket, _s3_key(src, src_rel), str(target))
        elif dst.is_s3():
            self.s3.upload_file(str(src.path / src_rel), dst.bucket, _s3_key(dst, dst_rel))
        else:
            target = dst.path / dst_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src.path / src_rel, target)

    def read_manifest(self, location_text: str) -> Manifest:
        location = parse_location(location_text)
        try:
            if not location.is_s3():
                return Manifest.read(location.path / "manifest.json")
            with tempfile.TemporaryDirectory(prefix="rapidpipe-run-inputs-") as tmp:
                path = Path(tmp) / "manifest.json"
                try:
                    self.s3.download_file(
                        location.bucket, _s3_key(location, "manifest.json"), str(path))
                except Exception as exc:  # noqa: BLE001 - ClientError-shaped
                    if _is_not_found(exc):
                        raise FileNotFoundError(path) from exc
                    raise
                return Manifest.read(path)
        except FileNotFoundError as exc:
            raise _Exit(int(ExitCode.USAGE),
                        f"no manifest.json at {location_text}") from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise _Exit(int(ExitCode.USAGE),
                        f"{location_text}/manifest.json: invalid manifest: {exc}") from exc

    def write_manifest(self, manifest: Manifest, location: Location) -> None:
        if not location.is_s3():
            location.path.mkdir(parents=True, exist_ok=True)
            manifest.write(location.path / "manifest.json")
            return
        with tempfile.TemporaryDirectory(prefix="rapidpipe-run-inputs-") as tmp:
            path = Path(tmp) / "manifest.json"
            manifest.write(path)
            self.s3.upload_file(str(path), location.bucket, _s3_key(location, "manifest.json"))


# ======================================================================
# run inputs
# ======================================================================

def _registered_instances(conn, instance_ids: list[str]) -> list[str]:
    """Those of ``instance_ids`` that exist in ``product_instances`` --
    the only ones ``unit_inputs.producer_instance`` (a foreign key) can
    bind. A template's entries may carry instance ids nothing registered."""
    if not instance_ids:
        return []
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM product_instances WHERE id = ANY(%s)", (instance_ids,))
        found = {row[0] for row in cur.fetchall()}
    return [i for i in instance_ids if i in found]


def inputs_root(run_id: str) -> str:
    """``<scratch outputs root>/runs/<run>/inputs`` -- for every run kind.

    An input set is a staged working copy, not a product, so it always
    lives under the scratch root (``RAPIDPIPE_OUTPUTS_ROOT_SCRATCH``, else
    ``RAPIDPIPE_OUTPUTS_ROOT``), never the production root: the launcher's
    role can read the products bucket but not write it. ``run delete``
    removes this prefix with the run (``rapidpipe.runs.cleanup``).
    """
    root = launch_batch.outputs_root_for("scratch")
    return join(parse_location(root), f"runs/{run_id}/inputs")


def default_inputs_dest(run_id: str, stage: str, unit_id: str) -> str:
    """``<scratch outputs root>/runs/<run>/inputs/<stage>/<unit>``."""
    return f"{inputs_root(run_id)}/{stage}/{unit_id}"


def _require_under_inputs_root(dest: str, run_id: str) -> None:
    """Refuse (64) a ``--dest`` outside ``<scratch root>/runs/<run>/inputs/``."""
    root = inputs_root(run_id)
    if parse_location(root).is_s3():
        inside = dest.rstrip("/").startswith(root + "/")
    else:
        root_path = Path(root).resolve()
        dest_path = Path(dest).resolve()
        inside = dest_path != root_path and root_path in dest_path.parents
    if not inside:
        raise _Exit(int(ExitCode.USAGE),
                    f"--dest {dest} is not under {root}/; an input set lives under "
                    "the scratch outputs root's runs/<run>/inputs/")


def _rehome_under_l2(entry: OutputEntry) -> tuple[OutputEntry, list[tuple[str, str]]]:
    """``entry`` with every member path moved to ``l2/<basename>``, and the
    (source path, new path) pairs to copy."""
    moves: list[tuple[str, str]] = []
    members = []
    for member in entry.members:
        new_path = f"l2/{PurePosixPath(member.path).name}"
        moves.append((member.path, new_path))
        members.append(Member(role=member.role, path=new_path,
                              bytes=member.bytes, sha256=member.sha256))
    if len({new for _, new in moves}) != len(moves):
        raise _Exit(int(ExitCode.USAGE),
                    f"output {entry.instance!r} has two members with the same file "
                    "name; cannot place them under l2/")
    primary = None if entry.primary is None else f"l2/{PurePosixPath(entry.primary).name}"
    return OutputEntry(
        kind=entry.kind, format_version=entry.format_version, instance=entry.instance,
        key=dict(entry.key), members=tuple(members), primary=primary,
        registration=dict(entry.registration)), moves


def compose_inputs(
    conn,
    *,
    run_id: str,
    stage: str,
    unit_id: str,
    from_stage: str,
    template: str,
    dest: str | None = None,
    kind: str = "l2-image",
    reuse_existing: bool = False,
    storage: _Storage | None = None,
) -> str:
    """Compose ``stage``'s input set for ``unit_id``; return its location.

    Reads ``<template>/manifest.json`` and the producer unit's selected
    attempt's manifest (``resolve_inputs_from_stage``), takes the
    producer's one output entry of ``kind``, and copies it (under ``l2/``)
    and every template entry of any other kind into ``dest``, checking
    each copied size against its manifest. Then binds the unit's inputs
    to whichever entries are registered product instances and commits,
    and only then writes ``<dest>/manifest.json`` (stage ``input-set``,
    the producer's unit kind and attempt, the template's inputs and
    execution record). The manifest is the last write, so an existing
    manifest always means the bindings were committed once.

    An existing ``<dest>/manifest.json`` is refused (exit 64), or with
    ``reuse_existing`` (``run start``, rerun after an interruption)
    reused: its registered instances are re-bound (idempotent) and
    committed, so a reuse always leaves the unit bound.
    """
    storage = storage or _Storage()
    _require_run(conn, run_id)
    dest_text = dest or default_inputs_dest(run_id, stage, unit_id)
    _require_under_inputs_root(dest_text, run_id)
    dest_loc = parse_location(dest_text)
    declaration = _declaration(stage)
    from rapidpipe.runs.repository import add_unit, bind_unit_inputs

    if storage.exists(dest_loc, "manifest.json"):
        if not reuse_existing:
            raise _Exit(int(ExitCode.USAGE),
                        f"refusing to overwrite {join(dest_loc, 'manifest.json')}")
        existing = storage.read_manifest(dest_text)
        add_unit(conn, run_id, stage, declaration.unit, unit_id)
        bound = _registered_instances(conn, [o.instance for o in existing.outputs])
        bind_unit_inputs(conn, run_id, stage, unit_id, bound)
        conn.commit()
        print(f"inputs={dest_text} (already composed)", flush=True)
        return dest_text

    template_manifest = storage.read_manifest(template)
    producer_text = launch_batch.resolve_inputs_from_stage(
        conn, run_id=run_id, unit_id=unit_id, upstream_stage=from_stage)
    producer = storage.read_manifest(producer_text)
    candidates = [o for o in producer.outputs if o.kind == kind]
    if len(candidates) != 1:
        raise _Exit(int(ExitCode.USAGE),
                    f"{producer_text}/manifest.json has {len(candidates)} output(s) of "
                    f"kind {kind!r}; expected exactly one")
    producer_entry, moves = _rehome_under_l2(candidates[0])

    # Admission fence first (idempotent), so a finished run is refused
    # before anything is copied.
    add_unit(conn, run_id, stage, declaration.unit, unit_id)

    template_loc = parse_location(template)
    producer_loc = parse_location(producer_text)
    copies: list[tuple[Location, str, str, int]] = [
        (producer_loc, src, new, member.bytes)
        for (src, new), member in zip(moves, producer_entry.members)]
    kept_entries = [o for o in template_manifest.outputs if o.kind != kind]
    for entry in kept_entries:
        for member in entry.members:
            copies.append((template_loc, member.path, member.path, member.bytes))

    for src_loc, src_rel, dst_rel, expected in copies:
        storage.copy(src_loc, src_rel, dest_loc, dst_rel)
        actual = storage.size(dest_loc, dst_rel)
        if actual != expected:
            raise _Exit(1, f"copied {join(dest_loc, dst_rel)} is {actual} bytes; its "
                           f"manifest says {expected}")

    manifest = Manifest(
        run=run_id,
        unit=Unit(kind=producer.unit.kind, id=unit_id),
        stage="input-set",
        attempt=producer.attempt,
        execution_record=template_manifest.execution_record or "exec/input-set.json",
        inputs=template_manifest.inputs,
        outputs=(producer_entry, *kept_entries),
    )
    instances = [o.instance for o in manifest.outputs]
    bound = _registered_instances(conn, instances)
    bind_unit_inputs(conn, run_id, stage, unit_id, bound)
    conn.commit()
    # Last: a manifest on disk means the bindings above were committed.
    storage.write_manifest(manifest, dest_loc)
    print(f"inputs={dest_text}", flush=True)
    return dest_text


def _inputs_command(args: argparse.Namespace) -> int:
    def body(conn) -> int:
        compose_inputs(
            conn, run_id=args.run_id, stage=args.stage, unit_id=args.unit_id,
            from_stage=args.from_stage, template=args.template, dest=args.dest,
            kind=args.kind)
        return int(ExitCode.SUCCESS)

    return _with_connection("inputs", body)


# ======================================================================
# run start
# ======================================================================

def _split_keyed(values: Iterable[str], flag: str, *, allow_unprefixed: bool = True,
                 ) -> tuple[str | None, dict[str, str]]:
    """``[<stage>=]<loc>`` values -> (the unprefixed one, {stage: loc}).

    Only a known stage name before the first ``=`` makes a prefix, so a
    location that itself contains ``=`` is still an unprefixed location.
    """
    unprefixed: str | None = None
    keyed: dict[str, str] = {}
    for value in values:
        head, sep, tail = value.partition("=")
        if sep and head in STAGE_NAMES:
            if head in keyed:
                raise _Exit(int(ExitCode.USAGE), f"{flag} given twice for stage {head!r}")
            keyed[head] = tail
            continue
        if not allow_unprefixed:
            raise _Exit(int(ExitCode.USAGE),
                        f"{flag} {value!r}: expected <stage>=<location>, <stage> one of "
                        f"{', '.join(STAGE_NAMES)}")
        if unprefixed is not None:
            raise _Exit(int(ExitCode.USAGE),
                        f"{flag} given twice without a <stage>= prefix")
        unprefixed = value
    return unprefixed, keyed


def _continue_command(args: argparse.Namespace) -> str:
    """The ``run start`` command that picks up where this one stopped
    (without ``--no-wait``: it waits for the attempt in flight and goes on)."""
    parts = ["rapidpipe", "run", "start", args.run_id, "--unit", args.unit_id]
    if args.stage:
        parts += ["--stage", args.stage]
    for flag, values in (("--inputs", args.inputs), ("--settings", args.settings),
                         ("--template", args.template)):
        for value in values:
            parts += [flag, value]
    if args.interval != 30.0:
        parts += ["--interval", f"{args.interval:g}"]
    if args.timeout != 14400.0:
        parts += ["--timeout", f"{args.timeout:g}"]
    return shlex.join(parts)


def _attempt_line(stage: str, unit_id: str, attempt: str, job: str | None,
                  disposition: str | None, outputs: str | None) -> str:
    return (f"stage={stage} unit={unit_id} attempt={attempt} job={job or '-'} "
            f"disposition={disposition or '-'} outputs={outputs or '-'}")


class _StartWalk:
    def __init__(self, conn, args: argparse.Namespace):
        self.conn = conn
        self.args = args
        self.inputs_unprefixed, self.inputs_keyed = _split_keyed(args.inputs, "--inputs")
        self.settings_unprefixed, self.settings_keyed = _split_keyed(args.settings, "--settings")
        _, self.templates = _split_keyed(args.template, "--template", allow_unprefixed=False)

    def _producer(self, selected: list[str], position: int) -> str | None:
        for stage in reversed(selected[:position]):
            if stage != "register":
                return stage
        return None

    def _inputs_for(self, selected: list[str], position: int, first: int) -> str:
        stage = selected[position]
        if position == first and self.inputs_unprefixed is not None:
            return self.inputs_unprefixed
        if stage in self.inputs_keyed:
            return self.inputs_keyed[stage]
        producer = self._producer(selected, position)
        if stage in self.templates:
            if producer is None:
                raise _Exit(int(ExitCode.USAGE),
                            f"--template {stage}=... needs a preceding producing stage "
                            "in the run's selected stages; there is none")
            return compose_inputs(
                self.conn, run_id=self.args.run_id, stage=stage,
                unit_id=self.args.unit_id, from_stage=producer,
                template=self.templates[stage], reuse_existing=True)
        if producer is None:
            raise _Exit(int(ExitCode.USAGE),
                        f"stage {stage!r} has no preceding producing stage in the run; "
                        f"give its inputs with --inputs {stage}=<location>")
        return launch_batch.resolve_inputs_from_stage(
            self.conn, run_id=self.args.run_id, unit_id=self.args.unit_id,
            upstream_stage=producer)

    def _settings_for(self, stage: str, position: int, first: int) -> str | None:
        if position == first and self.settings_unprefixed is not None:
            return self.settings_unprefixed
        return self.settings_keyed.get(stage)

    def _wait(self, stage: str, unit_id: str, attempt_id: str, deadline: float) -> UnitRow:
        """Reconcile every ``--interval`` seconds until ``attempt_id`` has a
        disposition; :class:`_Exit` 75 once ``deadline`` passes."""
        while True:
            results = _reconcile(self.conn, self.args.run_id)
            status = next((r.batch_status for r in results if r.attempt_id == attempt_id), None)
            row = _unit_row(self.conn, self.args.run_id, stage, unit_id)
            if row is not None and row.last_attempt == attempt_id and row.last_disposition:
                print(f"poll stage={stage} unit={unit_id} attempt={attempt_id} "
                      f"status={status or '-'} disposition={row.last_disposition}", flush=True)
                return row
            print(f"poll stage={stage} unit={unit_id} attempt={attempt_id} "
                  f"status={status or '-'}", flush=True)
            if now() >= deadline:
                raise _Exit(int(ExitCode.TRANSIENT_FAILURE),
                            f"timed out after {self.args.timeout:g}s waiting for attempt "
                            f"{attempt_id} ({stage} {unit_id}); continue with: "
                            f"{_continue_command(self.args)}")
            sleep(self.args.interval)

    def run(self) -> int:
        args = self.args
        run = _require_run(self.conn, args.run_id)
        selected = run.selected_stages
        if args.stage is not None:
            positions = [i for i, s in enumerate(selected) if s == args.stage]
            if not positions:
                raise _Exit(int(ExitCode.USAGE),
                            f"stage {args.stage!r} is not one of run {args.run_id}'s "
                            f"selected stages ({', '.join(selected)})")
        else:
            positions = list(range(len(selected)))
        if not positions:
            raise _Exit(int(ExitCode.USAGE), f"run {args.run_id} selects no stages")
        first = positions[0]

        for position in positions:
            stage = selected[position]
            declaration = _declaration(stage)
            inputs_location: str | None = None
            if stage == "register":
                inputs_location = self._inputs_for(selected, position, first)
                unit_id = _main_module()._resolve_register_unit_id(
                    unit_id_arg=None, inputs_location_arg=inputs_location)
            else:
                unit_id = args.unit_id

            # One deadline per stage, covering every attempt it takes: a
            # transient or lost result returns the unit to 'ready' and the
            # loop allocates the next attempt (the allowance decides).
            deadline = now() + args.timeout
            first_look = True
            while True:
                row = _unit_row(self.conn, args.run_id, stage, unit_id)
                if row is not None and row.state == "complete":
                    if first_look:
                        print(f"{stage} {unit_id} already complete", flush=True)
                    break
                first_look = False
                if row is not None and row.state in ("failed", "cancelled"):
                    print(f"{stage} {unit_id} is {row.state}", flush=True)
                    print(f"run={args.run_id} state=failed", flush=True)
                    return 1

                if (row is not None and row.state == "running" and row.last_attempt
                        and row.last_disposition is None):
                    attempt_id, job_id, outputs = row.last_attempt, row.last_job, row.last_output
                    print(f"{stage} {unit_id} attempt {attempt_id} already in flight",
                          flush=True)
                else:
                    if inputs_location is None:
                        inputs_location = self._inputs_for(selected, position, first)
                    submission = launch_batch.submit_unit(
                        self.conn, run_id=args.run_id, stage=stage,
                        unit_kind=declaration.unit, unit_id=unit_id,
                        inputs_location=inputs_location,
                        settings_location=self._settings_for(stage, position, first))
                    attempt_id, job_id = submission.attempt_id, submission.job_id
                    outputs = submission.output_location

                if args.no_wait:
                    print(f"attempt={attempt_id} job={job_id} outputs={outputs}")
                    print(f"continue: {_continue_command(args)}")
                    print(f"run={args.run_id} state=submitted", flush=True)
                    return int(ExitCode.SUCCESS)

                final = self._wait(stage, unit_id, attempt_id, deadline)
                print(_attempt_line(stage, unit_id, attempt_id, final.last_job or job_id,
                                    final.last_disposition, final.last_output or outputs),
                      flush=True)
                if final.last_disposition == "succeeded" and final.state == "complete":
                    break
                if final.state == "ready":
                    if now() >= deadline:
                        raise _Exit(int(ExitCode.TRANSIENT_FAILURE),
                                    f"timed out after {args.timeout:g}s: {stage} {unit_id} "
                                    f"is ready again after a {final.last_disposition} "
                                    "attempt; continue with: "
                                    f"{_continue_command(args)}")
                    print(f"{stage} {unit_id} is ready again after a "
                          f"{final.last_disposition} attempt; allocating another",
                          flush=True)
                    continue
                print(f"run={args.run_id} state=failed", flush=True)
                return 1
            if args.stage is not None and not first_look:
                break

        print(f"run={args.run_id} state=complete", flush=True)
        return int(ExitCode.SUCCESS)


def _start_command(args: argparse.Namespace) -> int:
    def body(conn) -> int:
        try:
            return _StartWalk(conn, args).run()
        except _Exit as exc:
            if exc.code == int(ExitCode.TRANSIENT_FAILURE):
                print(f"run={args.run_id} state=timeout", flush=True)
            raise

    return _with_connection("start", body)


# ======================================================================
# run status
# ======================================================================

_STATUS_COLUMNS = ("stage", "unit", "state", "selected_attempt", "last_attempt",
                   "last_job", "disposition")


def _status_rows(conn, run_id: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.stage, u.unit_id, u.state, u.selected_attempt,
                   a.id, a.scheduler_job_id, a.disposition
            FROM units u
            LEFT JOIN LATERAL (
                SELECT id, scheduler_job_id, disposition
                FROM attempts WHERE unit = u.id
                ORDER BY started DESC, id DESC LIMIT 1) a ON true
            WHERE u.run = %s
            ORDER BY u.created, u.stage, u.unit_id
            """,
            (run_id,))
        return cur.fetchall()


def _status_command(args: argparse.Namespace) -> int:
    def body(conn) -> int:
        _require_run(conn, args.run_id)
        while True:
            _reconcile(conn, args.run_id)
            rows = _status_rows(conn, args.run_id)
            print("\t".join(_STATUS_COLUMNS))
            for row in rows:
                print("\t".join("-" if v is None else str(v) for v in row))
            sys.stdout.flush()
            states = [row[2] for row in rows]
            all_terminal = bool(rows) and all(s in _TERMINAL_UNIT_STATES for s in states)
            if all_terminal or not rows or not args.watch:
                break
            sleep(args.interval)
            print()
        if not rows:
            return _STATUS_STILL_RUNNING
        if any(s in ("failed", "cancelled") for s in states):
            return 1
        if all(s == "complete" for s in states):
            return int(ExitCode.SUCCESS)
        return _STATUS_STILL_RUNNING

    return _with_connection("status", body)


# ======================================================================
# run compare
# ======================================================================

def _compare_units(conn, run_id: str) -> list[tuple[str, str, str | None, str | None]]:
    """(stage, unit, disposition, settings hash) of each unit's selected
    attempt, else its most recent one."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.stage, u.unit_id, a.disposition, er.settings_hash
            FROM units u
            LEFT JOIN attempts a ON a.id = COALESCE(
                u.selected_attempt,
                (SELECT id FROM attempts WHERE unit = u.id
                 ORDER BY started DESC, id DESC LIMIT 1))
            LEFT JOIN execution_records er ON er.attempt = a.id
            WHERE u.run = %s
            """,
            (run_id,))
        return cur.fetchall()


def _compare_instances(conn, run_id: str) -> list[tuple[str, str, str]]:
    """(kind, logical key as canonical jsonb text, instance id) per product
    instance of the run."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, logical_key::text, id FROM product_instances "
            "WHERE run = %s ORDER BY kind, logical_key::text, id",
            (run_id,))
        return cur.fetchall()


def _compare_command(args: argparse.Namespace) -> int:
    def body(conn) -> int:
        run_a = _require_run(conn, args.run_a)
        run_b = _require_run(conn, args.run_b)
        different = False

        def show(value: Any) -> str:
            return "-" if value is None else str(value)

        print(f"settings_overlay_ref\t{show(run_a.settings_overlay_ref)}\t"
              f"{show(run_b.settings_overlay_ref)}")
        different |= run_a.settings_overlay_ref != run_b.settings_overlay_ref

        units_a = {(s, u): (d, h) for s, u, d, h in _compare_units(conn, args.run_a)}
        units_b = {(s, u): (d, h) for s, u, d, h in _compare_units(conn, args.run_b)}
        for key in sorted(set(units_a) | set(units_b)):
            a = units_a.get(key, ("(absent)", None))
            b = units_b.get(key, ("(absent)", None))
            print(f"unit\t{key[0]}\t{key[1]}\t{show(a[0])}\t{show(b[0])}\t"
                  f"settings\t{show(a[1])}\t{show(b[1])}")
            different |= a != b

        instances_a: dict[tuple[str, str], list[str]] = {}
        instances_b: dict[tuple[str, str], list[str]] = {}
        for rows, into in ((_compare_instances(conn, args.run_a), instances_a),
                           (_compare_instances(conn, args.run_b), instances_b)):
            for kind, key, instance in rows:
                into.setdefault((kind, key), []).append(instance)
        for key in sorted(set(instances_a) | set(instances_b)):
            a_ids = instances_a.get(key, [])
            b_ids = instances_b.get(key, [])
            print(f"instance\t{key[0]}\t{key[1]}\t{','.join(a_ids) or '-'}\t"
                  f"{','.join(b_ids) or '-'}")
            different |= len(a_ids) != len(b_ids)

        print("different" if different else "same")
        return 1 if different else int(ExitCode.SUCCESS)

    return _with_connection("compare", body)


# ======================================================================
# run expire
# ======================================================================

def _expire_command(args: argparse.Namespace) -> int:
    from rapidpipe.runs import cleanup

    main = _main_module()
    try:
        s3_client = cleanup.cleanup_s3_client()
    except cleanup.CleanupRoleError as exc:
        sys.stderr.write(f"rapidpipe run expire: {exc}\n")
        return int(ExitCode.TRANSIENT_FAILURE)

    def print_reports(reports: list[Any]) -> None:
        for index, report in enumerate(reports):
            if index:
                print()
            main._print_deletion_report(report)
        refused = sum(1 for r in reports if r.refused)
        print(f"expired={len(reports) - refused} refused={refused}")

    return main._run_model_command(
        "expire",
        lambda conn: cleanup.expire_runs(conn, now=args.now, s3_client=s3_client),
        print_result=print_reports)


# ======================================================================
# Dispatch
# ======================================================================

def dispatch(args: argparse.Namespace) -> int:
    """Run the ``rapidpipe run`` subcommand named by ``args.run_command``,
    one of :data:`COMMANDS`."""
    return {
        "start": _start_command,
        "status": _status_command,
        "inputs": _inputs_command,
        "compare": _compare_command,
        "expire": _expire_command,
    }[args.run_command](args)
