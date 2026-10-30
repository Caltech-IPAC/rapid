# The SMDC step of the foundation

The SMDC step took the foundation's thin chain from a throwaway
PostgreSQL and `file://` prefixes onto SMDC: the `rapid` database
through its pooler, the inputs and scratch buckets, AWS Batch and the
Kafka alerts topic. This page is its state: what each of its thirteen
parts proved, with the numbers as they stand and the pull requests that
hold the record, and the procedure an operator runs today. The run log,
every command as it was run and every raw table, is this file at
`940e29b5` (`git show 940e29b5:docs/foundation-smdc-step.md`) and its
history (`git log -p -- docs/foundation-smdc-step.md`).

Every number below names the run and conditions that produced it. Where
a later part superseded a number, the earlier part points to the later
one, which alone states the current value.

An unattended step's record goes into part 13's table as one row (what
stands, its runs, its PRs), and into Current procedure and What remains,
edited in place, where it changes them; its commands and raw tables stay
in its pull request.

## Part 1: the schema

SMDC's `public` schema became `database/schema.sql` in one transaction
through the pooler as `postgres` (2026-10-21, `rebuild` at `1a528daa`,
PR 284): the run model's 45 tables dropped (108 objects cascaded),
`schema.sql`'s tables and official views and q3c created in 1 s; `ops`,
`pgbouncer` and every role untouched. Step 3, the grants: the pipeline
role `rapid_rebuild_pipeline` reads, writes and maintains every table and
sequence and, by `postgres`'s default privileges, everything created
later; the read role `rapid_read` selects every table and view, and the
same by default (that half added in part 6 step 1). SMDC is a prototype
(Ben, 2026-10-21; rapid_docs `decisions.md`): no backup before the
replace, no pooler fence; the rollback is the previous schema in git. The
statements are Current procedure's bring-up.

## Part 2: the inputs

The GBTDS field 4658155's directory went into the write-once inputs
bucket under `foundation/` with `put-object --if-none-match '*'`: 28
objects, 1,363,479,122 bytes, 34 s (2026-10-21). The bucket refuses an
upload without the create-only condition and every delete.

## Part 3: the baseline

Milestones 1 to 3's jobs (PRs 279 to 281: import, associate, packet) ran
again on SMDC under run 1, live, with every output on the scratch bucket
so that Batch stages can read them (the job role cannot read the
products bucket). Row counts equal PRs 279 to 281: 500 sources per
epoch, 38 then 77 objects, 38 and 39 packets, the reader CLEAN on both
sets (jobs 6 and 7). Every new table reaches the pipeline's roles
through part 1 step 3's default privileges. The associate stage and its
set chain are retired since part 11 step 7.

## Part 4: Batch, then the change group

`reference` and `difference` ran on Batch on the `run(job)` contract
(PRs 287, 288; `rapid-rebuild:31`, 1 vCPU, 15 GiB), with the chain after
them, under run 4, not live; one change group (9) replaced the imported
reference, difference images, sources, objects and packets together in
0.40 s, and its undo (10) restored every official view, every table's
row count unchanged but `official`'s. The reader read both Batch sets
CLEAN (82 and 73 packets). Milestone 4 is done. On that field's two
epochs a difference took 1,348 s and 1,324 s on one vCPU, peak 14.8 GiB
(the per-stage cost on HLWAS is part 8's). Run 4's reference coadded
the two epochs it differenced, so its sources match dev's within 0.055″
for under 2%: a proof that the chain runs on Batch, not a science
result; part 8 step 4 made the reference disjoint.

## Part 5: the field uncapped

The same chain with no source cap (run 5, `rapid-rebuild:32`): 5,703 and
5,544 sources per image before the cap, and nothing downstream changes,
because `associate`'s cuts keep fewer than 200 of them. Measured on run
5: one image's chain about 25 min, the difference 95% of it (1,369 s
uncapped against 1,348 capped: the cap is not a cost), its largest child
at 14.35 of 15 GiB; change group and undo 19.3 and 15.3 ms for 8 keys
through the pooler. A synthetic member chain of 10,000 sets read in
under 1 s (the chain itself is retired since part 11 step 7). rubrat,
NED and NED-LVS were data-limited (dev's bucket unreadable); NED's
reader and matcher are deleted since part 13's dead-trim.

**The difference stage's determinism** (runs 6 and 7, 2026-10-21): the
four unseeded runs of epoch 84624 gave 5,539 to 5,544 sources, because
dev draws the clipped statistics' correction factor unseeded; with
`[difference.statistics] clip_correction_seed = 0` two jobs wrote the
same pixels and rows, NaN for NaN, but `pid` and the FITS `DATE` card.
The reference's seed is pinned too since part 7 step 1.

## Part 6: the night

One GBTDS field's simulated night, 69 epochs of SCA 8 in one filter,
went through the chain twice (2026-10-21): run 9 in the live loop's
shape, run 10 as a campaign (reference job 231 from 25 frames, 69
differences on Batch, 4,708 to 5,748 sources an epoch). Step 1 gave the
PSF key its `kind` (`science`, `reference`) as the first incremental
SQL, and `rapid_read` its `SELECT`. Two seeded executions of one epoch
on different Batch instances (jobs 233, 301) wrote equal rows. Against
dev's night (step 10) the port's ZOGY difference correlates 0.970 with
dev's (0.961 to 0.978 an epoch), its MAD 4.78 to 4.80 against dev's 4.86 to 4.87. Checks 1 to 10 held
(Validation); one change group made the night official, 145 keys in
256.9 ms; an epoch replaced, an undo and two groups at once through the
pooler behaved by the rules. Database 239 MB after the night; run 10's
prefix 200.7 GB (cut to about a fifth by part 8).

## Part 7: HLWAS, from ASDF intake to Kafka

The SOC HLWAS simulation r00342 went from raw L2 ASDF through the
`intake` stage, the chain and Kafka (2026-10-22, run 12, revision
`c81e17ae`, `rapid-rebuild:35`): 426 epochs on 70 fields, F146 SCA 8
over the footprint then the 18 SCAs of one pointing, 55,436 packets on
the topic, every job published and bound, no Batch job failed. Step 1
added `jobs.instance_type`. Step 6: the intake reads a 204 MB file
unsigned in 0.8 s. Step 7: `scripts/validate_run.py` (Validation) 6/6
PASS on run 12. Step 10: every set read back from the topic byte-equal.
Step 11: `rapidpipe loop` carried steps 12 to 15 with no hand, 324 epochs
in 42.8 min and 102 in 34.6 min. Superseded since: the bytes per epoch
(part 8), the bright residuals under dev's copied F146 PSF (parts 9 and
10), a field's reference across SCAs (part 13's ref-overlap) and the
per-field associate chain (part 11 step 7).

## Part 8: housekeeping

Four steps (PRs 312 to 314, 316 to 318, 320 to 323, 325; runs 13 to 16
on run 12's pixels, their scratch prefixes deleted since) made the ramp
cheaper, measured against run 12:

| | run 12 | part 8 | step |
|---|---|---|---|
| bytes per difference job | 2,547 MB in 72 objects | 247 MB in 10, gzipped, the read set only | 1 |
| run prefix per epoch | 2.83 GB | 0.53 to 0.57 GB | 1, 2 |
| leaked working directories | 89, 22.2 GiB | 0 | 1 |
| memory asked per job | 24 GiB, every stage | 1 to 24 GiB by stage (`[batch.memory_mib]`) | 2 |
| a container that ends unrecorded | stays `running` | marked `failed` with Batch's reason in the next pass | 2 |
| idle loop pass | 29.6 to 40.6 s | 3.8 to 5.8 s | 3, 4 |
| `validate_run.py --run 12` | 1,512 s | 666 s | 3 |
| the reference's epochs | every epoch of the field | `[reference.selection] before_mjd`: passes 1 to 3 build it, passes 4 to 6 are differenced | 4 |
| the `catalogs` key | (name, version) | (dataset, name, version) | 4 |

The consumer check's time is part 11 step 5's. The difference
container's median, 227 s on these HLWAS epochs, did not change.

## Part 9: breadth

Step 1 (PRs 327 to 329): the `psf` stage derives each (filter, SCA)'s
PSF from the CRDS ePSF reference its L2 file names, and the loop submits
it for what has none; on the same pixels it cut the bright residuals by
a third against dev's copied F146 PSF (0.23 to 0.16). Steps 2 and 3
(PRs 331 to 335, 337): the six other filters, one dataset and live run
each, SCA 8 over the 54 pointings and the 18 SCAs of `001_0001`: 4,544
epochs ingested, 3,266 differenced, 643,004 packets, 2.1 TB of run
prefixes, database 2.74 GB (2026-10-23). The per-filter bright-residual
counts are superseded by part 10's. The failures seen at breadth (SSM
throttling, Kafka's dead sender thread, the associate rule on resume, a
validation false failure, the consumer's CRC) are fixed in those PRs.

Step 2's table, each filter's reference bound, the midpoint of its own
passes 3 and 4 at `001_0001`:

| filter | dataset | pass 3 | pass 4 | `before_mjd` |
|---|---|---|---|---|
| F087 | `r00342-f087` | 61590.00852 | 61591.51278 | 61590.76065 |
| F106 | `r00342-f106` | 61590.22362 | 61591.72788 | 61590.97575 |
| F129 | `r00342-f129` | 61590.43852 | 61591.94278 | 61591.19065 |
| F146 | | | | 61591.4056 |
| F158 | `r00342-f158` | 61590.86840 | 61592.37266 | 61591.62053 |
| F184 | `r00342-f184` | 61591.08330 | 61592.58756 | 61591.83543 |
| F213 | `r00342-f213` | 61591.29840 | 61592.80266 | 61592.05053 |

## Part 10: the null test's artefacts fixed on one pointing

The fixes of 2026-10-23, pointing `001_0001`, SCA 8, all seven filters.
Step 1 (PR 341): settings in layers, `run create` resolving base,
`--overlay` and `--set`s once and recording them in `runs.layers`.
Step 2 (PR 343): the DQ mask at intake, which removed the defects, most
of F184's bright end. Step 3 (PRs 344, 346): the reference PSF measured
in the difference job (`[difference] reference_psf = "measured"`, the
default; GBTDS's overlay uses `science`). Step 4 (PR 348): the SWarp
kernel stays BILINEAR against LANCZOS3. Step 5 (PRs 350 to 352): the
metrics in `job_metrics`.

| filter | sources above S/N 20 per epoch, before → after | packets per epoch | core / ring after |
|---|---|---|---|
| F087 | 112 → 18 | 192 → 64 | −0.003 / +0.007 |
| F106 | 100 → 22 | 200 → 56 | +0.004 / +0.005 |
| F129 | 101 → 42 | 213 → 108 | +0.007 / +0.002 |
| F146 | 79 → 19 | 213 → 71 | +0.006 / −0.002 (before +0.030 / −0.027) |
| F158 | 156 → 18 | 216 → 57 | +0.011 / −0.005 |
| F184 | 373 → 19 | 255 → 42 | +0.001 / +0.014 |
| F213 | 48 → 20 | 110 → 46 | +0.034 / +0.023 |

Medians per epoch; before is run 17 for F146 and part 9's official
products for the others. The healthy ranges read across these runs
(core within ±0.011 but F213, ring within ±0.015, registration within
0.12 px, usable fraction 0.999) are the source of the gate's bounds
(`[gate]`'s comment, part 11). What stays above S/N 20 is bright stars
(What remains).

## Part 11: the second iteration

The night of 2026-10-24, ten steps run unattended, each CLEAN by `check_run.sh` on its
first attempt.

| step | what stands | where |
|---|---|---|
| 1 GBTDS | r00340: the L2 WCS (9 to 50 px off) re-derived at intake from the file's own distortion reference, catalog stars +0.03 / −0.03 px; one pointing through the chain, run 67, 515 packets; the bulge is confusion-limited and a pointing is the visit modulo six (rapid_docs `datasets.md`) | PRs 361, 370 |
| 2 OpenUniverse | TDS is ASDF, one 0.27 deg² patch, truth in every file and per image on IRSA; the `openuniverse` reader's WCS on the truth to 0.0016 px; the WCS one-based and the zero point not AB, fixed on the overlay | PR 365 |
| 3 metrics | bit 16384 (bright footprint) informational; science stars 1.5 to 3.3% wider than the ePSF on SCAs 4, 5 and 8 alike; a 121 px stamp changes nothing | PRs 360, 363, 369 |
| 4 loop-ops | HLWAS's (+0.12, −0.99) px WCS offset corrected by `crpix_shift` (Gaia +0.007 / +0.003 px); `--passes`, `job wait`, one rerun and 1,000 submissions a pass, no SSM read in a job | PRs 354, 356, 359, 364, 366, 367, 374 |
| 5 producer | the packet job sends its own set 3 to 4 s after it ends, its offsets on the stamp, the gate at the send boundary; the consumer check 4 s; idempotence part 12's | PRs 358, 371, 372 |
| 6 db-guards | dataset in every key and the import identities tested in CI; `forced` rows per set = packets × window images | PRs 355, 357 |
| 7 association | association in the difference job over an append-only per-field catalog, reconciled at publish; 31 s last difference to last packet against 5.2 min; the chain's tables kept read-only as `objects_chain`, `memberships_chain`; the field rule part 12's | PRs 378, 379; rapid_docs 97 |
| 8 spot | four rules, one attempt on Spot; run 70, 7 of 14 jobs on Spot, 0 reclaims; a probe lane only | PR 381; rapid_systems 421, 423 |
| 9 injection | the injected footprint (200 sources per image, AB 21 to 27, 162 epochs of SCA 8): detected 0.94 to 0.98 to AB 25, 0.75 at 26; purity 0.93 at AB 21 to 0.20 at 26; the alerted fraction, 0.40 under dev's field rule (run 72), is part 12 step 1's; every 0.5 mag bin is this file at `940e29b5` | PRs 383 to 385; rapid_systems 424 |
| 10 docs | rapid_docs' foundation statement and amendment, the gate, Spot, the datasets page | rapid_docs 98 |

The gate (`rapidpipe.foundation.gate`, `[gate]`) holds a packet set when
its difference's `n_sources_snr_gt20` is above 3.0 times the run's
median or `|core_fraction|` above 0.025 or `|ring_fraction|` above
0.015, from `min_epochs` = 5 published differences on; part 13's
gate-metrics added lost sky and misregistration.

## Part 12: the day

The day of 2026-10-24, eight steps run unattended, each CLEAN by `check_run.sh` on its
first attempt.

| step | what stands | where |
|---|---|---|
| 1 field rule | association by the source's field, locks over each touched field and its neighbours, the forced window by position: run 81 alerted 27,750 of 28,623 detected injected (0.97) against run 72's 12,097 of 28,625 (0.42); purity per magnitude unchanged; false positives per epoch 95 → 224 | PRs 393, 404; rapid_docs 99 |
| 2 producer | idempotent, `acks="all"`; run 78, 3 of 3 sets sent by their own packet jobs, consumer CLEAN 280/280 | PRs 388, 396; rapid_docs 100 |
| 3 OpenUniverse | galsim.roman PSF in the `psf` stage, the loop's listing by obseq row, `scripts/completeness_ou.py`; run 80 (H158 SCA 10, six-frame reference, ten epochs): 410 transients detected 1.0 to AB 24, 0.5 at 25, the 500-source cap ending the count | PRs 390, 392, 400, 402; rapid_docs 101 |
| 4 GBTDS reference PSF | the crowded-field measured variant (run 79) is not lower on all three epochs than `science` (run 67), so not adopted; branch `day/gbtds-refpsf` | PR 403 |
| 5 F213 scale | r00342 F213 (runs 54, 76, 77): forcing `scalefacref` to the zero points' ratio leaves the ring, so the residual is a width mismatch (reference PSF 1.387 px, science ePSF 1.291, science stars 1.326); no `[difference] scale` setting | PR 397 |
| 6 SCAs 7 and 9 | their low detection is area lost: the finder missed any star within 4 px of a NaN (fixed by part 13's finder-mask); SCA 9's reference was coadded from SCA 8's frames | PR 391 |
| 7 ops | `job submit --job` reruns a failed row; rapidpipe's INFO lines in CloudWatch; `sets_late` in the pass line; a failed job holds only its own work; run 82 CLEAN 559/559 | PRs 389, 394, 395, 398, 399, 401 |
| 8 docs | the field rule and OpenUniverse across rapid_docs | rapid_docs 102 |

The night's grants closed the same day: the Spot interruption capture,
the three identity sequences past their imported ids, the job role's
idempotent write (Ben, 2026-10-24 14:02 UTC).

## Part 13: the unattended steps

From 2026-10-24 19:22 UTC each step is one task run unattended beside
others, with its pull requests as its record. Every run named is CLEAN by `check_run.sh` but run 83
(failed on purpose), runs 137 and 138 (failed on PR 465's
regression) and run 158 (FAIL on the consumer only: the gate holds all but one of its sets). A row whose finding a later row superseded says so.

| step | what stands | runs | where |
|---|---|---|---|
| dev-takenow | the reference converges on dev: point-source FWHM mode (2.70 → 2.425 px), SExtractor seeing 0.25 and deblend 32, CLASS_STAR > 0.8 411 → 1,059 of about 4,300; coadd pixels identical | 46, 85 | PRs 406, 411 |
| err-tools | a tool's nonzero exit fails the job, exit 70, `ToolFailed` (tool, code, last 20 lines) on the row | 83, 84 | PRs 407, 412; rapid_docs 105 |
| differencer | SFFT published on one epoch: core +0.251 against ZOGY's +0.006; superseded by diff-flavor | 86 | PRs 408, 410, 413; rapid_docs 103 |
| err-publish | an empty expected product is refused unbound unless `empty_ok`; `loop` exits 1 and `job wait` 0 / 1 / 2 | 87 | PRs 409, 414; rapid_docs 104 |
| err-inputs | an unreadable stamp, a bad reference catalog or a missing `refimcatalogs` row fails the packet job naming it; nothing caught around the PSF fit | 90 | PRs 415, 417 |
| differencer-measure | on the same bytes, HLWAS injected: SFFT 256 of 600 against ZOGY's 542, false positives per epoch 385 against 228; OpenUniverse 24 against 35 of 410; superseded for HLWAS by sfft-rearm | 88, 89 | PRs 418, 419; rapid_docs 107 |
| dev-refresh | 9100 px references (peak 4.5 of 6 GiB), nothing downstream changed at `001_0001` SCA 8; dev's grading, `status` and `infobits` equal dev's on 288 references | 91 to 94 | PRs 416, 421; rapid_docs 106 |
| err-transient | a transient failure exits 75 on process and row; every failed row's `refusal` is `<Class>: <message>`; an empty first pass exits 64; `validate_run.py` fails a failed job | 95 | PRs 420, 424; rapid_docs 108 |
| diff-flavor | the published differencer is `[difference] diff_flavor`, shipped `zogy` (Ben, 2026-10-24 21:19 UTC) | 98 | PR 425; rapid_docs 110 |
| dev-columns | dev's sumrat, nneg and nbad on every SFFT candidate; sumrat > 0.4 keeps 0.997 of injected, 0.68 of noise; schema statement `database/2026-10-24-pixel-columns.sql` | 96 | PRs 422, 427; rapid_docs 112 |
| mig-quickstart | the README's Getting started (one HLWAS epoch against a throwaway `postgres:18`, 465 packets) and rapid_docs' loop page Starting a night, each followed verbatim | 97 | PRs 423, 428; rapid_docs 109 |
| gate-metrics | the gate also holds at `usable_fraction` < 0.9 and a registration offset > 0.25 px; replayed over 4,627 sets it holds run 72 SCA 9's three (usable 0.704) and nothing healthy | 98 | PRs 426, 430; rapid_docs 111 |
| rimtimsim-reader | `[intake] reader = "rimtimsim"`, overlay `rimtimsim-260622.toml`, WCS within 0.06 px; F213 and F087, 10 reference frames and 5 epochs each; F213 core −0.075 to −0.050; the 250513 truth does not describe the 260622 images | 99 to 101 | PRs 429, 437; rapid_docs 118 |
| dev-prefilter | under `sfft`, sumrat > 0.4 and a 2 px edge before the cap: 275 of 600 against raw SFFT's 256 and ZOGY's 542; `zogy` stays; superseded for HLWAS by sfft-rearm | 102 to 107 | PRs 431, 432, 434; rapid_docs 114 |
| sfft-core | SFFT's HLWAS core residual was the zero-filled DQ-masked science cores in its kernel fit; the HLWAS overlay carries dev's OpenUniverse SFFT block, read only under `sfft`: core −0.000026 | 108 to 111 | PRs 435, 436; rapid_docs 116 |
| gbtds-second | `r00340-gb3`, 266.71 −28.93 SCA 2 F146: difference 1,036 s, reference peak 4,514 of 6,144 MiB, 1,278 packets (part 13's gate-sci-psf reads 1,196 on `:87`); `gaia_dy_px` +0.27 | 112 | PR 438 |
| sfft-rearm | under the HLWAS OpenUniverse block SFFT with the prefilter detects 530 of 600 against ZOGY's 542 (losses at AB 26 and 26.5 only) at false positives per epoch 102 against 214; the overlay stays `zogy` | 113, 114 | PR 440; rapid_docs 119 |
| finder-mask | the PSF-fit finder and fit take the difference's non-finite pixels as the mask, filled from their neighbours for the finder: SCA 7 below AB 25 0.728 → 0.959; SCA 8 542 → 545 of 600, false positives unchanged | 115 to 117 | PRs 439, 442; rapid_docs 121 |
| mig-stale | AGENTS.md, CONTRIBUTING, README and the rapid_docs pages describe the tip | | PR 441; rapid_docs 120; rapid_systems 427 |
| flight-first | program 1049's four F158 WFI02 epochs, overlay `flight-1049.toml` (HLWAS's DQ mask, no WCS correction): 328 packets; its hand reference for e4 superseded by ref-overlap | 118 | PRs 443, 445; rapid_docs 123 |
| ou-gain | OpenUniverse's `scalefacref` 2.2 is the socsim reference zero point (10^(0.4 × 0.8564) = 2.2006), not `sca_gain`; on the science's own it reads 1.018, outside 0.94 to 1.01, so the overlay stays | 121, 123 | PR 448 |
| self-contained | every live run's L2, truth and Gaia are in the inputs bucket (139 objects copied under `g0008-selfcontained/`, 489 verified in place); no run of 1 to 124 names an IMSS bucket | | PR 449; rapid_docs 124 |
| ou-zp | the science's own zero points with `sca_gain = 1.0` hold every line but false positives 493 against at most 492; landed on the overlay on Ben's ruling (2026-10-27) accepting the 493, no tolerance rule added | 125 | PRs 451, 492 |
| ref-overlap | a field's reference from every row before the bound whose `overlapfields` list it, of any SCA, as dev: flight e4 with no hand job; HLWAS injected 546 of 600 | 126, 127 | PRs 450, 452; rapid_docs 125 |
| gaia-pm | `[intake] gaia_proper_motion` carries Gaia's stars to the frame's mid-exposure, shipped `false`, `flight-1049.toml` `true`: flight dy −0.56..−0.35 → −0.060..−0.038 px; acceptance failed on e4's dx −0.0909 against 0.09, the setting left in force; GBTDS SCA 2's offset is not proper motion | 128 to 130 | PRs 453, 454 |
| forced-growth | a set's `forced` rows are its objects times its window images; 4.83 M rows, 1.02 GB; on the breadth runs a field's rows grow quadratically in epoch, copies 0.57 of run 38's | 36 to 38 | PR 455 |
| diff-trim | the naive subtraction makes no PSF-fit catalog: RimTimSim F213 epoch 4571 difference 4,169 → 2,645 s, `sources` equal | 122, 124 | PRs 446, 456; rapid_docs 126 |
| err-edges-ops | the operations paths' 36 edge and cut rows binned; only `NotFound` exits 64, a stage's own error 70 with its traceback; `check_run.sh` FAILs a run with no packet set (run 85 CLEAN → FAIL); 181 packets | 134 | PRs 458, 460 |
| err-edges-sci | the science code's 13 rows binned: a failed catalog write, an unreadable required node and an empty background raise; a rejected declared input exits 65; 181 packets, equal to run 134 | 135 | PRs 459, 461 |
| rts-f213-core | a measurement, not taken: `reference_psf = "science"` on RimTimSim F213 epoch 4571 brings the core inside (−0.0694 → −0.0044) and the ring further out (−0.0339 → −0.0557) | 124, 133 | PRs 462, 463 |
| mig-contracts | `run(job)` is the one stage contract; `admit`, `finalize`, `run_stage`, the `stage` and `selftest` verbs and `Storage` deleted; 181 packets, equal to run 135 | 136 | PRs 464, 466; rapid_docs 128 |
| gate-sci-psf | under `science` the core, ring and registration stars come from the reference catalog; GBTDS gb3 on `:87`: core −0.036 to −0.048, ring −0.038, registration at most 0.015 px, 1,196 packets; replayed at `min_epochs` the gate holds every epoch on core; `:85` and `:86` carry PR 465's `ring_count` regression, fixed by PR 468 | 137, 139 | PRs 465, 468, 470; rapid_docs 130 |
| dead-trim | the code nothing called is deleted; run 141 (every job on m6a) 181 packets, core and ring equal to run 136; run 140's 180 came from an r6i reference | 138, 140, 141 | PRs 467, 469, 471; rapid_docs 129 |
| release-v0.13 | the tip `e7f042f9` tagged `rebuild-v0.13` (annotated, the v0.12 JSON shape; docs-only past `9f4c54f6`), its image built on base `base-39826742678-1` (digest `7ab782c1…`), `rapid-rebuild:88` and `rapid-rebuild-production:79` registered by the deploy hook with `RAPID_RELEASE_IDENTITY=rebuild-v0.13`, the pins snapshot written (2026-10-25 23:58 UTC); proved on Batch by release-proof: run 142 (dataset `r00342-v013`, the HLWAS null epoch 4040101 on `rapid-rebuild:88`, reference on m6a.2xlarge) CLEAN with 181 packets, core −0.0010316, ring +0.0017106, 828 / 500 candidates kept, 25 at S/N > 20, equal to run 141 | 142 | tag rebuild-v0.13, PR 475 |
| window-gunzip | each gzipped window file is decompressed once per packet job, to a plain file beside its staged copy that forced and the cutouts both read (the trigger's difference and science images were read twice); on `rapid-rebuild:89` (m6a) the null epoch's packet job 18.18 → 15.27 s against run 142's, 181 packets, core and ring equal, packet content equal but for processing time and row ids | 143 | PR 476 |
| port-schema-07 | the packets are alert schema 00.07 (`rapid.v00_07`, dev 883e3876), the eight Roman observation identifiers and `survey` filled from the official `exposures` row intake writes and `surveys`, `reliability`, `reliabilityThreshold` and `reliabilityVersion` null; `exposureTime`, null on every packet before (the join keyed `exposures` by the difference job), filled; schema statement `database/2026-10-26-exposure-identifiers.sql` (eight nullable columns, no backfill) applied 2026-10-26 21:17 UTC; on `rapid-rebuild:90` (m6a) the null epoch's 181 packets carry all eight identifiers, `survey` SOCSIMS, reliability null, core and ring equal to run 143 | 144 | PR 481; rapid_docs 136 |
| sfft-default | the published differencer is SFFT with dev's prefilter, `[difference] diff_flavor` shipped `sfft` (PR 485 re-landed as 490 on Ben's acceptance of run 146, 2026-10-26 23:27 UTC); injected passes 4 to 6: 533 against ZOGY's 546 of 600, false positives per epoch 102.7 (119) against 213.3 (233); on `rapid-rebuild:93` (`0b7492fc`, `sha256:ed1dc64e…`, production `:84`) the null epoch's 86 packets, core +0.000445, ring −0.007365, against run 145's 86, +0.000463, −0.007423 (each run's reference built by its loop: rfid 90113 against 90110, both 3 frames, cov5percent 11.58) | 145, 146, 151 | PRs 485, 486, 490; rapid_docs 139 |
| rb-port | dev's RuBR-AT real/bogus score (`compute_rb`, model `2026-10-06_canonical9_seed42`) on every SFFT candidate, computed in the difference job by a sidecar venv holding TensorFlow 2.20.0 and Keras 3.14.1 (temporary; the pipeline env unchanged), loaded as `sources.rb` and `rblabel`, nothing cut on it; parity with dev's own code on 20 candidates: max score difference 0.0, labels equal; schema statement `database/2026-10-27-real-bogus.sql` (two nullable columns, no backfill) applied 2026-10-27 02:11 UTC; on `rapid-rebuild:94` (`cb4ba528`, `sha256:e582cd19…`, production `:85`) injected epoch 4040101: 155 of 266 packets scored (run 146: 0 of 268), each equal to its `sources.rb`, threshold 0.5134, version `2026-10-06_canonical9_seed42`; 111 unscored (a NaN, a DQ-masked pixel, in the 64 px difference window, dev's rule); packet purity 0.64 (171 of 266) → 0.99 (99 of 100) at the threshold, detected 180 → 99 of 200; detected and false positives 180 / 95 against run 146's 180 / 97 | 152 | PRs 493; rapid_docs 140 |
| sfft-block-base | dev's OpenUniverse `[difference.sfft]` block (cross-convolution, segmentation, gain-match catalogs, bsmask 50 / 100) is the base's and the HLWAS overlay drops it, HLWAS's resolved settings byte-identical (PR 495); on `rapid-rebuild:95` (`776bdc3b`, `sha256:c9c4ecfd…`, production `:86`), one epoch each, no `diff_flavor` override: HLWAS null 4040101 86 packets, core +0.000463, ring −0.007423, 293 / 293 candidates / kept against run 151's 86, +0.000445, −0.007365, 291 / 291 (rfid 90119 against 90113; run 145's rfid 90110 read 293 / 293, +0.000463, −0.007423, to the digit run 153's; cause of the spread unassigned); OpenUniverse 28737 detected 4 of 56, false positives 496, core −0.0057, ring −0.0005, candidates 1,300 against run 125's (ZOGY) 4 of 56, 493, −0.0202, +0.0136, 2,593; flight 1049 e4 core +0.0097, ring −0.0122, usable 0.781, 324 packets, difference 866 s against run 126's +0.0013, −0.0050, 0.781, 328, 222 s; the gate at `min_epochs` sends OpenUniverse's set under both and holds flight's under both on usable; GBTDS gb3 (epochs 2014201 and 2171201, run 157) and RimTimSim F213 4571 (run 156) fail every difference job, SFFT's kernel solve `Singular matrix`, exit 70, after about 60 and 33 minutes in SFFT (science frames of 144,464 and 82,866 SExtractor sources); RimTimSim's second attempt failed the same, after 52 minutes; no overlay pinned: OpenUniverse's SFFT set sends, and flight's, GBTDS's and RimTimSim's ZOGY rows (126, 139, 124) are held themselves; what GBTDS and RimTimSim publish is Ben's open ruling | 153, 154, 155, 156, 157 | PRs 495; rapid_docs 141 |
| socsims-1021 pilot | `dev`'s socsims lite FITS read (`[intake] reader = "socsims-lite"`, overlay `gbtds-r00340-fakesrc`, `loop --expids`, `[psf.epsf]` per SCA) on `rapid-rebuild:96` (`e99f3852`, `sha256:b7146c67…`, production `:87`); Russ Laher's 10/20/2026 subset, the 72 images of pass 5 observation 3 visits 61 to 64 (`--passes 2,5 --expids 2000000-2999999,5036101-5036401`, exactly req6's inputs), drained clean under `dev`'s socsims `[difference.sfft]` block, no `Singular matrix`: 2,603 intakes, 72 references of 25 pass-2 frames each (10/20's 109 had 25), 72 differences 658 / 760 / 853 s, 72 packet jobs, 29,555 packets; every difference fills the 500 cap (candidates 3,425 / 7,121 / 16,742); core +0.024 / +0.065 / +0.114, ring −0.022 / +0.021 / +0.072, usable 0.9986 or more: the gate holds all 72 sets (71 on the core), none sent, so `check_run.sh` reads FAIL on this run by its rule; Gaia DR3 through the file's WCS median (−0.004, −0.004) px over 2,603 frames, no `rederive_wcs`; reference membership equals Russ's 10/20 mosaics' `INFILnnn` on 45 of 72 fields, the rest 22 to 24 of 25 shared (`dev` breaks an `mjdobs` tie by distance from the tile centre) | 158 | PR 498; rapid_docs 142 |
| socsims-1021 window | Russ Laher's 10/21/2026 window, all 1,998 images of pass 5 observation 1 visit 25 through observation 3 visit 64 (111 exposures, `--passes 2,5 --expids 2000000-2999999,5012501-5036401`), published under run 158 on `rapid-rebuild:96`, six on `:97`: 1,998 differences and 1,998 packet jobs, 0 failed; 109 references (37 new) of 25 pass-2 frames each; difference 656 / 844 / 1,818 s; drain 08:44 to 11:07 UTC, 614.5 vCPU-hours of Batch attempts (intake 60.3, reference 2.7, difference 470.5, packet 81.0); the gate holds 1,997 of 1,998 sets (1,993 on the core, median +0.066; 4 on the ring). Six SCA-18 differences failed (exit 70) in the pixel columns' 64 px background: photutils 3.0 drops every mesh box that keeps under 90 % after the 3-sigma clip, and these heavy-tailed SFFT differences keep 88.5 %; `dev` measures on the image itself, so does PR 500 (`4ebff8a6`, `sha256:e8058891…`, production `:88`), and all six published on it. The run's bound 61685.81344 rounds the window's start up: its first exposure (visit 25, 61685.8134375) fell before it, was never differenced by the loop, and was hand-submitted under the run in the loop's job shape (18 differences, 18 packets; no reference took a visit-25 frame) | 158 | PR 500; rapid_docs 143 |
| socsims-1021 check and page | run 158 checked whole: validate lineage, tables, catalog, forced and counts PASS (0 failed, 0 pending), its packets check (about 159 GB of Avro) stopped unfinished at two hours, the reader 1,998 / 1,998 sets clean, the consumer 396 / 396 packets of the one sent set; against Russ's req7 image by image (1,998 of 1,998 matched by file name, his four SFFT sums reproduced exactly) the SFFT positive SExtractor count is the same (ratio median 1.000) and the PSF-fit count 1.154 times his (Q1 1.121, Q3 1.174; median 11,856 against 10,396 per image), cause not read; 15 panels read: the scene removed on 13, partly on 2 (one SCA 2 pointing, visits 30 and 66, a diagonal dipole at every star, the same in Russ's own SFFT difference of jid 146218); the gate holds 1,997 of 1,998 sets (core 1,993, ring 4), the one sent (packet job 31648) having no residual stars to measure; the team page is the rapid_docs log post of 2026-10-27, with the galleries under the run's `qa/pilot/` and `qa/full/` | 158 | rapid_docs 144 |
| outage-probe-prep | `rapidpipe outage-probe` (temporary) meets the Thursday database bracket with one size-three array job on the prompt queue per window, scheduled by two one-shot EventBridge Scheduler entries at 14:58 UTC on 2026-10-29 and 2026-11-05 (`--until` 16:15 UTC, `rapid-rebuild:98`, image `rebuild-dd9f97f5`): 0 fresh login, 1 idle connection, 2 transaction in flight. Dry run on the canary's loopback pooler: under a 75 s PAUSE a fresh login gives up after 57.5 s (exit 75; `auth_query` blocks, the client's 10 s connect timeout fires five times) and its retry completes at RESUME, while the idle connection and the in-flight transaction wait it out; PAUSE waits 2.6 s for the open transaction; a refused pooler spends the client's budget in 7.5 s; `systemctl restart pgbouncer` (SIGTERM) waits for connected clients, while a safe shutdown and start (3 s) severs the idle connection, which the client reopens in 3.5 s. outage-probe-read reads both windows and sets the client's budget | none (Batch array jobs, outside runs) | PRs 510, 511; rapid_docs 147 |
| gaia-mirror | the Gaia product is one mirror per release read as a cone per chip, not a box per dataset (rapid_docs `decision-gaia-mirror`, Emily Everetts, 2026-10-29): `dev`'s catalog store and cone readers (NED, NED-LVS, Gaia; a41c4d7b) ported as `rapidpipe.science.catalogs`, `python -m rapidpipe.science.catalogs.store ingest-gaia` builds the mirror once per release into the catalogs bucket under `gaia/DR3/` (rapid_systems' `roman-rapid-catalogs`, proposed with this step); the import scope's `gaia` names the prefix (`"partial": true` for a developer's copy of some leaves) and copies nothing; the packet job reads the chip's cone widened by the largest proper motion over the epoch offset and records the release, the epoch, the cone and the row groups read in its manifest's `packet.gaia`; the intake's WCS offset metric reads the frame's cone. The team's mirror is not yet built; the live runs' boxes stay bound to those runs | none yet | rapid_docs gaia-mirror |

## Current procedure

What an operator runs at the tip of `rebuild`. rapid_docs' loop page,
Starting a night, is the verbatim recipe for a night and its exit-code
table; the README's Getting started runs one epoch locally.

**The environment.** Account names come from rapid_systems, never from
this repository. A night runs on a workstation under its own instance
role, `rapid-<owner>-instance-role` on rapid_systems'
`WorkstationSubmitterRoleNames` (its `docs/workstation-submission.md`),
with no `AWS_PROFILE` set: `run create`, `official change`, the Gaia
import by `job local`, `loop` and `check_run.sh` need nothing more. An
administrator profile is needed only for the image (the ECR push and
the release `deploy` hook) and for a statement as `postgres` on the
database host (the bring-up below and a schema change). Proof: run 149,
the HLWAS null epoch 4040101 on `rapid-rebuild:91`, created, made live,
imported and drained CLEAN through `loop` under
`rapid-rivera-instance-role` alone (2026-10-26; 86 packets, consumer
86/86). The four teammate hosts move onto their roles from the shared
one at their next running swap.

```sh
unset AWS_PROFILE            # the workstation's own instance role
export AWS_REGION=us-east-1
export PY=...                # the rapid environment's python3
export PYTHONPATH=$PWD       # a checkout of rebuild
export RAPID_PARAMETER_PATH=/rapid/rebuild   # the job definition's parameter tree
unset PGHOST PGPORT PGDATABASE PGUSER PGPASSWORD
export RAPIDPIPE_OUTPUTS_ROOT=s3://<the scratch bucket>/rapidpipe/foundation
export RAPIDPIPE_BATCH_JOB_QUEUE=rapid-queue-prompt
rp() { $PY -m rapidpipe.cli.main "$@"; }
```

**Bring-up of a fresh `public`** (part 1). Nothing runs on Batch:
`aws batch list-jobs --job-queue <q> --job-status <s>` is empty for
`SUBMITTED` to `RUNNING` on `rapid-queue-prompt` and `rapid-queue-bulk`.
Then, as `postgres`, in one transaction, `DROP SCHEMA public CASCADE`,
`CREATE SCHEMA public` and `database/schema.sql`, then the grants:

```sql
GRANT USAGE ON SCHEMA public TO rapid_rebuild_pipeline;
GRANT SELECT, INSERT, UPDATE, DELETE, MAINTAIN ON ALL TABLES IN SCHEMA public TO rapid_rebuild_pipeline;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO rapid_rebuild_pipeline;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE, MAINTAIN ON TABLES TO rapid_rebuild_pipeline;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO rapid_rebuild_pipeline;
GRANT USAGE ON SCHEMA public TO rapid_read;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO rapid_read;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT ON TABLES TO rapid_read;
```

Check: no table (`relkind` `r`, `p`) of `public` fails
`has_table_privilege('rapid_rebuild_pipeline', oid,
'SELECT,INSERT,UPDATE,DELETE,MAINTAIN')` and no sequence fails
`has_sequence_privilege(..., 'USAGE,SELECT,UPDATE')`; `SELECT
q3c_version()` answers; `rp run show 1` exits 64, "run 1 does not exist"
(connected, empty). A fresh `schema.sql` takes no dated file
(`database/README.md`). A schema change to SMDC's existing database is
one dated file, one granted statement as `postgres` on the database host
(`podman exec -i rapid-postgres psql -U postgres -d rapid -X -v
ON_ERROR_STOP=1`), applied once and recorded in its step.

**The image.** rapid_systems' `containers/rapid-pipeline/build-rebuild.sh
<sha> --tag rebuild-<sha>`, the full 40-character sha (a short one fails
the deploy hook after the push), then its
`cloudformation/release-hooks/deploy` with `RELEASE_TAG` and
`RELEASE_IMAGE_DIGEST`, which registers the next `rapid-rebuild` revision
and moves `rapid-rebuild-production`'s with it. One deploy at a time
(one stack). A proof submits the revision its own deploy printed, or,
when two deploys meet one tag, the revision whose image digest the job
definition shows. Never prove on `rapid-rebuild:85` or `:86`: both carry
PR 465's `ring_count` regression. Nor on `rapid-rebuild:91`
(`rapid-rebuild-production:82`, PR 485 before its revert by PR 486) or
`rapid-rebuild:92` (`rapid-rebuild-production:83`, ZOGY by default):
`rapid-rebuild:93` (`rapid-rebuild-production:84`, `0b7492fc`, PR 490)
publishes the SFFT default and supersedes both; `rapid-rebuild:95`
(`rapid-rebuild-production:86`, `776bdc3b`, PR 495) is the newest.

**A run.** One dataset name and one live run per proof; a run's bindings
are its dataset's alone; two loops never run under one run at once. The
steps are the loop page's: `rp run create` with `--revision` the full
sha, `--overlay` the dataset's `rapidpipe/settings/datasets/*.toml`, and
`--set` the source root and the reference bound; the live binding by
`rp official change`; the dataset's Gaia import by `rp job local`; then

```sh
timeout 5400 $PY -m rapidpipe.cli.main loop --run $RUN --filters F146 --scas 8 \
  --pointings 001_0001 --passes 1,2,3,4 --job-definition rapid-rebuild:<n> --interval 60
```

The bounds: HLWAS's are part 9 step 2's table; GBTDS gb1 61679.1449,
gb3 61679.1465 (the first six epochs); flight 1049 61337.4594 (e1 to e3);
RimTimSim 61455.3; OpenUniverse 62026.3; the socsims lite 61685.8134 (just
before the 10/21/2026 window's first exposure, 61685.8134375; run 158's
61685.81344 left that exposure out). `[difference.paths]` is frozen
at `run create`, so a run that will also difference locally is created
with them. The difference stage refuses an `l2files` input keyed under
another dataset.

**The check.** `scripts/check_run.sh --run $RUN --inspect DIR` runs
`inspect_run.py` (the quality numbers and gallery), `validate_run.py`,
`read_packets.py` on every bound set and `consume_packets.py`, and prints
`Check run N: CLEAN|FAIL validate p/n reader c/s consumer m/m`. It
FAILs a run with a failed job (so a run that fails on purpose reads
FAIL) and a run with no stamped packet set or none holding a packet (so
a run whose every set the gate held reads FAIL). The Kafka topic keeps 7
days; a consumer check holds only inside them.

**The gate.** `[gate]` holds a packet set at the send boundary when its
difference's `n_sources_snr_gt20` exceeds 3.0 times the run's median,
`|core_fraction|` 0.025, `|ring_fraction|` 0.015, the registration
offset 0.25 px, or `usable_fraction` falls below 0.9, once the run has 5
published differences; a run of fewer epochs sends every set unjudged.

**The exit codes.** `loop`: 0 drained clean; 1 drained around failed or
refused jobs, each named in the last pass's `failed` (read the row's
`refusal`, fix, `rp job submit --job <id> --job-definition <def>`, start
the loop again); 64 a usage error, a first pass that admitted nothing
among them; 75 transient, start it again. `job wait`: 0 all published,
1 one failed, 2 timed out. A job: 64 only for the foundation's
`NotFound`; 65 a rejected declared input (difference, reference,
import); 70 a stage's own error, a missing dependency, a tool's nonzero
exit or a failed product write, with the traceback; 75 transient. Batch
retries 75 and a task that failed to start, nothing else.

**Comparisons.** Two settings or differencers are compared on the same
bytes: a dataset of their own, the loop for PSF and intake only, the
difference and packet jobs submitted against the baseline difference
job's own `refimages` and `psfs` rows (runs 88, 89, 96). A same-bytes
comparison reads the pixels, never `diffimages.checksum` (the file
carries its job id and timestamps). A one-epoch acceptance that asks
packet equality names the reference job's instance type: the compute
environment mixes m6a and r6i, and an r6i reference read 180 packets
against m6a's 181 at equal image (runs 140, 141). The paired
measurement is `scripts/compare_runs.py --run-a A --run-b B`: the
scorecard's null columns per shared epoch and, on an injected dataset,
detected per magnitude bin and false positives per epoch, by
`inspect_run.py`'s and `completeness.py`'s functions.

**Settings in force.** The published catalog is SFFT's, through dev's
prefilter, unless a run sets `difference.diff_flavor="zogy"`; ZOGY's
catalogs are under every difference job's `diff/` either way. Under SFFT
the difference job scores every candidate with dev's RuBR-AT model
(`[difference.rubrat]`), fetched from the bucket the SSM parameter
`model_bucket_parameter` names under `model_key` (the `[kafka]` venue,
no per-run `--set`; a run's `difference.rubrat.model_prefix` wins) and
checked against its `SHA256SUMS`; `sources.rb` and `rblabel` hold it,
the packet's three reliability fields carry it. SMDC's
`sources_official` view predates the two columns: read them from
`sources`. SFFT runs under dev's OpenUniverse `[difference.sfft]` block
(cross-convolution, segmentation, gain-match catalogs, bsmask 50 / 100)
for every dataset, the base's since sfft-block-base; dev's rule turns
cross-convolution off for a RimTimSim file. An overlay reaches a run from
the checkout at `rp run create`, which stores the resolved body; a job
reads that body, never an overlay file, so an overlay edit needs no revision.
A flight overlay sets
`[intake] gaia_proper_motion = true`; the SOC simulations leave it off.
The loop plans a field's reference from the rows before the bound whose
`overlapfields` list it; the live runs' references built before PR 450
are not re-planned. The CLI's verbs are `run`, `job`, `official`,
`publish` and `loop`; test doubles import from `tests.support`; the unit
suite runs under `env -u RAPID_PARAMETER_PATH` once a session has
exported it.

## Validation

The ten checks a night meets. Checks 1 to 6 are `scripts/validate_run.py
--run <id>` at the tip (lineage, tables, catalog, packets, forced, and
`--twice A,B`; `counts` beside them fails a run with a failed job);
`check_run.sh` runs it with the reader and the consumer. Checks 7 to 10
are operations, checked as they run. The outcomes are part 6's, on runs
9 and 10 (2026-10-21); the catalog check replaced the chain check with
part 11 step 7.

| check | outcome on runs 9 and 10 |
|---|---|
| 1 lineage closes | held on both |
| 2 Parquet and tables agree | held on both (counts for 309 jobs, bytes for ten) |
| 3 the chain (now the catalog) | held on both: depth 69 and 5, row for row, sums reproduced |
| 4 packets against their recorded inputs, and the reader | held on both: 69 and 5 sets CLEAN |
| 5 forced photometry measured once | held on both |
| 6 two executions of one key | held: one official, the other an unbound candidate |
| 7 one group makes the night official | held: 145 keys, every view moved, nothing else did |
| 8 a mid-night epoch replaced | held by conservation |
| 9 undo restores | held row for row; the undo of A refuses after B by the rule, and an explicit group restores |
| 10 two groups at once through the pooler | held: one commits, the other refuses naming the binding, nothing half-applied |

The scratch prefixes of runs 13 to 16, 18 to 45, 51, 52, 55 and 56 were
deleted on 2026-10-24: `validate_run.py` fails on them by design.
One-time cleanup, Ben 2026-10-27 06:27 UTC (socsims-1021): the prefixes
of runs 1 to 11 (8 never had one), 61, 63, 119, 120, 131 and 132 and the
pre-foundation `rapidpipe-firstrun/` were removed, 7,514 objects, 259.6
GB. Their database rows and bindings stay, and so does run 9's live
binding for `socsims-fakesrc-20260807`. The rows of runs 1, 3, 9 and 10
still appear in the views and point at removed products, until a later
rebinding or removal that nobody has ruled. `inj/` was kept because it is
run 71's source root.

## If a step fails

- Bring-up: stop. Before the drop nothing has changed. After it the
  replace kept no copy of the old schema's rows, by decision: the
  rollback is the previous schema, rebuilt from git. The database's
  standing backups are rapid_systems': one crash-consistent EBS snapshot
  set a day, 14 kept (`cloudformation/rapid-db-dlm.yaml`), and
  pgBackRest's WAL archive with a daily base backup, four fulls kept
  (`cloudformation/rapid-db-backup-schedule.yaml`,
  `rpms/rapid-db-tools/pgbackrest.conf.in`).
- The inputs bucket refuses overwrites and deletes: a file that went up
  stays, and a second upload's 412 means it is present, not wrong.
- Anything later: nothing is deleted. A failed job is read (`rp job show
  <id>`'s `refusal`; on Batch `aws batch describe-jobs`, then its
  CloudWatch log stream, where rapidpipe's INFO lines are), and rerun
  with `rp job submit --job <id> --job-definition <def>` if transient, or
  `rp job run --job <id>` here. A wrong binding is undone with `rp
  official undo <group>`. `rp job local` with `[difference.paths]` at a
  checkout reproduces a difference failure in about two minutes. A fault
  in the code is fixed by a pull request on `rebuild`, the image rebuilt
  for the new commit, and a new run naming that commit, so the run and
  job records name the code that ran.

## What remains

Carried from part 13's results (through PR 471), with the earlier parts'
items no later part closed; each names where its numbers are.

- **SFFT, the default since sfft-default**: under the OpenUniverse
  block it trails ZOGY on HLWAS only at AB 26 and 26.5 at half the false
  positives (runs 145, 146). OpenUniverse, GBTDS, flight and RimTimSim
  are sampled one epoch each under it (sfft-block-base, runs 154 to 157),
  no more; excluding the zeroed DQ cores from SFFT's fit in the stage
  (PR 435) is recorded, not built. On GBTDS and RimTimSim F213 every
  difference job fails under the block (SFFT's `Singular matrix`, exit 70;
  runs 156, 157): what those two publish is Ben's open ruling. `dev`'s
  socsims block on the socsims lite overlay differences GBTDS without the
  failure (run 158, 1,998 epochs), its core median +0.066 holding 1,997
  of 1,998 sets at the HLWAS-derived ±0.025.
- **The bright-star residual's cause**: flagged (bit 16384), not fixed;
  about 20 per epoch above S/N 20 on SCA 8 F146, two to three times that
  on SCAs 4 and 5 (parts 10, 11 step 3).
- **The false positives per epoch** under the field rule, a median 224 on
  the injected footprint under ZOGY (part 12 step 1).
- **A star whose peak pixel is masked** is still missed by the PSF-fit
  finder, about 0.6% of SCA 7's sources (PR 439).
- **The gate is inert on a sample run**: it judges a set only from
  `min_epochs` differences, so the five-epoch RimTimSim and three-epoch
  GBTDS runs sent unjudged sets (PRs 437, 438). Replayed at `min_epochs`
  it holds every run 139 epoch on the core (−0.036 to −0.048 against the
  HLWAS-derived ±0.025), so whether GBTDS gets bounds of its own is the team's
  open question (rapid_docs `decisions.md`, open questions; PRs 465, 470).
- **GBTDS gb3's packets fell 1,278 → 1,196** (run 112 on `:77`, run 139
  on `:87`) at equal kept sources, cause unread (PR 470); the reference
  catalog the metric stars come from includes galaxies (PR 465). GBTDS's
  stars are 1.31 times the ePSF's width, so ZOGY's science half is
  mismatched whatever the reference PSF (part 12 step 4). GBTDS SCA 2's
  constant `gaia_dy_px` +0.27 px is not proper motion (PRs 438, 454).
- **RimTimSim completeness** waits on a 260622 truth from the
  simulator's authors (PR 437). F213's core over-subtraction is a
  reference-against-science width mismatch (1.39 against 1.30 px):
  neither the transposed science PSF nor `reference_psf = "science"`
  clears both core and ring (PR 462); the next lever is the reference PSF
  itself or a matched-width kernel, a code knob. r00342's F213 has the
  same kind of width mismatch (part 12 step 5).
- **The OpenUniverse overlay** carries the science's own zero points and
  `sca_gain` 1.0, landed from branch `ou-zp/1-overlay` on Ben's ruling
  (2026-10-27) accepting false positives 493 against 492 inside the
  capped 500 (PRs 451, 492); run 155 read them from the overlay
  (sfft-block-base). Every OpenUniverse
  difference keeps 500 of 2,077 to 3,682 candidates, so its count below
  AB 25 measures the cap (part 12 step 3).
- **Flight program 1049**: 341 unflagged recurrent hot pixels, 472 not
  masked, no one bit fixes them; the pointing oscillation is not
  observable in four 60 s frames; run 118's field 3014319 has no
  reference (PR 445).
- **`forced` grows with the window**: a field's rows quadratically in
  epoch while its 30-day window is open, copies 0.57 of a deep set's
  rows; storing references instead of copies is the later item;
  saturation past a 30-day span is unmeasured (PR 455). Each gzipped
  window file is decompressed once per packet job (PR 476): the one-image
  null epoch's packet job 18.2 → 15.3 s; a window's other images were
  already read once, so the per-image decompression itself remains.
- **The packet's window query** reads the dataset's difference images in
  the MJD window through an unindexed haversine; a q3c or HEALPix
  predicate on `diffimages (ra0, dec0)` at thousands a month (part 12).
- **Cross-band association**: one dataset per filter avoids it; whether
  an object is per band or across bands, and forced photometry in the
  other bands, is undecided (part 9). So is whether a 107.5 s epoch gets a
  reference of its own exposure time (part 9).
- **The injected arms' 415 false positives per epoch** (runs 91, 92)
  against run 88's 385 under SFFT: the shift is in those runs' own
  reference products (`refimages` 22868, 22867), which part unread, PR
  406's reference catalog the candidate.
- **OpenUniverse 28737's ZOGY reproduction shares 323 of 500 ids**, Intel
  against Intel, cause unread (differencer-measure).
- **`diffimages.checksum` cannot agree across two jobs**; ZOGY's negative
  and SFFT's positive PSF-fit catalogs are 71% of a RimTimSim F213
  difference; the `[psfcat_diffimage] output_naive_psfcat_*` keys are
  read by nothing (PR 446).
- **Every `import` manifest's provenance** is the constant "imported from
  IMSS socsimsdb 20260821 run", even for archive Gaia boxes (PR 449).
- **`sources_official` on SMDC** lacks the three pixel columns: the
  view's `t.*` was expanded at creation, and re-creating it needs a `DROP
  VIEW` grant (dev-columns).
- **The publisher binds an expected key no loaded row holds** (caught
  afterwards by `validate_run.py`'s lineage check), and no setting turns
  the packet job's Kafka send off (mig-quickstart).
- **`gate.decide` reads the first `sources` input with no `ORDER BY`**;
  insertion order picks the set's own epoch in all 532 multi-source sets
  (PRs 426, 431).
- **`job wait` ends 0 for a job published with a refusal**, where the
  loop counts it failed (PRs 409, 420). **`ToolRunner` has no timeout**
  (PR 407). **A local job dying before `job run` records `running`**
  stays `launched` and a loop counts it pending (part 12 step 7). **Runs
  72's eight and 73's one official sets are unstamped**, cause unread
  (part 12 step 7).
- **The error-handling audit's cut candidates** (the rows of PRs 458's
  and 459's tables), each a mechanism for a
  failure not seen, listed and not built: rows 47, 49, 68, 69, 70, 130,
  132, 133 (PR 458) and 26, 66, 67 (PR 459); the reference's
  MAGZP-missing check and import's PSFKIND mismatch still exit 70, not 65
  (PR 459).
- **Code with no caller outside tests** remains: `db/ids.py` (`new_ulid`)
  and `products/ids.py`, the latter not tested (PR 467).
- **A one-epoch packet-equality acceptance depends on placement**:
  nothing pins the instance type (PRs 469, 471).
