#!/usr/bin/env bash
# run-w4-on-rapid-admin.sh — run the W1+W2+W3+W4 unit suites on AWS,
# inside the pipeline image.
#
# Same shape and same reasons as W2's runner
# (pipeline/runtime/test/run-on-rapid-admin.sh): psycopg2 is not installed
# on the laptop and would not be the right psycopg2 if it were, and team
# policy puts containers on rapid-admin, never the laptop (Ben,
# 2026-07-16).
#
# WHAT W4 ADDS to the suites W2's runner already ran:
#
#   pipeline/runtime/test/test_science_config.py — the release-content
#     reader, including the round-trip that holds cdf/science/pipeline.toml
#     to the master .ini it was extracted from. That round-trip needs BOTH
#     files present in the container, so the tarball carries cdf/ as well.
#   submission/test — the route matrix and the manifest vocabulary. These
#     are pytest-style, not unittest, so they run under pytest; the image
#     carries it.
#
# Usage: run-w4-on-rapid-admin.sh [image-ref]
# Exits 0 only if every suite passes.

set -euo pipefail
cd "$(dirname "$0")/../.."   # repo root of the worktree

: "${AWS_PROFILE:=rapid-admin}" ; export AWS_PROFILE
: "${AWS_DEFAULT_REGION:=us-east-1}" ; export AWS_DEFAULT_REGION

# The account is derived at runtime, never written down here: this repo is
# PUBLIC (Caltech-IPAC/rapid) and .githooks/pre-push hard-blocks the SMDC
# account number with no allowlist.
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
: "${RAPID_ACCOUNT:?set RAPID_ACCOUNT to the SMDC account id}"
[ "$ACCOUNT" = "$RAPID_ACCOUNT" ] \
  || { echo "!! wrong account: $ACCOUNT (expected $RAPID_ACCOUNT)" >&2; exit 1; }

IMAGE=${1:-${ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/rapid-pipeline@sha256:87fe2651fb3fc124708d70b344255495ba75eae7b028a6891ac12eebaaa2b1c2}
BUCKET="rapid-build-artifacts-${ACCOUNT}"
RUN_ID="w4-tests-$(date -u +%Y%m%dT%H%M%SZ)"
PREFIX="db-migrations-staging/${RUN_ID}"

id=$(aws ssm describe-instance-information \
    --filters "Key=tag:Name,Values=rapid-admin" \
    --query 'InstanceInformationList[0].InstanceId' --output text)
[ -n "$id" ] && [ "$id" != "None" ] || { echo "!! rapid-admin not found" >&2; exit 1; }
echo ">> target: rapid-admin ($id)"
echo ">> image: $IMAGE"

tarball=$(mktemp "${TMPDIR:-/tmp}/w4-tests.XXXXXX.tar.gz")
trap 'rm -f "$tarball"' EXIT
# cdf/ carries both the master .ini and cdf/science/pipeline.toml: the
# round-trip test compares them, so neither can be left behind.
tar czf "$tarball" \
    cdf \
    database/modules/utils/rapid_db.py \
    database/modules/utils/rapid_db_connect.py \
    database/modules/utils/test \
    observability \
    pipeline/runtime \
    submission
echo ">> staged tarball: $(du -h "$tarball" | cut -f1)"

aws s3 cp "$tarball" "s3://$BUCKET/$PREFIX/w4-tests.tar.gz" --only-show-errors
sha=$(shasum -a 256 "$tarball" | awk '{print $1}')
echo ">> sha256: $sha"

remote_script=$(cat <<REMOTE
set -euo pipefail
STAGE=\$(mktemp -d)
trap 'rm -rf "\$STAGE"' EXIT
aws s3 cp "s3://$BUCKET/$PREFIX/w4-tests.tar.gz" "\$STAGE/w4-tests.tar.gz" --only-show-errors
echo "$sha  \$STAGE/w4-tests.tar.gz" > "\$STAGE/manifest.sha256"
sha256sum -c "\$STAGE/manifest.sha256" || { echo "!! checksum FAILED" >&2; exit 1; }
mkdir -p "\$STAGE/repo"
tar xzf "\$STAGE/w4-tests.tar.gz" -C "\$STAGE/repo"

aws ecr get-login-password --region us-east-1 \\
  | podman login --username AWS --password-stdin ${ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com

echo
echo "=== W2+W4 runtime unit suite (includes test_science_config) ==="
podman run --rm --entrypoint="" -v "\$STAGE/repo":/w4:Z -w /w4 "$IMAGE" \\
  python3.11 -m unittest discover -s pipeline/runtime/test -t . 2>&1 | tail -12
rt=\${PIPESTATUS[0]}
echo ">> runtime unit suite exit code: \$rt"

echo
echo "=== W4 submission suite (routes + manifest vocabulary) ==="
podman run --rm --entrypoint="" -v "\$STAGE/repo":/w4:Z -w /w4 "$IMAGE" \\
  python3.11 -m pytest submission/test -q 2>&1 | tail -12
sub=\${PIPESTATUS[0]}
echo ">> submission suite exit code: \$sub"

echo
echo "=== W1 connection helper ==="
podman run --rm --entrypoint="" -v "\$STAGE/repo":/w4:Z -w /w4 "$IMAGE" \\
  python3.11 -m unittest database.modules.utils.test.test_rapid_db_connect 2>&1 | tail -8
w1a=\${PIPESTATUS[0]}
echo ">> connection-helper suite exit code: \$w1a"

echo
echo "=== W1 attempt writer ==="
podman run --rm --entrypoint="" -v "\$STAGE/repo":/w4:Z -w /w4 "$IMAGE" \\
  python3.11 -m unittest observability.test.test_attempts 2>&1 | tail -8
w1b=\${PIPESTATUS[0]}
echo ">> attempt-writer suite exit code: \$w1b"

echo
echo "=== W1 submission integration ==="
podman run --rm --entrypoint="" -v "\$STAGE/repo":/w4:Z -w /w4 "$IMAGE" \\
  python3.11 -m unittest observability.test.test_submission_integration 2>&1 | tail -8
w1c=\${PIPESTATUS[0]}
echo ">> submission-integration suite exit code: \$w1c"

echo
echo "=== W3 rapid_db parameterization (quote round-trip) ==="
podman run --rm --entrypoint="" -v "\$STAGE/repo":/w4:Z -w /w4 "$IMAGE" \\
  python3.11 -m unittest discover -s database/modules/utils/test -t . 2>&1 | tail -8
w3=\${PIPESTATUS[0]}
echo ">> W3 suite exit code: \$w3"

echo
if [ "\$rt" -eq 0 ] && [ "\$sub" -eq 0 ] && [ "\$w1a" -eq 0 ] \\
   && [ "\$w1b" -eq 0 ] && [ "\$w1c" -eq 0 ] && [ "\$w3" -eq 0 ]; then
  echo "W4-UNITS-OK"
else
  echo "!! suites FAILED (rt=\$rt sub=\$sub w1a=\$w1a w1b=\$w1b w1c=\$w1c w3=\$w3)" >&2
  exit 1
fi
REMOTE
)

remote_b64=$(printf '%s' "$remote_script" | base64 | tr -d '\n')
params=$(mktemp "${TMPDIR:-/tmp}/w4-params.XXXXXX")
printf '{"commands":["echo %s | base64 -d | bash"]}' "$remote_b64" > "$params"

echo
echo ">> running suites on rapid-admin via SSM..."
cmd=$(aws ssm send-command --instance-ids "$id" \
    --document-name "AWS-RunShellScript" --timeout-seconds 900 \
    --parameters "file://$params" \
    --query 'Command.CommandId' --output text)
echo ">> command id: $cmd"

aws ssm wait command-executed --command-id "$cmd" --instance-id "$id" 2>/dev/null || true
status=$(aws ssm get-command-invocation --command-id "$cmd" --instance-id "$id" \
    --query Status --output text)
echo "----- STDOUT -----"
aws ssm get-command-invocation --command-id "$cmd" --instance-id "$id" \
    --query StandardOutputContent --output text
echo "----- STDERR -----"
aws ssm get-command-invocation --command-id "$cmd" --instance-id "$id" \
    --query StandardErrorContent --output text
echo "----- STATUS: $status -----"
rm -f "$params"

[ "$status" = "Success" ] || { echo "!! SSM invocation did not succeed" >&2; exit 1; }
echo "W4-VERIFICATION-OK"
