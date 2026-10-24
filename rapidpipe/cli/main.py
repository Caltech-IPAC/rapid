"""The ``rapidpipe`` command-line tool.

``rapidpipe stage <name> ...`` imports ``rapidpipe.stages.<name>`` and calls
its ``main(argv)``, per the stage contract's "Invocation" section; it is
the ``stage run`` command of the ``stage`` group, with ``stage list`` and
``stage describe`` (``rapidpipe.cli.stagectl``). ``rapidpipe selftest``
runs one stage's packaged fixture (``rapidpipe.selftest``).

The five foundation verbs (foundation plan v5, milestone 0) wrap
``rapidpipe.foundation``:

- ``run create --who --why --revision [--settings FILE] [--overlay FILE]
  [--set TABLE.key=value ...] --survey --dataset`` records a run and prints
  its id; the body it stores is resolved from three layers
  (``rapidpipe.foundation.layers``): ``FILE``, the shipped
  ``foundation-thin.toml`` by default (TOML, or JSON when its name ends in
  ``.json``), the dataset's overlay, then each ``--set`` in order; the run
  also stores the body's hash and each layer's name and SHA-256, which
  ``run show`` prints as ``layers``;
- ``official change --group FILE --who --why`` applies one change group
  and prints its id; ``FILE`` is a JSON list of items, each with
  ``close`` (a binding id) and/or ``open`` (``{"product", "key", "job"}``
  or ``{"survey", "dataset", "run"}``), as ``rapidpipe.foundation.change``
  takes them;
- ``official undo <group-id> --who --why`` applies the inverse of a group
  and prints the new group's id;
- ``run show <id>`` and ``job show <id>`` print the run or job as JSON;
- ``job local --run --stage --scope JSON [--inputs JSON] [--expects JSON]``
  records a job and runs it; ``job run --job <id>``
  runs an existing job's next attempt. Both run the stage's ``run(job)``
  in this process under ``RAPIDPIPE_OUTPUTS_ROOT`` and publish it
  (``rapidpipe.foundation.run_job``), printing the publisher's result;
- ``job submit`` takes ``job local``'s arguments and ``--job-definition``
  (or ``--job <id>`` in their place, a new attempt of an existing job),
  records the job and submits one Batch job running ``job run --job <id>``
  in the image (``rapidpipe.launch.submit``) asking for the run's
  ``[batch.memory_mib]`` memory for the stage, recording the Batch job id
  on the row (``job show`` prints it) and printing both ids;
- ``job wait --job N [N ...] [--timeout S] [--interval S]`` blocks until
  every named job is ``published`` or ``failed``, reconciling their runs
  with Batch each check (``rapidpipe.foundation.loop.wait``), and prints the
  states; exit 0, or 75 when the timeout passes first: one blocking waiter
  for hand-submitted jobs;
- ``loop --run --filters --scas --pointings [--passes] --job-definition [--once]``
  carries the selection one stage forward per pass on Batch and ends each
  pass at the Kafka topic (``rapidpipe.foundation.loop``), printing one
  summary line per pass, until a pass finds the selection drained (exit 0)
  or a job of the run has failed (exit 2 after that pass); a job whose
  container Batch ended without ``job run`` recording it is resubmitted
  once (its Batch name ending ``-rerun``) and fails the loop only if the
  rerun's container dies too, and a pass submits at most the run's
  ``[batch] max_submissions_per_pass``; every pass reports ``held_sets``,
  the run's unsent official packet sets the quality gate holds
  (``rapidpipe.foundation.gate``), with the metric and value; ``--job-queue``
  names the prompt queue, and a run whose ``[batch.spot]`` names a Spot queue
  sends a backlog there, one attempt each, with the next attempt and any
  hedged straggler on the prompt queue (``rapidpipe.foundation.loop``), each
  pass reporting ``queues`` and ``attempts``; ``job submit --job-queue`` names
  one submission's queue;
- ``publish --run [--settings FILE]`` produces the run's official packet
  sets not yet stamped to the Kafka topic the ``[kafka]`` table's SSM
  parameters name (``rapidpipe.foundation.publish_kafka``), printing the
  counts; a producer error exits non-zero with the set unstamped.

A refused change group, or a job published but refused its automatic
binding, exits 1 (``ExitCode.FAILURE``) with the binding named on stderr;
a job published under a run that is not live exits 0 with the refusal on
stderr and on its row, so Batch's status means "the job ran". A run, job
or group that does not exist, or an unreadable file, exits 64. Data goes to stdout, everything else to stderr.

Stage modules are resolved by ``importlib.import_module`` at call time, so
``rapidpipe.cli`` stays the only place that decides which stage code loads.
Plain argparse; no third-party CLI library.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
import time
import tomllib
from pathlib import Path
from typing import Any, Sequence

from rapidpipe import __version__
from rapidpipe import foundation, launch
from rapidpipe import log as rapidpipe_log
from rapidpipe.cli import stagectl
from rapidpipe.db.connection import ConnectionConfigError, ConnectionUnavailable, connect
from rapidpipe.foundation import gate, layers, loop, publish_kafka
from rapidpipe.products import storage
from rapidpipe.exitcodes import ArgumentParser, CommandExit, ExitCode
from rapidpipe.selftest import run as run_selftest
from rapidpipe.selftest.runner import STAGE_NAMES as SELFTEST_STAGE_NAMES
from rapidpipe.stages.contract import STAGE_NAMES


#: The shipped settings body; ``publish`` reads its ``[kafka]`` table by default.
_SHIPPED_SETTINGS = Path(__file__).resolve().parents[1] / "settings" / "foundation-thin.toml"


def _build_parser() -> argparse.ArgumentParser:
    parser = ArgumentParser(
        prog="rapidpipe", description="The RAPID pipeline command-line tool.")
    parser.add_argument(
        "--version", action="version", version=f"rapidpipe {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    stagectl.add_parsers(subparsers)

    selftest_parser = subparsers.add_parser(
        "selftest",
        description="Run one stage's own packaged fixture and check its outputs.",
        help="Run one stage's own packaged fixture and check it.")
    selftest_parser.add_argument(
        "--stage", required=True, choices=SELFTEST_STAGE_NAMES, help="Which stage's fixture to run.")
    selftest_parser.add_argument(
        "--real-tools", action="store_true",
        help="Run the pipeline image's own tools instead of the packaged fakes "
             "(the default). Needs the pipeline image, or a checkout with the "
             "same tools on PATH.")
    selftest_parser.add_argument(
        "--work-dir", default=None,
        help="An empty or new directory to prepare the fixture in "
             "(default: a fresh temporary directory).")
    selftest_parser.add_argument(
        "--output-location", default=None,
        help="Where the stage's manifest and products end up: a local path "
             "or an s3:// prefix (default: <work-dir>/outputs). Every check "
             "runs against a local copy; an s3:// location is uploaded to "
             "only afterwards.")
    selftest_parser.add_argument("--python", default=sys.executable)

    run_parser = subparsers.add_parser("run", help="Create or show a run.",
                                       description="Create or show a run.")
    run_parser.set_defaults(group_parser=run_parser)
    run_subparsers = run_parser.add_subparsers(dest="verb")
    create = run_subparsers.add_parser(
        "create", help="Record a run and print its id.",
        description="Record a run (a recipe someone launched for a reason) and print its id.")
    create.add_argument("--who", required=True)
    create.add_argument("--why", required=True)
    create.add_argument("--revision", required=True, help="The code revision the run is for.")
    create.add_argument("--settings", type=Path, metavar="FILE", default=_SHIPPED_SETTINGS,
                        help="The base settings body: TOML, or JSON when FILE ends in .json "
                             "(default: the shipped foundation-thin.toml).")
    create.add_argument("--overlay", type=Path, metavar="FILE",
                        help="The dataset's overlay: a TOML holding only the keys that differ.")
    create.add_argument("--set", action="append", default=[], dest="sets", metavar="TABLE.key=VALUE",
                        help="One run's override, a TOML value (strings quoted); repeatable, "
                             "applied in order after the overlay.")
    create.add_argument("--survey", required=True)
    create.add_argument("--dataset", required=True)
    run_show = run_subparsers.add_parser("show", help="Print one run as JSON.",
                                         description="Print one run, its live binding and its jobs as JSON.")
    run_show.add_argument("id", type=int)

    job_parser = subparsers.add_parser("job", help="Show, launch or run a job.",
                                       description="Show a job, or run one here and publish it.")
    job_parser.set_defaults(group_parser=job_parser)
    job_subparsers = job_parser.add_subparsers(dest="verb")
    job_show = job_subparsers.add_parser(
        "show", help="Print one job as JSON.",
        description="Print one job, its recorded inputs and its bindings as JSON.")
    job_show.add_argument("id", type=int)
    job_local = job_subparsers.add_parser(
        "local", help="Record a job, run its stage here and publish it.",
        description="Record a job under a run with its inputs, run its stage in this "
                    "process under RAPIDPIPE_OUTPUTS_ROOT, publish it and print the result.")
    job_submit = job_subparsers.add_parser(
        "submit", help="Record a job and submit it to Batch as 'job run'.",
        description="Record a job under a run with its inputs and submit one Batch job "
                    "running 'job run --job <id>' under RAPIDPIPE_BATCH_JOB_QUEUE, "
                    "writing under RAPIDPIPE_OUTPUTS_ROOT; print the job and Batch job ids.")
    job_submit.add_argument("--job-definition", required=True,
                            help="The Batch job definition: name, name:revision or ARN.")
    job_submit.add_argument("--job", type=int,
                            help="Submit a new attempt of this existing job instead of recording one.")
    job_submit.add_argument("--job-queue",
                            help="The Batch job queue (default RAPIDPIPE_BATCH_JOB_QUEUE); the run's "
                                 "[batch.spot] queue gets one attempt.")
    for verb in (job_local, job_submit):
        new_only = verb is job_local       # job submit --job names an existing job instead
        verb.add_argument("--run", required=new_only, type=int)
        verb.add_argument("--stage", required=new_only)
        for name, meaning in (("scope", "the job's scope, a JSON object"),
                              ("inputs", "a JSON list of {product, key, source_job}"),
                              ("expects", "a JSON list of {product, key, binding}")):
            verb.add_argument(f"--{name}", required=new_only and name == "scope", type=_json_argument,
                              default=None if name == "scope" else [], metavar="JSON", help=meaning)
    job_run = job_subparsers.add_parser(
        "run", help="Run an existing job's next attempt here and publish it.",
        description="Run an existing job's next attempt in this process and publish it; "
                    "a published job stops before touching a file.")
    job_run.add_argument("--job", required=True, type=int)
    job_wait = job_subparsers.add_parser(
        "wait", help="Wait until every named job is published or failed.",
        description="Block until every named job is published or failed (a container Batch ended "
                    "without the row recording it counts as failed, Batch's own retry as not "
                    "ended); print the states; exit 0, or 75 when --timeout passes first.")
    job_wait.add_argument("--job", required=True, type=int, nargs="+", dest="jobs", metavar="N")
    job_wait.add_argument("--timeout", type=float, default=7000.0,
                          help="Seconds before giving up with exit 75 (default 7000).")
    job_wait.add_argument("--interval", type=float, default=30.0,
                          help="Seconds between checks (default 30).")

    official_parser = subparsers.add_parser(
        "official", help="Change official bindings, or undo a change.",
        description="Change official bindings in one recorded group, or undo a group.")
    official_parser.set_defaults(group_parser=official_parser)
    official_subparsers = official_parser.add_subparsers(dest="verb")
    change = official_subparsers.add_parser(
        "change", help="Apply one change group and print its id.",
        description="Apply one change group in one transaction and print its id.")
    change.add_argument("--group", required=True, type=Path, metavar="FILE",
                        help="A JSON list of items, each with 'close' and/or 'open'.")
    change.add_argument("--who", required=True)
    change.add_argument("--why", required=True)
    undo = official_subparsers.add_parser(
        "undo", help="Apply the inverse of a change group and print the new group's id.",
        description="Apply the inverse of a change group and print the new group's id.")
    undo.add_argument("group_id", type=int, metavar="group-id")
    undo.add_argument("--who", required=True)
    undo.add_argument("--why", required=True)

    publish_parser = subparsers.add_parser(
        "publish", help="Produce a run's official packet sets to the Kafka topic.",
        description="Produce every official packet set of a run not yet stamped "
                    "kafka_published_at to the Kafka topic, waiting for every "
                    "acknowledgement before stamping the set; print the counts.")
    publish_parser.add_argument("--run", required=True, type=int)
    publish_parser.add_argument(
        "--settings", type=Path, metavar="FILE", default=_SHIPPED_SETTINGS,
        help="A TOML file whose [kafka] table names the SSM parameters "
             "(default: the shipped foundation-thin.toml).")

    loop_parser = subparsers.add_parser(
        "loop", help="Carry a selection one stage forward per pass, through Kafka.",
        description="Admit what is new under the run's [intake] source_root for the selection "
                    "and submit every ready job one stage forward on Batch, never waiting; end "
                    "each pass by producing the run's newly official packet sets to the topic. "
                    "Repeat every --interval seconds until a pass finds nothing new, pending or "
                    "to submit (exit 0); a failed job of the run exits 2 after its pass.")
    loop_parser.add_argument("--run", required=True, type=int)
    loop_parser.add_argument("--filters", required=True, help="Comma-separated, e.g. F146.")
    loop_parser.add_argument("--scas", required=True, help="Comma-separated SCA numbers.")
    loop_parser.add_argument("--pointings", required=True,
                             help="Comma-separated VVV_EEEE (visit, exposure), or the observing "
                                  "sequence's rows for an OpenUniverse run, or 'all'.")
    loop_parser.add_argument("--passes", default="all",
                             help="Comma-separated survey pass numbers, e.g. 1,2,3,4, or 'all' (default).")
    loop_parser.add_argument("--job-definition", required=True,
                             help="The Batch job definition the run's revision is registered as.")
    loop_parser.add_argument("--job-queue",
                             help="The prompt (on-demand) Batch job queue (default "
                                  "RAPIDPIPE_BATCH_JOB_QUEUE); the run's [batch.spot] names the Spot one.")
    loop_parser.add_argument("--max-intake", type=int, default=100,
                             help="At most this many intake jobs per pass (default 100).")
    loop_parser.add_argument("--interval", type=float, default=120.0,
                             help="Seconds between passes (default 120).")
    loop_parser.add_argument("--once", action="store_true", help="Run one pass and exit 0.")

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

    A thin wrapper over :func:`rapidpipe.selftest.run`. Exit codes: 0 on a
    full pass, 1 on a fixture mismatch, and the stage's own exit code on a
    stage failure.
    """
    return run_selftest(
        stage=args.stage, real_tools=args.real_tools, work_dir=args.work_dir,
        output_location=args.output_location, python=args.python)


def _read(path: Path, what: str) -> Any:
    try:
        text = path.read_text()
        if what == "settings" and path.suffix != ".json":
            return tomllib.loads(text)
        return json.loads(text)
    except (OSError, ValueError) as exc:
        raise CommandExit(int(ExitCode.USAGE), f"cannot read the {what} file {path}: {exc}") from exc


def _json_argument(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not JSON: {exc}") from exc


#: Stage names whose module name differs (``import`` is a Python keyword).
_STAGE_MODULES = {"import": "import_products"}


def _stage_run(stage: str):
    """The stage's ``run(job)``, from ``rapidpipe.stages.<module>``."""
    module_name = f"rapidpipe.stages.{_STAGE_MODULES.get(stage, stage.replace('-', '_'))}"
    try:
        stage_run = getattr(importlib.import_module(module_name), "run", None)
    except ModuleNotFoundError:
        stage_run = None
    if stage_run is None:
        raise CommandExit(int(ExitCode.USAGE), f"stage {stage!r} has no run(job) in {module_name}")
    return stage_run


def _run_job(args: argparse.Namespace) -> int:
    root = os.environ.get("RAPIDPIPE_OUTPUTS_ROOT")
    if not root:
        raise CommandExit(int(ExitCode.USAGE), "RAPIDPIPE_OUTPUTS_ROOT is not set")

    def connect_job():
        return connect(application_name=f"rapidpipe-job-{args.verb}")

    if args.verb == "submit" and args.job is not None:      # a new attempt of an existing job
        job_id = args.job
        with connect_job() as conn:
            stage = foundation.job_show(conn, job_id)["stage"]
    elif args.verb in ("local", "submit"):
        if args.run is None or args.stage is None or args.scope is None:
            raise CommandExit(int(ExitCode.USAGE), "--run, --stage and --scope are required without --job")
        stage, stage_run = args.stage, _stage_run(args.stage)
        with connect_job() as conn:
            job_id = foundation.create_job(conn, args.run, stage, args.scope, args.inputs,
                                           args.expects)
    else:
        job_id = args.job
        with connect_job() as conn:
            stage = foundation.job_show(conn, job_id)["stage"]
        stage_run = _stage_run(stage)
    if args.verb == "submit":
        try:
            with connect_job() as conn:
                settings = foundation.run_show(conn, foundation.job_show(conn, job_id)["run"])["settings"]
                spot = launch.spot(settings)
                batch_job = launch.submit(job_id, stage, job_definition=args.job_definition, conn=conn,
                                          memory_mib=launch.memory_mib(settings, stage),
                                          environment_extra=launch.job_environment(settings),
                                          queue=args.job_queue,
                                          one_attempt=bool(spot) and args.job_queue == spot["queue"])
        except launch.LaunchError as exc:
            raise CommandExit(int(ExitCode.USAGE), f"job {job_id} recorded, not submitted: {exc}")
        _print_json({"job": job_id, "batch_job": batch_job})
        return int(ExitCode.SUCCESS)
    result = foundation.run_job(connect_job, job_id, root, stage_run)
    if stage == "packet" and result["published"] and result["group"] is not None:
        result["kafka"] = _send_own_set(connect_job, job_id)
    _print_json(result)
    if result["published"] and result["refusal"]:
        sys.stderr.write(f"rapidpipe job {args.verb}: job {job_id} published, binding refused: "
                         f"{result['refusal']}\n")
        if result["live"] is not False:
            return int(ExitCode.FAILURE)
    return int(ExitCode.SUCCESS)


def _send_own_set(connect_job, job_id: int) -> dict:
    """A packet job's last act: its own official set to the topic its run's ``[kafka]`` table names.

    Returns :func:`~rapidpipe.foundation.publish_kafka.deliver`'s result (``held``
    when the quality gate holds the set: nothing sent), or ``{"error": ...}``: a set this could not send stays unstamped for the
    loop's publish sweep, and the job, published and bound, still exits 0.
    """
    logger = logging.getLogger("rapidpipe.cli")
    logger.info("packet set %d published and bound; producing it to the topic", job_id)
    try:
        with connect_job() as conn:
            settings = foundation.run_show(conn, foundation.job_show(conn, job_id)["run"])["settings"]
            endpoint = publish_kafka.endpoint(settings["kafka"])
            producer = publish_kafka.producer(endpoint["bootstrap_servers"], endpoint["max_request_bytes"])
            try:
                return publish_kafka.deliver(conn, job_id, producer, endpoint["topic"])
            finally:
                producer.close(timeout=publish_kafka.ACK_TIMEOUT_S)
    except Exception as error:  # noqa: BLE001 - the sweep sends what this could not
        logger.error("packet set %d: not produced (%s); left unstamped for the loop's publish sweep",
                     job_id, error)
        return {"error": str(error)}


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _publish(args: argparse.Namespace) -> int:
    settings = _read(args.settings, "settings").get("kafka")
    if not isinstance(settings, dict):
        raise CommandExit(int(ExitCode.USAGE), f"the settings file {args.settings} has no [kafka] table")
    endpoint = publish_kafka.endpoint(settings)
    producer = publish_kafka.producer(endpoint["bootstrap_servers"], endpoint["max_request_bytes"])
    try:
        result = publish_kafka.publish(lambda: connect(application_name="rapidpipe-publish"),
                                       args.run, producer, endpoint["topic"])
    finally:
        producer.close(timeout=publish_kafka.ACK_TIMEOUT_S)
    _print_json({**result, "max_request_bytes": endpoint["max_request_bytes"]})
    return int(ExitCode.SUCCESS)


def _loop(args: argparse.Namespace) -> int:
    def connect_loop():
        return connect(application_name="rapidpipe-loop")

    with connect_loop() as conn:
        settings = foundation.run_show(conn, args.run)["settings"]
    intake = settings["intake"]
    try:
        selection = loop.Selection.parse(args.filters, args.scas, args.pointings, args.passes,
                                         intake.get("reader", "soc-l2"))
    except ValueError as exc:
        raise CommandExit(int(ExitCode.USAGE), str(exc)) from exc
    s3 = storage.s3_client(unsigned=bool(intake.get("unsigned")))
    batch = launch.batch_client()
    spot = launch.spot(settings)

    def submit(item: dict) -> int:
        with connect_loop() as conn:
            job_id = foundation.create_job(conn, args.run, item["stage"], item["scope"],
                                           item["inputs"], item["expects"])
            launch.submit(job_id, item["stage"], job_definition=args.job_definition, conn=conn,
                          memory_mib=launch.memory_mib(settings, item["stage"]),
                          environment_extra=launch.job_environment(settings),
                          queue=item.get("queue") or args.job_queue, one_attempt="queue" in item)
        return job_id

    def rerun(job_id: int, stage: str) -> None:          # always the prompt queue
        with connect_loop() as conn:
            launch.submit(job_id, stage, job_definition=args.job_definition, conn=conn,
                          memory_mib=launch.memory_mib(settings, stage), name_suffix=loop.RERUN_SUFFIX,
                          environment_extra=launch.job_environment(settings), queue=args.job_queue)

    def publish() -> dict:
        with connect_loop() as conn:
            if not publish_kafka.unpublished_sets(conn, args.run):
                return {"packets": 0, "sets": [], "waiting": publish_kafka.settling_sets(conn, args.run)}
        endpoint = publish_kafka.endpoint(settings["kafka"])
        producer = publish_kafka.producer(endpoint["bootstrap_servers"], endpoint["max_request_bytes"])
        try:
            return publish_kafka.publish(connect_loop, args.run, producer, endpoint["topic"])
        finally:
            producer.close(timeout=publish_kafka.ACK_TIMEOUT_S)

    def held_sets() -> list[dict]:
        with connect_loop() as conn:
            answers = [{"job": job, **gate.decide(conn, job, settings.get("gate"))}
                       for job in publish_kafka.unpublished_sets(conn, args.run)]
        return [{k: a[k] for k in ("job", "difference_job", "metric", "value", "bound", "reason")}
                for a in answers if not a["send"]]

    while True:
        started = time.monotonic()
        stems = loop.list_stems(s3, intake["source_root"], selection)
        summary = loop.run_pass(connect_loop, args.run, selection, stems, submit, publish,
                                args.max_intake, batch, rerun, held_sets, spot)
        summary["seconds"] = round(time.monotonic() - started, 1)
        summary["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        print(json.dumps(summary, sort_keys=True), flush=True)
        if summary["failed"]:
            sys.stderr.write(f"rapidpipe loop: failed jobs of run {args.run}: "
                             f"{', '.join(str(f['job']) for f in summary['failed'])}\n")
            return int(ExitCode.INCOMPLETE)
        if args.once or summary["drained"]:
            return int(ExitCode.SUCCESS)
        time.sleep(max(0.0, args.interval - (time.monotonic() - started)))


def _foundation_command(args: argparse.Namespace) -> int:
    command = (args.command, getattr(args, "verb", None))
    if command == ("run", "create"):
        try:
            settings, record = layers.resolve(args.settings, args.overlay, args.sets)
        except layers.LayerError as exc:
            raise CommandExit(int(ExitCode.USAGE), str(exc)) from exc
        with connect(application_name="rapidpipe-run-create") as conn:
            print(foundation.create_run(conn, args.who, args.why, args.revision, settings,
                                        args.survey, args.dataset, layers=record))
    elif command == ("run", "show"):
        with connect(application_name="rapidpipe-run-show") as conn:
            _print_json(foundation.run_show(conn, args.id))
    elif command in (("job", "local"), ("job", "run"), ("job", "submit")):
        return _run_job(args)
    elif command == ("job", "wait"):
        result = loop.wait(lambda: connect(application_name="rapidpipe-job-wait"), args.jobs,
                           launch.batch_client(), args.timeout, args.interval)
        _print_json(result)
        return int(ExitCode.SUCCESS) if result["ended"] else int(ExitCode.TRANSIENT_FAILURE)
    elif command == ("job", "show"):
        with connect(application_name="rapidpipe-job-show") as conn:
            _print_json(foundation.job_show(conn, args.id))
    elif args.command == "publish":
        return _publish(args)
    elif args.command == "loop":
        return _loop(args)
    elif command == ("official", "change"):
        group = _read(args.group, "group")
        if not isinstance(group, list) or not all(isinstance(item, dict) for item in group):
            raise CommandExit(int(ExitCode.USAGE), f"the group file {args.group} is not a list of items")
        with connect(application_name="rapidpipe-official-change") as conn:
            print(foundation.change(conn, group, args.who, args.why))
    elif command == ("official", "undo"):
        with connect(application_name="rapidpipe-official-undo") as conn:
            print(foundation.undo(conn, args.group_id, args.who, args.why))
    else:
        args.group_parser.print_help(sys.stderr)
        return int(ExitCode.USAGE)
    return int(ExitCode.SUCCESS)


def main(argv: Sequence[str] | None = None) -> int:
    # Configures Python's root logger with the same UTC line shape a stage
    # invocation uses, before argparse sees argv, so an invalid
    # RAPIDPIPE_LOG_LEVEL is a usage error (exit 64).
    try:
        rapidpipe_log.configure_root()
    except ValueError as exc:
        sys.stderr.write(
            f"rapidpipe: invalid {rapidpipe_log.LEVEL_ENV_VAR}: {exc}\n")
        return int(ExitCode.USAGE)
    parser = _build_parser()
    # "rapidpipe stage <name> ..." (the form a Batch job runs) is
    # "rapidpipe stage run <name> ...": rewritten before argparse sees it.
    argv_list = stagectl.rewrite_legacy_argv(
        list(argv) if argv is not None else sys.argv[1:])
    args = parser.parse_args(argv_list)
    where = " ".join(filter(None, (args.command, getattr(args, "verb", None))))

    # The unclassified-error boundary: an unexpected exception escaping a
    # command is logged with its traceback and exits 70. SystemExit and
    # KeyboardInterrupt are not Exception subclasses and pass through.
    try:
        if args.command == "stage":
            return stagectl.dispatch(args, _run_stage_command)
        if args.command == "selftest":
            return _run_selftest_command(args)
        if args.command in ("run", "job", "official", "publish", "loop"):
            return _foundation_command(args)
    except CommandExit as exc:
        sys.stderr.write(f"rapidpipe {where}: {exc}\n")
        return exc.code
    except foundation.Refused as exc:
        sys.stderr.write(f"rapidpipe {where}: refused: {exc}\n")
        return int(ExitCode.FAILURE)
    except LookupError as exc:
        sys.stderr.write(f"rapidpipe {where}: {exc.args[0] if exc.args else exc}\n")
        return int(ExitCode.USAGE)
    except ConnectionConfigError as exc:
        sys.stderr.write(f"rapidpipe {where}: database configuration error: {exc}\n")
        return int(ExitCode.USAGE)
    except ConnectionUnavailable as exc:
        sys.stderr.write(f"rapidpipe {where}: database unavailable: {exc}\n")
        return int(ExitCode.TRANSIENT_FAILURE)
    except Exception:  # noqa: BLE001 - the unclassified-error boundary
        logging.getLogger("rapidpipe.cli").exception("rapidpipe %s: unexpected error", where)
        return int(ExitCode.STAGE_ERROR)

    parser.print_help()
    return int(ExitCode.SUCCESS) if args.command is None else int(ExitCode.USAGE)


if __name__ == "__main__":
    sys.exit(main())
