#!/bin/bash
#
# Download the reference-image and difference-image products listed in a CSV written by
# reqid_products_to_csv.sh, keeping their S3 layout below the processing date:
#
#     s3://<bucket>/<yyyymmdd>/req<reqid>/jid<jid>/...  ->  <output_dir>/req<reqid>/jid<jid>/...
#
# A reference image made by an earlier processing request is downloaded under that request's
# req<reqid>/jid<jid> directory.  Each S3 directory is downloaded once, however many rows list it,
# and with aws s3 sync, so a rerun only fetches what is missing.
#
# Usage:  download_reqid_products.sh <products.csv> <output_dir>

set -euo pipefail

if [ $# -ne 2 ]; then
    echo "Usage: $0 <products.csv> <output_dir>" >&2
    exit 64
fi

products_csv=$1
output_dir=$2

if [ ! -f "$products_csv" ]; then
    echo "*** Error: $products_csv not found" >&2
    exit 64
fi

# Unique, non-empty S3 directories from the reference_dir and diff_dir columns, located by
# header name rather than position.

s3_dirs=$(awk -F, '
    NR == 1 {
        for (i = 1; i <= NF; i++) {
            if ($i == "reference_dir") ref_col = i
            if ($i == "diff_dir") diff_col = i
        }
        if (!ref_col || !diff_col) {
            print "*** Error: CSV header lacks reference_dir or diff_dir" > "/dev/stderr"
            exit 1
        }
        next
    }
    { if ($ref_col != "") print $ref_col; if ($diff_col != "") print $diff_col }
' "$products_csv" | sort -u)

if [ -z "$s3_dirs" ]; then
    echo "No product directories in $products_csv; nothing to download."
    exit 0
fi

ndirs=$(echo "$s3_dirs" | wc -l | tr -d ' ')
echo "Downloading $ndirs S3 directories into $output_dir..."

nfailed=0

while read -r s3_dir; do

    # Strip s3://<bucket>/<yyyymmdd>/ to keep req<reqid>/jid<jid> (or jid<jid> for a product of
    # the older layout without a request level).
    local_subdir=$(echo "$s3_dir" | sed -E 's#^s3://[^/]+/[0-9]{8}/##')

    if [ "$local_subdir" = "$s3_dir" ]; then
        echo "*** Warning: Unexpected S3 path (no s3://<bucket>/<yyyymmdd>/ prefix): $s3_dir; skipping..."
        nfailed=$((nfailed + 1))
        continue
    fi

    echo "$s3_dir/ -> $output_dir/$local_subdir/"

    if ! aws s3 sync --only-show-errors "$s3_dir/" "$output_dir/$local_subdir/"; then
        echo "*** Warning: Failed downloading $s3_dir/"
        nfailed=$((nfailed + 1))
    fi

done <<< "$s3_dirs"

echo "Done: $((ndirs - nfailed)) of $ndirs directories downloaded."

if [ $nfailed -gt 0 ]; then
    exit 1
fi
