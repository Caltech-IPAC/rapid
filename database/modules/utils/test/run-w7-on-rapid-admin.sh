#!/usr/bin/env bash
# run-w7-on-rapid-admin.sh — run W7's tessellation suites inside the
# pipeline image on rapid-admin.
#
# Same shape and same reasons as the W1 and W3 runners beside it: the
# image is where the pipeline's dependency set actually lives, and team
# policy puts containers on rapid-admin, never the laptop (Ben,
# 2026-07-16).
#
# W7's suite needs only numpy, so unlike the W1/W3 suites it also runs on
# a laptop with numpy installed. It is run in-image anyway, because "the
# tests pass somewhere" is not the claim worth making — the claim is that
# they pass where the code runs.
#
# The cross-repo parity check and the SQLite equivalence comparison both
# SKIP in-image, and say so: rapid_systems is not in the container, and
# the 1.4 GiB legacy database is exactly what W7 stopped baking. Their
# evidence is the recorded run in rapid_systems
# tools/tessellation/certification-2026-08-06.txt, plus a local run with
# ROMANTESSELLATIONDBNAME pointed at a copy of the artifact.
#
# Usage: run-w7-on-rapid-admin.sh [image-ref]
# Exits 0 only if the suite passes.

set -euo pipefail
cd "$(dirname "$0")/../../../.."   # repo root of the worktree

: "${AWS_PROFILE:=rapid-admin}" ; export AWS_PROFILE
: "${AWS_DEFAULT_REGION:=us-east-1}" ; export AWS_DEFAULT_REGION

# The account is derived at runtime, never written down: this repo is
# PUBLIC (Caltech-IPAC/rapid) and .githooks/pre-push hard-blocks the SMDC
# account number with no allowlist.
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
: "${RAPID_ACCOUNT:?set RAPID_ACCOUNT to the SMDC account id}"
[ "$ACCOUNT" = "$RAPID_ACCOUNT" ] \
  || { echo "!! wrong account: $ACCOUNT (expected $RAPID_ACCOUNT)" >&2; exit 1; }

IMAGE=${1:-${ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/rapid-pipeline:latest}
BUCKET="rapid-build-artifacts-${ACCOUNT}"
RUN_ID="w7-tests-$(date -u +%Y%m%dT%H%M%SZ)"
PREFIX="w7-test-staging/${RUN_ID}"

id=$(aws ssm describe-instance-information \
    --filters "Key=tag:Name,Values=rapid-admin" \
    --query 'InstanceInformationList[0].InstanceId' --output text)
[ -n "$id" ] && [ "$id" != "None" ] \
  || { echo "!! rapid-admin not found in SSM inventory" >&2; exit 1; }
echo ">> target: rapid-admin ($id)"
echo ">> image: $IMAGE"

# Stage the working tree the same way the sibling runners do: a tar of
# the tracked files at HEAD, so what runs is what is committed.
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
git archive HEAD -o "$WORK/repo.tar"
aws s3 cp "$WORK/repo.tar" "s3://$BUCKET/$PREFIX/repo.tar" --only-show-errors
SHA=$(shasum -a 256 "$WORK/repo.tar" | awk '{print $1}')
echo ">> staged repo.tar ($SHA)"

remote_script=$(cat <<REMOTE
set -euo pipefail
STAGE=\$(mktemp -d)
trap 'rm -rf "\$STAGE"' EXIT
aws s3 cp "s3://$BUCKET/$PREFIX/repo.tar" "\$STAGE/repo.tar" --only-show-errors
echo "$SHA  \$STAGE/repo.tar" | sed "s|\$STAGE/|\$STAGE/|" > "\$STAGE/manifest"
( cd "\$STAGE" && echo "$SHA  repo.tar" | sha256sum -c - ) \\
  || { echo "!! repo.tar checksum FAILED" >&2; exit 1; }
mkdir -p "\$STAGE/repo" && tar -xf "\$STAGE/repo.tar" -C "\$STAGE/repo"

aws ecr get-login-password --region us-east-1 \\
  | podman login --username AWS --password-stdin ${ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com

echo
echo "=== W7 tessellation suite (pipeline repo) ==="
podman run --rm --entrypoint="" -v "\$STAGE/repo":/w7:Z -w /w7 "$IMAGE" \\
  python3.11 -m unittest database.modules.utils.test.test_roman_tessellation -v 2>&1 | tail -30
w7=\${PIPESTATUS[0]}
echo ">> W7 tessellation suite exit code: \$w7"

echo
echo "=== W7 release pin is readable through W4's reader ==="
podman run --rm --entrypoint="" -v "\$STAGE/repo":/w7:Z -w /w7 -e RAPID_SW=/w7 "$IMAGE" \\
  python3.11 -c "
from pipeline.runtime import science_config as sc
c = sc.load()
t = sc.section(c, 'tessellation')
print('version', t['version'])
print('digest ', t['digest'])
print('nside  ', t['nside'], 'nrows', t['nrows'])
from database.modules.utils import roman_tessellation_db as a
h = a.RomanTessellationClosedForm()
ok = h.check_version(t['version'], t['digest'], t['nside'], t['nrows'])
print('check_version:', ok)
raise SystemExit(0 if ok else 1)
" 2>&1 | tail -10
pin=\${PIPESTATUS[0]}
echo ">> release-pin check exit code: \$pin"

echo
if [ "\$w7" -eq 0 ] && [ "\$pin" -eq 0 ]; then
  echo "W7-UNITS-OK"
else
  echo "!! W7 suites FAILED (w7=\$w7 pin=\$pin)" >&2
  exit 1
fi
REMOTE
)

script_b64=$(printf '%s' "$remote_script" | base64 | tr -d '\n')
params_json=$(mktemp "${TMPDIR:-/tmp}/rapid-w7-params.XXXXXX")
printf '{"commands":["echo %s | base64 -d | bash"]}' "$script_b64" > "$params_json"

echo ">> running on rapid-admin via SSM..."
cmd_id=$(aws ssm send-command --instance-ids "$id" \
    --document-name "AWS-RunShellScript" \
    --timeout-seconds 1800 \
    --parameters "file://$params_json" \
    --query 'Command.CommandId' --output text)
rm -f "$params_json"
echo ">> command id: $cmd_id"

aws ssm wait command-executed --command-id "$cmd_id" --instance-id "$id" 2>/dev/null || true
status=InProgress
for _ in $(seq 120); do
    status=$(aws ssm get-command-invocation --command-id "$cmd_id" \
        --instance-id "$id" --query Status --output text 2>/dev/null || echo Pending)
    case "$status" in
        Success|Failed|Cancelled|TimedOut) break ;;
    esac
    sleep 15
done

echo "----- STDOUT -----"
aws ssm get-command-invocation --command-id "$cmd_id" --instance-id "$id" \
    --query StandardOutputContent --output text
echo "----- STDERR -----"
aws ssm get-command-invocation --command-id "$cmd_id" --instance-id "$id" \
    --query StandardErrorContent --output text
echo "----- STATUS: $status -----"

aws s3 rm "s3://$BUCKET/$PREFIX/" --recursive --only-show-errors || true
[ "$status" = "Success" ]
