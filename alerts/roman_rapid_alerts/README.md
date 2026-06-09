# RAPID Alert Generation for Roman Space Telescope

Generate simulated transient alert packets for the Nancy Grace Roman Space
Telescope using the **RAPID** (Roman Alerts Promptly from Image Differencing)
pipeline.

Alerts are serialized in [Apache Avro](https://avro.apache.org/) format using
the `rapid.v01_00` schema, which follows
[Rubin/LSST](https://dmtn-093.lsst.io/) naming conventions (`diaSource`,
`diaObject`, `prvDiaSources`).

The input data comes from the **Open Universe 2024** (OU2024) Roman Time Domain
Survey simulation, processed through a ZOGY difference-imaging pipeline.

## Repository Contents

```
roman_rapid_alerts/
├── generate_alerts.py           # Core alert generation script
├── generate_inject_sidecar.py   # Injection catalog cross-match → sidecar
├── read_sample.py               # Read and display alert packets
├── download_data.sh             # Download pipeline products from S3
├── requirements.txt             # Python dependencies
├── schema/01/00/                # Avro schema files (rapid.v01_00)
│   ├── rapid.v01_00.alert.avsc
│   ├── rapid.v01_00.diaSource.avsc
│   ├── rapid.v01_00.diaObject.avsc
│   ├── rapid.v01_00.diaForcedSource.avsc
│   ├── rapid.v01_00.ssSource.avsc
│   └── rapid.v01_00.mpc_orbits.avsc
├── notebooks/
│   └── background.ipynb         # Pipeline architecture & calibration docs
└── sample/
    └── sample_alerts.avro       # 10 example alerts for quick inspection
```

## Quick Start

```bash
# 1. Clone the repository
git clone <repo-url>
cd roman_rapid_alerts

# 2. Install dependencies
pip install -r requirements.txt

# 3. Inspect the sample alerts
python read_sample.py
python read_sample.py --stamps    # also display cutout images

# 4. Download pipeline data from S3 (~3 GB with FITS, ~200 MB without)
bash download_data.sh             # full download (FITS images for stamps)
bash download_data.sh --no-fits   # catalogs only (no cutout stamps)

# 5. Generate alerts
python generate_alerts.py
```

## Prerequisites

- Python 3.10+
- AWS CLI (`pip install awscli`) for downloading data from S3 (public bucket,
  no credentials needed)

## Data Retrieval

All pipeline products are stored in the public S3 bucket
`s3://rapid-product-files` (us-west-2, no authentication required).

Run `download_data.sh` to fetch the 15 jobs from the 20260227 pipeline run.
Each job directory contains:

| File | Description | Needed for |
|------|-------------|------------|
| `diffimage_masked.txt` | SExtractor detection catalog | Alert generation |
| `diffimage_masked_psfcat.parquet` | PSF-fit quality metrics | Alert generation |
| `diffimage_masked.fits` | ZOGY difference image | Alert generation (cutouts + header) |
| `bkg_subbed_science_image.fits` | Science image | Alert generation (cutouts) |
| `awaicgen_output_mosaic_image_resampled_gainmatched.fits` | Gain-matched template | Alert generation (cutouts) |
| `Roman_TDS_index_*.txt` | OU2024 truth catalog | Only with `--include-truth` |

Light curve HEALPix tiles (HATS format) are downloaded on-the-fly during
alert generation and cached in `data/lc_tiles/`.

HTTPS fallback (no AWS CLI):
```
https://rapid-product-files.s3.us-west-2.amazonaws.com/20260227/jid1061/diffimage_masked.txt
```

## Running the Generator

```bash
# Generate all 15 jobs (default paths)
python generate_alerts.py

# Custom paths
python generate_alerts.py \
    --data-dir /path/to/data/20260227 \
    --output-dir ./my_output \
    --schema-dir ./schema/01/00

# Process specific jobs only
python generate_alerts.py --jobs jid1061 jid1231

# Also generate truth sidecar (requires Roman_TDS_index_*.txt in job dirs)
python generate_alerts.py --include-truth

# Generate inject sidecar (after alerts are generated)
python generate_inject_sidecar.py
```

### CLI Options (generate_alerts.py)

| Option | Default | Description |
|--------|---------|-------------|
| `--data-dir` | `./data/20260227` | Directory with `jid*/` job folders |
| `--schema-dir` | `./schema/01/00` | Directory with `.avsc` schema files |
| `--output-dir` | `./output` | Output for `.avro` alert files |
| `--sidecar-dir` | `./truth_sidecar` | Output for `truth_labels.parquet` |
| `--lc-tile-dir` | `./data/lc_tiles` | Cache for light curve tiles |
| `--provenance-dir` | `./provenance` | Saved FITS headers (JSON) |
| `--jobs` | all `jid*` dirs | Specific job IDs to process |
| `--include-truth` | off | Enable truth catalog matching and sidecar generation |

## Schema Overview (rapid.v01_00)

Each alert packet contains:

| Record | Fields | Description |
|--------|--------|-------------|
| `diaSource` | 63 | Detection: position, PSF flux (nJy), shape, flags, HEALPix |
| `diaObject` | 49 | Persistent object: per-filter flux statistics |
| `prvDiaSources` | list | Prior detections (full diaSource records, MJD < current) |
| Cutouts | 3 | 129x129 px stamps: difference, science, template (raw FITS bytes) |

## Photometric Calibration

Images are in **DN/s** (counts per second). Three components define the
calibration:

| Component | Description | Range |
|-----------|-------------|-------|
| BANDZPT | Filter bandpass sensitivity | 14.6 -- 15.3 |
| ZPTMAG | Collecting area + exposure (from OU headers) | 16.5 -- 18.8 |
| EXPTIME | Exposure duration (seconds) | 102 -- 901 |

### Conversion Formulas

```python
# Effective zero-point for DN/s images
ZP_eff = BANDZPT + ZPTMAG - 2.5 * log10(EXPTIME)

# Raw image flux (DN/s) → AB magnitude
mag_AB = -2.5 * log10(flux_dns) + ZP_eff

# Alert psfFlux (already in nJy) → AB magnitude
mag_AB = -2.5 * log10(psfFlux_nJy) + 31.4

# Truth catalog 'mag' column → AB magnitude (BANDZPT already applied)
mag_AB = truth_mag + ZPTMAG

# Inject catalog flux (DN) → AB magnitude
mag_AB = -2.5 * log10(flux_DN) + BANDZPT + ZPTMAG
```

### Per-Filter Constants

| Band | BANDZPT | ZPTMAG | EXPTIME (s) | ZP_eff |
|------|---------|--------|-------------|--------|
| F062 | 15.297 | 16.954 | 161.0 | 26.733 |
| F087 | 14.964 | 16.455 | 101.7 | 26.400 |
| F106 | 15.024 | 17.638 | 302.275 | 26.461 |
| F129 | 15.040 | 17.638 | 302.275 | 26.477 |
| F158 | 15.074 | 17.638 | 302.275 | 26.511 |
| F184 | 14.622 | 18.824 | 901.175 | 26.057 |
| F213 | 14.579 | 18.824 | 901.175 | 26.014 |

## Output

After running `generate_alerts.py`:

```
output/
├── jid1061_alerts.avro    # ~2,500 alerts per job
├── jid1231_alerts.avro
├── ...                    # 15 files total
truth_sidecar/
└── truth_labels.parquet   # 38,241 rows: diaSourceId → obj_type, truth_mag
```

### Dataset Statistics (20260227 run)

| Property | Value |
|----------|-------|
| Jobs | 15 (5 F184 + 10 F158) |
| Total alerts | 38,241 |
| Real (truth-matched) | 13,884 (36.3%) |
| Bogus (artifacts) | 24,357 (63.7%) |
| With light curves | ~57% |
| With cutout stamps | ~93.4% |
| Stamp size | 129 x 129 px (0.11"/px) |
| MJD range | 62022 -- 62726 (~2 yr) |
| Magnitude range | ~17 -- 31 AB |

## Background Notebook

See `notebooks/background.ipynb` for a detailed walkthrough of:
- Pipeline architecture (ZOGY differencing)
- FITS header metadata
- SExtractor catalog format
- PSF-fit quality metrics
- Truth catalog cross-matching
- Cutout stamp extraction
- Light curve tile structure (HATS/HEALPix)
- Avro serialization
