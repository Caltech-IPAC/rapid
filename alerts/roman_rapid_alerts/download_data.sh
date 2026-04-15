#!/usr/bin/env bash
#
# Download RAPID pipeline products from S3 for alert generation.
#
# Prerequisites: AWS CLI (pip install awscli). No credentials needed
# (public bucket, --no-sign-request).
#
# Usage:
#   bash download_data.sh              # download everything
#   bash download_data.sh --no-fits    # skip large FITS images (catalogs only)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="${SCRIPT_DIR}/data/20260227"
LC_DIR="${SCRIPT_DIR}/data/lc_tiles"
PROV_DIR="${SCRIPT_DIR}/provenance"

S3_BUCKET="s3://rapid-product-files"
S3_OPTS="--no-sign-request --quiet"

JOBS=(
    jid1061 jid1231 jid1319 jid1456 jid14608
    jid1461 jid14613 jid14746 jid14828 jid14831
    jid86420 jid86935 jid87100 jid87210 jid87215
)

SKIP_FITS=false
if [[ "${1:-}" == "--no-fits" ]]; then
    SKIP_FITS=true
    echo "Skipping FITS image downloads (catalogs only)."
fi

# -------------------------------------------------------------------------
# 1. Pipeline products per job
# -------------------------------------------------------------------------
echo "Downloading pipeline products for ${#JOBS[@]} jobs..."

for jid in "${JOBS[@]}"; do
    mkdir -p "${DATA_DIR}/${jid}"

    # Always download: SExtractor catalog, PSF-fit catalog, truth catalog, config
    for f in diffimage_masked.txt diffimage_masked_psfcat.parquet; do
        if [[ ! -f "${DATA_DIR}/${jid}/${f}" ]]; then
            echo "  ${jid}/${f}"
            aws s3 cp "${S3_BUCKET}/20260227/${jid}/${f}" \
                "${DATA_DIR}/${jid}/${f}" ${S3_OPTS} || echo "  WARN: ${jid}/${f} not found"
        fi
    done

    # Truth catalog (wildcard -- filename includes filter/pointing/SCA)
    if ! ls "${DATA_DIR}/${jid}"/Roman_TDS_index_*.txt &>/dev/null; then
        echo "  ${jid}/Roman_TDS_index_*.txt"
        aws s3 cp "${S3_BUCKET}/20260227/${jid}/" "${DATA_DIR}/${jid}/" \
            --exclude '*' --include 'Roman_TDS_index_*.txt' \
            --recursive ${S3_OPTS} || echo "  WARN: truth catalog not found for ${jid}"
    fi

    # Config file
    if ! ls "${DATA_DIR}/${jid}"/product_config_*.ini &>/dev/null; then
        echo "  ${jid}/product_config_*.ini"
        aws s3 cp "${S3_BUCKET}/20260227/${jid}/" "${DATA_DIR}/${jid}/" \
            --exclude '*' --include 'product_config_*.ini' \
            --recursive ${S3_OPTS} || echo "  WARN: config not found for ${jid}"
    fi

    # FITS images (large, ~200 MB each; needed for cutout stamps)
    if [[ "${SKIP_FITS}" == false ]]; then
        for f in diffimage_masked.fits bkg_subbed_science_image.fits \
                 awaicgen_output_mosaic_image_resampled_gainmatched.fits; do
            if [[ ! -f "${DATA_DIR}/${jid}/${f}" ]]; then
                echo "  ${jid}/${f}"
                aws s3 cp "${S3_BUCKET}/20260227/${jid}/${f}" \
                    "${DATA_DIR}/${jid}/${f}" ${S3_OPTS} &
            fi
        done
    fi
done
wait
echo "Pipeline products done."

# -------------------------------------------------------------------------
# 2. Provenance: FITS headers (JSON), inject catalogs
# -------------------------------------------------------------------------
echo ""
echo "Downloading provenance data..."

mkdir -p "${PROV_DIR}/headers" "${PROV_DIR}/inject_catalogs" "${PROV_DIR}/truth_catalogs"

# If provenance files are pre-packaged on S3:
aws s3 cp "${S3_BUCKET}/alerts_v100/provenance/" "${PROV_DIR}/" \
    --recursive ${S3_OPTS} 2>/dev/null || echo "  Provenance not pre-packaged on S3; headers will be extracted from FITS at generation time."

echo "Provenance done."

# -------------------------------------------------------------------------
# 3. Light curve HATS tiles (downloaded on-the-fly by generate_alerts.py,
#    but can be pre-fetched here)
# -------------------------------------------------------------------------
echo ""
echo "Light curve tiles will be downloaded on-the-fly by generate_alerts.py"
echo "into ${LC_DIR}/ as needed (HEALPix Norder 4/5/6 tiles, ~50-65 MB each)."
echo ""
echo "To pre-fetch all tiles, run:"
echo "  aws s3 cp ${S3_BUCKET}/lightcurve_hats_catalog/dataset/ ${LC_DIR}/ --recursive ${S3_OPTS}"
echo ""

# -------------------------------------------------------------------------
# Summary
# -------------------------------------------------------------------------
echo "=== Download complete ==="
echo "Data directory: ${DATA_DIR}"
echo "Jobs downloaded: ${#JOBS[@]}"
if [[ "${SKIP_FITS}" == true ]]; then
    echo "Note: FITS images were skipped. Alerts will be generated without cutout stamps."
    echo "Re-run without --no-fits to download FITS images."
fi
