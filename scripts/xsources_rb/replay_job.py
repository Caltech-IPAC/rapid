"""Run one difference job's stage locally, unpublished, under cProfile (xsources-rb-profile).

Reads the job's row, inputs and run settings (read-only; the stage itself
draws one diffimages pid from the sequence), runs
rapidpipe.stages.difference.run on a file:// prefix under OUT, and keeps
copies of the inputs and outputs of every _score_rb and _xsources_columns
call under OUT/snap for the leads. Nothing is uploaded or published.
"""
import cProfile
import json
import pickle
import resource
import shutil
import sys
import time
from pathlib import Path

import numpy as np
from psycopg2.extras import RealDictCursor

from rapidpipe.db.connection import connect
from rapidpipe.foundation.local import JobContext
from rapidpipe.stages import difference

job_id, out = int(sys.argv[1]), Path(sys.argv[2])
snap = out / "snap"
snap.mkdir(parents=True, exist_ok=True)
timings = {"score_rb": [], "xsources_columns": None}

with connect() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
    cur.execute("SELECT j.stage, j.scope, j.run, r.settings, r.survey, r.dataset"
                " FROM jobs j JOIN runs r ON r.id = j.run WHERE j.id = %s", (job_id,))
    job = cur.fetchone()
    cur.execute("SELECT kind, product, key, source_job, binding FROM job_inputs"
                " WHERE job = %s ORDER BY kind, product, source_job", (job_id,))
    # The publisher records the stage's objects and xobjects reads as inputs; the stage is
    # submitted with the rest.
    inputs = [dict(r) for r in cur.fetchall() if r["product"] not in ("objects", "xobjects")]
    conn.commit()
(snap / "settings.json").write_text(json.dumps(job["settings"]))

prefix_dir = out / "job" / "1"
prefix_dir.mkdir(parents=True, exist_ok=True)
ctx = JobContext(job=job_id, run=job["run"], stage=job["stage"], scope=job["scope"],
                 survey=job["survey"], dataset=job["dataset"], settings=job["settings"],
                 inputs=inputs, attempt=1, prefix=prefix_dir.as_uri() + "/", connect=connect)

original_score, original_xs = difference._score_rb, difference._xsources_columns


def score_rb(classifier, filter_name, catalog, science, reference, diff):
    n = len(timings["score_rb"])
    d = snap / f"rb{n}"
    d.mkdir(exist_ok=True)
    for p in (catalog, science, reference, diff):
        shutil.copy2(p, d / p.name)
    start = time.perf_counter()
    summary = original_score(classifier, filter_name, catalog, science, reference, diff)
    seconds = time.perf_counter() - start
    shutil.copy2(catalog, d / ("scored_" + catalog.name))
    (d / "call.json").write_text(json.dumps({
        "catalog": catalog.name, "science": science.name, "reference": reference.name,
        "difference": diff.name, "filter": filter_name, "seconds": seconds, "summary": summary,
        "workdir": str(catalog.parent)}, default=str))
    timings["score_rb"].append((catalog.name, seconds))
    return summary


def xsources_columns(kit, work_dir, settings, rb, signs, *, uncertainty_image, psf_image,
                     science, reference):
    d = snap / "xs"
    d.mkdir(exist_ok=True)
    names = {uncertainty_image, psf_image, science, reference}
    for catalog_name, image in signs.values():
        names |= {catalog_name, image}
    for name in names:
        shutil.copy2(work_dir / name, d / name)
    rb_scored_before = len(timings["score_rb"])
    start = time.perf_counter()
    found = original_xs(kit, work_dir, settings, rb, signs, uncertainty_image=uncertainty_image,
                        psf_image=psf_image, science=science, reference=reference)
    seconds = time.perf_counter() - start
    (d / "call.json").write_text(json.dumps({
        "signs": signs, "uncertainty_image": uncertainty_image, "psf_image": psf_image,
        "science": science, "reference": reference, "filter": None if rb is None else rb[1],
        "seconds": seconds, "rb_calls": len(timings["score_rb"]) - rb_scored_before}))
    with open(d / "extra.pkl", "wb") as f:
        pickle.dump({"extra": found["extra"], "fitted": found["fitted"], "scored": found["scored"]}, f)
    timings["xsources_columns"] = seconds
    return found


difference._score_rb = score_rb
difference._xsources_columns = xsources_columns

profile = cProfile.Profile()
start = time.perf_counter()
profile.enable()
try:
    difference.run(ctx)
finally:
    profile.disable()
    total = time.perf_counter() - start
    profile.dump_stats(str(out / "difference.prof"))
    usage = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    (out / "timing.json").write_text(json.dumps({
        "job": job_id, "stage_seconds": total, "timings": timings,
        "peak_rss_kib_self": usage.ru_maxrss, "peak_rss_kib_children": children.ru_maxrss,
        "utime_self": usage.ru_utime, "stime_self": usage.ru_stime,
        "utime_children": children.ru_utime}, indent=1))
print(json.dumps(json.loads((out / "timing.json").read_text())))
