# Data preparation

If you **already have NPZ shards**, skip product building and follow the
README section **Reproduce from NPZ** (`rubrat dataset scaler`,
`rubrat train supervised`, `rubrat evaluate-research` / `rubrat infer`).

## Required inputs (building from RAPID)

Each survey root must directly contain `jid*` directories.

HLTDS JIDs require science, reference, SFFT difference, PSF-fit detection
catalog, and at least one truth source (`Roman_TDS_index_*.txt` or an injection
sidecar). GBTDS JIDs require science, reference, SFFT difference, finder
catalog, and the external RTS catalog supplied in `config/local.yaml`.

Run `rubrat validate products` before labeling. Missing required files are a
hard failure; rows are never silently synthesized.

### Build commands (from RAPID products)

```bash
# HLTDS
rubrat labels build \
  --data-dir /path/to/hltds-products \
  --output-dir artifacts/labels/my_hltds \
  --truth-obj-types transient \
  --catalog-stems sfft_psfcat \
  --match-radius-px 4.0

rubrat dataset hltds \
  --data-dir /path/to/hltds-products \
  --labels-dir artifacts/labels/my_hltds \
  --output-dir artifacts/datasets/my_hltds \
  --image-size 64 \
  --rb-real-kind transient \
  --rb-real-bogus-ratio 3:7 \
  --seed 42

# GBTDS
rubrat dataset gbtds \
  --data-dir /path/to/gbtds-products \
  --catalog /path/to/catalog_F213.txt \
  --output-dir artifacts/datasets/my_gbtds \
  --image-size 64 \
  --task rb \
  --rb-real-kind transient \
  --rb-real-bogus-ratio 3:7 \
  --truth-match-radius-px 2.0 \
  --seed 42
```

See `rubrat dataset hltds --help` and `rubrat dataset gbtds --help` for
policies and split options. Then train/eval with the README NPZ recipes.

## Truth matching and magnitudes

HLTDS detection catalogs are matched one-to-one to simulation truth within the
configured pixel radius. Label generation does not apply a magnitude cutoff.
The per-exposure zeropoint is read from the RAPID science FITS header
(`ZPTMAG`, with `MAGZP`/`ZPT` aliases accepted); a missing zeropoint is a hard
error rather than silently assuming zero.
For every match it records:

- `truth_mag_instrumental`
- `truth_zpt`
- `truth_mag_ab = truth_mag_instrumental + truth_zpt`

An already calibrated injection `mag_ab` is also retained. Evaluation fails a
row closed—excluding it from the magnitude-limited real denominator—when no
finite corrected magnitude is available, and reports that count explicitly.

## NPZ schema

Every shard contains aligned arrays:

- `X`: `(N, 64, 64, 3)` float32, ordered science/reference/SFFT.
- `feats`: `(N, 9)` float32 raw catalog features.
- `y`: binary supervised label.
- `survey_id`: 0 for HLTDS, 1 for GBTDS.
- `filter_id`: filter registry index.
- `metadata`: row provenance including JID, centroid, row ID, truth fields,
  and source catalog information.
- `pu_label` and `pu_status` for RB datasets used by PU experiments.

Builders write split reports and exact shard lists. `rubrat validate dataset`
checks array alignment, shapes, finite model inputs, label domains, unique row
IDs, and optional SHA-256 digests.

## Legacy RuBR representation

RuBR-AT and legacy RuBR do not share image preprocessing. For a fair
comparison, `rubrat dataset manifest` records the selected CNN row identities
(including train), `rubrat dataset rubr` re-extracts raw reference/science/SFFT
cutouts and the six legacy features from the RAPID products, and
`rubrat dataset alignment` checks row order, labels, centroids, and counts.
`rubrat train rubr` fits RotInv RuBR on those aligned train/val NPZs; the
comparison command refuses a validation-selected run without aligned RuBR
validation and test datasets.

## Leakage prevention

HLTDS uses seeded, per-filter JID splitting. GBTDS preserves the spatial-corner
test design and seeded JID train/validation assignment. Scalers consume only
`train_*.npz`. Validation-selected thresholds never inspect test labels.

These are detection/exposure partitions, not fully source-disjoint partitions.
HLTDS has 1,114 positive source identities shared between development and test;
265 of its 1,968 test Real detections come from 244 identities absent from both
training and validation. GBTDS has no transient identity shared between its
spatial test corner and development, although 417 identities recur between its
train and validation exposures. Consequently, the primary HLTDS result measures
generalization to new exposures and detections of a partly familiar simulated
source population. A source-unseen HLTDS sensitivity cohort retains every test
Bogus row and only the 265 Real rows from development-unseen identities. Exact
counts are in `outputs/review_revision/dataset_diagnostics/source_identity_overlap.json`.

The GBTDS test region is the bottom-right spatial corner of every 4089 by 4089
pixel image, assigned from each detection centroid. The remaining area is
assigned to training or validation by JID. Consequently, a GBTDS JID may occur
in the test set and in one development set, but the detections occupy disjoint
image regions. This detail must be stated when JID is used as the exposure
cluster in uncertainty calculations.

Reference-product recurrence was checked from the FITS `DATASUM` of every
gain-matched, resampled reference used by a retained JID. All 1,000 HLTDS files
and all 121 GBTDS files were available. HLTDS has two reference image arrays
with matching checksums between training and validation JIDs, but none shared with the
test JIDs. In GBTDS, the spatial test region and the corresponding development
region come from the same exposures, so every test reference image array also occurs in
training or validation. This is an observed property of the established GBTDS
partition, not an independent-reference generalization test. The exact audit is
`outputs/review_revision/dataset_diagnostics/reference_recurrence.json`.

## Matching policy and diagnostics

HLTDS truth positions are matched to the nearest finite PSF-catalog detection
within 4 pixels. Candidate truth matches are processed from shortest to longest
distance, and a detection can be assigned only once. GBTDS uses a 2-pixel truth
radius. At the WFI pixel scale of 0.11 arcsec per pixel, these radii correspond
to 0.44 and 0.22 arcsec.

The archived HLTDS labels contain 65,051 matches from 1,000 exposure catalogs.
The median match distance is 1.433 pixels and the 95th percentile is 2.513
pixels. A four-direction, 37-pixel random-offset check estimates a 0.00357
chance association rate at the 4-pixel radius. Among truth positions with at
least one candidate detection inside the radius, 0.0462 have more than one.
The one-to-one assignment rejects 60 candidate matches because a closer truth
position has already claimed the same detection.

For GBTDS, the 121 test-region exposure catalogs contain 2,729,330 assignments
to the full, crowded RImTimSim source catalog within 2 pixels. The median
separation is 0.827 pixels and the 95th percentile is 1.655 pixels. Among truth
positions with a nearby detection, 0.0400 have more than one candidate
detection. The corresponding four-direction random-offset association rate is
0.0165. Most truth associations are static catalog sources and are not Real
examples; the Real sample is restricted to the injected transient identifiers.

The 4-pixel HLTDS radius is twice the 2-pixel FWHM supplied to DAOStarFinder,
while the 2-pixel GBTDS radius equals that configured finder FWHM. These are
catalog-matching settings rather than measurements of the wavelength-dependent
Roman PSF. The empirical separation distributions provide the relevant check:
the adopted radii lie beyond the 95th percentiles in both surveys while the
random-offset association rates remain below two percent.

## Real and bogus sampling

Real means a selected truth-matched transient or local injection. Bogus means
an unmatched source-finder detection selected from the candidate catalog. The
3:7 Real/Bogus ratio is a training and evaluation sampling policy, not an
estimate of alert-stream prevalence and not a demonstrated optimum. Sampling
is seeded and performed within the existing partition. HLTDS balances the
6,587 retained OpenUniverse2024 transients with 6,587 local injections before
selecting bogus rows. The unmatched HLTDS candidate pool contains 1,316,294
valid detections. GBTDS unmatched candidate pools contain 1,783,147 training,
313,912 validation, and 696,182 test detections before sampling.

Cutout extraction and finite-value checks are applied after candidate
selection. They retain all selected HLTDS rows. For GBTDS, they discard 13
training, 2 validation, and 9 test bogus cutouts; no selected real cutouts are
discarded. The full counts are in
`outputs/review_revision/dataset_diagnostics/sampling_and_cutout_qc.csv`.
Every discarded row is GBTDS Bogus and therefore F213; no selected Real row is
discarded. The builder did not preserve the detector coordinates of rows that
failed before serialization, so a more detailed spatial distribution of these
24 discarded cutouts cannot be reconstructed from the canonical artifacts.

## Real-source provenance and injection construction

The HLTDS selection first retains all 6,587 detected OpenUniverse2024 transient
rows and then samples 6,587 detected local-injection rows without replacement.
Thus "equal-sized" refers to the global retained-detection selection before the
existing exposure split, not to a forced equality inside every partition.
Training, validation, and test contain 4,601/4,580, 1,019/1,006, and 967/1,001
OpenUniverse/local-injection rows, respectively.

The local HLTDS sources are point sources drawn from field-based injection
catalogs. An audit of all 1,000 archived RAPID logs finds the same command limits
in every exposure: source-catalog magnitudes from 21 to 28, use of field
catalogs, and GalSim PSF rendering. The source catalog contains Gaussian and
sinusoidal light-curve models. Among the distinct sources represented by the
retained detections, the training partition contains 1,478 Gaussian and 847
sinusoidal injections; validation contains 546 and 269; test contains 529 and
265. The positions are projected from sky coordinates and retain noninteger
pixel phases. The pipeline obtains the reference first, injects sources into the
science image, then performs science-image background processing and SFFT
subtraction. The reference therefore contains no matching local injection. This
ordering is present in every audited log and is recorded in
`outputs/review_revision/dataset_diagnostics/hltds_injection_pipeline.json`.

GBTDS uses the RImTimSim source catalog, which contains approximately five
million crowded-field sources and 1,000 injected transient identities. The
canonical Real label is restricted to those transient IDs; variables and static
catalog sources are not Real examples in this experiment. All retained GBTDS
examples use the simulator label F213. The HLTDS products use K213 for the same
nominal long-wavelength band, so the two names are retained as provenance-aware
metadata aliases rather than merged silently. The archived GBTDS products do
not include a separate per-source injection recipe from which a broader
astrophysical transient prior could be reconstructed, and the 1,000 simulated
transients should not be treated as a representative flight population.

`provenance_composition.csv` and `provenance_distributions.csv` quantify the
retained magnitude, fitted SNR, subpixel phase, detector radius, fit quality,
filter, and outer-cutout background/structure proxies by partition and
provenance. They make an important construction difference explicit: local
HLTDS injections occupy a much brighter, higher-SNR regime than the retained
OpenUniverse transients. Aggregate performance can therefore mix Real/Bogus
separation with provenance-dependent observing conditions. The paper reports
provenance-specific metrics and a train-on-one-provenance/test-on-the-other
diagnostic. The canonical metadata do not retain a physical HLTDS host identity
or a direct crowding statistic, so host and crowding distributions cannot be
reported without rebuilding the simulation products.

## Candidate generation and residual sign

The archived RAPID logs for both survey products record a 5-sigma
DAOStarFinder threshold after 3-sigma clipping, a 2-pixel FWHM, sharpness range
0.2 to 1.0, roundness range -1 to 1, and zero minimum separation. PSF
photometry uses a 17 by 17 pixel fit window and an 8-pixel aperture radius.
The HLTDS products use the decorrelated SFFT difference image and its
uncertainty/PSF products; the current GBTDS products use the corresponding
SFFT masked difference image. Science, gain-matched resampled reference, and
SFFT difference products form the three model channels.

RAPID generated catalogs for positive and sign-inverted difference images.
The canonical datasets analyzed here select detections from the positive SFFT
PSF-fit catalog only. Negative residual catalogs are not merged into this
cohort, so the reported performance does not measure negative-residual
recovery. This is an observed dataset difference and a limitation, not a model
property.

## Canonical feature definitions

The nine features, in serialized order, are:

1. `arcsinh_snr`: `asinh(flux_fit / (3 flux_err))`. Dividing the fitted SNR by
   three sets the transition between the near-linear and logarithmic regimes
   near an SNR of three; the name is retained for checkpoint compatibility.
2. `cfit`: the dimensionless convergence statistic supplied by the RAPID PSF
   fitter.
3. `is_fit_clean`: one when the PSF fitter reports `flags == 0`, otherwise zero.
4. `log1p_chi2`: `log(1 + reduced_chi2)` from the PSF fit.
5. `log1p_pos_err`: `log(1 + sqrt(x_err^2 + y_err^2))`, with positional errors
   in pixels.
6. `log_npixfit`: the natural logarithm of the number of pixels used by the fit,
   clipped below at one.
7. `roundness1`: the DAOStarFinder symmetry statistic from marginal sums.
8. `roundness2`: the DAOStarFinder symmetry statistic from fitted axis widths.
9. `sharpness`: the DAOStarFinder central-pixel contrast statistic.

Nonfinite catalog values are replaced by the documented neutral defaults. If
the PSF-fit flags are nonzero, the four fit-dependent values (transformed flux
ratio, `cfit`, transformed chi-square, and transformed positional error) are
set to zero before scaling. After the training-only z-score transform, these
values map to the training mean while `is_fit_clean` preserves the missing-fit
signal. Finder morphology values remain available.

## Diagnostic artifacts

`scripts/build_review_dataset_diagnostics.py` regenerates the dataset report.
Its output directory contains composition by split and provenance, class and
filter counts, exposure composition, feature and magnitude quantiles, sampling
and cutout-QC counts, match distances, and the match-distance figure. These
files, rather than hand-transcribed values, are the source for manuscript
tables and figures.
