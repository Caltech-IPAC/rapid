# xsources and rb: profile and leads (exp/xsources-rb)

An investigation branch of the Exercise 2026-11 arc, never merged. It
profiles the difference stage's two X path steps, `rb_s` (RuBR-AT
scoring of SFFT's two PSF-fit catalogs) and `xsources_s` (the xsources'
PSF fit and their own RuBR-AT scoring), on one GBTDS window job, and
measures three leads on that job's own inputs. The findings are rapid_docs'
`log/2026-11-08-exercise-2026-11.md`, section "xsources and rb".

## The job

Run 187's difference job 46889 (exposure 5033601, SCA 3, F146), the job
nearest the run's 4,736 s median `container_seconds` (4,735.7 s), on
`rapid-rebuild:112`, an r6i.8xlarge.

## The scripts

- `replay_job.py JOB OUT`: the stage's `run(job)` on the job's own row,
  inputs and run settings, on a `file://` prefix under `OUT`, under
  cProfile; nothing is uploaded or published (the stage draws one
  `diffimages` pid). The `objects` and `xobjects` inputs the publisher
  recorded from the job's reads are dropped. It keeps the inputs and
  outputs of every `_score_rb` and `_xsources_columns` call under
  `OUT/snap`.
- `leads.py SNAP LABEL rb|xs`: those calls again on the snapshot, with
  the branch's switches or a `--set` of a `[difference]` key, timed, each
  output compared with the replay's (`rb_score` and `rb_label`; every
  extra column of the xsources).
- `fitcheck.py N`: the positive xsources' fit columns against the
  replay's, with agreement fractions, for a fit that is not identical.
- `podrun.sh CPU CODE ARGS`: python in the pipeline image with its
  threads pinned by `taskset`, as the measurements ran.
- `prof.py PROFILE N`: a profile's top N by cumulative time.

## The switches (environment, not settings)

- `RAPID_EXP_ORT_THREADS=N`: onnxruntime's intra- and inter-op threads
  (unset: its default, 16 on a 16-vCPU host).
- `RAPID_EXP_ORT_SPIN=0`: its pool threads do not spin.
- `RAPID_EXP_FIT_BATCH=1`: `xsources._fit_batched`, every source fitted
  at once by one vectorised Levenberg-Marquardt with the spline's own
  derivatives, in place of photutils' per-source TRF.

## Measurements (2026-11-08, m8a.4xlarge, one pinned core, image `rebuild-41d671e7…`)

| Step | Seconds | Outputs |
|---|---|---|
| Batch, job 46889: `rb_s` / `xsources_s` / `container_seconds` | 2,676.4 / 832.7 / 4,735.7 | the record |
| Replay, onnxruntime default: `rb_s` / `xsources_s` / stage | 263.7 / 563.3 / 1,610.7 (6,670 CPU s) | equal to Batch's (pid aside) |
| rb, SFFT catalogs, unchanged | 264.9 | identical |
| rb, one onnxruntime thread | 580.1 | identical |
| rb, one thread, no spinning | 634.1 | identical |
| rb, one thread, batch 1,024 | 621.3 | identical |
| rb, one thread, batch 64 | 555.7 | identical |
| xsources fit, unchanged (two processes) | 85.5 | identical |
| xsources fit, one process | 82.1 | identical |
| xsources fit, batched | 36.7 | not identical |
| xsources whole, one thread, one process | 573.9 (rb 492.0, fit 81.9) | identical |

onnxruntime opens 16 pool threads and pins 15 of them to vCPUs 1 to 15
whatever the process's affinity, so the default replay's scoring ran on
up to 16 cores. The one-thread rows are the job's real one-vCPU cost.
