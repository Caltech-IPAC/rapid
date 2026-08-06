#!/usr/bin/env python

import io
import json
import sys
from pathlib import Path

from confluent_kafka import Producer
import fastavro

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from alerts.produce import load_schema, schema_paths

if __name__ == '__main__':

    config = {
        # User-specific properties that you must set
        'bootstrap.servers': 'localhost:9092',
        'message.max.bytes': '15728640'
    }

    # Create Producer instance
    producer = Producer(config)

    # Optional per-message delivery callback (triggered by poll() or flush())
    # when a message has been successfully delivered or permanently
    # failed delivery (after retries).
    def delivery_callback(err, msg):
        if err:
            print('ERROR: Message failed delivery: {}'.format(err))
        else:
            print("Produced event to topic {topic}".format(topic=msg.topic()))

    topic = "alerts"

    # Load the current schema (version from schema/latest.txt, verified
    # against param_registry.py)
    schema = load_schema()

    # Load sample alert data from the same version's schema directory
    sample_data_path = schema_paths()[0].parent / 'sample_data' / 'alert.json'
    with open(sample_data_path, 'r') as f:
        alert_data = json.load(f)

    # Serialize to Avro
    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, schema, alert_data)
    data = buf.getvalue()

    producer.produce(topic, data, callback=delivery_callback)
    producer.flush()
    print('sent avro alert to kafka topic:', topic, f'({len(data)} bytes)')
