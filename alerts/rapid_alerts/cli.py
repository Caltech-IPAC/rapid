"""
Command-line alert production (successor to running produce_alert.py).

Usage:
    python -m rapid_alerts.cli <source_id> [--kafka] [--cutout-dir DIR]
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from .serialize import produce_alert

logging.basicConfig(level=logging.INFO)


def make_provider(name, cutout_dir=None):
    if name == "db":
        # RAPIDDB lives at <repo>/database/modules/utils/rapid_db.py
        repo_root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repo_root))
        from database.modules.utils.rapid_db import RAPIDDB
        from .providers.database import DatabaseProvider
        return DatabaseProvider(RAPIDDB(), cutout_dir=cutout_dir)
    if name == "filesystem":
        from .providers.filesystem import FilesystemProvider
        return FilesystemProvider(cutout_dir)
    raise ValueError(f"Unknown provider: {name}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Produce one RAPID alert")
    parser.add_argument("source_id", type=int, help="source ID (sources.sid)")
    parser.add_argument("--provider", choices=["db", "filesystem"],
                        default="db", help="data backend (default: db)")
    parser.add_argument("--cutout-dir", default=None,
                        help="directory containing cutout FITS files")
    parser.add_argument("--kafka", action="store_true",
                        help="publish to Kafka ($KAFKA_BROKER, default "
                             "localhost:9092)")
    parser.add_argument("--topic", default="alerts", help="Kafka topic")
    args = parser.parse_args(argv)

    provider = make_provider(args.provider, cutout_dir=args.cutout_dir)

    producer = None
    if args.kafka:
        from confluent_kafka import Producer
        producer = Producer({
            "bootstrap.servers": os.environ.get("KAFKA_BROKER",
                                                "localhost:9092"),
            "message.max.bytes": "15728640",
        })

    alert_bytes = produce_alert(provider, args.source_id,
                                producer=producer, topic=args.topic)
    print(f"Alert produced: {len(alert_bytes)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
