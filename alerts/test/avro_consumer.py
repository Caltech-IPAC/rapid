#!/usr/bin/env python

import io
import sys
from pathlib import Path

from confluent_kafka import Consumer
import fastavro

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from alerts.produce import load_schema

if __name__ == '__main__':

    config = {
        # User-specific properties that you must set
        'bootstrap.servers': 'localhost:9092',
        'session.timeout.ms': 60000,
        'group.id': 'avro_kafka_test'
    }

    # Create Consumer instance
    consumer = Consumer(config)

    # Subscribe to topic
    topic = "alerts"

    # Load the current schema (version from schema/latest.txt, verified
    # against param_registry.py)
    schema = load_schema()

    consumer.subscribe([topic])

    # Poll for new messages from Kafka and print them.
    try:
        while True:
            msg = consumer.poll(1.0)
            if msg is None:
                # Initial message consumption may take up to
                # `session.timeout.ms` for the consumer group to
                # rebalance and start consuming
                print("Waiting...")
            elif msg.error():
                print("ERROR: {}".format(msg.error()))
            else:
                message = msg.value()
                bytes_io = io.BytesIO(message)
                bytes_io.seek(0)
                decoded_alert = fastavro.schemaless_reader(bytes_io, schema)
                print(f"Received alert for diaSourceId={decoded_alert['diaSourceId']}")
                print(decoded_alert)
    except KeyboardInterrupt:
        pass
    finally:
        # Leave group and commit final offsets
        consumer.close()
