# The Batch scoring anomaly (ort-threads-batch)

Run 187's difference jobs against their placement, and the two-arm sample
(runs 189, one onnxruntime thread, and 190, the default; run 187's SCA 3 on
this branch's image), read from the database and `aws batch describe-jobs`.
Findings: rapid_docs `log/2026-11-08-exercise-2026-11.md`, "xsources and rb",
"On Batch".

- `jobs_dump.py RUNS OUT`: the runs' job rows (scope, attempts, metrics).
- `batch_describe.py JOBS BATCH`: every attempt's container instance and
  start and stop times.
- `records.py` / `records_read.py`: run 187's per-job placement, co-tenancy
  (other jobs on the host, time-weighted) and `rb_s` per scored source.
- `queue_jobs.py`: jobs on the queue in a window that no run dump holds.
- `sample_read.py` / `within.py`: the sample matched by expid to run 187,
  with the branch's probe metrics (`rb_cpu_s`, `rb_cg_*`, `cgroup_cpu_*`).
- `scores.py BASE RUNS SCA`: `rb` of `sources` and `xsources` compared by
  expid, sign and catalog id.
