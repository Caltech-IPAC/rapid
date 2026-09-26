"""The ``rapidpipe`` command-line tool.

``rapidpipe stage <name> ...`` imports ``rapidpipe.stages.<name>`` and calls
its ``main(argv)``, per the stage contract's "Invocation" section: "``rapidpipe
stage <name>`` calls that same entrypoint." It is the ``stage run`` command
of the ``stage`` group, with ``stage list`` and ``stage describe``
(``rapidpipe.cli.stagectl``). The ``rapidpipe run`` subcommands:

- ``create``, ``list``, ``show`` -- record a run, list runs, inspect one
  (its units, attempts, expiry, pin and promotions), over
  ``rapidpipe.runs.repository``;
- ``local`` -- run one stage attempt as a local subprocess through
  ``rapidpipe.runs.local``;
- ``submit``, ``reconcile``, ``cancel`` -- one attempt on AWS Batch,
  through ``rapidpipe.launch.batch``;
- ``promote`` -- promote a production run's deliverables
  (``repository.promote_run``); ``rollback`` -- reverse one promotion
  (``repository.rollback_promotion``);
- ``finish`` -- mark a run whose units are all terminal finished
  (``repository.finish_run``);
- ``delete`` -- delete a scratch run and its outputs
  (``rapidpipe.runs.cleanup.delete_run``); ``pin`` / ``unpin`` -- keep a
  scratch run from expiring, or release it (``cleanup.pin_run``).

``rapidpipe loop run|plan|show`` is the scheduled processing-date loop
(``rapidpipe.cli.loopctl`` over ``rapidpipe.launch.loop``).

``rapidpipe check list|run|show`` (``rapidpipe.cli.checkctl``) lists the
registered checks and shipped check policies, runs a policy's checks over
a run's candidates recording each result, and shows recorded results
(supervisor step 6, 2026-09-24, R6).

``rapidpipe release cut|show|list|verify`` is ``python -m
rapidpipe.release``: the ``release`` subparser is built and dispatched by
``rapidpipe.release.__main__``, so both forms are one code path.

The specification's "Tools" section's Batch-level run management is in
``rapidpipe.cli.runctl``: ``start`` walks a run's selected stages for one
unit on Batch (skipping what is complete, so rerunning it restarts or
reruns part of a run), ``status`` reconciles and shows progress (``--watch``
to follow it), ``inputs`` composes a unit's input set, ``compare`` sets two
runs side by side, and ``expire`` deletes expired scratch runs. ``delete``
and ``expire`` act as ``RAPIDPIPE_CLEANUP_ROLE_ARN`` when it is set
(``rapidpipe.runs.cleanup.cleanup_s3_client``).

This module resolves a stage's ``DECLARATION`` by
``importlib.import_module(f"rapidpipe.stages.{name}")`` rather than
importing a stage module at the top level, so ``rapidpipe.cli`` -- the one
subpackage allowed to import anything (stage contract, "The package") --
stays the only place that decides which stage code loads, and
``rapidpipe.runs.local`` (which the ``run local`` subcommand calls into)
never has to import a stage itself.

Plain argparse; no third-party CLI library.
"""

from __future__ import annotations

import argparse
import getpass
import importlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

from rapidpipe import __version__
from rapidpipe import log as rapidpipe_log
from rapidpipe.db.connection import ConnectionConfigError, ConnectionUnavailable
from rapidpipe.db.connection import connect as _default_connect
from rapidpipe.launch.batch import (
    DependencyIncomplete,
    LaunchError,
    MissingEnvironmentVariable,
    ProfileNotAllowed,
    ReleaseDefinitionRefused,
)
from rapidpipe.launch import batch as launch_batch
from rapidpipe.products.manifest import Manifest, ManifestError, register_unit_id
from rapidpipe.products.storage import fetch_object, parse_location
from rapidpipe.cli import checkctl, loopctl, runctl, stagectl
from rapidpipe.release import __main__ as release_cli
from rapidpipe.runs.inputs import InputsRefused
from rapidpipe.runs.local import run_stage_locally
from rapidpipe.runs.repository import RunModelError
from rapidpipe.selftest import run as run_selftest
from rapidpipe.selftest.runner import STAGE_NAMES as SELFTEST_STAGE_NAMES
from rapidpipe.stages.contract import STAGE_NAMES, ExitCode

#: Recognised as network-shaped, the same rule
#: ``rapidpipe.stages.contract._map_storage_error`` uses: matched by class
#: name suffix, not ``isinstance``, so a test's stand-in exception (or a
#: real ``botocore.exceptions`` one) without this module importing
#: botocore is treated identically. A Batch error of this shape, or any
#: ``ClientError``-shaped exception, exits 75 -- retryable, since the CLI
#: invocation itself can simply be retried.
_BATCH_ERROR_NAMES = (
    "ClientError",
    "EndpointConnectionError",
    "ConnectionError",
    "ConnectTimeoutError",
    "ReadTimeoutError",
    "ThrottlingException",
    "RequestTimeout",
    "RequestTimeoutException",
    "BotoCoreError",
)


def _is_batch_error(exc: BaseException) -> bool:
    class_name = type(exc).__name__
    return any(class_name.endswith(name) for name in _BATCH_ERROR_NAMES)

#: Module-level indirection so tests can monkeypatch
#: ``rapidpipe.cli.main.connect`` without reaching into
#: ``rapidpipe.db.connection`` and affecting other callers -- the same
#: pattern ``rapidpipe.stages.register`` uses for the same reason.
connect = _default_connect


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


def _resolve_register_unit_id(*, unit_id_arg: str | None, inputs_location_arg: str) -> str:
    """The ``--unit`` value to use for a `register` invocation.

    register's unit id is always derived from the manifest it reads
    (register_unit_id, ruling: "a register unit is identified by what it
    registers"), never chosen by the caller, so an explicit ``--unit`` for
    register is refused rather than silently overridden.
    """
    if unit_id_arg is not None:
        raise RegisterUnitIdError(
            "--unit is not accepted for register: its unit id is always "
            "derived from the manifest it reads")
    manifest = _read_manifest_at(inputs_location_arg)
    return register_unit_id(manifest)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rapidpipe", description="The RAPID pipeline command-line tool.")
    parser.add_argument(
        "--version", action="version", version=f"rapidpipe {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    stagectl.add_parsers(subparsers)

    selftest_parser = subparsers.add_parser(
        "selftest",
        description="Run one stage's own packaged fixture and check its outputs.",
        help="Run one stage's own packaged fixture and check it: "
             "rapidpipe selftest --stage difference|finalize|load|maintain|crossmatch|alerts|statistics|prune|photometry|export [--real-tools] "
             "[--work-dir DIR] [--output-location s3://... or path]")
    selftest_parser.add_argument(
        "--stage", required=True, choices=SELFTEST_STAGE_NAMES, help="Which stage's fixture to run.")
    selftest_parser.add_argument(
        "--real-tools", action="store_true",
        help="Run the pipeline image's own tools/database instead of the "
             "packaged fakes (the default). Needs the pipeline image, or a "
             "checkout with the same tools on PATH.")
    selftest_parser.add_argument(
        "--work-dir", default=None,
        help="An empty or new directory to prepare the fixture in "
             "(default: a fresh temporary directory).")
    selftest_parser.add_argument(
        "--output-location", default=None,
        help="Where the stage's manifest and products end up: a local "
             "path or an s3:// prefix (default: <work-dir>/outputs). The "
             "stage always runs against a local directory first, and "
             "every check runs against that copy; an s3:// location is "
             "uploaded to only afterwards, so the check count is the "
             "same either way.")
    selftest_parser.add_argument("--python", default=sys.executable)

    run_parser = subparsers.add_parser(
        "run", help="Create, list, inspect, run, promote and delete runs.",
        description="Create, list, inspect, run, promote and delete runs.")
    run_parser.set_defaults(run_group_parser=run_parser)
    run_subparsers = run_parser.add_subparsers(dest="run_command")

    create_parser = run_subparsers.add_parser(
        "create", help="Create a run and print its id.",
        description="Record a new run and print its id.")
    create_parser.add_argument(
        "--kind", default=None, choices=("scratch", "production"),
        help="Required, except with --only-failed (the seed's kind; if given "
             "it must match).")
    create_parser.add_argument(
        "--purpose", default=None,
        help="Required, except with --only-failed (default \"re-run of failed "
             "units of <seed>: <seed purpose>\").")
    create_parser.add_argument(
        "--stages", default=None,
        help="Comma-separated list of selected stage names. Required, except "
             "with --only-failed, which takes them from the seed.")
    create_parser.add_argument("--owner", default=None)
    create_parser.add_argument("--lane", default=None, help="Default local.")
    create_parser.add_argument("--profile", default=None, help="Default local.")
    create_parser.add_argument("--db-target", default=None)
    create_parser.add_argument("--max-attempts", type=int, default=None, help="Default 1.")
    create_parser.add_argument("--settings-overlay-ref", default=None)
    create_parser.add_argument("--input-selection-ref", default=None)
    create_parser.add_argument(
        "--release", default=None, metavar="TAG",
        help="Create the run from a complete release: its source revision and "
             "image digest are the run's, and Batch submissions use its job "
             "definition revisions.")
    create_parser.add_argument(
        "--seed", default=None, metavar="RUN_ID",
        help="Record the run this one was seeded from. Alone, lineage only: the "
             "new run inherits no configuration from it (give every option "
             "explicitly) and may not reuse its outputs. With --only-failed, "
             "the new run re-runs the seed's failed units.")
    create_parser.add_argument(
        "--check-policy", default=None, metavar="NAME@VERSION", dest="check_policy",
        help="The check policy this run's promotions are validated under "
             "(default when promoting: rebuild-trial@1). Must be a shipped policy.")
    create_parser.add_argument(
        "--auto-promote", action="store_true", dest="auto_promote",
        help="Promote automatically at the end of run start when every check "
             "passes. Refused unless the run's policy is lead-approved and "
             "permits it; no shipped policy does.")
    create_parser.add_argument(
        "--only-failed", action="store_true", dest="only_failed",
        help="With --seed: re-run the seed's non-complete units (failed or "
             "cancelled, or left running or ready by a lost, killed or "
             "job-less attempt), copying the seed's configuration (kind, "
             "owner unless --owner, release or revision/digest, settings and "
             "input refs, lane, profile, database target, max attempts, "
             "check policy). A production seed: the stages start at the "
             "earliest one holding a non-complete unit, every non-complete "
             "unit is seeded wherever it sits, its input bindings are "
             "copied, and run start reads its inputs and settings from the "
             "seed attempt; stages a unit completed in the seed are "
             "inherited, not re-run. A scratch seed (its outputs may not "
             "feed another run): every stage is re-run from the first, and "
             "only the first stage's recorded inputs and settings are "
             "carried. Refused when the seed is deleting or deleted or has "
             "no non-complete unit.")

    list_parser = run_subparsers.add_parser("list", help="List runs.",
        description="List runs, newest first, optionally filtered.")
    list_parser.add_argument("--kind", default=None, choices=("scratch", "production"))
    list_parser.add_argument("--owner", default=None)
    list_parser.add_argument("--state", default=None)

    show_parser = run_subparsers.add_parser("show", help="Show one run.",
        description="Show one run: its fields, units, attempts and promotions. "
                    "Exit 1 if run_id names no run.")
    show_parser.add_argument("run_id")

    local_parser = run_subparsers.add_parser(
        "local", help="Run one stage attempt locally as a subprocess.",
        description="Allocate an attempt and run one stage as a subprocess on this machine.")
    local_parser.add_argument("run_id")
    local_parser.add_argument("stage")
    local_parser.add_argument(
        "--unit", required=False, default=None, dest="unit_id",
        help="Required for every stage except register, whose unit id is "
             "always derived from the manifest it reads (--unit is refused "
             "for register).")
    local_parser.add_argument("--inputs", required=True)
    local_parser.add_argument("--outputs-root", required=True)
    local_parser.add_argument("--settings", default=None)
    local_parser.add_argument("--python", default=sys.executable)
    local_parser.add_argument(
        "--profile", action="store_true", default=False,
        help="Profile the stage body under cProfile (RAPIDPIPE_PROFILE=1 "
             "for the subprocess); refused on a production run (exit 64), "
             "since profiles land in the attempt's own outputs prefix, "
             "which for production is the products bucket.")

    submit_parser = run_subparsers.add_parser(
        "submit", help="Submit one stage attempt to Batch.",
        description="Allocate an attempt and submit it to AWS Batch as one job.")
    submit_parser.add_argument("run_id")
    submit_parser.add_argument("stage")
    submit_parser.add_argument(
        "--unit", required=False, default=None, dest="unit_id",
        help="Required for every stage except register, whose unit id is "
             "always derived from the manifest it reads (--unit is refused "
             "for register).")
    inputs_group = submit_parser.add_mutually_exclusive_group(required=True)
    inputs_group.add_argument("--inputs", default=None)
    inputs_group.add_argument("--inputs-from-stage", default=None, dest="inputs_from_stage")
    submit_parser.add_argument("--settings", default=None)
    submit_parser.add_argument(
        "--profile", action="store_true", default=False,
        help="Profile the stage body under cProfile (sets RAPIDPIPE_PROFILE=1 "
             "in the Batch job's environment); refused on a production run "
             "(exit 64), since profiles land in the attempt's own outputs "
             "prefix, which for production is the products bucket.")

    reconcile_parser = run_subparsers.add_parser(
        "reconcile", help="Reconcile a run's unresolved Batch attempts.",
        description="Record the outcome of every unresolved Batch attempt of a run.")
    reconcile_parser.add_argument("run_id")
    reconcile_parser.add_argument(
        "--resolve-jobless", action="store_true", dest="resolve_jobless",
        help="First look up each attempt with no disposition and no scheduler "
             "job (its submission failed after allocation) on the Batch queue "
             "by the job name submit uses: one job found is recorded on the "
             "attempt (REPAIRED) and reconciled; more than one is left "
             "(AMBIGUOUS); none, and the attempt started more than "
             "--older-than seconds ago, records it 'lost' (NOJOB), and its "
             "unit returns to ready while attempts remain, else failed.")
    reconcile_parser.add_argument(
        "--older-than", type=float, default=None, dest="older_than", metavar="SECONDS",
        help="With --resolve-jobless: the minimum age of a job-less attempt "
             f"(default {launch_batch.DEFAULT_JOBLESS_AFTER_SECONDS}).")

    cancel_parser = run_subparsers.add_parser(
        "cancel", help="Terminate an attempt's Batch job.",
        description="Terminate an attempt's Batch job, recording the reason.")
    cancel_parser.add_argument("attempt_id")
    cancel_parser.add_argument("--reason", required=True)

    promote_parser = run_subparsers.add_parser(
        "promote", help="Promote a production run's candidates; print the promotion id.",
        description="Promote a production run's candidates; print the promotion id.")
    promote_parser.add_argument("run_id")
    promote_parser.add_argument("--reason", required=True)
    promote_parser.add_argument(
        "--who", default=None, help="Who is promoting (default: the current user).")
    promote_parser.add_argument(
        "--kinds", default=None,
        help="Comma-separated product kinds to promote (default: every kind).")
    promote_parser.add_argument(
        "--allow-unreleased", action="store_true",
        help="Promote even if a deliverable's attempt ran no complete release's "
             "image; recorded in the promotion's request_context.")
    promote_parser.add_argument(
        "--check-policy", default=None, metavar="NAME@VERSION", dest="check_policy",
        help="Validate under this check policy (default: the run's check policy, "
             "else rebuild-trial@1).")

    rollback_parser = run_subparsers.add_parser(
        "rollback", help="Reverse one promotion; print the reversing promotion id.",
        description="Reverse one promotion; print the reversing promotion id.")
    rollback_parser.add_argument("promotion_id")
    rollback_parser.add_argument("--reason", required=True)
    rollback_parser.add_argument(
        "--who", default=None, help="Who is rolling back (default: the current user).")

    delete_parser = run_subparsers.add_parser(
        "delete", help="Delete a scratch run's outputs and science rows.",
        description="Delete a scratch run's S3 outputs and science rows, keeping "
                    "its run-model rows. Honours RAPIDPIPE_CLEANUP_ROLE_ARN.")
    delete_parser.add_argument("run_id")
    delete_parser.add_argument(
        "--requested-by", default=None, dest="requested_by",
        help="The run's owner (default: the current user).")

    finish_parser = run_subparsers.add_parser(
        "finish", help="Mark a run whose units are all terminal as finished.",
        description="Mark a run whose units are all terminal as finished.")
    finish_parser.add_argument("run_id")

    pin_parser = run_subparsers.add_parser(
        "pin", help="Pin a run so it never expires.",
        description="Pin a scratch run so it never expires.")
    pin_parser.add_argument("run_id")

    unpin_parser = run_subparsers.add_parser(
        "unpin", help="Unpin a run so it expires at its expires_at.",
        description="Unpin a scratch run so it expires at its expires_at.")
    unpin_parser.add_argument("run_id")

    runctl.add_parsers(run_subparsers)

    release_parser = subparsers.add_parser(
        "release", help="Cut, show, list and verify releases (python -m rapidpipe.release).",
        description="Cut, show, list and verify releases.")
    release_cli.build_parser(release_parser)

    checkctl.add_parser(subparsers)

    loopctl.add_parser(subparsers)

    return parser


def _run_stage_command(name: str | None, stage_argv: list[str]) -> int:
    if not name:
        sys.stderr.write(
            "rapidpipe stage: a stage name is required; known stages: "
            f"{', '.join(STAGE_NAMES)}\n")
        return int(ExitCode.USAGE)
    if name not in STAGE_NAMES:
        sys.stderr.write(
            f"rapidpipe stage: unknown stage {name!r}; known stages: "
            f"{', '.join(STAGE_NAMES)}\n")
        return int(ExitCode.USAGE)

    module_name = f"rapidpipe.stages.{name.replace('-', '_')}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        sys.stderr.write(
            f"rapidpipe stage: {name!r} is a known stage name but "
            f"{module_name} is not implemented yet; known stages: "
            f"{', '.join(STAGE_NAMES)}\n")
        return int(ExitCode.USAGE)

    stage_main = getattr(module, "main", None)
    if stage_main is None:
        sys.stderr.write(
            f"rapidpipe stage: {module_name} has no main(argv)\n")
        return int(ExitCode.USAGE)

    return stage_main(stage_argv)


def _run_selftest_command(args: argparse.Namespace) -> int:
    """``rapidpipe selftest``: run one stage's own packaged fixture.

    A thin wrapper over :func:`rapidpipe.selftest.run`, which does the
    actual prepare/run/check work; this function only translates argparse
    into that call. Exit codes per the ruling this subcommand implements
    (2026-09-23): 0 on a full pass, 1 on a fixture mismatch, and the
    stage's own exit code on a stage failure.
    """
    return run_selftest(
        stage=args.stage, real_tools=args.real_tools, work_dir=args.work_dir,
        output_location=args.output_location, python=args.python)


def _source_revision_or_unknown() -> str:
    """``git rev-parse HEAD`` in the current working directory, else the
    literal ``"unknown"`` (brief: "else the literal unknown"; distinct from
    the stage contract's execution record, which uses ``None`` for the same
    failure -- this is a run-creation field, not an execution record)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    revision = result.stdout.strip()
    return revision or "unknown"


def _last_applied_schema_version(cur) -> str | None:
    """The filename of the most recently applied migration.

    ``database/apply-migrations.sh`` tracks applied migrations in
    ``schema_migrations(filename, sha256, applied_at)``; filenames sort
    lexicographically in applied order (``YYYYMMDD-NN-short-name.sql``),
    so the greatest filename is the last one applied.
    """
    cur.execute("SELECT filename FROM schema_migrations ORDER BY filename DESC LIMIT 1")
    row = cur.fetchone()
    return row[0] if row else None


class ReleaseNotComplete(Exception):
    """``run create --release`` (and ``loop run``) named a release that is
    absent or not ``complete``; the run is not created."""


def create_run_record(
    conn,
    *,
    kind: str,
    owner: str,
    purpose: str,
    stages: Sequence[str],
    release: str | None,
    lane: str,
    profile: str,
    db_target: str | None,
    max_attempts: int,
    settings_overlay_ref: str | None = None,
    input_selection_ref: str | None = None,
    check_policy_ref: str | None = None,
    auto_promote: bool = False,
    seed: str | None = None,
) -> str:
    """``run create``'s one code path (``rapidpipe loop run`` uses it too):
    with ``release``, the run's source revision and image digest are the
    ``releases`` row's, and a release that is absent or not ``complete``
    raises :class:`ReleaseNotComplete`; without, the checkout's revision
    and ``RAPIDPIPE_IMAGE_DIGEST``. Does not commit."""
    from rapidpipe.runs.repository import create_run

    with conn.cursor() as cur:
        schema_version = _last_applied_schema_version(cur) or "unknown"
        release_row = None
        if release is not None:
            cur.execute(
                "SELECT state, source_revision, image_digest FROM releases "
                "WHERE tag = %s", (release,))
            release_row = cur.fetchone()
    if release is not None:
        if release_row is None or release_row[0] != "complete":
            state = "absent" if release_row is None else release_row[0]
            raise ReleaseNotComplete(f"release {release} is {state}, not complete; refusing")
        _, code_revision, image_digest = release_row
    else:
        code_revision = _source_revision_or_unknown()
        image_digest = os.environ.get("RAPIDPIPE_IMAGE_DIGEST")

    return create_run(
        conn,
        kind=kind,
        owner=owner,
        purpose=purpose,
        selected_stages=list(stages),
        code_revision=code_revision,
        image_digest=image_digest,
        schema_version=schema_version,
        settings_overlay_ref=settings_overlay_ref,
        input_selection_ref=input_selection_ref,
        lane=lane,
        resource_profile=profile,
        database_target=db_target or os.environ.get("PGDATABASE", ""),
        max_attempts_per_unit=max_attempts,
        auto_promote=auto_promote,
        check_policy_ref=check_policy_ref,
        release=release,
        seed_run=seed,
    )


def _run_create_command(args: argparse.Namespace) -> int:
    if args.only_failed:
        return _run_create_only_failed_command(args)
    missing = [flag for flag, value in (("--kind", args.kind), ("--purpose", args.purpose),
                                        ("--stages", args.stages)) if value is None]
    if missing:
        sys.stderr.write(
            f"rapidpipe run create: {', '.join(missing)} required (unless --seed "
            "<run> --only-failed)\n")
        return int(ExitCode.USAGE)
    owner = args.owner or getpass.getuser()
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    if not stages:
        sys.stderr.write("rapidpipe run create: --stages must name at least one stage\n")
        return int(ExitCode.USAGE)

    try:
        cm = connect(application_name="rapidpipe-run-create")
    except ConnectionConfigError as exc:
        sys.stderr.write(f"rapidpipe run create: database configuration error: {exc}\n")
        return int(ExitCode.USAGE)
    except ConnectionUnavailable as exc:
        sys.stderr.write(f"rapidpipe run create: database unavailable: {exc}\n")
        return int(ExitCode.TRANSIENT_FAILURE)

    with cm as conn:
        try:
            run_id = create_run_record(
                conn, kind=args.kind, owner=owner, purpose=args.purpose, stages=stages,
                release=args.release, lane=args.lane or "local",
                profile=args.profile or "local", db_target=args.db_target,
                max_attempts=1 if args.max_attempts is None else args.max_attempts,
                settings_overlay_ref=args.settings_overlay_ref,
                input_selection_ref=args.input_selection_ref,
                check_policy_ref=args.check_policy,
                auto_promote=args.auto_promote, seed=args.seed)
            conn.commit()
        except ReleaseNotComplete as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run create: {exc}\n")
            return 2
        except RunModelError as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run create: {exc}\n")
            return int(ExitCode.USAGE)
        except BaseException:
            conn.rollback()
            raise

    print(run_id)
    return int(ExitCode.SUCCESS)


#: ``run create`` options ``--only-failed`` refuses: the re-run copies each
#: from the seed (supervisor step 6, 2026-09-24, R7).
_ONLY_FAILED_COPIED_OPTIONS = (
    ("--stages", "stages"), ("--release", "release"), ("--lane", "lane"),
    ("--profile", "profile"), ("--db-target", "db_target"),
    ("--max-attempts", "max_attempts"),
    ("--settings-overlay-ref", "settings_overlay_ref"),
    ("--input-selection-ref", "input_selection_ref"),
    ("--check-policy", "check_policy"),
)


class OnlyFailedKindMismatch(RunModelError):
    """``run create --seed <run> --only-failed --kind K`` with K not the seed's kind."""


def create_only_failed_run(
    conn,
    seed_run: str,
    *,
    owner: str | None = None,
    purpose: str | None = None,
    kind: str | None = None,
):
    """``run create --seed <run> --only-failed``'s one code path (the
    processing-date loop's ``--retry-failed`` uses it too): a run re-running
    the seed's non-complete units (supervisor step 6, 2026-09-24, R7).

    Configuration is copied from the seed row
    (:func:`~rapidpipe.runs.repository.failed_rerun_plan`); ``owner`` and
    ``purpose`` may be given, ``kind`` only if it equals the seed's
    (:class:`OnlyFailedKindMismatch` otherwise). Creates the run and its
    seeded units (:func:`~rapidpipe.runs.repository.seed_failed_units`);
    does not commit. Returns ``(run id, plan, seeded unit ids)``; a refusal
    is a :class:`RunModelError` (``SeedRefused``, ``RunNotFound``).
    """
    from rapidpipe.runs.repository import create_run, failed_rerun_plan, seed_failed_units

    plan = failed_rerun_plan(conn, seed_run)
    seed = plan.seed
    if kind is not None and kind != seed["kind"]:
        raise OnlyFailedKindMismatch(
            f"--kind {kind} differs from seed run {seed_run}'s kind {seed['kind']}; a "
            "--only-failed re-run keeps the seed's kind")
    with conn.cursor() as cur:
        schema_version = _last_applied_schema_version(cur) or "unknown"
    purpose = purpose or (
        f"re-run of failed units of {seed_run}: {seed['purpose']}"
        if seed["purpose"] else f"re-run of failed units of {seed_run}")
    run_id = create_run(
        conn,
        kind=seed["kind"],
        owner=owner or seed["owner"],
        purpose=purpose,
        selected_stages=plan.stages,
        code_revision=seed["code_revision"],
        image_digest=seed["image_digest"],
        schema_version=schema_version,
        settings_overlay_ref=seed["settings_overlay_ref"],
        input_selection_ref=seed["input_selection_ref"],
        lane=seed["lane"],
        resource_profile=seed["resource_profile"],
        database_target=seed["database_target"],
        max_attempts_per_unit=seed["max_attempts_per_unit"],
        auto_promote=False,
        check_policy_ref=seed["check_policy_ref"],
        release=seed["release"],
        seed_run=seed_run,
    )
    unit_ids = seed_failed_units(conn, seed_run=seed_run, new_run=run_id)
    return run_id, plan, unit_ids


def _run_create_only_failed_command(args: argparse.Namespace) -> int:
    """``run create --seed <run> --only-failed``: :func:`create_only_failed_run`
    from the command line. ``--owner`` and ``--purpose`` may be given,
    ``--kind`` only if it equals the seed's, and every other configuration
    option is refused. The run and its seeded units commit together; the run
    id is printed on stdout, the seeded units on stderr.
    """
    name = "rapidpipe run create"
    if args.seed is None:
        sys.stderr.write(f"{name}: --only-failed requires --seed <run>\n")
        return int(ExitCode.USAGE)
    given = [flag for flag, attr in _ONLY_FAILED_COPIED_OPTIONS
             if getattr(args, attr, None) is not None]
    if getattr(args, "auto_promote", False):
        given.append("--auto-promote")
    if given:
        sys.stderr.write(
            f"{name}: {', '.join(given)} not accepted with --only-failed: the "
            "re-run copies them from the seed\n")
        return int(ExitCode.USAGE)

    try:
        cm = connect(application_name="rapidpipe-run-create")
    except ConnectionConfigError as exc:
        sys.stderr.write(f"{name}: database configuration error: {exc}\n")
        return int(ExitCode.USAGE)
    except ConnectionUnavailable as exc:
        sys.stderr.write(f"{name}: database unavailable: {exc}\n")
        return int(ExitCode.TRANSIENT_FAILURE)

    with cm as conn:
        try:
            run_id, plan, unit_ids = create_only_failed_run(
                conn, args.seed, owner=args.owner, purpose=args.purpose, kind=args.kind)
            conn.commit()
        except RunModelError as exc:
            conn.rollback()
            sys.stderr.write(f"{name}: {exc}\n")
            return int(ExitCode.USAGE)
        except BaseException:
            conn.rollback()
            raise

    seed = plan.seed
    sys.stderr.write(
        f"seeded {len(unit_ids)} unit(s) from {seed['kind']} run {args.seed}; stages "
        f"{','.join(plan.stages)} (from seed position {plan.position}): "
        f"{' '.join(unit_ids)}\n")
    if plan.uncarried:
        sys.stderr.write(
            f"not carried (no {plan.stages[0]} unit in the seed; give --inputs to run "
            f"start): {' '.join(plan.uncarried)}\n")
    print(run_id)
    return int(ExitCode.SUCCESS)


def _run_list_command(args: argparse.Namespace) -> int:
    try:
        cm = connect(application_name="rapidpipe-run-list")
    except ConnectionConfigError as exc:
        sys.stderr.write(f"rapidpipe run list: database configuration error: {exc}\n")
        return int(ExitCode.USAGE)
    except ConnectionUnavailable as exc:
        sys.stderr.write(f"rapidpipe run list: database unavailable: {exc}\n")
        return int(ExitCode.TRANSIENT_FAILURE)

    with cm as conn:
        query = "SELECT id, kind, owner, state, created, purpose FROM runs WHERE 1=1"
        params: list[Any] = []
        if args.kind:
            query += " AND kind = %s"
            params.append(args.kind)
        if args.owner:
            query += " AND owner = %s"
            params.append(args.owner)
        if args.state:
            query += " AND state = %s"
            params.append(args.state)
        query += " ORDER BY created DESC"
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    for run_id, kind, owner, state, created, purpose in rows:
        created_str = created.isoformat(timespec="seconds") if created is not None else ""
        print(f"{run_id}\t{kind}\t{owner}\t{state}\t{created_str}\t{purpose or ''}")
    return int(ExitCode.SUCCESS)


def _run_show_command(args: argparse.Namespace) -> int:
    """Print one run's fields, units, attempts and promotions.

    Exit 0 on success, 64 on a database configuration error, 75 if the
    database is unavailable, 1 if ``args.run_id`` names no run."""
    try:
        cm = connect(application_name="rapidpipe-run-show")
    except ConnectionConfigError as exc:
        sys.stderr.write(f"rapidpipe run show: database configuration error: {exc}\n")
        return int(ExitCode.USAGE)
    except ConnectionUnavailable as exc:
        sys.stderr.write(f"rapidpipe run show: database unavailable: {exc}\n")
        return int(ExitCode.TRANSIENT_FAILURE)

    with cm as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, kind, owner, purpose, selected_stages, state, "
                "code_revision, image_digest, schema_version, lane, "
                "resource_profile, database_target, max_attempts_per_unit, "
                "auto_promote, created, expires_at, pinned, finished_at, release "
                "FROM runs WHERE id = %s",
                (args.run_id,),
            )
            run_row = cur.fetchone()
            if run_row is None:
                sys.stderr.write(f"rapidpipe run show: no such run: {args.run_id}\n")
                return 1

            columns = [
                "id", "kind", "owner", "purpose", "selected_stages", "state",
                "code_revision", "image_digest", "schema_version", "lane",
                "resource_profile", "database_target", "max_attempts_per_unit",
                "auto_promote", "created", "expires_at", "pinned", "finished_at",
                "release",
            ]
            for column, value in zip(columns, run_row):
                print(f"{column}: {value}")

            cur.execute(
                "SELECT stage, unit_id, state, selected_attempt "
                "FROM units WHERE run = %s ORDER BY stage, unit_id",
                (args.run_id,),
            )
            print("units:")
            for stage, unit_id, state, selected_attempt in cur.fetchall():
                print(f"  {stage}\t{unit_id}\t{state}\t{selected_attempt}")

            cur.execute(
                "SELECT id, stage, unit, disposition, exit_code, started, ended "
                "FROM attempts WHERE run = %s ORDER BY started",
                (args.run_id,),
            )
            print("attempts:")
            for attempt_id, stage, unit, disposition, exit_code, started, ended in cur.fetchall():
                print(
                    f"  {attempt_id}\t{stage}\t{unit}\t{disposition}\t"
                    f"{exit_code}\t{started}\t{ended}")

            # Promotions that selected or unselected one of this run's
            # instances, with how many of their changes did.
            cur.execute(
                """
                SELECT p.id, p.who, p.happened_at, p.reason, count(*)
                FROM promotions p
                JOIN promotion_changes pc ON pc.promotion = p.id
                WHERE EXISTS (
                    SELECT 1 FROM product_instances pi
                    WHERE pi.run = %s
                      AND pi.id IN (pc.before_instance, pc.after_instance))
                GROUP BY p.id, p.who, p.happened_at, p.reason
                ORDER BY p.happened_at, p.id
                """,
                (args.run_id,),
            )
            promotions = cur.fetchall()
            if promotions:
                print("promotions:")
                for promotion_id, who, happened_at, reason, changes in promotions:
                    print(f"  {promotion_id}\t{who}\t{happened_at}\t{reason}\t{changes}")

    return int(ExitCode.SUCCESS)


def _run_local_command(args: argparse.Namespace) -> int:
    if args.stage not in STAGE_NAMES:
        sys.stderr.write(
            f"rapidpipe run local: unknown stage {args.stage!r}; known stages: "
            f"{', '.join(STAGE_NAMES)}\n")
        return int(ExitCode.USAGE)

    module_name = f"rapidpipe.stages.{args.stage.replace('-', '_')}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        sys.stderr.write(
            f"rapidpipe run local: {args.stage!r} is a known stage name but "
            f"{module_name} is not implemented yet\n")
        return int(ExitCode.USAGE)

    declaration = getattr(module, "DECLARATION", None)
    if declaration is None:
        sys.stderr.write(f"rapidpipe run local: {module_name} has no DECLARATION\n")
        return int(ExitCode.USAGE)

    if args.stage == "register":
        try:
            unit_id = _resolve_register_unit_id(
                unit_id_arg=args.unit_id, inputs_location_arg=args.inputs)
        except RegisterUnitIdError as exc:
            sys.stderr.write(f"rapidpipe run local: {exc}\n")
            return int(ExitCode.USAGE)
    elif args.unit_id is None:
        sys.stderr.write("rapidpipe run local: --unit is required\n")
        return int(ExitCode.USAGE)
    else:
        unit_id = args.unit_id

    try:
        cm = connect(application_name="rapidpipe-run-local")
    except ConnectionConfigError as exc:
        sys.stderr.write(f"rapidpipe run local: database configuration error: {exc}\n")
        return int(ExitCode.USAGE)
    except ConnectionUnavailable as exc:
        sys.stderr.write(f"rapidpipe run local: database unavailable: {exc}\n")
        return int(ExitCode.TRANSIENT_FAILURE)

    with cm as conn:
        # The run's kind is needed whether or not --profile was given: a
        # production run must never profile, including via a
        # RAPIDPIPE_PROFILE=1 the calling shell's own environment already
        # carries (local.py's subprocess otherwise inherits it), not only
        # via an explicit --profile this command would refuse outright.
        try:
            kind = launch_batch._run_kind(conn, args.run_id)
        except RunModelError as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run local: {exc}\n")
            return int(ExitCode.USAGE)

        if args.profile and kind == "production":
            conn.rollback()
            sys.stderr.write(
                "rapidpipe run local: --profile is refused for a "
                f"production run ({args.run_id}); profiling is for "
                "scratch runs, since profiles land in the attempt's "
                "own outputs prefix, which for production is the "
                "products bucket\n")
            return int(ExitCode.USAGE)

        if kind == "production":
            # Strip it, not merely set it falsy: the point is that a
            # production run never profiles, even via an ambient
            # RAPIDPIPE_PROFILE=1 this process itself inherited.
            profile_env = {"RAPIDPIPE_PROFILE": None}
        elif args.profile:
            profile_env = {"RAPIDPIPE_PROFILE": "1"}
        else:
            profile_env = None

        try:
            result = run_stage_locally(
                conn,
                run_id=args.run_id,
                stage=args.stage,
                unit_kind=declaration.unit,
                unit_id=unit_id,
                inputs=args.inputs,
                outputs_root=args.outputs_root,
                settings=args.settings,
                python=args.python,
                env=profile_env,
            )
        except InputsRefused as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run local: {exc}\n")
            return int(ExitCode.INPUT_REJECTED)
        except RunModelError as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run local: {exc}\n")
            return int(ExitCode.USAGE)

    print(
        f"attempt={result.attempt_id} exit={result.exit_code} "
        f"disposition={result.disposition} selected={result.selected} "
        f"outputs={result.output_location}")
    return result.exit_code


def _run_submit_command(args: argparse.Namespace) -> int:
    if args.stage not in STAGE_NAMES:
        sys.stderr.write(
            f"rapidpipe run submit: unknown stage {args.stage!r}; known stages: "
            f"{', '.join(STAGE_NAMES)}\n")
        return int(ExitCode.USAGE)

    module_name = f"rapidpipe.stages.{args.stage.replace('-', '_')}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        sys.stderr.write(
            f"rapidpipe run submit: {args.stage!r} is a known stage name but "
            f"{module_name} is not implemented yet\n")
        return int(ExitCode.USAGE)

    declaration = getattr(module, "DECLARATION", None)
    if declaration is None:
        sys.stderr.write(f"rapidpipe run submit: {module_name} has no DECLARATION\n")
        return int(ExitCode.USAGE)

    # register's own --unit is refused (derived, from the manifest it
    # reads); --inputs-from-stage still names the producing unit's own id
    # (admit's/difference's), needed to look up its output location --
    # carried on --unit for this one case, since register's own unit id
    # does not exist yet to disambiguate it from. Checked before any
    # connection is attempted, matching the plain-stage --unit-required
    # check below (unless the location is --inputs-from-stage, which
    # itself needs a connection to resolve).
    if args.stage == "register":
        if args.inputs_from_stage is None:
            try:
                unit_id = _resolve_register_unit_id(
                    unit_id_arg=args.unit_id, inputs_location_arg=args.inputs)
            except RegisterUnitIdError as exc:
                sys.stderr.write(f"rapidpipe run submit: {exc}\n")
                return int(ExitCode.USAGE)
        elif args.unit_id is None:
            sys.stderr.write(
                "rapidpipe run submit: --unit is required together with "
                "--inputs-from-stage for register: it names the producing "
                "unit to resolve, not register's own unit id\n")
            return int(ExitCode.USAGE)
    elif args.unit_id is None:
        sys.stderr.write("rapidpipe run submit: --unit is required\n")
        return int(ExitCode.USAGE)
    else:
        unit_id = args.unit_id

    try:
        cm = connect(application_name="rapidpipe-run-submit")
    except ConnectionConfigError as exc:
        sys.stderr.write(f"rapidpipe run submit: database configuration error: {exc}\n")
        return int(ExitCode.USAGE)
    except ConnectionUnavailable as exc:
        sys.stderr.write(f"rapidpipe run submit: database unavailable: {exc}\n")
        return int(ExitCode.TRANSIENT_FAILURE)

    with cm as conn:
        try:
            if args.stage == "register" and args.inputs_from_stage is not None:
                inputs_location = launch_batch.resolve_inputs_from_stage(
                    conn, run_id=args.run_id, unit_id=args.unit_id,
                    upstream_stage=args.inputs_from_stage)
                unit_id = _resolve_register_unit_id(
                    unit_id_arg=None, inputs_location_arg=inputs_location)
            elif args.inputs_from_stage is not None:
                inputs_location = launch_batch.resolve_inputs_from_stage(
                    conn, run_id=args.run_id, unit_id=unit_id,
                    upstream_stage=args.inputs_from_stage)
            else:
                inputs_location = args.inputs

            submission = launch_batch.submit_unit(
                conn,
                run_id=args.run_id,
                stage=args.stage,
                unit_kind=declaration.unit,
                unit_id=unit_id,
                inputs_location=inputs_location,
                settings_location=args.settings,
                profile=args.profile,
            )
        except ProfileNotAllowed as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run submit: {exc}\n")
            return int(ExitCode.USAGE)
        except RegisterUnitIdError as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run submit: {exc}\n")
            return int(ExitCode.USAGE)
        except InputsRefused as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run submit: {exc}\n")
            return int(ExitCode.INPUT_REJECTED)
        except MissingEnvironmentVariable as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run submit: {exc}\n")
            return int(ExitCode.USAGE)
        except DependencyIncomplete as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run submit: {exc}\n")
            return int(ExitCode.USAGE)
        except ReleaseDefinitionRefused as exc:
            # A permanent refusal, not a retryable one: the run's release
            # names a job definition revision that is not ACTIVE.
            conn.rollback()
            sys.stderr.write(f"rapidpipe run submit: {exc}\n")
            return 1
        except RunModelError as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run submit: {exc}\n")
            return int(ExitCode.USAGE)
        except Exception as exc:  # noqa: BLE001 - Batch/botocore-shaped errors
            if _is_batch_error(exc):
                conn.rollback()
                sys.stderr.write(f"rapidpipe run submit: Batch error: {exc}\n")
                return int(ExitCode.TRANSIENT_FAILURE)
            raise

    print(
        f"attempt={submission.attempt_id} job={submission.job_id} "
        f"outputs={submission.output_location}")
    return int(ExitCode.SUCCESS)


def _run_reconcile_command(args: argparse.Namespace) -> int:
    resolve_jobless = getattr(args, "resolve_jobless", False)
    older_than = getattr(args, "older_than", None)
    if older_than is not None and not resolve_jobless:
        sys.stderr.write("rapidpipe run reconcile: --older-than needs --resolve-jobless\n")
        return int(ExitCode.USAGE)
    if older_than is None:
        older_than = launch_batch.DEFAULT_JOBLESS_AFTER_SECONDS
    if older_than < 0:
        sys.stderr.write("rapidpipe run reconcile: --older-than must be >= 0\n")
        return int(ExitCode.USAGE)
    try:
        cm = connect(application_name="rapidpipe-run-reconcile")
    except ConnectionConfigError as exc:
        sys.stderr.write(f"rapidpipe run reconcile: database configuration error: {exc}\n")
        return int(ExitCode.USAGE)
    except ConnectionUnavailable as exc:
        sys.stderr.write(f"rapidpipe run reconcile: database unavailable: {exc}\n")
        return int(ExitCode.TRANSIENT_FAILURE)

    with cm as conn:
        try:
            # supervisor step 6, 2026-09-24, R9 with amendment B3: look for
            # each job-less attempt's Batch job first, so a repaired one is
            # reconciled by the ordinary pass just below.
            jobless = (launch_batch.resolve_jobless(
                conn, run_id=args.run_id, older_than_seconds=older_than)
                if resolve_jobless else [])
            results = launch_batch.reconcile(conn, run_id=args.run_id)
        except (RunModelError, MissingEnvironmentVariable) as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run reconcile: {exc}\n")
            return int(ExitCode.USAGE)
        except Exception as exc:  # noqa: BLE001 - Batch/botocore-shaped errors
            if _is_batch_error(exc):
                conn.rollback()
                sys.stderr.write(f"rapidpipe run reconcile: Batch error: {exc}\n")
                return int(ExitCode.TRANSIENT_FAILURE)
            raise

    for result in jobless:
        if result.batch_status == "NOJOB":
            print(f"attempt={result.attempt_id} job=- status=NOJOB disposition=lost")
        else:  # REPAIRED or AMBIGUOUS
            print(f"attempt={result.attempt_id} job={result.job_id} "
                  f"status={result.batch_status}")
    for result in results:
        print(
            f"attempt={result.attempt_id} job={result.job_id} "
            f"status={result.batch_status} disposition={result.disposition} "
            f"selected={result.selected}")
    return int(ExitCode.SUCCESS)


def _run_cancel_command(args: argparse.Namespace) -> int:
    try:
        cm = connect(application_name="rapidpipe-run-cancel")
    except ConnectionConfigError as exc:
        sys.stderr.write(f"rapidpipe run cancel: database configuration error: {exc}\n")
        return int(ExitCode.USAGE)
    except ConnectionUnavailable as exc:
        sys.stderr.write(f"rapidpipe run cancel: database unavailable: {exc}\n")
        return int(ExitCode.TRANSIENT_FAILURE)

    with cm as conn:
        try:
            launch_batch.cancel(conn, attempt_id=args.attempt_id, reason=args.reason)
        except RunModelError as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run cancel: {exc}\n")
            return int(ExitCode.USAGE)
        except LaunchError as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run cancel: {exc}\n")
            return int(ExitCode.USAGE)
        except Exception as exc:  # noqa: BLE001 - Batch/botocore-shaped errors
            if _is_batch_error(exc):
                conn.rollback()
                sys.stderr.write(f"rapidpipe run cancel: Batch error: {exc}\n")
                return int(ExitCode.TRANSIENT_FAILURE)
            raise

    return int(ExitCode.SUCCESS)


def _run_model_command(
    name: str,
    action,
    *,
    print_result=None,
) -> int:
    """Shared shape of the run-model subcommands: connect, call
    ``action(conn)``, commit, print; a refusal (any
    :class:`~rapidpipe.runs.repository.RunModelError`) rolls back, prints
    the exception message and exits 64; an AWS-shaped error (``run
    delete``'s S3 calls) exits 75, retryable -- ``delete`` resumes a
    ``deleting`` run."""
    try:
        cm = connect(application_name=f"rapidpipe-run-{name}")
    except ConnectionConfigError as exc:
        sys.stderr.write(f"rapidpipe run {name}: database configuration error: {exc}\n")
        return int(ExitCode.USAGE)
    except ConnectionUnavailable as exc:
        sys.stderr.write(f"rapidpipe run {name}: database unavailable: {exc}\n")
        return int(ExitCode.TRANSIENT_FAILURE)

    with cm as conn:
        try:
            result = action(conn)
            conn.commit()
        except RunModelError as exc:
            conn.rollback()
            sys.stderr.write(f"rapidpipe run {name}: {exc}\n")
            return int(ExitCode.USAGE)
        except Exception as exc:  # noqa: BLE001 - AWS/botocore-shaped errors
            conn.rollback()
            if _is_batch_error(exc):
                sys.stderr.write(f"rapidpipe run {name}: AWS error: {exc}\n")
                return int(ExitCode.TRANSIENT_FAILURE)
            raise
        except BaseException:
            conn.rollback()
            raise

    if print_result is not None:
        print_result(result)
    return int(ExitCode.SUCCESS)


def _run_promote_command(args: argparse.Namespace) -> int:
    from rapidpipe.runs.repository import promote_run

    who = args.who or getpass.getuser()
    kinds = None
    if args.kinds is not None:
        kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
        if not kinds:
            sys.stderr.write("rapidpipe run promote: --kinds must name at least one kind\n")
            return int(ExitCode.USAGE)
    # promote_run resolves the check policy: --check-policy > the run's
    # check_policy_ref > rebuild-trial@1 (supervisor step 6, 2026-09-24, R4).
    policy_kwargs = {} if args.check_policy is None else {"check_policy": args.check_policy}
    return _run_model_command(
        "promote",
        lambda conn: promote_run(conn, args.run_id, who, args.reason, kinds=kinds,
                                 allow_unreleased=args.allow_unreleased, **policy_kwargs),
        print_result=print)


def _run_rollback_command(args: argparse.Namespace) -> int:
    from rapidpipe.runs.repository import rollback_promotion

    who = args.who or getpass.getuser()
    return _run_model_command(
        "rollback",
        lambda conn: rollback_promotion(conn, args.promotion_id, who, args.reason),
        print_result=print)


def _print_deletion_report(report) -> None:
    print(f"run_id: {report.run_id}")
    print(f"already_deleted: {report.already_deleted}")
    print(f"objects_deleted: {report.objects_deleted}")
    print(f"versions_deleted: {report.versions_deleted}")
    for table, count in report.rows_deleted.items():
        print(f"rows_deleted.{table}: {count}")
    print(f"instances_marked: {report.instances_marked}")
    if report.refused is not None:
        print(f"refused: {report.refused}")


def _run_delete_command(args: argparse.Namespace) -> int:
    from rapidpipe.runs.cleanup import CleanupRoleError, cleanup_s3_client, delete_run

    requested_by = args.requested_by or getpass.getuser()
    try:
        s3_client = cleanup_s3_client()
    except CleanupRoleError as exc:
        sys.stderr.write(f"rapidpipe run delete: {exc}\n")
        return int(ExitCode.TRANSIENT_FAILURE)
    return _run_model_command(
        "delete",
        lambda conn: delete_run(conn, args.run_id, requested_by, s3_client=s3_client),
        print_result=_print_deletion_report)


def _run_finish_command(args: argparse.Namespace) -> int:
    from rapidpipe.runs.repository import finish_run

    return _run_model_command("finish", lambda conn: finish_run(conn, args.run_id))


def _run_pin_command(args: argparse.Namespace, pinned: bool) -> int:
    from rapidpipe.runs.cleanup import pin_run

    return _run_model_command(
        "pin" if pinned else "unpin", lambda conn: pin_run(conn, args.run_id, pinned))


def _run_command(args: argparse.Namespace) -> int:
    if args.run_command == "create":
        return _run_create_command(args)
    if args.run_command == "list":
        return _run_list_command(args)
    if args.run_command == "show":
        return _run_show_command(args)
    if args.run_command == "local":
        return _run_local_command(args)
    if args.run_command == "submit":
        return _run_submit_command(args)
    if args.run_command == "reconcile":
        return _run_reconcile_command(args)
    if args.run_command == "cancel":
        return _run_cancel_command(args)
    if args.run_command == "promote":
        return _run_promote_command(args)
    if args.run_command == "rollback":
        return _run_rollback_command(args)
    if args.run_command == "delete":
        return _run_delete_command(args)
    if args.run_command == "finish":
        return _run_finish_command(args)
    if args.run_command == "pin":
        return _run_pin_command(args, True)
    if args.run_command == "unpin":
        return _run_pin_command(args, False)
    if args.run_command in runctl.COMMANDS:
        return runctl.dispatch(args)
    args.run_group_parser.print_help(sys.stderr)
    sys.stderr.write(
        "rapidpipe run: a subcommand is required: create, list, show, local, "
        "submit, reconcile, cancel, promote, rollback, delete, finish, pin, "
        "unpin, start, status, inputs, compare, expire, timings\n")
    return int(ExitCode.USAGE)


def main(argv: Sequence[str] | None = None) -> int:
    # Configures Python's real root logger (WARNING by default,
    # RAPIDPIPE_LOG_LEVEL overrides) with the same UTC line shape a stage
    # invocation uses, so a library's bare warning gets a timestamp and
    # run context instead of Python's last-resort handler. CLI data
    # output (print to stdout) is unaffected.
    rapidpipe_log.configure_root()
    parser = _build_parser()
    # "rapidpipe stage <name> ..." (the form the Batch launcher submits) is
    # "rapidpipe stage run <name> ...": rewritten before argparse sees it.
    argv_list = stagectl.rewrite_legacy_argv(
        list(argv) if argv is not None else sys.argv[1:])
    args = parser.parse_args(argv_list)

    if args.command == "stage":
        return stagectl.dispatch(args, _run_stage_command)

    if args.command == "selftest":
        return _run_selftest_command(args)

    if args.command == "run":
        return _run_command(args)

    if args.command == "release":
        return release_cli.dispatch(args)

    if args.command == "check":
        return checkctl.dispatch(args)

    if args.command == "loop":
        return loopctl.dispatch(args)

    parser.print_help()
    return int(ExitCode.SUCCESS) if args.command is None else int(ExitCode.USAGE)


if __name__ == "__main__":
    sys.exit(main())
