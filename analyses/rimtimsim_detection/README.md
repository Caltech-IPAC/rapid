# RimTimSim detection downselect

Runs the detection-algorithm matrix over the RimTimSim (TRExS GBTDS) simulation
products and scores it against the two truth populations present in that dataset.

Everything is driven by `rimtimsim.toml`. To re-run, re-scope, or point the
analysis at a different pipeline run, edit that file — no other file hard-codes a
path, a job id, or a matrix entry.

## The dataset

| | |
|---|---|
| processing date | `20260813` (database `rimtimsims3db`) |
| jobs | `143917`–`144181`: two reference, 263 science |
| field / detector | 4682737, SCA 2 — one pointing, many epochs |
| filters | Z087 (132 images), K213 (131 images) |
| baseline | ~70 days, 2027-02-14 → 04-24 |

This geometry differs from the SOC downselect in ways that matter:

* **One field observed repeatedly**, not many independent pointings. Trials are
  (source × epoch) and are strongly correlated — same positions, same background,
  same reference. Do not treat the error on a completeness estimate as `1/sqrt(N)`
  over all trials; aggregate per source and treat epochs as repeated measures.
* **Both signs are present.** The TRExS variables are eclipsing binaries and
  transiting planets — dips, which appear as *negative* difference residuals. The
  RAPID-added variables are brightenings. Detection therefore runs on both the
  positive and the negative difference image, and aggregation is sign-aware: a
  fading source is only scored on the negative branch.
* **Two filters at opposite ends of the Roman range**, so matched-filter kernels
  are built per filter from the measured PSF rather than inherited from W146.

## Truth

`dflux = f(t_sci) - <f(t_ref,j)>`, in DN/s, taken directly from the delivered
light curves. Three properties make this exact rather than approximate:

* The light-curve epoch grid **is** the image epoch grid (`OBS_TIME_BJD - 2400000.5`
  equals the difference image's `MJD-OBS` to full precision), so truth is a lookup.
* Light-curve values are already **flux in DN/s on the difference image's own
  zeropoint** — verified as 26.2982 vs a header `ZPTMAG` of 26.29818 (Z087) and
  25.8573 vs 25.85727 (K213). No photometric conversion happens anywhere.
* Each reference has 25 named constituent exposures listed in its job log, so the
  baseline is known exactly. Note the reference window covers only the first ~13
  days of the ~70 day survey, so early science frames are themselves reference
  constituents; that self-inclusion dilutes their own signal by ~1/25 and the
  formula above accounts for it.

Two populations, distinguished by `sicbro_id`:

* `>= 5000000` — the 1000 RAPID-added variables (`ForRobby_2026Jun5-selected`),
  overwhelmingly brightenings, peak/quiescent median 15.5x.
* `< 5000000` — TRExS-native (eclipsing binaries, transiting planets, red-noise).

Sources whose `|dflux|` falls below `truth.static_max_dflux` are the **static
control population** used to measure the chance-match floor. Completeness is
reported floor-corrected as `(c_obs - f) / (1 - f)`.

## Running it

Stages are idempotent — finished work is skipped unless `--force`, so a run can be
interrupted and resumed, or one stage re-run after a config change without
regenerating the earlier ones.

Nothing is tied to one person's paths. Three environment variables cover every
difference between machines, and none of them requires editing the config:

| variable | meaning |
|---|---|
| `RTS_WORK` | working directory for everything the analysis derives |
| `RTS_CACHE` | where fetched pipeline products are cached — point several people at one copy |
| `RTS_CATALOGS` | where the variable delivery archive lives — likewise shared, not copied |
| `RTS_CACHE_POLICY` | `keep` (default) or `discard`, see [Disk](#disk) |

### On rapid, in the container

`rts_docker.sh` runs any stage in `rapid_science_pipeline:1.0` as the calling user,
so nothing comes out root-owned. It takes the working directory from
`RTS_HOST_WORK`, so you can work anywhere:

```bash
export RTS_HOST_WORK=/data/$USER/rts
# reuse the existing product cache and delivery archive rather than copying either
export RTS_HOST_CACHE=/data/jj/work/rts_downselect/cache/img
export RTS_HOST_CATALOGS=/data/jj/work/rts_downselect/catalogs

./rts_docker.sh "python3.11 -m analyses.rimtimsim_detection.cli catalogs"
./rts_docker.sh "python3.11 -m analyses.rimtimsim_detection.cli kernels"
./rts_docker.sh "python3.11 -m analyses.rimtimsim_detection.cli truth"
DOCK_NAME=rtssweep ./rts_docker.sh \
  "python3.11 -m analyses.rimtimsim_detection.cli sweep > sweep.log 2>&1"
./rts_docker.sh "python3.11 -m analyses.rimtimsim_detection.cli aggregate"
```

`DOCK_NAME` makes the run detached and named, so the sweep survives a dropped ssh
session; watch it with `docker logs -f <name>` or by tailing the log you redirect
to. The full matrix is 263 jobs × 3 difference images × 2 branches = 1578 units at
roughly 6 minutes each, so parallelise it — one process per job id:

```bash
seq 143919 144181 > jids.txt
DOCK_NAME=rtssweep ./rts_docker.sh "xargs -a jids.txt -P 16 -I{} \
  python3.11 -m analyses.rimtimsim_detection.cli sweep --jids {} > sweep.log 2>&1"
```

### On a laptop, without the container

The stages need Python ≥ 3.11 with numpy, astropy, photutils and pyarrow ≥ 20,
plus a SExtractor binary and its config directory for the `SE_*` variants. Point
the last two at your own install:

```bash
export RTS_WORK=~/rts_work
export RTS_SEX=/opt/homebrew/bin/sex RTS_CDF=/path/to/rapid/c/cdf
export RTS_CACHE_POLICY=discard          # see Disk, below

python -m analyses.rimtimsim_detection.cli catalogs
python -m analyses.rimtimsim_detection.cli truth --jids 143919,143920
python -m analyses.rimtimsim_detection.cli sweep --jids 143919 --diff sfft
python -m analyses.rimtimsim_detection.cli aggregate --diff sfft --branch positive
```

### Re-running one variant

The sweep resumes at **variant granularity**, not per job. Each variant's results
carry a signature covering its parameters and, for the SExtractor families, the
*content* of its convolution kernel. On a re-run each variant is recomputed only
if it is absent, if its signature no longer matches, or if you ask for it — and
whatever is recomputed is merged back into the existing results rather than
replacing them.

So adding a variant to the matrix, or correcting one, costs that variant alone:

```bash
# add or correct one family everywhere, merging into the existing results
... cli sweep --variants 'SE-dao' --refresh-variants
# a quick look at what one new threshold does, on a handful of jobs
... cli sweep --jids 143919,143920 --variants 'SE-gauss-fN@4'
```

`--variants` takes a regex matched against variant labels. Results written before
signatures existed carry none; those are trusted rather than assumed stale, so a
plain `cli sweep` over an existing run does not silently recompute everything.
`--refresh-variants` is what forces them.

Aggregation warns if two variants produce bitwise identical detections, which is
a configuration error rather than a result — `SE-gauss` and `SE-dao` once shared a
kernel and reported the same numbers under two names.

### Disk

A full matrix caches about **100 GB** of difference images — 99 GB of the 105 GB a
completed run occupies. Two knobs control that:

* **`cache_policy = "discard"`** (or `RTS_CACHE_POLICY=discard`, or `--discard-images`)
  deletes each difference image *that the run downloaded* as soon as its sweep
  result is written. Peak usage drops to a few GB and a re-run re-downloads. This
  is the laptop setting. An image that was **already** in the cache is never
  deleted, so `discard` is safe to use against a shared cache.
* **A shared `cache`** — an absolute `[paths] cache`, or `RTS_CACHE` — lets everyone
  on rapid read and write one copy of the products. Fetches land in the shared
  cache, so nobody downloads the same image twice. Use this instead of `discard`
  on rapid, where disk is plentiful and bandwidth is not.

The derived outputs are small by comparison: truth 1.1 GB, sweep results 2.2 GB.

### Useful variations

```bash
# one job, for a smoke test
... cli truth --jids 143919,143920
# one difference image and branch
... cli sweep --diff sfft --branch negative
# score only the RAPID-added population
... cli aggregate --population rapid
# report each filter separately as well as pooled
... cli aggregate --filter all --filter each
# just K213
... cli aggregate --filter K213
```

Z087 and K213 are worth looking at separately: the measured PSF FWHM is 1.32 vs
1.73 px and the zeropoints are 26.298 vs 25.857, so they are two different
sensitivity regimes. `--filter` re-normalises FP/img over that filter's images
alone and re-measures the chance-match floor within it, both of which are
per-filter quantities.

To **start clean and regenerate everything**, delete the working directory named
in `[paths] work` (keeping `catalogs/`, which holds the delivery archive, and the
product cache if it lives elsewhere) and run the stages in order.

## Inputs that must be present

* **The variable delivery** — `ForRobby_2026Jun5-selected.zip`, holding
  `catalog_RAPID.txt` and `lightcurves_RAPID_{F087,F213}.pqt`. Members are
  extracted on demand. It is looked for in `RTS_CATALOGS`, else `[catalogs] dir`,
  else `<work>/catalogs`. On rapid a copy already sits at
  `/data/jj/work/rts_downselect/catalogs` — point at it rather than copying 1.17 GB.
* **AWS credentials** able to read `rapid-product-files` and `rapid-pipeline-logs`.
  `rts_docker.sh` mounts `$HOME/.aws` read-only by default; override with
  `RTS_HOST_AWS`.

Nothing else. Everything the analysis reports is derived from those two inputs
plus the config, which is what makes a re-run cheap and a re-scope a one-file edit.

## Provenance

Every stage appends an entry to `<work>/provenance.jsonl`: git SHA, config path,
processing date, database, input checksums, and the stage's own parameters — for
`sweep`, the measured PSF widths, the cache policy and the unit counts; for
`aggregate`, the match radius, the control cut, and which populations and filters
were reported.

This is not decoration. Two RimTimSim deliveries exist with near-identical
catalogue files, and without a recorded checksum it is not possible to tell after
the fact which one a result was scored against.

## Gotchas

* **`pyarrow` 19.0.0 cannot read the light-curve parquet** — it raises "Repetition
  level histogram size mismatch" on their `SizeStatistics`. Use >= 20; the
  pipeline container has 25.0.0. `catalogs.py` checks and fails loudly.
* Job ids are **per-database**. `jid143917` here is a *reference* job; the
  identically numbered job in `socsimsdb` is a science job.
* The two references are not interchangeable: jid143917 is K213, jid143918 is Z087.
