#!/usr/bin/env bash
# run-w9-ramp-on-rapid-admin.sh — drive one W9 ramp STEP on rapid-admin.
#
# Same shape as the FixD live runner (pipeline/registration/test/
# run-fixd-chain-on-rapid-db.sh): tar the tree, stage it to the build
# bucket, and run it inside the PINNED image via SSM, so what executes is
# the release under test rather than a laptop checkout.
#
# rapid-admin rather than rapid-db: submission needs Batch and the
# parameter tree under a role that can submit, and the reconciler already
# runs here. The pooler is reachable from here (the reconciler connects
# to it every 60 s from this host).
#
# Usage: run-w9-ramp-on-rapid-admin.sh <image-ref> <reference|science> <cap> [tag]

set -euo pipefail
cd "$(dirname "$0")/../.."      # repo root (pipeline/test/ is two deep)

: "${AWS_PROFILE:=rapid-admin}" ; export AWS_PROFILE
: "${AWS_DEFAULT_REGION:=us-east-1}" ; export AWS_DEFAULT_REGION

IMAGE=${1:?pass the image ref}
PHASE=${2:?pass the phase: reference|science}
CAP=${3:?pass the child cap}
TAG=${4:-}

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
: "${RAPID_ACCOUNT:?set RAPID_ACCOUNT to the SMDC account id}"
[ "$ACCOUNT" = "$RAPID_ACCOUNT" ] \
  || { echo "!! wrong account: $ACCOUNT (expected $RAPID_ACCOUNT)" >&2; exit 1; }

BUCKET="rapid-build-artifacts-${ACCOUNT}"
RUN_ID="w9-ramp-$(date -u +%Y%m%dT%H%M%SZ)"
PREFIX="w9-ramp-staging/${RUN_ID}"

admin_id=$(aws ssm describe-instance-information \
    --filters "Key=tag:Name,Values=rapid-admin" \
    --query 'InstanceInformationList[0].InstanceId' --output text)
[ -n "$admin_id" ] && [ "$admin_id" != "None" ] \
  || { echo "!! rapid-admin not found in SSM inventory" >&2; exit 1; }
echo ">> target: rapid-admin ($admin_id)"
echo ">> image:  $IMAGE"
echo ">> step:   phase=$PHASE cap=$CAP"

tarball=$(mktemp "${TMPDIR:-/tmp}/w9-ramp.XXXXXX.tar.gz")
trap 'rm -f "$tarball"' EXIT
tar czf "$tarball" \
    cdf \
    database/modules/utils \
    modules \
    observability \
    pipeline \
    submission
echo ">> staged tarball: $(du -h "$tarball" | cut -f1)"

aws s3 cp "$tarball" "s3://$BUCKET/$PREFIX/w9-ramp.tar.gz" --only-show-errors
sha=$(shasum -a 256 "$tarball" | awk '{print $1}')
echo ">> sha256: $sha"

remote_script=$(cat <<REMOTE
set -uo pipefail
STAGE=\$(mktemp -d)
trap 'rm -rf "\$STAGE"' EXIT
aws s3 cp "s3://$BUCKET/$PREFIX/w9-ramp.tar.gz" "\$STAGE/w9-ramp.tar.gz" --only-show-errors
echo "$sha  \$STAGE/w9-ramp.tar.gz" > "\$STAGE/manifest.sha256"
sha256sum -c "\$STAGE/manifest.sha256" || { echo "!! checksum FAILED" >&2; exit 1; }
mkdir -p "\$STAGE/repo"
tar xzf "\$STAGE/w9-ramp.tar.gz" -C "\$STAGE/repo"

aws ecr get-login-password --region us-east-1 \\
  | podman login --username AWS --password-stdin ${ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com >/dev/null

DBSERVER=\$(aws ssm get-parameter --name /rapid/pipeline/db/server --query Parameter.Value --output text)
DBPORT=\$(aws ssm get-parameter --name /rapid/pipeline/db/port --query Parameter.Value --output text)
DBNAME=\$(aws ssm get-parameter --name /rapid/pipeline/db/name --query Parameter.Value --output text)
# The tree's secret-id is the PIPELINE service credential, which only the
# job role can read. Submission here runs under the orchestrator role
# (below), so it uses the orchestrator secret -- the same identity the
# reconciler connects with, and one that holds write on the operation
# tables the pre-created attempt rows need.
DBSECRET=rapid/db/service/orchestrator

# Submission runs under the ORCHESTRATOR role: the instance role can read
# no DB secret by design and chains into this one, which is the same
# identity the reconciler uses.
CREDS=\$(aws sts assume-role \\
  --role-arn arn:aws:iam::${ACCOUNT}:role/rapid-orchestrator-role \\
  --role-session-name w9-ramp --query Credentials --output json)
AKI=\$(printf '%s' "\$CREDS" | python3 -c "import json,sys; print(json.load(sys.stdin)['AccessKeyId'])")
SAK=\$(printf '%s' "\$CREDS" | python3 -c "import json,sys; print(json.load(sys.stdin)['SecretAccessKey'])")
STK=\$(printf '%s' "\$CREDS" | python3 -c "import json,sys; print(json.load(sys.stdin)['SessionToken'])")

podman run --rm --entrypoint="" -v "\$STAGE/repo":/w9:Z -w /w9 \\
  -e DBSERVER="\$DBSERVER" -e DBPORT="\$DBPORT" -e DBNAME="\$DBNAME" \\
  -e RAPID_DB_SECRET_ID="\$DBSECRET" \\
  -e AWS_DEFAULT_REGION=us-east-1 \\
  -e AWS_ACCESS_KEY_ID="\$AKI" \\
  -e AWS_SECRET_ACCESS_KEY="\$SAK" \\
  -e AWS_SESSION_TOKEN="\$STK" \\
  -e RAPID_RECORDS_BUCKET=roman-rapid-records \\
  -e RAPID_DIAGNOSTICS_BUCKET=roman-rapid-diagnostics \\
  -e RAPID_RECORDS_PREFIX=attempts \\
  -e RAPID_MANIFEST_BUCKET=roman-rapid-products \\
  -e RAPID_MANIFEST_PREFIX=submissions \\
  -e RAPID_IMAGE_DIGEST="${IMAGE#*@}" \\
  -e RAPID_RELEASE_IDENTITY="\${RAPID_RELEASE_IDENTITY:-smdc-7655dcc}" \\
  --network host \\
  "$IMAGE" python3.11 -m pipeline.test.live_w9_ramp "$PHASE" "$CAP" "$TAG" \\
  > "\$STAGE/ramp.log" 2>&1
rc=\$?
# The summary is the ONE line this step exists to produce, and it is printed
# LAST -- so a tail window is exactly the wrong way to carry it. At cap 90 the
# submitter emits two log lines per unit and the summary fell outside a
# tail -120, which read as "the step produced no summary" when in fact it had
# submitted 90 children successfully (sims campaign W3, 2026-08-15). Grep the
# summary out of the full log FIRST, then show a bounded tail for context.
grep -a 'W9-RAMP-SUMMARY' "\$STAGE/ramp.log" || echo "!! no W9-RAMP-SUMMARY line in the step's output"
echo "----- last 60 lines for context -----"
tail -60 "\$STAGE/ramp.log"
echo ">> ramp step exit code: \$rc"
[ "\$rc" -eq 0 ] || exit 1
echo "W9-RAMP-STEP-OK"
REMOTE
)

remote_b64=$(printf '%s' "$remote_script" | base64 | tr -d '\n')
params=$(mktemp "${TMPDIR:-/tmp}/w9-ramp-params.XXXXXX")
printf '{"commands":["echo %s | base64 -d | bash"]}' "$remote_b64" > "$params"

echo
echo ">> running the ramp step on rapid-admin via SSM..."
cmd=$(aws ssm send-command --instance-ids "$admin_id" \
    --document-name "AWS-RunShellScript" --timeout-seconds 3600 \
    --parameters "file://$params" \
    --query 'Command.CommandId' --output text)
echo ">> command id: $cmd"
rm -f "$params"

aws ssm wait command-executed --command-id "$cmd" --instance-id "$admin_id" 2>/dev/null || true
status=$(aws ssm get-command-invocation --command-id "$cmd" --instance-id "$admin_id" \
    --query Status --output text)
echo "----- STDOUT -----"
aws ssm get-command-invocation --command-id "$cmd" --instance-id "$admin_id" \
    --query StandardOutputContent --output text
echo "----- STDERR -----"
aws ssm get-command-invocation --command-id "$cmd" --instance-id "$admin_id" \
    --query StandardErrorContent --output text
echo "----- STATUS: $status -----"

aws s3 rm "s3://$BUCKET/$PREFIX/" --recursive --only-show-errors 2>/dev/null || true

[ "$status" = "Success" ] || { echo "!! the ramp step did not succeed" >&2; exit 1; }
