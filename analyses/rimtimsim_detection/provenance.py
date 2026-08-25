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
    """Git revision of the analysis code.

    The pipeline container has no `git` binary, so asking it here would always
    yield "unknown" for a containerised run -- which is most of them.  The host
    does have git, so `rts_docker.sh` resolves the revision outside and passes it
    in as `RTS_GIT_SHA`; that takes precedence.  A dirty working tree is recorded
    as such, because a SHA alone would misrepresent what actually ran.
    """
    env = os.environ.get("RTS_GIT_SHA")
    if env:
        return env.strip()
    cwd = repo or os.path.dirname(os.path.abspath(__file__))
    r = subprocess.run("git rev-parse HEAD", shell=True, capture_output=True,
                       text=True, cwd=cwd)
    sha = r.stdout.strip()
    if not sha:
        return "unknown"
    d = subprocess.run("git status --porcelain --untracked-files=no", shell=True,
                       capture_output=True, text=True, cwd=cwd)
    return sha + ("-dirty" if d.stdout.strip() else "")


# The catalogue inputs run to hundreds of MB, and hashing them in full on every
# stage would dominate the runtime of the cheap stages.  A leading-block hash plus
# the exact size is enough to tell the two RimTimSim deliveries apart -- they
# differ in their very first bytes (tab- vs comma-separated) as well as in length.
HASH_LIMIT = 64 << 20


def describe(path):
    """Path, size and a leading-block checksum -- enough to identify one input."""
    try:
        size = os.path.getsize(path)
        return dict(path=path, bytes=size,
                    sha256=sha256(path, limit=HASH_LIMIT),
                    hashed_bytes=min(size, HASH_LIMIT))
    except OSError as e:
        return dict(path=path, error=str(e))


def write(cfg, stage, inputs, extra=None):
    """Append a manifest entry for one stage."""
    path = os.path.join(cfg.work, "provenance.jsonl")
    os.makedirs(cfg.work, exist_ok=True)
    entry = dict(stage=stage, utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 git=git_sha(), config=os.path.abspath(cfg.path),
                 proc_date=cfg.run["proc_date"], database=cfg.run["database"],
                 inputs=[describe(p) for p in inputs], extra=extra or {})
    with open(path, "a") as fh:
        fh.write(json.dumps(entry) + "\n")
    return path
