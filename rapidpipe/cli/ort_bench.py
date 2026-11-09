"""exp/ort-settings' scoring bench, never merged: the RuBR-AT scorer on one difference job's
published inputs, alone and five copies at once in one container, with the kernel evidence.

``rapidpipe ort-bench --inputs <job's diff/ prefix> --model <model prefix> --out <prefix>``
runs, in order, each scorer a child process (``ort-bench --worker``):

- ``kernel``: one scorer at one thread on the first 1,024 sources with onnxruntime's
  profiler and its verbose log (the graph after optimisation, each kernel's op, provider,
  input types and shapes; the NCHWc block size MLAS chose is the shapes' last dimension);
- ``alone``: one scorer at one thread on every source, its main thread's instruction
  pointer sampled by ``ptrace`` every 20 ms, each sample's library offset and 32 bytes;
- ``packed``: five scorers at one thread at once, each its start and stop;
- ``shipped_alone`` and ``shipped_packed``: the same at onnxruntime's default threads, the
  shipped session (``rebuild`` before ort-settings);

and writes ``bench.json`` (and the profile and log) under ``--out``. Scores of every arm are
compared with the first's.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

FILES = {"science": "bkg_subbed_science_image.fits.gz",
         "reference": "awaicgen_output_mosaic_image_resampled_gainmatched.fits.gz",
         "difference": "sfftdiffimage_masked.fits.gz",
         "catalog": "sfftdiffimage_masked_psfcat.parquet"}


def _fetch(prefix: str, name: str, dest: Path) -> Path:
    from rapidpipe.foundation.prefix import get
    path = get(f"{prefix.rstrip('/')}/{name}")
    target = dest / name
    if path != target:
        target.write_bytes(path.read_bytes())
    return target


def worker(args) -> int:
    """One scorer: load, score ``--rows`` sources, write its record."""
    import onnxruntime
    import pyarrow.parquet as pq
    from astropy.io import fits

    from rapidpipe.science.difference import realbogus

    work = Path(args.work)
    t_load = time.time()
    model = work / "model"
    classifier = realbogus.load_classifier(model / "best.onnx", model / "canonical_9.json",
                                           model / "threshold.json", 0, 256,
                                           vcpus=None if args.threads == "job" else 1)
    if args.threads == "default":
        classifier.model = realbogus.OnnxSession(onnxruntime.InferenceSession(
            str(classifier.checkpoint), providers=["CPUExecutionProvider"]))
    if args.profile:
        options = realbogus.session_options(onnxruntime, 1)
        options.enable_profiling = True
        options.profile_file_prefix = str(Path(args.record).with_suffix(""))
        options.log_severity_level = 0
        options.log_verbosity_level = 1
        classifier.model = realbogus.OnnxSession(onnxruntime.InferenceSession(
            str(classifier.checkpoint), sess_options=options, providers=["CPUExecutionProvider"]))
    table = pq.read_table(work / FILES["catalog"])
    table = table.drop_columns([c for c in realbogus.RB_COLUMNS if c in table.column_names])
    if args.rows:
        table = table.slice(0, args.rows)
    images = [fits.getdata(work / FILES[k], 0) for k in ("science", "reference", "difference")]
    Path(args.record + ".ready").write_text("")
    while args.barrier and not Path(args.barrier).exists():
        time.sleep(0.05)
    start = time.time()
    values = realbogus.score(classifier, *images, table, "F146")
    stop = time.time()
    profile = classifier.model.session.end_profiling() if args.profile else None
    scored = int(np.isfinite(values["rb_score"]).sum())
    np.save(args.record + ".npy", values["rb_score"])
    Path(args.record).write_text(json.dumps({
        "threads": args.threads, "rows": table.num_rows, "scored": scored, "load_s": start - t_load,
        "start": start, "stop": stop, "wall_s": stop - start,
        "ms_per_score": 1000 * (stop - start) / scored,
        "ort_s": classifier.model.counts["seconds"], "ort_runs": classifier.model.counts["runs"],
        "ort_ms_per_row": 1000 * classifier.model.counts["seconds"] / classifier.model.counts["rows"],
        "settings": getattr(classifier, "settings", {}), "profile": profile,
        "threads_in_process": len(os.listdir("/proc/self/task"))}))
    return 0


# ptrace on x86_64 (linux/ptrace.h): the tracer is the scorer's parent.
_SEIZE, _INTERRUPT, _GETREGS, _CONT, _DETACH, _WALL = 0x4206, 0x4207, 12, 7, 17, 0x40000000


def _sample(pid: int, until: threading.Event, interval: float, out: list, errors: list) -> None:
    """The main thread's ``rip`` every ``interval`` s: its mapping, offset and 32 bytes, until
    ``until`` is set or the child exits. Only this thread waits on the child: a ``Popen.wait``
    elsewhere would take the ptrace stops and leave the child stopped."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.ptrace.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
    libc.ptrace.restype = ctypes.c_long
    regs = (ctypes.c_ulonglong * 27)()
    if libc.ptrace(_SEIZE, pid, None, None) != 0:
        errors.append(f"PTRACE_SEIZE: errno {ctypes.get_errno()}")
        return
    try:
        mem = open(f"/proc/{pid}/mem", "rb", buffering=0)
    except OSError as exc:
        errors.append(f"/proc/{pid}/mem: {exc}")
        mem = None
    maps: list = []
    while not until.is_set():
        time.sleep(interval)
        if libc.ptrace(_INTERRUPT, pid, None, None) != 0:
            break
        try:
            _, status = os.waitpid(pid, _WALL)
        except ChildProcessError:
            break
        if os.WIFEXITED(status) or os.WIFSIGNALED(status):
            until.set()
            return
        if libc.ptrace(_GETREGS, pid, None, ctypes.byref(regs)) == 0:
            rip = int(regs[16])
            region = next((m for m in maps if m[0] <= rip < m[1]), None)
            if region is None:
                maps = _maps(pid)
                region = next((m for m in maps if m[0] <= rip < m[1]), None)
            code = b""
            if mem is not None:
                try:
                    mem.seek(rip)
                    code = mem.read(32)
                except OSError:
                    pass
            out.append({"path": region[3] if region else "", "offset":
                        rip - region[0] + region[2] if region else rip, "code": code.hex()})
        libc.ptrace(_CONT, pid, None, None)
    libc.ptrace(_DETACH, pid, None, None)


def _maps(pid: int) -> list:
    rows = []
    for line in Path(f"/proc/{pid}/maps").read_text().splitlines():
        parts = line.split()
        if len(parts) >= 6 and "x" in parts[1]:
            a, b = parts[0].split("-")
            rows.append((int(a, 16), int(b, 16), int(parts[2], 16), parts[5]))
    return rows


def _spawn(work: Path, name: str, threads: str, *, rows: int = 0, barrier: str = "",
           profile: bool = False):
    record = str(work / f"{name}.json")
    cmd = [sys.executable, "-m", "rapidpipe.cli.main", "ort-bench", "--worker", "--work", str(work),
           "--record", record, "--threads", threads, "--rows", str(rows), "--barrier", barrier]
    if profile:
        cmd.append("--profile")
    log = open(work / f"{name}.log", "w")
    return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT), record


def _arm(work: Path, name: str, threads: str, copies: int, sample: bool = False, rows: int = 0) -> dict:
    barrier = work / f"{name}.go"
    procs = [_spawn(work, f"{name}{k}", threads, rows=rows, barrier=str(barrier)) for k in range(copies)]
    while not all(Path(r + ".ready").exists() or p.poll() is not None for p, r in procs):
        time.sleep(0.1)
    samples: list = []
    errors: list = []
    stop = threading.Event()
    sampler = None
    if sample:
        sampler = threading.Thread(target=_sample, args=(procs[0][0].pid, stop, 0.02, samples, errors))
        sampler.start()
    barrier.write_text("")
    if sampler:
        while not Path(procs[0][1]).exists() and not stop.is_set():
            time.sleep(0.2)
        stop.set()
        sampler.join()
    codes = [p.wait() for p, _ in procs]
    records = [json.loads(Path(r).read_text()) if Path(r).exists() else None for _, r in procs]
    done = [r for r in records if r]
    overlap = (min(r["stop"] for r in done) - max(r["start"] for r in done)) if done else None
    out = {"copies": copies, "threads": threads, "exit": codes, "records": records,
           "overlap_s": overlap, "ms_per_score": [r["ms_per_score"] for r in done]}
    if sample:
        out.update(samples=samples, sample_errors=errors)
    return out


def bench(args) -> int:
    work = Path(tempfile.mkdtemp(prefix="ort-bench-", dir=os.environ.get("TMPDIR") or "/tmp"))
    (work / "model").mkdir()
    for name in ("best.onnx", "canonical_9.json", "threshold.json"):
        _fetch(args.model, name, work / "model")
    for name in FILES.values():
        _fetch(args.inputs, name, work)
    from rapidpipe.science.difference.realbogus import job_vcpus
    meta_uri = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    result: dict = {"job_vcpus": job_vcpus(), "batch_job_id": os.environ.get("AWS_BATCH_JOB_ID"),
                    "cpu_count": os.cpu_count(), "affinity": len(os.sched_getaffinity(0))}
    if meta_uri:
        import urllib.request
        with urllib.request.urlopen(meta_uri, timeout=2) as r:
            result["ecs_container"] = json.load(r)
    info = Path("/proc/cpuinfo").read_text().split("\n\n")[0]
    result["cpuinfo"] = {k.strip(): v.strip() for k, _, v in (l.partition(":") for l in info.splitlines())
                         if k.strip() in ("vendor_id", "model name", "cpu family", "model", "flags")}
    for name in ("cpu.max", "cpu.weight"):
        p = Path("/sys/fs/cgroup") / name
        result[f"cgroup_{name}"] = p.read_text().strip() if p.exists() else None
    proc, record = _spawn(work, "kernel", "1", rows=1024, profile=True)
    result["kernel"] = {"exit": proc.wait()}
    if Path(record).exists():
        result["kernel"].update(json.loads(Path(record).read_text()))
    arms = [("alone", "1", 1, True), ("packed", "1", 5, False), ("job_alone", "job", 1, False),
            ("shipped_alone", "default", 1, False), ("shipped_packed", "default", 5, False)]
    for name, threads, copies, sample in arms:
        result[name] = _arm(work, name, threads, copies, sample, args.rows)
        print(json.dumps({name: result[name]["ms_per_score"], "overlap_s": result[name]["overlap_s"]}),
              flush=True)
    first = np.load(work / "alone0.json.npy")
    result["max_abs_score_diff"] = {
        name: float(np.nanmax(np.abs(np.load(work / f"{name}{k}.json.npy") - first)))
        for name, _, copies, _ in arms for k in range(copies) if (work / f"{name}{k}.json.npy").exists()}
    upload = work / "upload"
    upload.mkdir()
    (upload / "bench.json").write_text(json.dumps(result))
    for path in [*work.glob("*.log"), *work.glob("kernel*.json")]:
        (upload / path.name).write_bytes(path.read_bytes())
    from rapidpipe.foundation.prefix import put
    put(upload, args.out)
    print(json.dumps({k: v for k, v in result.items() if k not in ("kernel", "ecs_container")})[:4000])
    return 0


def main(args) -> int:
    return worker(args) if args.worker else bench(args)
