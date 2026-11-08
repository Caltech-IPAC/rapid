"""Replay one published difference job's stage locally, read-only, with the background step timed.

Usage: PYTHONPATH=<checkout> python scripts/replay_difference.py --job N --code <checkout> --out DIR

The job's own scope, inputs and run settings are read from the database in
read-only sessions; the pid draw (the one write read_inputs makes, a
sequence nextval) is answered locally with -1. Nothing is published or
recorded: the stage writes under DIR only. The background step's two calls
are timed and their maps (input minus output) saved as bkg_sci.fits and
bkg_ref.fits beside the stage's own outputs, plus summary.json.
"""
import argparse
import json
import resource
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np
from astropy.io import fits

from rapidpipe.db.connection import connect
from rapidpipe.foundation.local import JobContext
from rapidpipe.science.difference import background
from rapidpipe.stages import difference


class _Cursor:
    def __init__(self, cur):
        self._cur, self._fake = cur, None

    def execute(self, sql, params=None):
        if "nextval" in sql:
            self._fake = (-1,)
            return None
        self._fake = None
        return self._cur.execute(sql, params)

    def fetchone(self):
        return self._fake if self._fake is not None else self._cur.fetchone()

    def __getattr__(self, name):
        return getattr(self._cur, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._cur.close()


class _Conn:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self, *a, **k):
        return _Cursor(self._conn.cursor(*a, **k))

    def commit(self):
        self._conn.rollback()           # nothing to commit: every session is read-only

    def __getattr__(self, name):
        return getattr(self._conn, name)


@contextmanager
def read_only():
    with connect(application_name="bkgest-photutils-replay") as conn:
        conn.set_session(readonly=True)
        yield _Conn(conn)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", type=int, required=True)
    ap.add_argument("--code", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    with read_only() as conn, conn.cursor() as cur:
        cur.execute("SELECT j.run, j.stage, j.scope, r.settings, r.survey, r.dataset FROM jobs j"
                    " JOIN runs r ON r.id = j.run WHERE j.id = %s", (args.job,))
        run, stage, scope, settings, survey, dataset = cur.fetchone()
        cur.execute("SELECT kind, product, key, source_job, binding FROM job_inputs WHERE job = %s",
                    (args.job,))
        cols = [d[0] for d in cur.description]
        # the association's reads are recorded on the row after the stage ran; not stage inputs
        inputs = [dict(zip(cols, r)) for r in cur.fetchall()
                  if r[1] not in ("objects", "xobjects")]
    assert stage == "difference"
    code = str(args.code.resolve())
    paths = settings["difference"]["paths"]
    paths.update(rapid_sw=code, cfg_path=code + "/cdf",
                 zogy_code=code + "/modules/zogy/v21Aug2018/py_zogy.py")
    if "sfft_code" in settings["difference"].get("sfft", {}):
        settings["difference"]["sfft"]["sfft_code"] = code + "/modules/sfft/sfft_rapid_crowded.py"
    for table in settings["difference"].values():
        if isinstance(table, dict):
            for k, v in table.items():
                if isinstance(v, str) and v.startswith("/code/"):
                    table[k] = code + v[len("/code"):]

    timing = {}
    sci_call, ref_call = background.subtract_background, background.subtract_reference_background

    def sci(runner, work_dir, code_, inc, input_image, d):
        before = fits.getdata(work_dir / input_image, 0).astype(np.float32)
        t = time.perf_counter()
        name = sci_call(runner, work_dir, code_, inc, input_image, d)
        timing["science_s"] = time.perf_counter() - t
        after = fits.getdata(work_dir / name, 0).astype(np.float32)
        fits.writeto(out / "bkg_sci.fits", before - after, overwrite=True)
        return name

    def ref(runner, work_dir, code_, inc, image, cov, d):
        before = fits.getdata(work_dir / image, 0).astype(np.float32)
        t = time.perf_counter()
        n = ref_call(runner, work_dir, code_, inc, image, cov, d)
        timing["reference_s"] = time.perf_counter() - t
        after = fits.getdata(work_dir / image, 0).astype(np.float32)
        bkg = before - after
        bkg[background.uncovered_pixels(work_dir / cov)] = np.nan
        fits.writeto(out / "bkg_ref.fits", bkg, overwrite=True)
        return n

    background.subtract_background, background.subtract_reference_background = sci, ref
    job = JobContext(job=args.job, run=run, stage=stage, scope=scope, survey=survey,
                     dataset=dataset, settings=settings, inputs=inputs, attempt=1,
                     prefix=(out / "attempt").as_uri() + "/", connect=read_only)
    (out / "attempt").mkdir(exist_ok=True)
    t0 = time.perf_counter()
    difference.run(job)
    wall = time.perf_counter() - t0
    manifest = json.loads((out / "attempt" / "manifest.json").read_text())
    summary = {"job": args.job, "run": run, "scope": scope, "code": code, "wall_s": wall,
               "background_s": timing,
               "peak_rss_kib": {"self": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                                "children": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss},
               "metrics": manifest["metrics"],
               "sources_before_cap": manifest["difference"]["sources_before_cap"],
               "sources_kept": manifest["difference"]["sources_kept"]}
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: summary[k] for k in ("job", "wall_s", "background_s", "sources_kept")}))


if __name__ == "__main__":
    main()
