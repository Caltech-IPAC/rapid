"""Record exactly which inputs produced a set of outputs.

Every stage writes a manifest entry.  The failure mode this guards against is a
real one for this dataset: two RimTimSim deliveries exist with near-identical
catalogue files, and without a recorded checksum it is impossible to tell after
the fact which one a given result was scored against.
"""
import hashlib
import json
import os
import subprocess
import time


def sha256(path, limit=None):
    """Checksum a file.  `limit` hashes only the leading bytes, for huge inputs."""
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            if limit is not None and n + len(chunk) > limit:
                h.update(chunk[:limit - n])
                break
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest()


def git_sha(repo=None):
    r = subprocess.run("git rev-parse HEAD", shell=True, capture_output=True,
                       text=True, cwd=repo or os.path.dirname(os.path.abspath(__file__)))
    return r.stdout.strip() or "unknown"


def write(cfg, stage, inputs, extra=None):
    """Append a manifest entry for one stage."""
    path = os.path.join(cfg.work, "provenance.jsonl")
    os.makedirs(cfg.work, exist_ok=True)
    entry = dict(stage=stage, utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 git=git_sha(), config=os.path.abspath(cfg.path),
                 proc_date=cfg.run["proc_date"], database=cfg.run["database"],
                 inputs=inputs, extra=extra or {})
    with open(path, "a") as fh:
        fh.write(json.dumps(entry) + "\n")
    return path
