"""Re-run the baseline's rb and xsources calls on its snapshots and compare (xsources-rb-profile).

leads.py SNAP LABEL rb|xs [--profile] [--set key=value ...]

rb: every snap/rbN catalog rescored by realbogus.add_rb_columns; rb_score
and rb_label compared with the baseline's scored catalog. xs: the stage's
_xsources_columns on snap/xs; every column of its extra arrays compared
with the baseline's. --set overrides a [difference] key (dotted). The
classifier is built by the stage's own loader; the leads' knobs are the
branch's RAPID_EXP_* environment variables.
"""
import argparse
import cProfile
import json
import os
import pickle
import shutil
import time
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from rapidpipe.science.difference import realbogus
from rapidpipe.stages import difference

p = argparse.ArgumentParser()
p.add_argument("snap", type=Path)
p.add_argument("label")
p.add_argument("what", choices=["rb", "xs"])
p.add_argument("--profile", action="store_true")
p.add_argument("--only", default="", help="comma-separated rbN to score (rb)")
p.add_argument("--set", action="append", default=[])
p.add_argument("--no-rb", action="store_true", help="xs: fit only, score nothing")
a = p.parse_args()

settings = json.loads((a.snap / "settings.json").read_text())["difference"]
for item in a.set:
    key, value = item.split("=", 1)
    table = settings
    *parents, last = key.split(".")
    for name in parents:
        table = table[name]
    table[last] = json.loads(value)
rubrat = settings["rubrat"]
model_dir = Path(os.environ.get("XRB_ROOT", "/t1")) / "model"
classifier = realbogus.load_classifier(model_dir / rubrat["model"], model_dir / rubrat["feats_scaler"],
                                       model_dir / rubrat["threshold_json"], rubrat["survey_id"],
                                       rubrat["batch_size"])
out = Path(os.environ.get("XRB_ROOT", "/t1")) / "leads" / a.label
out.mkdir(parents=True, exist_ok=True)
result = {"label": a.label, "what": a.what, "set": a.set,
          "env": {k: v for k, v in os.environ.items() if k.startswith("RAPID_EXP_")},
          "threads_after_load": len(os.listdir("/proc/self/task")), "calls": []}


def same(x, y) -> bool:
    x, y = np.asarray(x), np.asarray(y)
    if x.dtype.kind == "f" or y.dtype.kind == "f":
        return x.shape == y.shape and np.array_equal(x.astype(float), y.astype(float), equal_nan=True)
    return np.array_equal(x, y)


profile = cProfile.Profile() if a.profile else None
start_all = time.perf_counter()
if a.what == "rb":
    only = set(filter(None, a.only.split(",")))
    for d in sorted(a.snap.glob("rb*")):
        if only and d.name not in only:
            continue
        call = json.loads((d / "call.json").read_text())
        work = out / d.name
        work.mkdir(exist_ok=True)
        catalog = work / call["catalog"]
        shutil.copy2(d / call["catalog"], catalog)
        output = work / "scored.parquet"
        if profile:
            profile.enable()
        start = time.perf_counter()
        summary = realbogus.add_rb_columns(catalog, output, d / call["science"], d / call["reference"],
                                           d / call["difference"], call["filter"], classifier)
        seconds = time.perf_counter() - start
        if profile:
            profile.disable()
        new = pq.read_table(output)
        old = pq.read_table(d / ("scored_" + call["catalog"]))
        result["calls"].append({
            "call": d.name, "catalog": call["catalog"], "rows": summary["rows"],
            "scored": summary["scored"], "seconds": round(seconds, 3),
            "baseline_seconds": round(call["seconds"], 3),
            "rb_score_identical": same(new.column("rb_score"), old.column("rb_score")),
            "rb_label_identical": same(new.column("rb_label"), old.column("rb_label")),
            "max_abs_rb_score_diff": float(np.nanmax(np.abs(
                new.column("rb_score").to_numpy(zero_copy_only=False)
                - old.column("rb_score").to_numpy(zero_copy_only=False)), initial=0.0)),
            "table_identical": new.equals(old)})
        catalog.unlink()
        output.unlink()
else:
    d = a.snap / "xs"
    call = json.loads((d / "call.json").read_text())
    work = out / "xs"
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(d, work)
    rb_seconds = []
    original = difference._score_rb

    def timed(*args):
        s = time.perf_counter()
        r = original(*args)
        rb_seconds.append(time.perf_counter() - s)
        return r

    difference._score_rb = timed
    if profile:
        profile.enable()
    start = time.perf_counter()
    found = difference._xsources_columns(
        None, work, settings, None if a.no_rb else (classifier, call["filter"]),
        {k: tuple(v) for k, v in call["signs"].items()},
        uncertainty_image=call["uncertainty_image"], psf_image=call["psf_image"],
        science=call["science"], reference=call["reference"])
    seconds = time.perf_counter() - start
    if profile:
        profile.disable()
    with open(d / "extra.pkl", "rb") as f:
        old = pickle.load(f)
    columns = {}
    for sign, extra in found["extra"].items():
        for name, values in extra.items():
            columns[f"{sign}.{name}"] = same(values, old["extra"][sign][name])
    expected = {f"{s}.{n}" for s, e in old["extra"].items() for n in e
                if not (a.no_rb and n in ("rb_score", "rb_label"))}
    result["calls"].append({
        "call": "xs", "seconds": round(seconds, 3), "baseline_seconds": round(call["seconds"], 3),
        "rb_seconds": [round(s, 3) for s in rb_seconds], "fit_seconds": round(seconds - sum(rb_seconds), 3),
        "fitted": found["fitted"], "scored": found["scored"],
        "all_identical": all(columns.values()) and set(columns) == expected,
        "not_identical": sorted(k for k, v in columns.items() if not v)})
    shutil.rmtree(work)
result["seconds"] = round(time.perf_counter() - start_all, 3)
if profile:
    profile.dump_stats(str(out / f"{a.what}.prof"))
(out / f"{a.what}.json").write_text(json.dumps(result, indent=1))
print(json.dumps(result))
