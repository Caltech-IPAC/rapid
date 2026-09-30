#!/usr/bin/env bash
# Regenerate the sample alert packet linked from the docs
# (docs/source/prod/sample_alert.avro; see the "Sample Alert Packet"
# subsection of docs/source/prod/products.rst).
#
# Usage: ./docs/update_sample_alert.sh [SID] [alerts.cli options...]
#
# Extra arguments are passed through to alerts.cli, e.g.
#   ./docs/update_sample_alert.sh 6091788102 --kona-file kona_84776.json --no-ned --no-lvs
# (a GBTDS bulge source: solar-system association on, extragalactic
# cross-matches off).
#
# Sources rapid_setup.env for the DB environment, produces one alert for
# SID (default: 6091788102, the round-trip source pinned in
# alerts/test/test_live_db.py; RA 266.2376, Dec -28.8971, field 4666327),
# overwrites the committed sample, and verifies
# that the embedded schema matches the current registry-generated .avsc.
# The docs filename reference is stable (sample_alert.avro), but the schema
# version in the "Sample Alert Packet" prose of products.rst is written by
# hand -- this script warns if it no longer matches param_registry.VERSION.
#
# Run this whenever the alert schema changes (after gen_schema.py), then
# commit the updated sample_alert.avro.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-/opt/miniconda3/envs/astroconda/bin/python}"
SID="${1:-6091788102}"
SAMPLE="$REPO_ROOT/docs/source/prod/sample_alert.avro"

cd "$REPO_ROOT"
source rapid_setup.env

# Keep the previous sample as sample_alert_old<N>.avro (next free N) so
# packets from older schema versions stay available for comparison.
if [ -f "$SAMPLE" ]; then
    n=1
    while [ -e "${SAMPLE%.avro}_old${n}.avro" ]; do n=$((n + 1)); done
    mv "$SAMPLE" "${SAMPLE%.avro}_old${n}.avro"
    echo "previous sample kept as ${SAMPLE%.avro}_old${n}.avro"
fi

"$PYTHON" -m alerts.cli "$SID" --save "$SAMPLE" "${@:2}"

# Fail loudly if the sample disagrees with the current .avsc (e.g. the
# registry was edited but gen_schema.py was not re-run before this script).
"$PYTHON" - "$SAMPLE" <<'EOF'
import sys
import fastavro
from fastavro.schema import load_schema, to_parsing_canonical_form
from alerts.param_registry import VERSION

sample = sys.argv[1]
avsc = (f"alerts/schema/{VERSION.replace('.', '/')}"
        f"/rapid.v{VERSION.replace('.', '_')}.alert.avsc")
with open(sample, "rb") as f:
    embedded = fastavro.reader(f).writer_schema
if (to_parsing_canonical_form(embedded)
        != to_parsing_canonical_form(load_schema(avsc))):
    sys.exit(f"ERROR: schema embedded in {sample} does not match {avsc}")
print(f"schema OK: sample matches {avsc} (version {VERSION})")

# The version in the docs prose is hand-written; nag if it went stale.
rst = "docs/source/prod/products.rst"
with open(rst) as f:
    stale = [line.strip() for line in f
             if "schema version" in line and "``" in line
             and f"``{VERSION}``" not in line]
if stale:
    print(f"WARNING: {rst} says {stale[0]!r} but the current schema "
          f"version is {VERSION} -- update the Sample Alert Packet prose")
EOF

echo "Updated ${SAMPLE#"$REPO_ROOT"/} (sid ${SID}) -- remember to commit it."
