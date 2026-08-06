#!/usr/bin/env bash
# run-on-rapid-admin.sh — run W2's unit suites and the live ownership proof
# on AWS, inside the pipeline image.
#
# Same shape as W1's runner (database/modules/utils/test/run-on-rapid-admin.sh),
# and for the same reasons: psycopg2 is not installed on the laptop and would
# not be the right psycopg2 if it were — the image is where the pipeline's
# dependency set actually lives — and team policy puts containers on
# rapid-admin, never the laptop (Ben, 2026-07-16).
#
# WHERE EACH PART RUNS
#
# Unit suites on rapid-admin: they stub every boundary, so they need only the
# image.
#
# The live ownership proof on rapid-db: in production the payload reads
# rapid/db/service/pipeline through the BATCH JOB ROLE, which trusts only
# ecs-tasks.amazonaws.com and cannot be assumed from an EC2 host.
# rapid-admin-instance-role cannot read that secret at all (proven live by W1:
# AccessDenied); rapid-db-instance-role can. Running there lets boto3 do the
# real Secrets Manager fetch under a genuine instance role, with no credential
# ever placed in an SSM document.
#
# Usage: run-on-rapid-admin.sh [image-ref]
# Exits 0 only if the unit suites pass AND the live proof prints
# LIVE-OWNERSHIP-OK.

set -euo pipefail
cd "$(dirname "$0")/../../.."   # repo root of the worktree

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
RUN_ID="w2-tests-$(date -u +%Y%m%dT%H%M%SZ)"
PREFIX="db-migrations-staging/${RUN_ID}"

id=$(aws ssm describe-instance-information \
    --filters "Key=tag:Name,Values=rapid-admin" \
    --query 'InstanceInformationList[0].InstanceId' --output text)
[ -n "$id" ] && [ "$id" != "None" ] || { echo "!! rapid-admin not found" >&2; exit 1; }
echo ">> unit target: rapid-admin ($id)"

db_id=$(aws ssm describe-instance-information \
    --filters "Key=tag:Name,Values=rapid-db" \
    --query 'InstanceInformationList[0].InstanceId' --output text)
[ -n "$db_id" ] && [ "$db_id" != "None" ] || { echo "!! rapid-db not found" >&2; exit 1; }
echo ">> live target: rapid-db ($db_id)"
echo ">> image: $IMAGE"

tarball=$(mktemp "${TMPDIR:-/tmp}/w2-tests.XXXXXX.tar.gz")
trap 'rm -f "$tarball"' EXIT
tar czf "$tarball" \
    database/modules/utils/rapid_db.py \
    database/modules/utils/rapid_db_connect.py \
    database/modules/utils/test \
    observability \
    pipeline/runtime
echo ">> staged tarball: $(du -h "$tarball" | cut -f1)"

aws s3 cp "$tarball" "s3://$BUCKET/$PREFIX/w2-tests.tar.gz" --only-show-errors
sha=$(shasum -a 256 "$tarball" | awk '{print $1}')
echo ">> sha256: $sha"

# --- part 1: unit suites on rapid-admin -------------------------------------
unit_script=$(cat <<REMOTE
set -euo pipefail
STAGE=\$(mktemp -d)
trap 'rm -rf "\$STAGE"' EXIT
aws s3 cp "s3://$BUCKET/$PREFIX/w2-tests.tar.gz" "\$STAGE/w2-tests.tar.gz" --only-show-errors
echo "$sha  \$STAGE/w2-tests.tar.gz" > "\$STAGE/manifest.sha256"
sha256sum -c "\$STAGE/manifest.sha256" || { echo "!! checksum FAILED" >&2; exit 1; }
mkdir -p "\$STAGE/repo"
tar xzf "\$STAGE/w2-tests.tar.gz" -C "\$STAGE/repo"

aws ecr get-login-password --region us-east-1 \\
  | podman login --username AWS --password-stdin ${ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com

echo
echo "=== W2 runtime unit suite (all boundaries stubbed) ==="
podman run --rm --entrypoint="" -v "\$STAGE/repo":/w2:Z -w /w2 "$IMAGE" \\
  python3.11 -m unittest discover -s pipeline/runtime/test -t . 2>&1 | tail -12
w2=\${PIPESTATUS[0]}
echo ">> W2 runtime unit suite exit code: \$w2"

echo
echo "=== W1 connection helper (unmodified assertions) ==="
podman run --rm --entrypoint="" -v "\$STAGE/repo":/w2:Z -w /w2 "$IMAGE" \\
  python3.11 -m unittest database.modules.utils.test.test_rapid_db_connect 2>&1 | tail -8
w1a=\${PIPESTATUS[0]}
echo ">> connection-helper suite exit code: \$w1a"

echo
echo "=== W1 attempt writer ==="
podman run --rm --entrypoint="" -v "\$STAGE/repo":/w2:Z -w /w2 "$IMAGE" \\
  python3.11 -m unittest observability.test.test_attempts 2>&1 | tail -8
w1b=\${PIPESTATUS[0]}
echo ">> attempt-writer suite exit code: \$w1b"

echo
echo "=== W1 submission integration ==="
podman run --rm --entrypoint="" -v "\$STAGE/repo":/w2:Z -w /w2 "$IMAGE" \\
  python3.11 -m unittest observability.test.test_submission_integration 2>&1 | tail -8
w1c=\${PIPESTATUS[0]}
echo ">> submission-integration suite exit code: \$w1c"

echo
if [ "\$w2" -eq 0 ] && [ "\$w1a" -eq 0 ] && [ "\$w1b" -eq 0 ] && [ "\$w1c" -eq 0 ]; then
  echo "W2-UNITS-OK"
else
  echo "!! unit suites FAILED (w2=\$w2 w1a=\$w1a w1b=\$w1b w1c=\$w1c)" >&2
  exit 1
fi
REMOTE
)

unit_b64=$(printf '%s' "$unit_script" | base64 | tr -d '\n')
unit_params=$(mktemp "${TMPDIR:-/tmp}/w2-unit-params.XXXXXX")
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

# --- part 2: the live ownership proof on rapid-db ---------------------------
live_script=$(cat <<REMOTE
set -euo pipefail
STAGE=\$(mktemp -d)
trap 'rm -rf "\$STAGE"' EXIT
aws s3 cp "s3://$BUCKET/$PREFIX/w2-tests.tar.gz" "\$STAGE/w2-tests.tar.gz" --only-show-errors
echo "$sha  \$STAGE/w2-tests.tar.gz" > "\$STAGE/manifest.sha256"
sha256sum -c "\$STAGE/manifest.sha256" || { echo "!! checksum FAILED" >&2; exit 1; }
mkdir -p "\$STAGE/repo"
tar xzf "\$STAGE/w2-tests.tar.gz" -C "\$STAGE/repo"

aws ecr get-login-password --region us-east-1 \\
  | podman login --username AWS --password-stdin ${ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com

# The pooler is on this host. Port 6432 is pgbouncer; 5432 is not reachable
# off-host by design (pooler-only, one door).
DBSERVER=127.0.0.1
DBPORT=6432
DBNAME=rapid
DBSECRET=rapid/db/service/pipeline
echo ">> DBSERVER=\$DBSERVER DBPORT=\$DBPORT DBNAME=\$DBNAME SECRET=\$DBSECRET"

# No credentials passed in: boto3 inside the container picks up
# rapid-db-instance-role from IMDS and does the real Secrets Manager fetch.
podman run --rm --entrypoint="" -v "\$STAGE/repo":/w2:Z -w /w2 \\
  -e DBSERVER="\$DBSERVER" -e DBPORT="\$DBPORT" -e DBNAME="\$DBNAME" \\
  -e RAPID_DB_SECRET_ID="\$DBSECRET" \\
  -e AWS_DEFAULT_REGION=us-east-1 \\
  --network host \\
  "$IMAGE" python3.11 -m pipeline.runtime.test.live_ownership 2>&1 | tail -60
live=\${PIPESTATUS[0]}
echo ">> live ownership proof exit code: \$live"
[ "\$live" -eq 0 ] || exit 1
echo "W2-LIVE-OK"
REMOTE
)

live_b64=$(printf '%s' "$live_script" | base64 | tr -d '\n')
live_params=$(mktemp "${TMPDIR:-/tmp}/w2-live-params.XXXXXX")
printf '{"commands":["echo %s | base64 -d | bash"]}' "$live_b64" > "$live_params"

echo
echo ">> running the live ownership proof on rapid-db via SSM..."
live_cmd=$(aws ssm send-command --instance-ids "$db_id" \
    --document-name "AWS-RunShellScript" --timeout-seconds 900 \
    --parameters "file://$live_params" \
    --query 'Command.CommandId' --output text)
echo ">> command id: $live_cmd"

aws ssm wait command-executed --command-id "$live_cmd" --instance-id "$db_id" 2>/dev/null || true
live_status=$(aws ssm get-command-invocation --command-id "$live_cmd" --instance-id "$db_id" \
    --query Status --output text)
echo "----- LIVE STDOUT -----"
aws ssm get-command-invocation --command-id "$live_cmd" --instance-id "$db_id" \
    --query StandardOutputContent --output text
echo "----- LIVE STDERR -----"
aws ssm get-command-invocation --command-id "$live_cmd" --instance-id "$db_id" \
    --query StandardErrorContent --output text
echo "----- LIVE STATUS: $live_status -----"
rm -f "$live_params"

aws s3 rm "s3://$BUCKET/$PREFIX/" --recursive --only-show-errors || true

echo
if [ "$unit_status" = "Success" ] && [ "$live_status" = "Success" ]; then
    echo "W2-TESTS-OK (units on rapid-admin, live proof on rapid-db)"
    exit 0
fi
echo "!! W2 tests FAILED (units=$unit_status live=$live_status)" >&2
exit 1
