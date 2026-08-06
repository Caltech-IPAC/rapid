#!/usr/bin/env bash
# run-w3-on-rapid-admin.sh — full test suite (W1 + W2 + W3) in-image on
# rapid-admin via SSM.
#
# Same staging shape as pipeline/runtime/test/run-on-rapid-admin.sh, for the
# same reason: psycopg2 is not installed on the laptop, and the image is
# where the pipeline's actual dependency set lives. W3 adds no live-DB
# component of its own — test_rapid_db.py stubs conn/cur directly — so this
# runner has only the unit-suite part, no rapid-db leg.
#
# Usage: run-w3-on-rapid-admin.sh [image-ref]
# Exits 0 only if every suite passes.

set -euo pipefail
cd "$(dirname "$0")/../../../.."   # repo root of the worktree

: "${AWS_PROFILE:=rapid-admin}" ; export AWS_PROFILE
: "${AWS_DEFAULT_REGION:=us-east-1}" ; export AWS_DEFAULT_REGION

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
: "${RAPID_ACCOUNT:?set RAPID_ACCOUNT to the SMDC account id}"
[ "$ACCOUNT" = "$RAPID_ACCOUNT" ] \
  || { echo "!! wrong account: $ACCOUNT (expected $RAPID_ACCOUNT)" >&2; exit 1; }

IMAGE=${1:-${ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/rapid-pipeline@sha256:87fe2651fb3fc124708d70b344255495ba75eae7b028a6891ac12eebaaa2b1c2}
BUCKET="rapid-build-artifacts-${ACCOUNT}"
RUN_ID="w3-tests-$(date -u +%Y%m%dT%H%M%SZ)"
PREFIX="db-migrations-staging/${RUN_ID}"

id=$(aws ssm describe-instance-information \
    --filters "Key=tag:Name,Values=rapid-admin" \
    --query 'InstanceInformationList[0].InstanceId' --output text)
[ -n "$id" ] && [ "$id" != "None" ] || { echo "!! rapid-admin not found" >&2; exit 1; }
echo ">> unit target: rapid-admin ($id)"
echo ">> image: $IMAGE"

tarball=$(mktemp "${TMPDIR:-/tmp}/w3-tests.XXXXXX.tar.gz")
trap 'rm -f "$tarball"' EXIT
# The cdf/ configuration files are staged because W4B's science-config
# suite asserts THE SHIPPED FILES load — not fixtures, deliberately, since
# the claim is that the release's own configuration parses and that the
# extracted TOML still round-trips against the master .ini it came from.
# Those tests arrived after this runner did, so neither file was staged and
# the suite reported five errors then two failures in-image, the first of
# them "the release's science configuration is missing at
# /w3/cdf/science/pipeline.toml" — an error correctly naming a staging gap
# that reads as a wrong image (W6b, 2026-08-06).
#
# Both named explicitly rather than staging cdf/ whole: that directory also
# holds romantessellation_nside10.fits, which this suite does not read.
# The master .ini's own retirement is gated on its 23 surviving readers
# (docs/source/dev/config_homes.rst); when it goes, its round-trip test
# goes with it and this line loses its second file.
tar czf "$tarball" \
    database/modules/utils/rapid_db.py \
    database/modules/utils/rapid_db_connect.py \
    database/modules/utils/test \
    observability \
    pipeline/runtime \
    cdf/science/pipeline.toml \
    cdf/awsBatchSubmitJobs_launchSingleSciencePipeline.ini
echo ">> staged tarball: $(du -h "$tarball" | cut -f1)"

aws s3 cp "$tarball" "s3://$BUCKET/$PREFIX/w3-tests.tar.gz" --only-show-errors
sha=$(shasum -a 256 "$tarball" | awk '{print $1}')
echo ">> sha256: $sha"

unit_script=$(cat <<REMOTE
set -euo pipefail
STAGE=\$(mktemp -d)
trap 'rm -rf "\$STAGE"' EXIT
aws s3 cp "s3://$BUCKET/$PREFIX/w3-tests.tar.gz" "\$STAGE/w3-tests.tar.gz" --only-show-errors
echo "$sha  \$STAGE/w3-tests.tar.gz" > "\$STAGE/manifest.sha256"
sha256sum -c "\$STAGE/manifest.sha256" || { echo "!! checksum FAILED" >&2; exit 1; }
mkdir -p "\$STAGE/repo"
tar xzf "\$STAGE/w3-tests.tar.gz" -C "\$STAGE/repo"

aws ecr get-login-password --region us-east-1 \\
  | podman login --username AWS --password-stdin ${ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com

echo
echo "=== W1 connection helper ==="
podman run --rm --entrypoint="" -v "\$STAGE/repo":/w3:Z -w /w3 "$IMAGE" \\
  python3.11 -m unittest database.modules.utils.test.test_rapid_db_connect 2>&1 | tail -8
w1a=\${PIPESTATUS[0]}
echo ">> connection-helper suite exit code: \$w1a"

echo
echo "=== W1 attempt writer ==="
podman run --rm --entrypoint="" -v "\$STAGE/repo":/w3:Z -w /w3 "$IMAGE" \\
  python3.11 -m unittest observability.test.test_attempts 2>&1 | tail -8
w1b=\${PIPESTATUS[0]}
echo ">> attempt-writer suite exit code: \$w1b"

echo
echo "=== W1 submission integration ==="
podman run --rm --entrypoint="" -v "\$STAGE/repo":/w3:Z -w /w3 "$IMAGE" \\
  python3.11 -m unittest observability.test.test_submission_integration 2>&1 | tail -8
w1c=\${PIPESTATUS[0]}
echo ">> submission-integration suite exit code: \$w1c"

echo
echo "=== W2 runtime unit suite ==="
podman run --rm --entrypoint="" -v "\$STAGE/repo":/w3:Z -w /w3 "$IMAGE" \\
  python3.11 -m unittest discover -s pipeline/runtime/test -t . 2>&1 | tail -12
w2=\${PIPESTATUS[0]}
echo ">> W2 runtime unit suite exit code: \$w2"

echo
echo "=== W3 rapid_db.py parameterization round-trip suite ==="
podman run --rm --entrypoint="" -v "\$STAGE/repo":/w3:Z -w /w3 "$IMAGE" \\
  python3.11 -m unittest database.modules.utils.test.test_rapid_db 2>&1 | tail -12
w3=\${PIPESTATUS[0]}
echo ">> W3 test_rapid_db suite exit code: \$w3"

echo
if [ "\$w1a" -eq 0 ] && [ "\$w1b" -eq 0 ] && [ "\$w1c" -eq 0 ] && [ "\$w2" -eq 0 ] && [ "\$w3" -eq 0 ]; then
  echo "W3-UNITS-OK"
else
  echo "!! unit suites FAILED (w1a=\$w1a w1b=\$w1b w1c=\$w1c w2=\$w2 w3=\$w3)" >&2
  exit 1
fi
REMOTE
)

unit_b64=$(printf '%s' "$unit_script" | base64 | tr -d '\n')
unit_params=$(mktemp "${TMPDIR:-/tmp}/w3-unit-params.XXXXXX")
printf '{"commands":["echo %s | base64 -d | bash"]}' "$unit_b64" > "$unit_params"

echo
echo ">> running unit suites on rapid-admin via SSM..."
unit_cmd=$(aws ssm send-command --instance-ids "$id" \
    --document-name "AWS-RunShellScript" --timeout-seconds 900 \
    --parameters "file://$unit_params" \
    --query 'Command.CommandId' --output text)
echo ">> command id: $unit_cmd"

aws ssm wait command-executed --command-id "$unit_cmd" --instance-id "$id" 2>/dev/null || true
unit_status=$(aws ssm get-command-invocation --command-id "$unit_cmd" --instance-id "$id" \
    --query Status --output text)
echo "----- UNIT STDOUT -----"
aws ssm get-command-invocation --command-id "$unit_cmd" --instance-id "$id" \
    --query StandardOutputContent --output text
echo "----- UNIT STDERR -----"
aws ssm get-command-invocation --command-id "$unit_cmd" --instance-id "$id" \
    --query StandardErrorContent --output text
echo "----- UNIT STATUS: $unit_status -----"
rm -f "$unit_params"

aws s3 rm "s3://$BUCKET/$PREFIX/" --recursive --only-show-errors || true

echo
if [ "$unit_status" = "Success" ]; then
    echo "W3-TESTS-OK"
    exit 0
fi
echo "!! W3 tests FAILED (units=$unit_status)" >&2
exit 1
