#!/usr/bin/env python

import io
from confluent_kafka import Consumer
import fastavro

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
    schema = fastavro.schema.load_schema_ordered(['../sample_data/SExtractorSourceSingle.avsc','../sample_data/alert.avsc'])
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
                print("ERROR: %s".format(msg.error()))
            else:
                message = msg.value()
                bytes_io = io.BytesIO(message)
                bytes_io.seek(0)
                decoded_message = fastavro.reader(bytes_io,schema)
                for record in decoded_message:
                    print(record) 
    except KeyboardInterrupt:
        pass
    finally:
        # Leave group and commit final offsets
        consumer.close()
