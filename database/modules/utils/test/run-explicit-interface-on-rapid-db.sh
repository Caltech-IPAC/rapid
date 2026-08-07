#!/usr/bin/env bash
#
# run-explicit-interface-on-rapid-db.sh — the O1 explicit-interface live
# probe, inside the pipeline image, on rapid-db via SSM.
#
# WHY rapid-db and not rapid-admin: the probe resolves the real credential
# from Secrets Manager under an instance role, and rapid-admin-instance-role
# cannot read rapid/db/service/pipeline at all (proven live by W1:
# AccessDenied). rapid-db-instance-role can, and the pooler is on that host.
# Same placement, and the same reasons, as W1's own live round-trip.
#
# WHAT IT DOES NOT DO: it writes nothing. Two connections and three SELECTs.
# The write-bearing live proof is `live_roundtrip.py`, which is a different
# script run by a different runner.
#
# Usage: run-explicit-interface-on-rapid-db.sh [image-ref]
# Exits 0 only if the probe prints LIVE-EXPLICIT-OK.

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
RUN_ID="o1-explicit-$(date -u +%Y%m%dT%H%M%SZ)"
PREFIX="db-migrations-staging/${RUN_ID}"

db_id=$(aws ssm describe-instance-information \
    --filters "Key=tag:Name,Values=rapid-db" \
    --query 'InstanceInformationList[0].InstanceId' --output text)
[ -n "$db_id" ] && [ "$db_id" != "None" ] || { echo "!! rapid-db not found" >&2; exit 1; }
echo ">> target: rapid-db ($db_id)"
echo ">> image: $IMAGE"

tarball=$(mktemp "${TMPDIR:-/tmp}/o1-explicit.XXXXXX.tar.gz")
trap 'rm -f "$tarball"' EXIT
tar --no-xattrs -czf "$tarball" \
    database/modules/utils/rapid_db.py \
    database/modules/utils/rapid_db_connect.py \
    database/modules/utils/test/live_explicit_interface.py
echo ">> staged tarball: $(du -h "$tarball" | cut -f1)"

aws s3 cp "$tarball" "s3://$BUCKET/$PREFIX/o1-explicit.tar.gz" --only-show-errors
sha=$(shasum -a 256 "$tarball" | awk '{print $1}')
echo ">> sha256: $sha"

script=$(cat <<REMOTE
set -euo pipefail
STAGE=\$(mktemp -d)
trap 'rm -rf "\$STAGE"' EXIT
aws s3 cp "s3://$BUCKET/$PREFIX/o1-explicit.tar.gz" "\$STAGE/o1-explicit.tar.gz" --only-show-errors
echo "$sha  \$STAGE/o1-explicit.tar.gz" > "\$STAGE/manifest.sha256"
sha256sum -c "\$STAGE/manifest.sha256" || { echo "!! checksum FAILED" >&2; exit 1; }
mkdir -p "\$STAGE/repo"
tar xzf "\$STAGE/o1-explicit.tar.gz" -C "\$STAGE/repo" 2>/dev/null

aws ecr get-login-password --region us-east-1 \\
  | podman login --username AWS --password-stdin ${ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com

# The pooler is on this host; port 6432 is pgbouncer, 5432 is not reachable
# from off-host by design. No credential is passed in: boto3 inside the
# container picks up rapid-db-instance-role from IMDS and does the real
# Secrets Manager fetch, which is the point of running here.
podman run --rm --entrypoint="" -v "\$STAGE/repo":/w:Z -w /w \\
  -e DBSERVER=127.0.0.1 -e DBPORT=6432 -e DBNAME=rapid \\
  -e RAPID_DB_SECRET_ID=rapid/db/service/pipeline \\
  -e AWS_DEFAULT_REGION=us-east-1 \\
  --network host \\
  "$IMAGE" python3.11 -m database.modules.utils.test.live_explicit_interface 2>&1 | tail -30
rc=\${PIPESTATUS[0]}
echo ">> live explicit-interface probe exit code: \$rc"
exit \$rc
REMOTE
)

b64=$(printf '%s' "$script" | base64 | tr -d '\n')
params=$(mktemp "${TMPDIR:-/tmp}/o1-explicit-params.XXXXXX")
printf '{"commands":["echo %s | base64 -d | bash"]}' "$b64" > "$params"

echo
echo ">> running the explicit-interface probe on rapid-db via SSM..."
cmd=$(aws ssm send-command --instance-ids "$db_id" \
    --document-name "AWS-RunShellScript" --timeout-seconds 900 \
    --parameters "file://$params" \
    --query 'Command.CommandId' --output text)
rm -f "$params"
echo ">> command id: $cmd"

for _ in $(seq 40); do
    status=$(aws ssm get-command-invocation --command-id "$cmd" \
        --instance-id "$db_id" --query Status --output text 2>/dev/null || echo Pending)
    case "$status" in
        Success|Failed|Cancelled|TimedOut) break ;;
    esac
    sleep 15
done
echo ">> status: $status"

aws ssm get-command-invocation --command-id "$cmd" --instance-id "$db_id" \
    --query StandardOutputContent --output text
aws ssm get-command-invocation --command-id "$cmd" --instance-id "$db_id" \
    --query StandardErrorContent --output text >&2

[ "$status" = "Success" ] || { echo "!! probe FAILED" >&2; exit 1; }
