#!/usr/bin/env python

import io
import json
from pathlib import Path

from confluent_kafka import Producer
import fastavro
import fastavro.schema

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

    # Load v01.00 schema
    schema_dir = Path(__file__).parent.parent / 'schema' / '01' / '00'
    schema = fastavro.schema.load_schema_ordered([
        str(schema_dir / 'rapid.v01_00.diaSource.avsc'),
        str(schema_dir / 'rapid.v01_00.diaForcedSource.avsc'),
        str(schema_dir / 'rapid.v01_00.diaObject.avsc'),
        str(schema_dir / 'rapid.v01_00.ssSource.avsc'),
        str(schema_dir / 'rapid.v01_00.mpc_orbits.avsc'),
        str(schema_dir / 'rapid.v01_00.alert.avsc'),
    ])

    # Load sample alert data
    sample_data_path = schema_dir / 'sample_data' / 'alert.json'
    with open(sample_data_path, 'r') as f:
        alert_data = json.load(f)

    # Serialize to Avro
    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, schema, alert_data)
    data = buf.getvalue()

    producer.produce(topic, data, callback=delivery_callback)
    producer.flush()
    print('sent avro alert to kafka topic:', topic, f'({len(data)} bytes)')
