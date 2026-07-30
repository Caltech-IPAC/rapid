"""
Command-line alert production.

The selector implies the mode:

    python -m rapid_alerts.cli <source_id>                  # one alert
    python -m rapid_alerts.cli --pid <pid>                  # one diff image
    python -m rapid_alerts.cli --exposure <expid> --sca <n> # same, by
                                                            # exposure + SCA
"""

import argparse
import contextlib
import logging
import os
import sys
from pathlib import Path

# Support both `python -m rapid_alerts.cli` (module) and `python cli.py` (script).
if __package__:
    from .produce import batch_produce, open_alert_archive, produce_alert
    from .providers import AlertDataProvider
else:
    # Run directly as a script: no package context, so make the package
    # importable by its name and switch to absolute imports.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from rapid_alerts.produce import (batch_produce, open_alert_archive,
                                      produce_alert)
    from rapid_alerts.providers import AlertDataProvider


def make_provider(diff_flavor="sfft"):
    """Connect to the RAPID operations database.

    A future file-system or sqlite backend would be constructed here instead
    (see the porting notes at the bottom of providers.py).
    """
    # RAPIDDB lives at <repo>/database/modules/utils/rapid_db.py
    repo_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo_root))
    from database.modules.utils.rapid_db import RAPIDDB
    db = RAPIDDB()
    # RAPIDDB reports connection failure by exit_code/conn=None rather than
    # raising; fail here with the actual problem, not an AttributeError on
    # conn.cursor() at first query.
    if getattr(db, "conn", None) is None or db.exit_code >= 64:
        raise SystemExit(
            "cannot connect to the RAPID database: check that DBSERVER, "
            "DBPORT, DBNAME, DBUSER and DBPASS are set in this shell, and "
            "that this machine can reach the DB (VPN up / EC2 security "
            "group allows it)")
    return AlertDataProvider(db, diff_flavor=diff_flavor)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Produce RAPID alerts. The selector implies the mode: "
                    "a source ID produces one alert; --pid or "
                    "--exposure/--sca batch-produce a difference image.")
    parser.add_argument("sid", type=int, nargs="?",
                        help="source ID (sources.sid): produce one alert")
    parser.add_argument("--pid", type=int,
                        help="batch-produce every source on this "
                             "difference image (diffimages.pid)")
    parser.add_argument("--exposure", type=int, metavar="EXPID",
                        help="batch-produce one exposure + SCA (needs "
                             "--sca); uses the newest vbest>0 processing")
    parser.add_argument("--sca", type=int,
                        help="SCA number (1-18), goes with --exposure")
    parser.add_argument("--kafka", action="store_true",
                        help="publish to Kafka ($KAFKA_BROKER, default "
                             "localhost:9092)")
    parser.add_argument("--topic", default="alerts", help="Kafka topic")
    parser.add_argument("--save", metavar="FILE",
                        help="also write the run's alerts to one Avro "
                             "object-container file (self-describing; read "
                             "back with fastavro.reader)")
    parser.add_argument("--no-compress", action="store_true",
                        help="store --save archives uncompressed; by "
                             "default they are deflate-compressed, the "
                             "MAST delivery format")
    parser.add_argument("--diff-flavor", choices=["sfft", "zogy"],
                        default="sfft",
                        help="which differencing algorithm's image feeds "
                             "cutoutDifference (default: %(default)s)")
    parser.add_argument("--log-level", default="WARNING",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="diagnostic verbosity on stderr; quiet by "
                             "default, use INFO for per-step progress "
                             "(default: %(default)s)")
    args = parser.parse_args(argv)

    # the CLI is the application, so logging policy is decided here (library
    # modules only ever create loggers); log records go to stderr, the final
    # result line goes to stdout
    logging.basicConfig(level=getattr(logging, args.log_level))

    if (args.exposure is None) != (args.sca is None):
        parser.error("--exposure and --sca must be given together")
    selectors = (args.sid is not None) + (args.pid is not None) \
        + (args.exposure is not None)
    if selectors != 1:
        parser.error("give exactly one of: a source ID, --pid, or "
                     "--exposure with --sca")

    provider = make_provider(diff_flavor=args.diff_flavor)

    producer = None
    if args.kafka:
        from confluent_kafka import Producer
        producer = Producer({
            "bootstrap.servers": os.environ.get("KAFKA_BROKER",
                                                "localhost:9092"),
            "message.max.bytes": "15728640",
        })

    archive_ctx = (open_alert_archive(
                       args.save,
                       codec="null" if args.no_compress else "deflate")
                   if args.save else contextlib.nullcontext())
    with archive_ctx as archive:
        if args.sid is not None:
            alert_bytes = produce_alert(provider, args.sid,
                                        producer=producer, topic=args.topic,
                                        archive=archive)
            print(f"Alert produced: {len(alert_bytes)} bytes")
        else:
            pid = (args.pid if args.pid is not None
                   else provider.resolve_pid(args.exposure, args.sca))
            count = batch_produce(provider, pid,
                                  producer=producer, topic=args.topic,
                                  archive=archive)
            print(f"pid={pid}: {count} alerts produced")
    if args.save:
        print(f"saved to {args.save}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
