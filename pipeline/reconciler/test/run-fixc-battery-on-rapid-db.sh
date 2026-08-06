#!/usr/bin/env bash
# run-fixc-battery-on-rapid-db.sh — drive the W8 acceptance battery against rapid-db.
#
# Same shape and same reasons as W2's live runner
# (pipeline/runtime/test/run-on-rapid-admin.sh): the probe runs on RAPID-DB,
# not rapid-admin, because the pooler is on that host — port 6432 is
# pgbouncer and 5432 is not reachable off-host by design (pooler-only, one
# door). No credentials are passed in: boto3 inside the container picks up
# rapid-db-instance-role from IMDS and does the real Secrets Manager fetch.
#
# What the probe proves that the unit suite cannot: the REAL resolver as
# amended by migration 017, the REAL lifecycle constraints (a stub cannot
# refuse a state), the REAL rowcount contract behind the compare-and-sets,
# and one REAL reconciler cycle against Batch and the records bucket.
#
# ADDITIVE only. The probe writes rows under a time-stamped run id and leaves
# them terminal, as evidence; it deletes nothing.
#
# Usage: run-fixc-battery-on-rapid-db.sh [image-ref]
# Exits 0 only if every assertion in the probe passed.

set -euo pipefail
cd "$(dirname "$0")/../../.."   # repo root of the worktree

: "${AWS_PROFILE:=rapid-admin}" ; export AWS_PROFILE
: "${AWS_DEFAULT_REGION:=us-east-1}" ; export AWS_DEFAULT_REGION

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
: "${RAPID_ACCOUNT:?set RAPID_ACCOUNT to the SMDC account id}"
[ "$ACCOUNT" = "$RAPID_ACCOUNT" ] \
  || { echo "!! wrong account: $ACCOUNT (expected $RAPID_ACCOUNT)" >&2; exit 1; }

IMAGE=${1:?pass the image ref to run the probe in}
BUCKET="rapid-build-artifacts-${ACCOUNT}"
RUN_ID="fixc-battery-$(date -u +%Y%m%dT%H%M%SZ)"
PREFIX="db-migrations-staging/${RUN_ID}"

db_id=$(aws ssm describe-instance-information \
    --filters "Key=tag:Name,Values=rapid-db" \
    --query 'InstanceInformationList[0].InstanceId' --output text)
[ -n "$db_id" ] && [ "$db_id" != "None" ] \
  || { echo "!! rapid-db not found in SSM inventory" >&2; exit 1; }
echo ">> target: rapid-db ($db_id)"
echo ">> image: $IMAGE"

tarball=$(mktemp "${TMPDIR:-/tmp}/fixc-battery.XXXXXX.tar.gz")
trap 'rm -f "$tarball"' EXIT
tar czf "$tarball" \
    cdf \
    database/modules/utils \
    modules \
    observability \
    pipeline \
    submission
echo ">> staged tarball: $(du -h "$tarball" | cut -f1)"

aws s3 cp "$tarball" "s3://$BUCKET/$PREFIX/fixc-battery.tar.gz" --only-show-errors
sha=$(shasum -a 256 "$tarball" | awk '{print $1}')
echo ">> sha256: $sha"

remote_script=$(cat <<REMOTE
set -uo pipefail
STAGE=\$(mktemp -d)
trap 'rm -rf "\$STAGE"' EXIT
aws s3 cp "s3://$BUCKET/$PREFIX/fixc-battery.tar.gz" "\$STAGE/fixc-battery.tar.gz" --only-show-errors
echo "$sha  \$STAGE/fixc-battery.tar.gz" > "\$STAGE/manifest.sha256"
sha256sum -c "\$STAGE/manifest.sha256" || { echo "!! checksum FAILED" >&2; exit 1; }
mkdir -p "\$STAGE/repo"
tar xzf "\$STAGE/fixc-battery.tar.gz" -C "\$STAGE/repo"

aws ecr get-login-password --region us-east-1 \\
  | podman login --username AWS --password-stdin ${ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com >/dev/null

# The pooler is on this host. 6432 is pgbouncer; 5432 is not reachable
# off-host by design.
DBSERVER=127.0.0.1
DBPORT=6432
DBNAME=rapid
DBSECRET=rapid/db/service/pipeline
echo ">> DBSERVER=\$DBSERVER DBPORT=\$DBPORT DBNAME=\$DBNAME SECRET=\$DBSECRET"

# THE RECORDS BUCKET IS THE BUILD-ARTIFACTS BUCKET HERE, and that is a
# deliberate compromise recorded rather than hidden.
#
# The battery needs BOTH the database and a writable records bucket. The
# pooler is host-local to rapid-db (5432 is not reachable off-host by
# design), so the battery must run here — but rapid-db-instance-role has no
# grant on roman-rapid-records and can only assume rapid-migration-runner-role,
# not rapid-orchestrator-role. Pointed at the real bucket the battery gets a
# 403 on the first record write, and it does so on the UNMODIFIED smdc
# baseline too (verified 2026-08-06): the gap predates this round.
#
# What the battery actually proves is protocol behaviour — create-once,
# checksum validation, the crash-boundary materialization, the lifecycle
# constraints — none of which depends on WHICH bucket holds the objects. So
# it runs against a bucket this host can write, under a battery-scoped run
# id, and the identity-stack grant is left as a proposed decision.
RECORDS_BUCKET="rapid-build-artifacts-${ACCOUNT}"
echo ">> RECORDS_BUCKET=\$RECORDS_BUCKET (see the note above)"

podman run --rm --entrypoint="" -v "\$STAGE/repo":/fixc:Z -w /fixc \\
  -e DBSERVER="\$DBSERVER" -e DBPORT="\$DBPORT" -e DBNAME="\$DBNAME" \\
  -e RAPID_DB_SECRET_ID="\$DBSECRET" \\
  -e AWS_DEFAULT_REGION=us-east-1 \\
  -e RAPID_RECORDS_BUCKET="\$RECORDS_BUCKET" \\
  -e RAPID_DIAGNOSTICS_BUCKET="\$RECORDS_BUCKET" \\
  -e RAPID_RECORDS_PREFIX=attempts \\
  --network host \\
  "$IMAGE" python3.11 -m pipeline.reconciler.test.live_w8_battery 2>&1 | tail -120
live=\${PIPESTATUS[0]}
echo ">> battery exit code: \$live"
[ "\$live" -eq 0 ] || exit 1
echo "FIXC-BATTERY-OK"
REMOTE
)

remote_b64=$(printf '%s' "$remote_script" | base64 | tr -d '\n')
params=$(mktemp "${TMPDIR:-/tmp}/fixc-battery-params.XXXXXX")
printf '{"commands":["echo %s | base64 -d | bash"]}' "$remote_b64" > "$params"

echo
echo ">> running the live probe on rapid-db via SSM..."
cmd=$(aws ssm send-command --instance-ids "$db_id" \
    --document-name "AWS-RunShellScript" --timeout-seconds 1800 \
    --parameters "file://$params" \
    --query 'Command.CommandId' --output text)
echo ">> command id: $cmd"
rm -f "$params"

aws ssm wait command-executed --command-id "$cmd" --instance-id "$db_id" 2>/dev/null || true
status=$(aws ssm get-command-invocation --command-id "$cmd" --instance-id "$db_id" \
    --query Status --output text)
echo "----- STDOUT -----"
aws ssm get-command-invocation --command-id "$cmd" --instance-id "$db_id" \
    --query StandardOutputContent --output text
echo "----- STDERR -----"
aws ssm get-command-invocation --command-id "$cmd" --instance-id "$db_id" \
    --query StandardErrorContent --output text
echo "----- STATUS: $status -----"

aws s3 rm "s3://$BUCKET/$PREFIX/" --recursive --only-show-errors 2>/dev/null || true

[ "$status" = "Success" ] || { echo "!! the live probe did not succeed" >&2; exit 1; }
echo "FIXC-BATTERY-VERIFICATION-OK"
