#!/usr/bin/env bash
# run-fixe-on-rapid-admin.sh — run every suite the FixE round-4 fixes
# touch, in-image on rapid-admin.
#
# Same shape and same reasons as FixD's runner beside it: psycopg2 is not
# installed on the laptop and would not be the right psycopg2 if it were,
# pytest is not there at all, and team policy puts containers on
# rapid-admin, never the laptop (Ben, 2026-07-16).
#
# WHAT FIXE'S ROUND-4 FIXES REACH, suite by suite:
#
#   pipeline/test — the VPO's per-phase routing, asserted at the
#     submit-call boundary so no submission is needed to know which queue
#     a phase would reach, and the registrar factory binding to the
#     pass's own connection (#1, #2).
#   database/modules/utils/test — the overlap query's exclusion clause:
#     what each branch emits, and that its placeholder count matches its
#     parameters (#3). What the SERVER makes of that text is a separate
#     run — see run-fixe-overlap-sql-on-rapid-db.sh, which executes both
#     branches against a real PostgreSQL, because a mocked cursor accepts
#     a string that cannot parse and that is exactly how #3 survived.
#   pipeline/stages/test — a checksumless replay decided by fetching and
#     hashing the bytes rather than by comparing lengths (#4).
#   pipeline/reconciler/test — the bundle before EVERY close, including
#     never-started and never-resolved attempts, and before the closure
#     record rather than after it (#5).
#   submission/test — the gathering side of the exclusion sentinel (#3).
#
# The remaining suites run because these fixes reach into the attempt
# writer, the registration consumer and the runtime too.
#
# Usage: run-fixe-on-rapid-admin.sh [image-ref]
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

IMAGE=${1:?pass the image ref to test}
BUCKET="rapid-build-artifacts-${ACCOUNT}"
RUN_ID="fixe-tests-$(date -u +%Y%m%dT%H%M%SZ)"
PREFIX="db-migrations-staging/${RUN_ID}"

id=$(aws ssm describe-instance-information \
    --filters "Key=tag:Name,Values=rapid-admin" \
    --query 'InstanceInformationList[0].InstanceId' --output text)
[ -n "$id" ] && [ "$id" != "None" ] || { echo "!! rapid-admin not found" >&2; exit 1; }
echo ">> target: rapid-admin ($id)"
echo ">> image: $IMAGE"

tarball=$(mktemp "${TMPDIR:-/tmp}/fixe-tests.XXXXXX.tar.gz")
trap 'rm -f "$tarball"' EXIT
tar czf "$tarball" \
    cdf \
    database/modules/utils \
    modules \
    observability \
    pipeline \
    submission
echo ">> staged tarball: $(du -h "$tarball" | cut -f1)"

aws s3 cp "$tarball" "s3://$BUCKET/$PREFIX/fixe-tests.tar.gz" --only-show-errors
sha=$(shasum -a 256 "$tarball" | awk '{print $1}')
echo ">> sha256: $sha"

remote_script=$(cat <<REMOTE
set -uo pipefail
STAGE=\$(mktemp -d)
trap 'rm -rf "\$STAGE"' EXIT
aws s3 cp "s3://$BUCKET/$PREFIX/fixe-tests.tar.gz" "\$STAGE/fixe-tests.tar.gz" --only-show-errors
echo "$sha  \$STAGE/fixe-tests.tar.gz" > "\$STAGE/manifest.sha256"
sha256sum -c "\$STAGE/manifest.sha256" || { echo "!! checksum FAILED" >&2; exit 1; }
mkdir -p "\$STAGE/repo"
tar xzf "\$STAGE/fixe-tests.tar.gz" -C "\$STAGE/repo"

aws ecr get-login-password --region us-east-1 \\
  | podman login --username AWS --password-stdin ${ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com >/dev/null

run() {
  podman run --rm --entrypoint="" -v "\$STAGE/repo":/fixe:Z -w /fixe "$IMAGE" "\$@"
}

echo
echo "=== FixE reconciler suite (bundle before every close) ==="
run python3.11 -m pytest pipeline/reconciler/test -q 2>&1 | tail -15
rec=\${PIPESTATUS[0]}
echo ">> reconciler suite exit code: \$rec"

echo
echo "=== FixE VPO/seams suite (phase routing, registrar connection) ==="
run python3.11 -m pytest pipeline/test -q 2>&1 | tail -15
seam=\${PIPESTATUS[0]}
echo ">> seams suite exit code: \$seam"

echo
echo "=== entrypoint suite ==="
run python3.11 -m unittest discover -s pipeline/entrypoints/test -t . 2>&1 | tail -12
ep=\${PIPESTATUS[0]}
echo ">> entrypoint suite exit code: \$ep"

echo
echo "=== stages suite (checksumless replay by bytes) ==="
run python3.11 -m unittest discover -s pipeline/stages/test -t . 2>&1 | tail -12
st=\${PIPESTATUS[0]}
echo ">> stages suite exit code: \$st"

echo
echo "=== runtime unit suite ==="
run python3.11 -m unittest discover -s pipeline/runtime/test -t . 2>&1 | tail -12
rt=\${PIPESTATUS[0]}
echo ">> runtime unit suite exit code: \$rt"

echo
echo "=== submission suite (routes, vocabulary, retry contract) ==="
run python3.11 -m pytest submission/test -q 2>&1 | tail -12
sub=\${PIPESTATUS[0]}
echo ">> submission suite exit code: \$sub"

echo
echo "=== observability suite (attempt writer, registration, integration) ==="
run python3.11 -m pytest observability/test -q 2>&1 | tail -12
obs=\${PIPESTATUS[0]}
echo ">> observability suite exit code: \$obs"

echo
echo "=== registration consumer suite ==="
run python3.11 -m pytest pipeline/registration/test -q 2>&1 | tail -12
reg=\${PIPESTATUS[0]}
echo ">> registration suite exit code: \$reg"

echo
echo "=== connection helper + rapid_db (overlap exclusion clause) ==="
run python3.11 -m unittest discover -s database/modules/utils/test -t . 2>&1 | tail -8
w3=\${PIPESTATUS[0]}
echo ">> database suite exit code: \$w3"

echo
if [ "\$rec" -eq 0 ] && [ "\$seam" -eq 0 ] && [ "\$ep" -eq 0 ] && [ "\$st" -eq 0 ] \\
   && [ "\$rt" -eq 0 ] && [ "\$sub" -eq 0 ] && [ "\$obs" -eq 0 ] \\
   && [ "\$reg" -eq 0 ] && [ "\$w3" -eq 0 ]; then
  echo "FIXE-UNITS-OK"
else
  echo "!! suites FAILED (rec=\$rec seam=\$seam ep=\$ep st=\$st rt=\$rt sub=\$sub obs=\$obs reg=\$reg db=\$w3)" >&2
  exit 1
fi
REMOTE
)

remote_b64=$(printf '%s' "$remote_script" | base64 | tr -d '\n')
params=$(mktemp "${TMPDIR:-/tmp}/fixe-params.XXXXXX")
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
echo "FIXE-VERIFICATION-OK"
