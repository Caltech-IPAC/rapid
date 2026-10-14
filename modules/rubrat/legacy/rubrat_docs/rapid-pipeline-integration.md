# RAPID pipeline integration

RuBR-AT can run as a real/bogus stage directly on one RAPID science-pipeline
job directory. It does not build an NPZ at inference time. The adapter reads
the canonical images and catalogs, constructs the exact tensors used in the
paper experiments, scores detections in catalog order, and emits a scored
Parquet catalog plus a checksummed provenance sidecar.

The integration described here follows the current
[RAPID pipeline design](https://caltech-ipac-rapid.readthedocs.io/en/latest/pl/pl.html),
[execution model](https://caltech-ipac-rapid.readthedocs.io/en/latest/ops/bulk_run.html),
and [product names](https://caltech-ipac-rapid.readthedocs.io/en/latest/prod/products.html).
Those upstream documents explicitly describe RAPID as evolving. Pin and test a
RAPID commit when deploying RuBR-AT; do not treat `latest` as a reproducible
software version.

## Where the classifier runs

One RAPID job differences one science image. RuBR-AT belongs after positive
SFFT PSF photometry has produced the PSF-fit and DAOStarFinder catalogs, and
before candidates are filtered, loaded into an operational source table, or
packaged as alerts:

```text
background-subtracted science + gain-matched reference
                        |
                        v
                 SFFT subtraction
                        |
                        v
           PSFPhotometry + DAOStarFinder
                        |
                        v
                   RuBR-AT score
                        |
                        v
       database / candidate policy / alert packaging
```

In the current public RAPID source, the science pipeline joins the photometry
and finder tables by `id` and writes
`sfftdiffimage_masked_psfcat.parquet`. That joined Parquet file is the simplest
catalog input. The adapter also accepts the two text catalogs and performs the
same strict one-to-one `id` join.

## Required products

The default resolver consumes these files below a `jid*` directory:

| Role | RAPID product | RuBR-AT use |
|---|---|---|
| science | `bkg_subbed_science_image.fits` | channel 0 |
| reference | `awaicgen_output_mosaic_image_resampled_gainmatched.fits` | channel 1 |
| difference | `sfftdiffimage_dconv_masked.fits`, then `sfftdiffimage_masked.fits` | channel 2 |
| PSF table | `sfftdiffimage_masked_psfcat.parquet` or `.txt` | position and fit features |
| finder table | `sfftdiffimage_masked_psfcat_finder.parquet` or `.txt` | morphology features when not already joined |

The difference-image preference is intentional. RAPID produces the
decorrelated `dconv` image when SFFT cross-convolution is enabled; otherwise it
produces `sfftdiffimage_masked.fits`. The selected filename is recorded in the
provenance JSON. In production, pass `--difference` explicitly so a pipeline
configuration change cannot silently change the model input domain.

The catalog must provide the PSF-fit columns `x_fit`, `y_fit`, `flux_fit`,
`flux_err`, `cfit`, `reduced_chi2`, `x_err`, `y_err`, `n_pixels_fit`, and
`flags`, plus finder columns `sharpness`, `roundness1`, and `roundness2`.
RAPID aliases are normalized without changing row order. PSF-fit positions,
not finder centroids, define the cutout center when both are present.

## Install in a RAPID container

Build an immutable wheel from a tagged RuBR-AT commit and add it, the selected
checkpoint, scaler, and threshold report to a derived RAPID image:

```bash
python -m pip wheel --no-deps --wheel-dir dist .
```

Example Dockerfile fragment:

```dockerfile
FROM public.ecr.aws/y9b1s7h8/rapid_science_pipeline@sha256:<pinned-digest>
COPY dist/rubrat-0.1.0-py3-none-any.whl /opt/rubrat/
RUN python3.11 -m pip install --no-deps /opt/rubrat/rubrat-0.1.0-py3-none-any.whl
COPY artifacts/rb_best.keras artifacts/feats_scaler.json artifacts/metrics.json /opt/rubrat/artifacts/
```

Use the fully resolved RAPID base-image digest and verify that its TensorFlow,
Keras, NumPy, pandas, and PyArrow versions match the environment recorded by
the paper run. If the RAPID image does not contain the pinned RuBR-AT runtime,
install from this repository's environment contract instead of allowing `pip`
to select current packages.

Large model artifacts are intentionally not tracked by Git. The emitted
sidecar records SHA-256 digests of the checkpoint, scaler, and scored catalog.

## Command-line hook

The threshold must come from validation data. `rubrat evaluate-research`
writes it to `metrics.json`, which can be passed directly:

```bash
rubrat rapid score \
  --job-dir /work/jid90828 \
  --checkpoint /opt/rubrat/artifacts/rb_best.keras \
  --feats-scaler /opt/rubrat/artifacts/feats_scaler.json \
  --threshold-json /opt/rubrat/artifacts/metrics.json \
  --survey-id 0 \
  --difference sfftdiffimage_dconv_masked.fits \
  --output /work/jid90828/sfftdiffimage_masked_psfcat_rubrat.parquet
```

Use `--survey-id 0` for the HLTDS context token and `--survey-id 1` for the
GBTDS context token. This is a learned model input, not descriptive metadata,
so the command requires it explicitly. A flight deployment must choose and
validate a token during domain-transfer/calibration work; RuBR-AT does not
claim that either simulation token is automatically calibrated for on-sky
Roman data.

For a non-cross-convolved GBTDS-style RAPID run, use:

```bash
rubrat rapid score \
  --job-dir /work/jid91950 \
  --checkpoint /opt/rubrat/artifacts/rb_best.keras \
  --feats-scaler /opt/rubrat/artifacts/feats_scaler.json \
  --threshold-json /opt/rubrat/artifacts/metrics.json \
  --survey-id 1 \
  --difference sfftdiffimage_masked.fits
```

The command exits nonzero for missing products, incomplete feature schemas,
catalog join failures, unknown filters, invalid thresholds, incompatible
checkpoints, or existing output files. Add `--overwrite` only when RAPID's job
retry semantics require replacement.

## In-process hook

Loading TensorFlow once and reusing the classifier is preferable when one
worker scores multiple jobs:

```python
from rubrat.rapid import (
    RAPIDRealBogusClassifier,
    load_validation_threshold,
    write_scored_catalog,
)

classifier = RAPIDRealBogusClassifier(
    "/opt/rubrat/artifacts/rb_best.keras",
    "/opt/rubrat/artifacts/feats_scaler.json",
    threshold=load_validation_threshold("/opt/rubrat/artifacts/metrics.json"),
    survey_id=0,
    batch_size=128,
)

rows, provenance = classifier.score_job(
    "/work/jid90828",
    difference="sfftdiffimage_dconv_masked.fits",
)
write_scored_catalog(
    rows,
    provenance,
    "/work/jid90828/sfftdiffimage_masked_psfcat_rubrat.parquet",
)
```

For the least invasive RAPID patch, invoke this block immediately after the
positive SFFT joined Parquet catalog is written. Add the scored Parquet and its
`.provenance.json` sidecar to the science job's S3 upload list. A separate
post-processing job is also valid, but it must download all three images and
the joined catalog and must finish before database/alert consumers that need
the score.

## Output contract

Every input catalog row is retained. The original columns are followed by:

| Column | Meaning |
|---|---|
| `rb_score` | model real score; `NaN` if the row cannot be scored |
| `rb_label` | `1` real, `0` bogus, `-1` invalid, using the recorded threshold |
| `rb_valid` | whether inference succeeded for the row |
| `rb_status` | empty on success; explicit invalid reason otherwise |
| `rb_threshold` | validation-selected operating threshold |
| `rb_model_filter`, `rb_model_filter_id` | canonical filter token |
| `rb_model_survey_id` | selected simulation-domain token |

Rows are never silently discarded. A cutout containing a non-finite pixel is
marked invalid because training excluded such stacks. The adjacent provenance
JSON records inputs, preprocessing, feature order, artifact hashes, threshold,
and scored/invalid counts.

RuBR-AT produces a score product; RAPID still needs an explicit operational
policy for using it. To persist scores in RAPID's source tables or alerts, add
`rb_score`, `rb_valid`, model version/hash, and threshold fields to the relevant
schema and loader. Do not replace source-catalog identity with the thresholded
label, and do not drop invalid rows without a separately documented policy.

## Deployment acceptance test

Before enabling candidate filtering:

1. Pin a RAPID commit/base-image digest and a RuBR-AT commit.
2. Pin one checkpoint, training-only feature scaler, and validation threshold;
   retain their hashes.
3. Run the CLI on held-out complete JIDs from every supported filter and SFFT
   mode.
4. Compare output scores bit-for-bit or within an agreed numerical tolerance
   with `rubrat evaluate-research` predictions for the same rows.
5. Assert input/output row and `id` alignment, zero unexplained invalid rows,
   and deterministic retry behavior.
6. Measure CPU/GPU memory, cold-start time, and per-detection latency in the
   actual RAPID container.
7. Shadow-score without filtering until simulation-to-flight calibration and
   alert-level false-positive policy are approved.

The model card's limitations continue to apply: the current checkpoint is a
research classifier trained on simulated HLTDS/GBTDS products, not yet a
calibrated flight alert policy.
