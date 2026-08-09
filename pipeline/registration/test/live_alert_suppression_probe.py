"""
File:    live_alert_suppression_probe.py

Drive the REAL `produce_alerts` stage against the LIVE watermark, for a unit
that has already emitted, and show that it publishes nothing.

Why this exists as a separate probe: the suppression path is the half of
gate 4 that a publication probe cannot demonstrate. A run that publishes
proves emission works; only a run against an already-claimed unit proves that
a second attempt at the same (unit, release) stays silent — and that is the
property protecting the stream from replays, re-executions and serial-later
registrations.

It is safe to run repeatedly and needs no publication budget: the claim is
taken before any producer is constructed, so a suppressed unit never reaches
Kafka at all. That ordering is the thing under test.

Usage (inside the pipeline image, with the DB environment set):

    python3.11 -m pipeline.registration.test.live_alert_suppression_probe \
        --release vpo-restructure-probe --exposure 20 --sca 7
"""

import argparse
import sys


class _Unit:
    def __init__(self, exposure, sca, fields):
        self.exposure = exposure
        self.sca = sca
        self.fields = fields
        self.facts = None


class _Context:
    """The stage context surface `produce_alerts` uses, over the live DB."""

    def __init__(self, unit, parameters):
        self.unit = unit
        self.parameters = dict(parameters)
        self.provenance = {}
        self.logger = _Logger()

    def parameter(self, name):
        return self.parameters.get(name)

    def record_effect(self, rows_written=0, rows_removed=0, **extra):
        self.provenance.update(extra)


class _Logger:
    def info(self, message, *args):
        print("   INFO  " + (message % args if args else message))

    def warning(self, message, *args):
        print("   WARN  " + (message % args if args else message))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--exposure", type=int, required=True)
    parser.add_argument("--sca", type=int, required=True)
    parser.add_argument("--attempt", type=int, default=0)
    parser.add_argument("--pid", type=int, default=0)
    args = parser.parse_args(argv)

    from pipeline.stages import alert_production

    unit = _Unit(args.exposure, args.sca,
                 {"attempt_id": args.attempt, "release_identity": args.release,
                  "difference_image_pid": args.pid,
                  "job_type": "alert-production"})
    # Deliberately a topic that WOULD be refused if the guard were reached, so
    # a run that somehow got past the claim would fail loudly here rather than
    # publishing. The suppressed path returns before the topic is resolved.
    context = _Context(unit, {"kafka/topic": "rapid.internal.alerts.v1",
                              "kafka/bootstrap-servers": "unreachable:9098"})

    print(">> driving produce_alerts for unit {}/{} under release {}".format(
        args.exposure, args.sca, args.release))
    alert_production.produce_alerts(context)

    print(">> effect counts recorded:")
    for key in sorted(context.provenance):
        print("     {:<28} {}".format(key, context.provenance[key]))

    suppressed = context.provenance.get("emissions_suppressed")
    published = context.provenance.get("alerts_published")
    if suppressed == 1 and published == 0:
        print(">> SUPPRESSED as ruled: nothing published, suppression "
              "recorded, attempt closes successfully")
        return 0
    print("!! expected a suppression; got suppressed={} published={}".format(
        suppressed, published))
    return 1


if __name__ == "__main__":
    sys.exit(main())
