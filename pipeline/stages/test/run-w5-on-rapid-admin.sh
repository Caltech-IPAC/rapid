#!/usr/bin/env bash
# run-w5-on-rapid-admin.sh — run the W1+W2+W3+W4+W5 unit suites on AWS,
# inside the pipeline image.
#
# Same shape and same reasons as W4's runner
# (submission/test/run-w4-on-rapid-admin.sh) and W2's before it: psycopg2
# is not installed on the laptop and would not be the right psycopg2 if it
# were, pytest is not there at all, and team policy puts containers on
# rapid-admin, never the laptop (Ben, 2026-07-16).
#
# WHAT W5 ADDS to the suites W4's runner already ran:
#
#   pipeline/entrypoints/test — the dispatching entrypoint: argument
#     parsing, the manifest checksum gate, route rejection on class and on
#     queue, provenance absence, registration refusal.
#   pipeline/stages/test — the stage context and the sequence table.
#   submission/test/test_submit.py gains the scheduler-retry contract
#     assertions (no retryStrategy override, no command override), which
#     ride the existing submission suite.
#
# The two new suites run under unittest, matching pipeline/runtime/test.
# The tarball carries pipeline/ whole rather than pipeline/runtime alone,
# because pipeline/stages imports pipeline/differenceImageSubs and
# pipeline/referenceImageSubs, and modules/ because it imports
# modules/utils/rapid_pipeline_subs.
#
# Usage: run-w5-on-rapid-admin.sh [image-ref]
# Exits 0 only if every suite passes.

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

IMAGE=${1:?pass the image ref to test — W5 changes the image, so there is no safe default}
BUCKET="rapid-build-artifacts-${ACCOUNT}"
RUN_ID="w5-tests-$(date -u +%Y%m%dT%H%M%SZ)"
PREFIX="db-migrations-staging/${RUN_ID}"

id=$(aws ssm describe-instance-information \
    --filters "Key=tag:Name,Values=rapid-admin" \
    --query 'InstanceInformationList[0].InstanceId' --output text)
[ -n "$id" ] && [ "$id" != "None" ] || { echo "!! rapid-admin not found" >&2; exit 1; }
echo ">> target: rapid-admin ($id)"
echo ">> image: $IMAGE"

tarball=$(mktemp "${TMPDIR:-/tmp}/w5-tests.XXXXXX.tar.gz")
trap 'rm -f "$tarball"' EXIT
tar czf "$tarball" \
    cdf \
    database/modules/utils/rapid_db.py \
    database/modules/utils/rapid_db_connect.py \
    database/modules/utils/test \
    modules \
    observability \
    pipeline \
    submission
echo ">> staged tarball: $(du -h "$tarball" | cut -f1)"

aws s3 cp "$tarball" "s3://$BUCKET/$PREFIX/w5-tests.tar.gz" --only-show-errors
sha=$(shasum -a 256 "$tarball" | awk '{print $1}')
echo ">> sha256: $sha"

remote_script=$(cat <<REMOTE
set -uo pipefail
STAGE=\$(mktemp -d)
trap 'rm -rf "\$STAGE"' EXIT
aws s3 cp "s3://$BUCKET/$PREFIX/w5-tests.tar.gz" "\$STAGE/w5-tests.tar.gz" --only-show-errors
echo "$sha  \$STAGE/w5-tests.tar.gz" > "\$STAGE/manifest.sha256"
sha256sum -c "\$STAGE/manifest.sha256" || { echo "!! checksum FAILED" >&2; exit 1; }
mkdir -p "\$STAGE/repo"
tar xzf "\$STAGE/w5-tests.tar.gz" -C "\$STAGE/repo"

aws ecr get-login-password --region us-east-1 \\
  | podman login --username AWS --password-stdin ${ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com >/dev/null

run() {
  podman run --rm --entrypoint="" -v "\$STAGE/repo":/w5:Z -w /w5 "$IMAGE" "\$@"
}

echo
echo "=== W5 entrypoint suite ==="
run python3.11 -m unittest discover -s pipeline/entrypoints/test -t . 2>&1 | tail -12
ep=\${PIPESTATUS[0]}
echo ">> entrypoint suite exit code: \$ep"

echo
echo "=== W5 stages suite ==="
run python3.11 -m unittest discover -s pipeline/stages/test -t . 2>&1 | tail -12
st=\${PIPESTATUS[0]}
echo ">> stages suite exit code: \$st"

echo
echo "=== W2+W4 runtime unit suite ==="
run python3.11 -m unittest discover -s pipeline/runtime/test -t . 2>&1 | tail -12
rt=\${PIPESTATUS[0]}
echo ">> runtime unit suite exit code: \$rt"

echo
echo "=== W4+W5 submission suite (routes, vocabulary, retry contract) ==="
run python3.11 -m pytest submission/test -q 2>&1 | tail -12
sub=\${PIPESTATUS[0]}
echo ">> submission suite exit code: \$sub"

echo
echo "=== W1 connection helper ==="
run python3.11 -m unittest database.modules.utils.test.test_rapid_db_connect 2>&1 | tail -8
w1a=\${PIPESTATUS[0]}
echo ">> connection-helper suite exit code: \$w1a"

echo
echo "=== W1 attempt writer ==="
run python3.11 -m unittest observability.test.test_attempts 2>&1 | tail -8
w1b=\${PIPESTATUS[0]}
echo ">> attempt-writer suite exit code: \$w1b"

echo
echo "=== W1 submission integration ==="
run python3.11 -m unittest observability.test.test_submission_integration 2>&1 | tail -8
w1c=\${PIPESTATUS[0]}
echo ">> submission-integration suite exit code: \$w1c"

echo
echo "=== W3 rapid_db parameterization ==="
run python3.11 -m unittest discover -s database/modules/utils/test -t . 2>&1 | tail -8
w3=\${PIPESTATUS[0]}
echo ">> W3 suite exit code: \$w3"

echo
echo "=== W5 deletion proof: no runSingle monoliths, no payload .sh ==="
run sh -c 'ls pipeline/awsBatchSubmitJobs_runSingle* 2>/dev/null && echo "MONOLITHS PRESENT" && exit 1
ls pipeline/*.sh 2>/dev/null && echo "PAYLOAD WRAPPERS PRESENT" && exit 1
echo "no runSingle monoliths, no .sh in pipeline/"'
del=\${PIPESTATUS[0]}
echo ">> deletion proof exit code: \$del"

echo
echo "=== W5 grep proof: no execute_command, no terminating_exitcode ==="
run sh -c 'if grep -rn "execute_command" --include="*.py" pipeline modules database submission observability \\
     | grep -v "pipeline/runtime/process.py" | grep -v "^Binary"; then
     echo "EXECUTE_COMMAND CALL SITES REMAIN"; exit 1; fi
   if grep -rln "terminating_exitcode" --include="*.py" pipeline modules database submission observability; then
     echo "TERMINATING_EXITCODE REMAINS"; exit 1; fi
   echo "zero execute_command call sites, zero terminating_exitcode"'
grp=\${PIPESTATUS[0]}
echo ">> grep proof exit code: \$grp"

echo
if [ "\$ep" -eq 0 ] && [ "\$st" -eq 0 ] && [ "\$rt" -eq 0 ] && [ "\$sub" -eq 0 ] \\
   && [ "\$w1a" -eq 0 ] && [ "\$w1b" -eq 0 ] && [ "\$w1c" -eq 0 ] && [ "\$w3" -eq 0 ] \\
   && [ "\$del" -eq 0 ] && [ "\$grp" -eq 0 ]; then
  echo "W5-UNITS-OK"
else
  echo "!! suites FAILED (ep=\$ep st=\$st rt=\$rt sub=\$sub w1a=\$w1a w1b=\$w1b w1c=\$w1c w3=\$w3 del=\$del grep=\$grp)" >&2
  exit 1
fi
REMOTE
)

remote_b64=$(printf '%s' "$remote_script" | base64 | tr -d '\n')
params=$(mktemp "${TMPDIR:-/tmp}/w5-params.XXXXXX")
printf '{"commands":["echo %s | base64 -d | bash"]}' "$remote_b64" > "$params"

echo
echo ">> running suites on rapid-admin via SSM..."
cmd=$(aws ssm send-command --instance-ids "$id" \
    --document-name "AWS-RunShellScript" --timeout-seconds 1800 \
    --parameters "file://$params" \
    --query 'Command.CommandId' --output text)
echo ">> command id: $cmd"
rm -f "$params"

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

[ "$status" = "Success" ] || { echo "!! SSM invocation did not succeed" >&2; exit 1; }
echo "W5-VERIFICATION-OK"
