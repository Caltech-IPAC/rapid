#!/usr/bin/env python

from confluent_kafka import Producer
import fastavro

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

    # Produce data by selecting random values from these lists.
    topic = "alerts"

    with open('../sample_data/sextractor_combined_alert.avro', 'rb') as avro_file:
        data = avro_file.read()
        producer.produce(topic, data, callback=delivery_callback)
        producer.flush()
    print('sent avro file to kafka topic:', topic)
    avro_file.close()
