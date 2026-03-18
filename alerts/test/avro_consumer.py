#!/usr/bin/env python

import io
from pathlib import Path
from confluent_kafka import Consumer
import fastavro
import fastavro.schema

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
