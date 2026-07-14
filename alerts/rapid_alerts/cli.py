"""
Command-line alert production.

Usage:
    python -m rapid_alerts.cli <source_id> [--kafka]          # one alert
    python -m rapid_alerts.cli <pid> --chip [--kafka]         # whole chip
"""

import argparse
import logging
import os
import sys
from pathlib import Path

# Support both `python -m rapid_alerts.cli` (module) and `python cli.py` (script).
if __package__:
    from .produce import produce_alert, produce_chip
    from .providers import DatabaseProvider
else:
    # Run directly as a script: no package context, so make the package
    # importable by its name and switch to absolute imports.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from rapid_alerts.produce import produce_alert, produce_chip
    from rapid_alerts.providers import DatabaseProvider

logging.basicConfig(level=logging.INFO)


def make_provider(diff_flavor="sfft"):
    """Connect to the RAPID operations database.

    A future file-system or sqlite backend would be constructed here instead
    (see the porting notes at the bottom of providers.py).
    """
    # RAPIDDB lives at <repo>/database/modules/utils/rapid_db.py
    repo_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo_root))
    from database.modules.utils.rapid_db import RAPIDDB
    return DatabaseProvider(RAPIDDB(), diff_flavor=diff_flavor)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Produce RAPID alerts")
    parser.add_argument("id", type=int,
                        help="source ID (sources.sid), or with --chip a "
                             "processing ID (diffimages.pid)")
    parser.add_argument("--chip", action="store_true",
                        help="produce alerts for every source on the chip "
                             "with this processing ID")
    parser.add_argument("--kafka", action="store_true",
                        help="publish to Kafka ($KAFKA_BROKER, default "
                             "localhost:9092)")
    parser.add_argument("--topic", default="alerts", help="Kafka topic")
    parser.add_argument("--diff-flavor", choices=["sfft", "zogy"],
                        default="sfft",
                        help="which differencing algorithm's image feeds "
                             "cutoutDifference (default: %(default)s)")
    args = parser.parse_args(argv)

    provider = make_provider(diff_flavor=args.diff_flavor)

    producer = None
    if args.kafka:
        from confluent_kafka import Producer
        producer = Producer({
            "bootstrap.servers": os.environ.get("KAFKA_BROKER",
                                                "localhost:9092"),
            "message.max.bytes": "15728640",
        })

    if args.chip:
        count = produce_chip(provider, args.id,
                             producer=producer, topic=args.topic)
        print(f"Chip pid={args.id}: {count} alerts produced")
    else:
        alert_bytes = produce_alert(provider, args.id,
                                    producer=producer, topic=args.topic)
        print(f"Alert produced: {len(alert_bytes)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
