"""Every job on a queue created in [t0, t1] (ms), any terminal status; prints those not in the run dump."""
import json, sys, boto3
queue, t0, t1, dump = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
known = {b for r in json.load(open(dump)) for b in (r["attempts"] or {})}
b = boto3.client("batch"); seen = []
for status in ("SUCCEEDED", "FAILED"):
    for page in b.get_paginator("list_jobs").paginate(
            jobQueue=queue, filters=[{"name": "AFTER_CREATED_AT", "values": [str(t0)]}]):
        seen += [j for j in page["jobSummaryList"] if j["createdAt"] <= t1]
other = [j for j in seen if j["jobId"] not in known]
print(len(seen), len(other))
for j in other[:20]: print(j["jobName"], j.get("status"), j.get("startedAt"), j.get("stoppedAt"))
