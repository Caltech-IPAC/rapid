"""``run start``'s walk: one unit through a run's selected stages on Batch.

The walk skips a complete stage, submits the next attempt of any other
(:func:`rapidpipe.launch.batch.submit_unit`), waits for it by polling
reconcile, and moves on (tool.md §Operations). :func:`start` is
``run start``; :func:`walk_unit` is the same walk as a callable, which
the processing-date loop (``rapidpipe.launch.loop``) calls per unit and
stage position; :func:`compose_inputs` composes an input set (``run
inputs``, and ``run start --template``); :func:`maybe_auto_promote` ends
a complete walk (checks.md §Automatic promotion).

Seams tests replace, always called through this module's attribute so
one patch reaches every caller, the command-line tool's included:
``sleep`` and ``now`` (``--interval`` and ``--timeout``), ``_reconcile``
(each poll), ``_run_row`` and ``_unit_row`` (the SQL the walk reads),
:func:`compose_inputs` and :func:`resolve_register_unit_id`.

A refusal raises :class:`rapidpipe.exitcodes.CommandExit` with its exit
code (64 a usage error, 75 ``--timeout``); the command-line tool maps it.
"""

from __future__ import annotations

import argparse
import getpass
import importlib
import json
import shlex
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from rapidpipe.exitcodes import CommandExit, ExitCode
from rapidpipe.launch import batch as launch_batch
from rapidpipe.products.manifest import (
    Manifest,
    ManifestError,
    Member,
    OutputEntry,
    Unit,
    register_unit_id,
)
from rapidpipe.products.storage import (
    Location,
    Storage,
    fetch_object,
    join,
    parse_location,
)
from rapidpipe.runs import binding
from rapidpipe.stages.contract import STAGE_NAMES

#: Indirections for tests: ``start --interval`` / ``status --watch`` sleep
#: through ``sleep``; ``start --timeout`` measures with ``now``.
sleep: Callable[[float], None] = time.sleep
now: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class RunRow:
    kind: str
    selected_stages: list[str]
    state: str
    settings_overlay_ref: str | None
    seed_run: str | None = None


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
            "SELECT kind, selected_stages, state, settings_overlay_ref, seed_run "
            "FROM runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
    if row is None:
        return None
    return RunRow(row[0], list(row[1] or []), row[2], row[3], row[4])


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


def _seeded_unit(conn, run_id: str, stage: str, unit_id: str) -> str | None:
    """The ``units.id`` of (run, stage, unit_id) when it was seeded from
    another run's unit (``units.seeded_from_unit`` set), else ``None``."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM units WHERE run = %s AND stage = %s AND unit_id = %s "
            "AND seeded_from_unit IS NOT NULL", (run_id, stage, unit_id))
        row = cur.fetchone()
    return None if row is None else row[0]


def _units_at(conn, run_id: str, stage: str, unit_ids: list[str], *,
              seeded_only: bool = False) -> list[tuple[str, str]]:
    """``(units.id, unit_id)`` of the run's ``stage`` units among
    ``unit_ids`` (only those with ``seeded_from_unit`` set, if
    ``seeded_only``)."""
    if not unit_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, unit_id FROM units WHERE run = %s AND stage = %s "
            "AND unit_id = ANY(%s) AND (NOT %s OR seeded_from_unit IS NOT NULL) "
            "ORDER BY unit_id",
            (run_id, stage, list(unit_ids), seeded_only))
        return [(row[0], row[1]) for row in cur.fetchall()]


def _seeded_locations(conn, unit_row_id: str) -> tuple[str | None, str | None]:
    from rapidpipe.runs.repository import seeded_inputs_for_unit

    return seeded_inputs_for_unit(conn, unit_row_id)


def require_run(conn, run_id: str) -> RunRow:
    """The run's row; :class:`CommandExit` 64 when there is no such run."""
    row = _run_row(conn, run_id)
    if row is None:
        raise CommandExit(int(ExitCode.USAGE), f"no such run: {run_id}")
    return row


def _declaration(stage: str) -> Any:
    module_name = f"rapidpipe.stages.{stage.replace('-', '_')}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise CommandExit(int(ExitCode.USAGE),
                          f"{stage!r} is a known stage name but {module_name} is not "
                          "implemented yet") from exc
    declaration = getattr(module, "DECLARATION", None)
    if declaration is None:
        raise CommandExit(int(ExitCode.USAGE), f"{module_name} has no DECLARATION")
    return declaration


def _reconcile(conn, run_id: str) -> list[Any]:
    return launch_batch.reconcile(conn, run_id=run_id)


def reconcile_run(conn, run_id: str) -> list[Any]:
    """Reconcile ``run_id``'s unresolved attempts through the ``_reconcile``
    seam, the one every poll of the walk uses (``run status`` too)."""
    return _reconcile(conn, run_id)


# ======================================================================
# register's unit id
# ======================================================================

class RegisterUnitIdError(ValueError):
    """The register unit id could not be derived from its input manifest."""


def _read_manifest_at(location_arg: str) -> Manifest:
    """Read ``manifest.json`` from a local directory or S3 prefix.

    Same fetch used by a stage's own ``--dry-run`` path
    (``rapidpipe.stages.contract._read_input_manifest_from``, one object,
    not the whole prefix): a local location is read directly; an S3
    location is fetched to a throwaway temp file first, since
    :meth:`~rapidpipe.products.manifest.Manifest.read` only takes a local
    path.
    """
    location = parse_location(location_arg)
    if not location.is_s3():
        manifest_path = Path(location_arg) / "manifest.json"
    else:
        with tempfile.TemporaryDirectory(prefix="rapidpipe-register-unit-") as tmp:
            manifest_path = fetch_object(
                location, "manifest.json", Path(tmp) / "manifest.json")
            return _load_manifest(manifest_path)
    return _load_manifest(manifest_path)


def _load_manifest(manifest_path: Path) -> Manifest:
    try:
        return Manifest.read(manifest_path)
    except FileNotFoundError as exc:
        raise RegisterUnitIdError(f"input manifest not found: {manifest_path}") from exc
    except (json.JSONDecodeError, ManifestError) as exc:
        raise RegisterUnitIdError(f"{manifest_path}: invalid manifest: {exc}") from exc


def resolve_register_unit_id(*, unit_id_arg: str | None, inputs_location_arg: str) -> str:
    """The ``--unit`` value to use for a `register` invocation.

    register's unit id is always derived from the manifest it reads
    (register_unit_id: "a register unit is identified by what it
    registers"), never chosen by the caller, so an explicit ``--unit`` for
    register is refused rather than silently overridden.
    """
    if unit_id_arg is not None:
        raise RegisterUnitIdError(
            "--unit is not accepted for register: its unit id is always "
            "derived from the manifest it reads")
    manifest = _read_manifest_at(inputs_location_arg)
    return register_unit_id(manifest)


# ======================================================================
# Input sets (run inputs, run start --template)
# ======================================================================

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
        raise CommandExit(int(ExitCode.USAGE),
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
        raise CommandExit(int(ExitCode.USAGE),
                          f"output {entry.instance!r} has two members with the same file "
                          "name; cannot place them under l2/")
    primary = None if entry.primary is None else f"l2/{PurePosixPath(entry.primary).name}"
    return OutputEntry(
        kind=entry.kind, format_version=entry.format_version, instance=entry.instance,
        key=dict(entry.key), members=tuple(members), primary=primary,
        registration=dict(entry.registration)), moves


# register derives its unit id from its inputs' manifest (``<stage>/<unit>``),
# so a composed set (stage ``input-set``) would admit ``register/U`` and then
# submit ``register/input-set/U``.
_REGISTER_TEMPLATE_REFUSAL = (
    "--template is refused for register: register's inputs are always the "
    "producing stage's output; use --inputs, or --template on the producer")


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
    storage: Storage | None = None,
    producer_run: str | None = None,
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
    manifest always means the bindings were committed once. The order is
    :func:`rapidpipe.runs.binding.bind_input_set`'s: admission
    (``add_unit``) comes first, before the template or producer is read.

    An existing ``<dest>/manifest.json`` is refused (exit 64), or with
    ``reuse_existing`` (``run start``, rerun after an interruption)
    reused: its registered instances are re-bound (idempotent) and
    committed, so a reuse always leaves the unit bound.

    ``producer_run`` (default ``run_id``) is the run whose ``from_stage``
    unit is read: a seeded run's production seed when the producer was
    inherited from it (``run start``).
    """
    if stage == "register":
        raise CommandExit(int(ExitCode.USAGE), _REGISTER_TEMPLATE_REFUSAL)
    storage = storage or Storage()
    require_run(conn, run_id)
    dest_text = dest or default_inputs_dest(run_id, stage, unit_id)
    _require_under_inputs_root(dest_text, run_id)
    dest_loc = parse_location(dest_text)
    declaration = _declaration(stage)

    def compose() -> Manifest:
        # Called by bind_input_set only after admission (add_unit) and only
        # when no manifest exists at dest.
        template_manifest = storage.read_manifest(template)
        producer_text = launch_batch.resolve_inputs_from_stage(
            conn, run_id=producer_run or run_id, unit_id=unit_id, upstream_stage=from_stage)
        producer = storage.read_manifest(producer_text)
        candidates = [o for o in producer.outputs if o.kind == kind]
        if len(candidates) != 1:
            raise CommandExit(int(ExitCode.USAGE),
                              f"{producer_text}/manifest.json has {len(candidates)} output(s) of "
                              f"kind {kind!r}; expected exactly one")
        producer_entry, moves = _rehome_under_l2(candidates[0])

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
                raise CommandExit(int(ExitCode.FAILURE),
                                  f"copied {join(dest_loc, dst_rel)} is {actual} bytes; its "
                                  f"manifest says {expected}")

        return Manifest(
            run=run_id,
            unit=Unit(kind=producer.unit.kind, id=unit_id),
            stage="input-set",
            attempt=producer.attempt,
            execution_record=template_manifest.execution_record or "exec/input-set.json",
            inputs=template_manifest.inputs,
            outputs=(producer_entry, *kept_entries),
        )

    try:
        # Labelled (stage contract, "The manifest"): the id rule is
        # manifest_instances, so a template's inputs.result_sets are bound
        # too; a deleting or deleted producer at bind time exits 65
        # (InputsRefused), not 64.
        result = binding.bind_input_set(
            conn, storage, run_id=run_id, stage=stage, unit_kind=declaration.unit,
            unit_id=unit_id, dest=dest_text, compose=compose,
            reuse_existing=reuse_existing)
    except binding.InputSetExists as exc:
        raise CommandExit(int(ExitCode.USAGE), str(exc)) from exc
    suffix = " (already composed)" if result.reused else ""
    print(f"inputs={dest_text}{suffix}", flush=True)
    return dest_text


# ======================================================================
# The walk (run start, the processing-date loop)
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
                raise CommandExit(int(ExitCode.USAGE), f"{flag} given twice for stage {head!r}")
            keyed[head] = tail
            continue
        if not allow_unprefixed:
            raise CommandExit(int(ExitCode.USAGE),
                              f"{flag} {value!r}: expected <stage>=<location>, <stage> one of "
                              f"{', '.join(STAGE_NAMES)}")
        if unprefixed is not None:
            raise CommandExit(int(ExitCode.USAGE),
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
    """``run start``'s walk. ``positions`` (indexes into the run's selected
    stages) restricts the walk to those occurrences, in order, for a caller
    that walks one run with different units per stage (the processing-date
    loop, ``rapidpipe.launch.loop``); ``continue_hint`` replaces the
    ``run start`` command a timeout message names."""

    def __init__(self, conn, args: argparse.Namespace, *,
                 positions: list[int] | None = None, continue_hint: str | None = None):
        self.conn = conn
        self.args = args
        self.positions = positions
        self.continue_hint = continue_hint
        self.inputs_unprefixed, self.inputs_keyed = _split_keyed(args.inputs, "--inputs")
        self.settings_unprefixed, self.settings_keyed = _split_keyed(args.settings, "--settings")
        _, self.templates = _split_keyed(args.template, "--template", allow_unprefixed=False)
        if "register" in self.templates:
            raise CommandExit(int(ExitCode.USAGE), _REGISTER_TEMPLATE_REFUSAL)

    def _producer(self, selected: list[str], position: int) -> str | None:
        for stage in reversed(selected[:position]):
            if stage != "register":
                return stage
        return None

    def _explicit_inputs(self, stage: str, position: int, first: int) -> str | None:
        if position == first and self.inputs_unprefixed is not None:
            return self.inputs_unprefixed
        return self.inputs_keyed.get(stage)

    def _explicit_settings(self, stage: str, position: int, first: int) -> str | None:
        if position == first and self.settings_unprefixed is not None:
            return self.settings_unprefixed
        return self.settings_keyed.get(stage)

    def _seeded(self, stage: str, unit_id: str | None) -> tuple[str | None, str | None]:
        """The seed attempt's (inputs, settings) locations for a seeded unit,
        else ``(None, None)`` (runs page, "Rules")."""
        if unit_id is None:
            return None, None
        unit_row_id = _seeded_unit(self.conn, self.args.run_id, stage, unit_id)
        if unit_row_id is None:
            return None, None
        return _seeded_locations(self.conn, unit_row_id)

    def _candidate_unit_ids(self, selected: list[str], position: int) -> list[str]:
        """The unit ids ``--unit`` stands for at ``position``: itself, or for a
        ``register`` ``<producer>/<unit>``. With no producing stage before it
        in this run (a seeded run starting at a register), the producer is
        the one before the matching position in the seed's stage list, so a
        leading ``register`` stands for ``register(admit)``, never a later
        register unit such as ``difference/<unit>``. Only when the seed's
        list does not end with this run's does it fall back to any
        producing stage's."""
        unit_id = self.args.unit_id
        if selected[position] != "register":
            return [unit_id]
        producer = self._producer(selected, position)
        if producer is None:
            producer = self._seed_producer(selected, position, any_seed_kind=True)
        if producer is not None:
            return [f"{producer}/{unit_id}"]
        return [f"{stage}/{unit_id}" for stage in STAGE_NAMES if stage != "register"]

    def _inherited(self, selected: list[str], position: int, first: int) -> bool:
        """Whether a seeded run inherits this position's result from its seed
        (runs page, "Rules"): no unit row for the unit here or at any
        earlier position, no explicit inputs or ``--template`` for this
        stage (an explicit template asks for the stage to run), and a
        seeded unit for it at a later position -- its upstream completed
        in the seed."""
        run_id = self.args.run_id
        if self.run.seed_run is None:
            return False
        if self._explicit_inputs(selected[position], position, first) is not None:
            return False
        if selected[position] in self.templates:
            return False
        for earlier in range(position + 1):
            if _units_at(self.conn, run_id, selected[earlier],
                         self._candidate_unit_ids(selected, earlier)):
                return False
        return any(
            _units_at(self.conn, run_id, selected[later],
                      self._candidate_unit_ids(selected, later), seeded_only=True)
            for later in range(position + 1, len(selected)))

    def _seeded_register(self, selected: list[str], position: int, first: int,
                         ) -> tuple[str, str | None, str | None] | None:
        """For a ``register`` position with no explicit inputs, the seeded
        register unit's ``(unit_id, inputs, settings)`` when this run has
        one for the unit here: its unit id is the seed's, copied as is, so
        it is not derived from the inputs' manifest (runs page, "Rules").
        ``None`` when there is no such unit; more than one is a usage error."""
        if self._explicit_inputs("register", position, first) is not None:
            return None
        units = _units_at(self.conn, self.args.run_id, "register",
                          self._candidate_unit_ids(selected, position), seeded_only=True)
        if not units:
            return None
        if len(units) > 1:
            raise CommandExit(int(ExitCode.USAGE),
                              f"run {self.args.run_id} has {len(units)} seeded register units "
                              f"for unit {self.args.unit_id} ({', '.join(u for _, u in units)}); "
                              "give --inputs register=<location>")
        unit_row_id, unit_id = units[0]
        inputs, settings = _seeded_locations(self.conn, unit_row_id)
        return unit_id, inputs, settings

    def _inputs_for(self, selected: list[str], position: int, first: int,
                    unit_id: str | None = None) -> str:
        """``--inputs`` > ``--template`` > a seeded unit's seed attempt's
        recorded inputs (runs page, "Rules") > the preceding producing
        stage's output."""
        stage = selected[position]
        explicit = self._explicit_inputs(stage, position, first)
        if explicit is not None:
            return explicit
        producer = self._producer(selected, position)
        if stage in self.templates:
            if producer is None:
                producer = self._seed_producer(selected, position)
            if producer is None:
                raise CommandExit(int(ExitCode.USAGE),
                                  f"--template {stage}=... needs a preceding producing stage "
                                  "in the run's selected stages; there is none")
            return compose_inputs(
                self.conn, run_id=self.args.run_id, stage=stage,
                unit_id=self.args.unit_id, from_stage=producer,
                template=self.templates[stage], reuse_existing=True,
                producer_run=self._producer_run(producer))
        seeded_inputs, _ = self._seeded(stage, unit_id)
        if seeded_inputs is not None:
            return seeded_inputs
        if producer is None:
            producer = self._seed_producer(selected, position)
        if producer is None:
            raise CommandExit(int(ExitCode.USAGE),
                              f"stage {stage!r} has no preceding producing stage in the run; "
                              f"give its inputs with --inputs {stage}=<location>")
        return launch_batch.resolve_inputs_from_stage(
            self.conn, run_id=self._producer_run(producer), unit_id=self.args.unit_id,
            upstream_stage=producer)

    def _production_seed(self) -> RunRow | None:
        seed_run = self.run.seed_run
        if seed_run is None:
            return None
        seed = _run_row(self.conn, seed_run)
        return seed if seed is not None and seed.kind == "production" else None

    def _seed_producer(self, selected: list[str], position: int, *,
                       any_seed_kind: bool = False) -> str | None:
        """For a run seeded from a production run, whose stage list is a
        suffix of the seed's, the producing stage before ``position`` in the
        seed's list (e.g. ``load`` after a first-position ``register``
        reads the seed's ``difference``); ``None`` otherwise.
        ``any_seed_kind`` accepts a scratch seed too: for naming a unit,
        not for reading the seed's outputs."""
        if any_seed_kind:
            seed = (None if self.run.seed_run is None
                    else _run_row(self.conn, self.run.seed_run))
        else:
            seed = self._production_seed()
        if seed is None:
            return None
        offset = len(seed.selected_stages) - len(selected)
        if offset < 0 or seed.selected_stages[offset:] != selected:
            return None
        return self._producer(seed.selected_stages, offset + position)

    def _producer_run(self, producer: str) -> str:
        """The run whose ``producer`` output this run reads: its own, or --
        when the producer's unit was inherited from a production seed (no
        unit row here) -- the seed, whose outputs are project custody
        (runs page, "Rules"). A scratch seed's outputs never feed
        another run."""
        run_id = self.args.run_id
        if _units_at(self.conn, run_id, producer, [self.args.unit_id]):
            return run_id
        return self.run.seed_run if self._production_seed() is not None else run_id

    def _settings_for(self, stage: str, position: int, first: int,
                      unit_id: str | None = None) -> str | None:
        """``--settings`` > a seeded unit's seed attempt's recorded settings
        location when its inputs came from the seed too (runs page,
        "Rules") > none.
        A seed attempt recorded with no settings ran with the defaults, so
        ``None`` from it is kept."""
        explicit = self._explicit_settings(stage, position, first)
        if explicit is not None:
            return explicit
        if (self._explicit_inputs(stage, position, first) is None
                and stage not in self.templates):
            seeded_inputs, seeded_settings = self._seeded(stage, unit_id)
            if seeded_inputs is not None:
                return seeded_settings
        return None

    def _wait(self, stage: str, unit_id: str, attempt_id: str, deadline: float) -> UnitRow:
        """Reconcile every ``--interval`` seconds until ``attempt_id`` has a
        disposition; :class:`CommandExit` 75 once ``deadline`` passes."""
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
                raise CommandExit(int(ExitCode.TRANSIENT_FAILURE),
                                  f"timed out after {self.args.timeout:g}s waiting for attempt "
                                  f"{attempt_id} ({stage} {unit_id}); continue with: "
                                  f"{self._continue()}")
            sleep(self.args.interval)

    def _continue(self) -> str:
        return self.continue_hint or _continue_command(self.args)

    def run(self) -> int:
        args = self.args
        run = self.run = require_run(self.conn, args.run_id)
        if getattr(args, "profile", False) and run.kind == "production":
            raise CommandExit(
                int(ExitCode.USAGE),
                f"--profile is refused for a production run ({args.run_id}); "
                "profiling is for scratch runs, since profiles land in the "
                "attempt's own outputs prefix, which for production is the "
                "products bucket")
        selected = run.selected_stages
        if self.positions is not None:
            positions = list(self.positions)
            bad = [i for i in positions if not 0 <= i < len(selected)]
            if bad:
                raise CommandExit(int(ExitCode.USAGE),
                                  f"positions {bad} are outside run {args.run_id}'s "
                                  f"{len(selected)} selected stages")
        elif args.stage is not None:
            positions = [i for i, s in enumerate(selected) if s == args.stage]
            if not positions:
                raise CommandExit(int(ExitCode.USAGE),
                                  f"stage {args.stage!r} is not one of run {args.run_id}'s "
                                  f"selected stages ({', '.join(selected)})")
        else:
            positions = list(range(len(selected)))
        if not positions:
            raise CommandExit(int(ExitCode.USAGE), f"run {args.run_id} selects no stages")
        first = positions[0]

        for position in positions:
            stage = selected[position]
            declaration = _declaration(stage)
            if self._inherited(selected, position, first):
                print(f"{stage} {args.unit_id} inherited from seed {run.seed_run}",
                      flush=True)
                continue
            inputs_location: str | None = None
            settings_location: str | None = None
            settings_resolved = False
            seeded_register = (self._seeded_register(selected, position, first)
                               if stage == "register" else None)
            if seeded_register is not None:
                # The seeded unit's id is the seed's, not derived (runs page,
                # "Rules").
                unit_id, inputs_location, settings_location = seeded_register
                settings_resolved = inputs_location is not None
                explicit_settings = self._explicit_settings(stage, position, first)
                if explicit_settings is not None:
                    settings_location = explicit_settings
                if inputs_location is None:
                    inputs_location = self._inputs_for(selected, position, first, unit_id)
            elif stage == "register":
                inputs_location = self._inputs_for(selected, position, first)
                unit_id = resolve_register_unit_id(
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
                    return int(ExitCode.FAILURE)
                if (row is not None and row.state == "running" and row.last_attempt
                        and row.last_disposition is None):
                    attempt_id, job_id, outputs = row.last_attempt, row.last_job, row.last_output
                    if job_id is None:
                        # Allocation committed but the Batch submit or
                        # record_scheduler_job failed: reconcile skips an
                        # attempt without a job id, so waiting never ends,
                        # and run cancel needs a job id too.
                        raise CommandExit(
                            int(ExitCode.USAGE),
                            f"run {args.run_id} stage {stage} unit {unit_id}: attempt "
                            f"{attempt_id} is running but has no scheduler job (its "
                            "submission failed after the attempt was allocated); "
                            "reconcile cannot resolve it and run cancel cannot "
                            "terminate it, so it must be resolved by hand: check "
                            "Batch for a job named for the attempt, then run "
                            f"'rapidpipe run reconcile {args.run_id} --resolve-jobless' "
                            "to record it lost before rerunning run start")
                    print(f"{stage} {unit_id} attempt {attempt_id} already in flight",
                          flush=True)
                else:
                    if inputs_location is None:
                        inputs_location = self._inputs_for(
                            selected, position, first, unit_id)
                    if not settings_resolved:
                        settings_location = self._settings_for(
                            stage, position, first, unit_id)
                    submission = launch_batch.submit_unit(
                        self.conn, run_id=args.run_id, stage=stage,
                        unit_kind=declaration.unit, unit_id=unit_id,
                        inputs_location=inputs_location,
                        settings_location=settings_location,
                        profile=getattr(args, "profile", False))
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
                        raise CommandExit(int(ExitCode.TRANSIENT_FAILURE),
                                          f"timed out after {args.timeout:g}s: {stage} {unit_id} "
                                          f"is ready again after a {final.last_disposition} "
                                          "attempt; continue with: "
                                          f"{self._continue()}")
                    print(f"{stage} {unit_id} is ready again after a "
                          f"{final.last_disposition} attempt; allocating another",
                          flush=True)
                    continue
                print(f"run={args.run_id} state=failed", flush=True)
                return int(ExitCode.FAILURE)
            if args.stage is not None and not first_look:
                break

        maybe_auto_promote(self.conn, args.run_id)
        print(f"run={args.run_id} state=complete", flush=True)
        return int(ExitCode.SUCCESS)


def maybe_auto_promote(conn, run_id: str) -> None:
    """End of a ``run start`` walk: automatic promotion, designed in and off
    (checks page, "Automatic promotion").

    Calls :func:`rapidpipe.checks.runner.maybe_auto_promote`, commits what
    it recorded (check rows, and the promotion when one was made), and
    prints its one line -- ``auto-promote off (policy <ref>)`` for every
    run today, since no shipped policy permits automatic promotion. A
    check-policy error (the run names a policy that no longer loads) is
    printed as a refusal, not raised: the walk itself succeeded.
    """
    from rapidpipe.checks.registry import CheckError
    from rapidpipe.checks.runner import maybe_auto_promote as _maybe_auto_promote

    conn.commit()  # the walk's own writes are already committed; start clean
    try:
        outcome = _maybe_auto_promote(conn, run_id, who=getpass.getuser())
    except CheckError as exc:
        conn.rollback()
        print(f"auto-promote refused: {exc}", flush=True)
        return
    conn.commit()
    print(outcome.message, flush=True)


def walk_unit(
    conn,
    *,
    run_id: str,
    unit_id: str,
    positions: list[int] | None = None,
    inputs: Iterable[str] = (),
    settings: Iterable[str] = (),
    templates: Iterable[str] = (),
    interval: float = 30.0,
    timeout: float = 14400.0,
    continue_hint: str | None = None,
) -> int:
    """``run start``'s walk as a callable: the same :class:`_StartWalk`
    over ``run_id`` for ``unit_id``, restricted to ``positions`` of the
    run's selected stages when given. ``inputs``/``settings``/``templates``
    take ``run start``'s ``[<stage>=]<loc>`` forms (unprefixed = the first
    walked position). Returns 0 (complete) or 1 (a unit failed or was
    cancelled); raises :class:`CommandExit` 75 on ``timeout`` and 64 on a
    refusal. The processing-date loop (``rapidpipe.launch.loop``) calls it
    through this module's attribute."""
    args = argparse.Namespace(
        run_id=run_id, unit_id=unit_id, stage=None, inputs=list(inputs),
        settings=list(settings), template=list(templates), no_wait=False,
        interval=interval, timeout=timeout)
    return _StartWalk(conn, args, positions=positions, continue_hint=continue_hint).run()


def start(conn, args: argparse.Namespace) -> int:
    """``run start``: the walk over ``args``, ``run start``'s parsed
    arguments (``run_id``, ``unit_id``, ``stage``, ``inputs``,
    ``settings``, ``template``, ``no_wait``, ``interval``, ``timeout``,
    ``profile``). Returns 0 (complete, or submitted with ``no_wait``) or 1
    (a unit failed or was cancelled); raises :class:`CommandExit` 75 on
    ``timeout`` and 64 on a refusal."""
    return _StartWalk(conn, args).run()
