"""Run one difference job's stage locally, unpublished, with its memory sampled by holder (memory-diet).

    replay_job.py JOB OUT [--tracemalloc]

Run inside the pipeline image, in a container of its own, so that
``/sys/fs/cgroup`` is the job's cgroup as on Batch. It reads the job's row,
inputs and run settings (read-only; the stage draws one diffimages pid from
the sequence, as xsources-rb's replay did), runs
``rapidpipe.stages.difference.run`` on a ``file://`` prefix under OUT and
publishes nothing. Every 0.25 s a thread writes one line to
OUT/samples.jsonl: the cgroup's ``memory.stat`` ``anon`` and ``file``, each
process's ``RssAnon`` and the step running, the stack of
``rapidpipe.science.difference`` and stage functions and the external tool
open at that instant. ``--tracemalloc`` adds the main process's traced
Python and NumPy bytes to each line and, at the traced peak, the 25 largest
allocation sites to OUT/tracemalloc_peak.txt. OUT/timing.json holds the wall,
the peaks and the step seconds.
"""
import functools
import importlib
import inspect
import json
import os
import pkgutil
import resource
import sys
import threading
import time
import tracemalloc
from pathlib import Path

from psycopg2.extras import RealDictCursor

from rapidpipe.db.connection import connect
from rapidpipe.foundation.local import JobContext
from rapidpipe.science import difference as science_difference
from rapidpipe.science.difference import tools
from rapidpipe.stages import difference

job_id, out = int(sys.argv[1]), Path(sys.argv[2])
trace = "--tracemalloc" in sys.argv
out.mkdir(parents=True, exist_ok=True)
stack: list[str] = []
step_seconds: dict[str, float] = {}


def _phase(name, fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        stack.append(name)
        start = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            step_seconds[" > ".join(stack)] = step_seconds.get(" > ".join(stack), 0.0) + \
                time.perf_counter() - start
            stack.pop()
    return wrapped


def _wrap_module(module, label):
    for name, obj in list(vars(module).items()):
        if inspect.isfunction(obj) and obj.__module__ == module.__name__ and name != "run":
            setattr(module, name, _phase(f"{label}.{name}", obj))


for info in pkgutil.iter_modules(science_difference.__path__):
    _wrap_module(importlib.import_module(f"{science_difference.__name__}.{info.name}"), info.name)
_wrap_module(difference, "stage")

_run, _run_shell = tools.ToolRunner.run, tools.ToolRunner.run_shell


def run_tool(self, args, **kw):
    return _phase("tool:" + tools._tool_name([str(a) for a in args]), _run)(self, args, **kw)


def run_shell(self, command, **kw):
    return _phase("tool:" + tools._tool_name(command.split()), _run_shell)(self, command, **kw)


tools.ToolRunner.run, tools.ToolRunner.run_shell = run_tool, run_shell
_toolkit = difference.toolkit


def toolkit():
    kit = _toolkit()
    for name in ("psf_catalog", "reference_psf", "rb_model", "rb_classifier"):
        setattr(kit, name, _phase(f"kit.{name}", getattr(kit, name)))
    return kit


difference.toolkit = toolkit


def _memory_stat():
    fields = dict(line.split() for line in Path("/sys/fs/cgroup/memory.stat").read_text().splitlines())
    return int(fields["anon"]), int(fields["file"])


def _processes():
    found = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text()
            cmd = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        anon = next((int(l.split()[1]) for l in status.splitlines() if l.startswith("RssAnon:")), 0)
        if anon:
            words = [Path(c.decode()).name for c in cmd[:2] if c]
            found[entry.name] = {"cmd": " ".join(words), "anon_kib": anon}
    return found


stop = threading.Event()
peaks = {"anon": 0, "file": 0, "traced": 0}


def sample():
    me = str(os.getpid())
    with open(out / "samples.jsonl", "w") as stream:
        while not stop.is_set():
            anon, file = _memory_stat()
            line = {"t": round(time.time(), 2), "anon": anon, "file": file,
                    "step": " > ".join(stack), "procs": _processes(), "self": me}
            peaks["anon"], peaks["file"] = max(peaks["anon"], anon), max(peaks["file"], file)
            if trace:
                current, _ = tracemalloc.get_traced_memory()
                line["traced"] = current
                if current > peaks["traced"] * 1.02:
                    peaks["traced"] = current
                    top = tracemalloc.take_snapshot().statistics("traceback")[:25]
                    with open(out / "tracemalloc_peak.txt", "w") as t:
                        t.write(f"traced {current} at step {line['step']}\n")
                        for stat in top:
                            t.write(f"\n{stat.size / 2**20:.1f} MiB in {stat.count} blocks\n")
                            t.write("\n".join(stat.traceback.format()) + "\n")
            stream.write(json.dumps(line) + "\n")
            stream.flush()
            stop.wait(0.25)


with connect() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
    cur.execute("SELECT j.stage, j.scope, j.run, r.settings, r.survey, r.dataset"
                " FROM jobs j JOIN runs r ON r.id = j.run WHERE j.id = %s", (job_id,))
    job = cur.fetchone()
    cur.execute("SELECT kind, product, key, source_job, binding FROM job_inputs"
                " WHERE job = %s ORDER BY kind, product, source_job", (job_id,))
    inputs = [dict(r) for r in cur.fetchall() if r["product"] not in ("objects", "xobjects")]
    conn.commit()
(out / "settings.json").write_text(json.dumps(job["settings"]))
prefix_dir = out / "job" / "1"
prefix_dir.mkdir(parents=True, exist_ok=True)
ctx = JobContext(job=job_id, run=job["run"], stage=job["stage"], scope=job["scope"],
                 survey=job["survey"], dataset=job["dataset"], settings=job["settings"],
                 inputs=inputs, attempt=1, prefix=prefix_dir.as_uri() + "/", connect=connect)

if trace:
    tracemalloc.start(8)
sampler = threading.Thread(target=sample, daemon=True)
sampler.start()
start = time.perf_counter()
try:
    difference.run(ctx)
finally:
    total = time.perf_counter() - start
    stop.set()
    sampler.join()
    memory_peak = int(Path("/sys/fs/cgroup/memory.peak").read_text())
    (out / "timing.json").write_text(json.dumps({
        "job": job_id, "stage_seconds": total, "anon_peak": peaks["anon"],
        "file_peak": peaks["file"], "memory_peak": memory_peak, "traced_peak": peaks["traced"],
        "peak_rss_kib_self": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "peak_rss_kib_children": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
        "steps": step_seconds}, indent=1))
print((out / "timing.json").read_text())
