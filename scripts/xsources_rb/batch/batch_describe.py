"""describe-jobs for every Batch attempt id in a jobs dump; container instance -> EC2 instance id."""
import json, sys, boto3
rows = json.load(open(sys.argv[1])); out = sys.argv[2]
ids = sorted({b for r in rows for b in (r["attempts"] or {})})
batch = boto3.client("batch"); ecs = boto3.client("ecs")
desc = {}
for i in range(0, len(ids), 100):
    for j in batch.describe_jobs(jobs=ids[i:i + 100])["jobs"]:
        c = j.get("container", {}); a = (j.get("attempts") or [{}])[-1].get("container", {})
        desc[j["jobId"]] = dict(ci=c.get("containerInstanceArn") or a.get("containerInstanceArn"),
                                started=j.get("startedAt"), stopped=j.get("stoppedAt"), status=j["status"],
                                jobdef=j.get("jobDefinition"), ce=None, log=c.get("logStreamName"),
                                vcpus=[r for r in c.get("resourceRequirements", [])])
cis = {}
by_cluster = {}
for d in desc.values():
    if d["ci"]:
        cluster = d["ci"].split("/")[1]
        by_cluster.setdefault(cluster, set()).add(d["ci"])
for cluster, arns in by_cluster.items():
    arns = sorted(arns)
    for i in range(0, len(arns), 100):
        for c in ecs.describe_container_instances(cluster=cluster, containerInstances=arns[i:i + 100])["containerInstances"]:
            t = {a["name"]: a.get("value") for a in c.get("attributes", [])}
            cis[c["containerInstanceArn"]] = dict(ec2=c["ec2InstanceId"], type=t.get("ecs.instance-type"),
                                                  az=t.get("ecs.availability-zone"))
        for f in ecs.describe_container_instances(cluster=cluster, containerInstances=arns[i:i + 100]).get("failures", []):
            cis.setdefault(f["arn"], dict(ec2=None, type=None, az=None, failure=f.get("reason")))
json.dump(dict(jobs=desc, cis=cis), open(out, "w"))
print(len(ids), len(desc), sum(1 for d in desc.values() if d["ci"]), len(cis),
      sum(1 for c in cis.values() if c["ec2"]))
