"""
File:    live_alert_probe.py

ONE alert-production unit through the wired path to the internal topic, with
the packet size and per-chip alert count the step-4 co-design asks for by name
(gate 9: "the internal phase measures real packet size and per-chip alert
counts for the sizing model").

It submits ONE unit. That bound is the point: the design's internal phase is a
measurement, not a run, and the publication cap for this work is one chip.

The measurement is taken from the SERIALIZED BYTES, not estimated from the
alert dict — a packet's size is what goes on the wire after Avro encoding and
Glue framing, and an estimate from the Python object would answer a different
question than the sizing model asks.

Usage (inside the pipeline image, with the submission environment set):

    python3.11 -m pipeline.registration.test.live_alert_probe --release <id>
"""

import argparse
import base64
import json
import os
import sys
import uuid


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True,
                        help="release identity the emission is scoped to")
    parser.add_argument("--dry-run", action="store_true",
                        help="gather and report, submit nothing")
    parser.add_argument("--measure-only", action="store_true",
                        help="assemble and serialize this unit's alerts "
                             "locally and report the sizes, WITHOUT "
                             "publishing or claiming the watermark")
    args = parser.parse_args(argv)

    from database.modules.utils.rapid_db import RAPIDDB
    from submission.gathering import gather_alert_production_units
    from submission.routes import JOB_TYPE_ALERT_PRODUCTION

    handle = RAPIDDB()
    if getattr(handle, "conn", None) is None or handle.exit_code >= 64:
        print("*** cannot reach the database; quitting")
        return 64

    units = list(gather_alert_production_units(handle, args.release, limit=1))
    if not units:
        print("*** no unit awaiting alert emission under release {}. Either "
              "nothing has promoted since the watermark was initialized, or "
              "every promoted unit has already emitted — both are ordinary "
              "states, not failures.".format(args.release))
        return 65

    unit = units[0]
    pid = unit.fields["difference_image_pid"]
    print(">> unit {}: attempt={} pid={} product={} role_from={}".format(
        unit.key, unit.fields["attempt_id"], pid,
        unit.fields["difference_image_product"],
        unit.fields["role_resolved_from"]))

    if args.dry_run:
        print(">> --dry-run: gathered only, nothing submitted")
        return 0

    if args.measure_only:
        return _measure(handle, pid)

    from pipeline import seams
    from pipeline.operator.submission import submission_env
    from database.modules.utils.rapid_db_connect import ConnectionExecutor

    parameters = None
    if os.environ.get("RAPID_PARAMETERS_B64"):
        parameters = json.loads(
            base64.b64decode(os.environ["RAPID_PARAMETERS_B64"]))

    env = submission_env(JOB_TYPE_ALERT_PRODUCTION, parameters=parameters)
    run_id = "alert-probe-{}".format(uuid.uuid4().hex[:12])
    print(">> run_id: {}".format(run_id))
    print(">> queue={} job_definition={}".format(
        env["queue"], env["job_definition"]))

    executor = ConnectionExecutor(handle.conn)
    results = seams.submit_gathered(
        [unit], job_type=JOB_TYPE_ALERT_PRODUCTION,
        queue=env["queue"], job_definition=env["job_definition"],
        binding=env["binding"],
        manifest_bucket=env["manifest_bucket"],
        manifest_prefix=env["manifest_prefix"],
        s3_client=env["s3_client"], batch_client=env["batch_client"],
        execute=executor.execute, run_id=run_id, reason="alert-probe")

    for submission, attempt_ids in results:
        print(">> submitted job {} ({} children), attempt rows: {}".format(
            submission.job_id, submission.array_size, attempt_ids))
    return 0


def _measure(handle, pid):
    """Assemble and serialize this chip's alerts; report the sizes.

    Publishes NOTHING and claims no watermark — this is the sizing
    measurement on its own, so it can be taken without spending the one
    publication the caps allow.
    """
    from alerts.produce import (assemble_alert_for_source, load_schema,
                                serialize_alert)
    from alerts.providers import AlertDataProvider
    from pipeline.stages.alert_production import select_candidates

    provider = AlertDataProvider(handle)
    schema = load_schema()

    sources = list(provider.iter_sources(int(pid)))
    selected = select_candidates(sources)
    print(">> pid {}: {} source(s), {} selected by the placeholder rule"
          .format(pid, len(sources), len(selected)))

    sizes = []
    dropped = {}
    for source in selected:
        try:
            payload = serialize_alert(
                assemble_alert_for_source(provider, source), schema=schema)
        except Exception as exc:                      # noqa: BLE001
            reason = type(exc).__name__
            dropped[reason] = dropped.get(reason, 0) + 1
            continue
        sizes.append(len(payload))

    if not sizes:
        print(">> no alert could be assembled; dropped: {}".format(dropped))
        return 66

    sizes.sort()
    total = sum(sizes)
    print(">> MEASUREMENT: {} packet(s), total {} bytes ({:.2f} MiB)".format(
        len(sizes), total, total / (1024 * 1024)))
    print(">>   min {} / median {} / max {} bytes".format(
        sizes[0], sizes[len(sizes) // 2], sizes[-1]))
    print(">>   mean {:.0f} bytes".format(total / len(sizes)))
    if dropped:
        print(">>   dropped by reason: {}".format(dropped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
