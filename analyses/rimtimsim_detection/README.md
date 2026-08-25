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

```bash
./dock.sh "python3.11 -m analyses.rimtimsim_detection.cli catalogs"
./dock.sh "python3.11 -m analyses.rimtimsim_detection.cli kernels"
./dock.sh "python3.11 -m analyses.rimtimsim_detection.cli truth"
DOCK_NAME=rtssweep ./dock_bg.sh \
  "python3.11 -m analyses.rimtimsim_detection.cli sweep"
./dock.sh "python3.11 -m analyses.rimtimsim_detection.cli aggregate"
```

Useful variations:

```bash
# one job, for a smoke test
... cli truth --jids 143919,143920
# one difference image and branch
... cli sweep --diff sfft --branch negative
# score only the RAPID-added population
... cli aggregate --population rapid
```

To **start clean and regenerate everything**, delete the working directory named
in `[paths] work` (keeping `catalogs/`, which holds the delivery archive) and run
the stages in order.

## Inputs that must be present

* `<work>/catalogs/ForRobby_2026Jun5-selected.zip` — the variable delivery
  (`catalog_RAPID.txt`, `lightcurves_RAPID_F087.pqt`, `lightcurves_RAPID_F213.pqt`).
  Members are extracted on demand.
* AWS credentials able to read `rapid-product-files` and `rapid-pipeline-logs`.

## Provenance

Each stage appends to `<work>/provenance.jsonl`: git SHA, config path, processing
date, database, and input checksums. This is not decoration — two RimTimSim
deliveries exist with near-identical catalogue files, and without a recorded
checksum it is not possible to tell after the fact which one a result was scored
against.

## Gotchas

* **`pyarrow` 19.0.0 cannot read the light-curve parquet** — it raises "Repetition
  level histogram size mismatch" on their `SizeStatistics`. Use >= 20; the
  pipeline container has 25.0.0. `catalogs.py` checks and fails loudly.
* Job ids are **per-database**. `jid143917` here is a *reference* job; the
  identically numbered job in `socsimsdb` is a science job.
* The two references are not interchangeable: jid143917 is K213, jid143918 is Z087.
