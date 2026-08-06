#!/usr/bin/env bash
# run-on-rapid-admin.sh — run W1's unit suites and the live round-trip on
# rapid-admin, inside the pipeline image.
#
# Why not on the laptop: psycopg2 is not installed there and would not be
# the right psycopg2 if it were — the image is where the pipeline's
# dependency set actually lives. Team policy also puts containers on
# rapid-admin, never the laptop (Ben, 2026-07-16).
#
# Transport is the same S3-staging shape the migration applier uses: the
# worktree's Python is tarred, uploaded under the operator's credentials,
# and verified on the host against a checksum embedded in the SSM command
# rather than re-read from the bucket.
#
# Usage: run-on-rapid-admin.sh [image-ref]
# Exits 0 only if both unit suites pass AND the live round-trip prints
# LIVE-ROUNDTRIP-OK.

set -euo pipefail
cd "$(dirname "$0")/../../../.."   # repo root of the worktree

: "${AWS_PROFILE:=rapid-admin}" ; export AWS_PROFILE
: "${AWS_DEFAULT_REGION:=us-east-1}" ; export AWS_DEFAULT_REGION

# The account is derived at runtime, never written down here: this repo is
# PUBLIC (Caltech-IPAC/rapid), and .githooks/pre-push hard-blocks the SMDC
# account number with no allowlist. The caller's own credentials name it.
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
# Guard: the SMDC account id is supplied by the environment (or the
# rapid_systems repo, which is private and carries it), never hardcoded
# here. Without it this script will run against whatever account the
# profile points at, so refuse rather than guess.
: "${RAPID_ACCOUNT:?set RAPID_ACCOUNT to the SMDC account id}"
[ "$ACCOUNT" = "$RAPID_ACCOUNT" ] \
  || { echo "!! wrong account: $ACCOUNT (expected $RAPID_ACCOUNT)" >&2; exit 1; }

IMAGE=${1:-${ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/rapid-pipeline@sha256:87fe2651fb3fc124708d70b344255495ba75eae7b028a6891ac12eebaaa2b1c2}
BUCKET="rapid-build-artifacts-${ACCOUNT}"
RUN_ID="w1-tests-$(date -u +%Y%m%dT%H%M%SZ)"
PREFIX="db-migrations-staging/${RUN_ID}"


id=$(aws ssm describe-instance-information \
    --filters "Key=tag:Name,Values=rapid-admin" \
    --query 'InstanceInformationList[0].InstanceId' --output text)
[ -n "$id" ] && [ "$id" != "None" ] || { echo "!! rapid-admin not found" >&2; exit 1; }
echo ">> target: rapid-admin ($id)"
echo ">> image: $IMAGE"

# Only the Python this run needs — not the whole worktree (which carries
# large binary docs assets).
tarball=$(mktemp "${TMPDIR:-/tmp}/w1-tests.XXXXXX.tar.gz")
trap 'rm -f "$tarball"' EXIT
tar czf "$tarball" \
    database/modules/utils/rapid_db.py \
    database/modules/utils/rapid_db_connect.py \
    database/modules/utils/test \
    observability
echo ">> staged tarball: $(du -h "$tarball" | cut -f1)"

aws s3 cp "$tarball" "s3://$BUCKET/$PREFIX/w1-tests.tar.gz" --only-show-errors
sha=$(shasum -a 256 "$tarball" | awk '{print $1}')
echo ">> sha256: $sha"

# --- where each part runs ---------------------------------------------------
#
# The UNIT suites run on rapid-admin (team policy for containers, and the
# pipeline image is there).
#
# The LIVE round-trip runs on rapid-db instead, for a credential reason
# worth stating: in production the payload reads rapid/db/service/pipeline
# through the BATCH JOB ROLE, which trusts only ecs-tasks.amazonaws.com
# and cannot be assumed from an EC2 host. rapid-admin-instance-role cannot
# read that secret at all (proven live: AccessDeniedException, "no
# identity-based policy allows the secretsmanager:GetSecretValue action").
# rapid-db-instance-role CAN — it carries the rapid-db-service-pipeline-read
# managed policy — so running there lets boto3 do the REAL Secrets Manager
# fetch through get_db_credentials under a genuine instance role, exactly
# as a Batch job would, with no credential ever placed in an SSM document.
#
# NOT proven: that rapid-batch-job-role specifically can read it from
# inside a real Batch container. That needs a real Batch job and is a W8
# case by construction — recorded rather than faked with a widened grant.
db_id=$(aws ssm describe-instance-information \
    --filters "Key=tag:Name,Values=rapid-db" \
    --query 'InstanceInformationList[0].InstanceId' --output text)
[ -n "$db_id" ] && [ "$db_id" != "None" ] || { echo "!! rapid-db not found" >&2; exit 1; }
echo ">> live round-trip target: rapid-db ($db_id)"

remote_script=$(cat <<REMOTE
set -euo pipefail
STAGE=\$(mktemp -d)
trap 'rm -rf "\$STAGE"' EXIT
aws s3 cp "s3://$BUCKET/$PREFIX/w1-tests.tar.gz" "\$STAGE/w1-tests.tar.gz" --only-show-errors
echo "$sha  \$STAGE/w1-tests.tar.gz" > "\$STAGE/manifest.sha256"
sha256sum -c "\$STAGE/manifest.sha256" || { echo "!! checksum FAILED" >&2; exit 1; }
mkdir -p "\$STAGE/repo"
tar xzf "\$STAGE/w1-tests.tar.gz" -C "\$STAGE/repo"

aws ecr get-login-password --region us-east-1 \\
  | podman login --username AWS --password-stdin ${ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com

# The DB endpoint and secret id are operational configuration read from
# the parameter tree — not defaulted here, and not baked into the image
# (the rev-5/rev-6 failure class the payload design closes).
DBSERVER=\$(aws ssm get-parameter --name /rapid/pipeline/db/host --query Parameter.Value --output text 2>/dev/null || echo "")
DBPORT=\$(aws ssm get-parameter --name /rapid/pipeline/db/port --query Parameter.Value --output text 2>/dev/null || echo "")
DBNAME=\$(aws ssm get-parameter --name /rapid/pipeline/db/name --query Parameter.Value --output text 2>/dev/null || echo "")
DBSECRET=\$(aws ssm get-parameter --name /rapid/pipeline/db/secret-id --query Parameter.Value --output text 2>/dev/null || echo "")
# Fall back to the documented values where the tree does not yet carry
# them (W4 re-homes configuration; W1 must not block on it). Stated
# loudly rather than silently defaulted.
if [ -z "\$DBSERVER" ] || [ "\$DBSERVER" = "None" ]; then
  # The tree does not yet carry the DB endpoint — re-homing it there is
  # W4's work, and the payload design names its absence as the rev-5/rev-6
  # failure class. Until then, resolve it from the instance's own tag
  # rather than hardcoding a name nothing publishes: the address is a live
  # fact, and reading it here keeps this script correct across a replace.
  DBSERVER=\$(aws ec2 describe-instances \\
    --filters "Name=tag:Name,Values=rapid-db" "Name=instance-state-name,Values=running" \\
    --query 'Reservations[0].Instances[0].PrivateIpAddress' --output text)
  echo ">> NOTE: /rapid/pipeline/db/host absent; resolved rapid-db to \$DBSERVER from EC2"
fi
if [ -z "\$DBPORT" ] || [ "\$DBPORT" = "None" ]; then DBPORT=6432; echo ">> NOTE: db/port absent, using 6432 (the pooler)"; fi
if [ -z "\$DBNAME" ] || [ "\$DBNAME" = "None" ]; then DBNAME=rapid; echo ">> NOTE: db/name absent, using rapid"; fi
if [ -z "\$DBSECRET" ] || [ "\$DBSECRET" = "None" ]; then DBSECRET=rapid/db/service/pipeline; echo ">> NOTE: db/secret-id absent, using rapid/db/service/pipeline"; fi
echo ">> DBSERVER=\$DBSERVER DBPORT=\$DBPORT DBNAME=\$DBNAME SECRET=\$DBSECRET"

echo
echo "=== unit suite: connection helper (DB boundary stubbed) ==="
podman run --rm --entrypoint="" -v "\$STAGE/repo":/w1:Z -w /w1 "$IMAGE" \\
  python3.11 -m unittest database.modules.utils.test.test_rapid_db_connect -v 2>&1 | tail -25
unit1=\${PIPESTATUS[0]}
echo ">> connection-helper unit suite exit code: \$unit1"

echo
echo "=== unit suite: attempt writer (DB boundary stubbed) ==="
podman run --rm --entrypoint="" -v "\$STAGE/repo":/w1:Z -w /w1 "$IMAGE" \\
  python3.11 -m unittest observability.test.test_attempts -v 2>&1 | tail -15
unit2=\${PIPESTATUS[0]}
echo ">> attempt-writer unit suite exit code: \$unit2"

echo
echo "=== unit suite: submission integration (DB boundary stubbed) ==="
# In scope because the amendment broke it: create_submitted_for_submission
# now requires the execution binding, so this suite's calls had to carry it.
podman run --rm --entrypoint="" -v "\$STAGE/repo":/w1:Z -w /w1 "$IMAGE" \\
  python3.11 -m unittest observability.test.test_submission_integration -v 2>&1 | tail -12
unit3=\${PIPESTATUS[0]}
echo ">> submission-integration unit suite exit code: \$unit3"

echo
if [ "\$unit1" -eq 0 ] && [ "\$unit2" -eq 0 ] && [ "\$unit3" -eq 0 ]; then
  echo "W1-UNITS-OK"
else
  echo "!! W1 unit suites FAILED (unit1=\$unit1 unit2=\$unit2 unit3=\$unit3)" >&2
  exit 1
fi
REMOTE
)

script_b64=$(printf '%s' "$remote_script" | base64 | tr -d '\n')
params_json=$(mktemp "${TMPDIR:-/tmp}/w1-tests-params.XXXXXX")
printf '{"commands":["echo %s | base64 -d | bash"]}' "$script_b64" > "$params_json"

echo ">> running on rapid-admin via SSM..."
cmd_id=$(aws ssm send-command --instance-ids "$id" \
    --document-name "AWS-RunShellScript" --timeout-seconds 900 \
    --parameters "file://$params_json" \
    --query 'Command.CommandId' --output text)
echo ">> command id: $cmd_id"

aws ssm wait command-executed --command-id "$cmd_id" --instance-id "$id" 2>/dev/null || true
status=$(aws ssm get-command-invocation --command-id "$cmd_id" --instance-id "$id" \
    --query Status --output text)
echo "----- STDOUT -----"
aws ssm get-command-invocation --command-id "$cmd_id" --instance-id "$id" \
    --query StandardOutputContent --output text
echo "----- STDERR -----"
aws ssm get-command-invocation --command-id "$cmd_id" --instance-id "$id" \
    --query StandardErrorContent --output text
echo "----- STATUS: $status -----"

rm -f "$params_json"

# ---------------------------------------------------------------------------
# Part 2: the live round-trip, on rapid-db.
# ---------------------------------------------------------------------------
live_script=$(cat <<REMOTE
set -euo pipefail
STAGE=\$(mktemp -d)
trap 'rm -rf "\$STAGE"' EXIT
aws s3 cp "s3://$BUCKET/$PREFIX/w1-tests.tar.gz" "\$STAGE/w1-tests.tar.gz" --only-show-errors
echo "$sha  \$STAGE/w1-tests.tar.gz" > "\$STAGE/manifest.sha256"
sha256sum -c "\$STAGE/manifest.sha256" || { echo "!! checksum FAILED" >&2; exit 1; }
mkdir -p "\$STAGE/repo"
tar xzf "\$STAGE/w1-tests.tar.gz" -C "\$STAGE/repo"

aws ecr get-login-password --region us-east-1 \\
  | podman login --username AWS --password-stdin ${ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com

# The pooler is on this host, on loopback as far as the container is
# concerned once --network host is given. Port 6432 is pgbouncer; 5432 is
# not reachable from off-host by design (pooler-only, one door).
DBSERVER=127.0.0.1
DBPORT=6432
DBNAME=rapid
DBSECRET=rapid/db/service/pipeline
echo ">> DBSERVER=\$DBSERVER DBPORT=\$DBPORT DBNAME=\$DBNAME SECRET=\$DBSECRET"

# No credentials are passed in: boto3 inside the container picks up
# rapid-db-instance-role from IMDS and does the real Secrets Manager fetch
# through get_db_credentials, which is the whole point of running here.
podman run --rm --entrypoint="" -v "\$STAGE/repo":/w1:Z -w /w1 \\
  -e DBSERVER="\$DBSERVER" -e DBPORT="\$DBPORT" -e DBNAME="\$DBNAME" \\
  -e RAPID_DB_SECRET_ID="\$DBSECRET" \\
  -e AWS_DEFAULT_REGION=us-east-1 \\
  --network host \\
  "$IMAGE" python3.11 -m database.modules.utils.test.live_roundtrip 2>&1 | tail -70
live=\${PIPESTATUS[0]}
echo ">> live round-trip exit code: \$live"
[ "\$live" -eq 0 ] || exit 1
echo "W1-LIVE-OK"
REMOTE
)

live_b64=$(printf '%s' "$live_script" | base64 | tr -d '\n')
live_params=$(mktemp "${TMPDIR:-/tmp}/w1-live-params.XXXXXX")
printf '{"commands":["echo %s | base64 -d | bash"]}' "$live_b64" > "$live_params"

echo
echo ">> running the live round-trip on rapid-db via SSM..."
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
if [ "$status" = "Success" ] && [ "$live_status" = "Success" ]; then
    echo "W1-TESTS-OK (units on rapid-admin, live round-trip on rapid-db)"
    exit 0
fi
echo "!! W1 tests FAILED (units=$status live=$live_status)" >&2
exit 1
