#!/usr/bin/env python
import json
import lsst.alert.packet as packet
import struct
from io import BytesIO

with open('alert.json','r') as file:
    json_data_combined = json.load(file)

schema = packet.SchemaRegistry.from_filesystem(root="schemas",schema_root="rapid.alert").get_by_version("0.1")
with open("sextractor_combined_alert.avro", "wb") as f:
    schema.store_alerts(f, [json_data_combined])

with open("sextractor_combined_alert.avro","rb") as file:
    alert_data = file.read()

avro_bytes = BytesIO()
avro_bytes.write(struct.pack("!b", 0))
avro_bytes.write(struct.pack("!I", packet.SchemaRegistry.calculate_id(schema)))
avro_bytes.write(schema.serialize(json_data_combined))

print("Packet size reduction (bytes) : %d"%(len(alert_data)-len(avro_bytes.getvalue())))