#!/usr/bin/env bash
# run-fixd-on-rapid-admin.sh — run every suite the FixD round-3 fixes
# touch, in-image on rapid-admin.
#
# Same shape and same reasons as FixC's runner
# (pipeline/reconciler/test/run-fixc-on-rapid-admin.sh) and FixA's before
# it: psycopg2 is not installed on the laptop and would not be the right
# psycopg2 if it were, pytest is not there at all, and team policy puts
# containers on rapid-admin, never the laptop (Ben, 2026-07-16).
#
# WHAT FIXD'S ROUND-3 FIXES REACH, suite by suite:
#
#   observability/test — the citation TRIPLE: `mark_terminal_after_start`
#     carries the advancing checksum, so a row never cites a sequence-1
#     key beside a sequence-0 checksum (#1).
#   pipeline/reconciler/test — the same triple asserted for coherence
#     rather than as the inconsistent pair the probe used to pin, and
#     bundle reconstruction from the CloudWatch stream for ran-but-
#     bundle-less attempts (#5); run_forever acting on health (#6).
#   pipeline/registration/test — records built through the REAL
#     serialization path rather than hand-constructed bodies, candidate
#     selection restricted to registrable job types, and the single
#     transaction per attempt (#2, #7, #8).
#   pipeline/entrypoints/test — unit facts reaching the record through
#     the provenance path (#2), and registration jobs closing with a
#     disposition that never becomes a candidate (#7).
#   submission/test — the reference gathering window and readiness
#     semantics, and rapid_db exit-code errors surfaced as errors rather
#     than "no work" (#4); conditional creates on coadd-input manifests
#     (#9).
#   pipeline/stages/test — post-process onto publish_products/publish
#     with raising uploads and a consistent S3OBJPRF (#7), and
#     conditional creates on product publication (#9).
#
# The remaining suites run because these fixes reach into the attempt
# writer, the runtime and the VPO too.
#
# Usage: run-fixd-on-rapid-admin.sh [image-ref]
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
RUN_ID="fixc-tests-$(date -u +%Y%m%dT%H%M%SZ)"
PREFIX="db-migrations-staging/${RUN_ID}"

id=$(aws ssm describe-instance-information \
    --filters "Key=tag:Name,Values=rapid-admin" \
    --query 'InstanceInformationList[0].InstanceId' --output text)
[ -n "$id" ] && [ "$id" != "None" ] || { echo "!! rapid-admin not found" >&2; exit 1; }
echo ">> target: rapid-admin ($id)"
echo ">> image: $IMAGE"

tarball=$(mktemp "${TMPDIR:-/tmp}/fixc-tests.XXXXXX.tar.gz")
trap 'rm -f "$tarball"' EXIT
tar czf "$tarball" \
    cdf \
    database/modules/utils \
    modules \
    observability \
    pipeline \
    submission
echo ">> staged tarball: $(du -h "$tarball" | cut -f1)"

aws s3 cp "$tarball" "s3://$BUCKET/$PREFIX/fixc-tests.tar.gz" --only-show-errors
sha=$(shasum -a 256 "$tarball" | awk '{print $1}')
echo ">> sha256: $sha"

remote_script=$(cat <<REMOTE
set -uo pipefail
STAGE=\$(mktemp -d)
trap 'rm -rf "\$STAGE"' EXIT
aws s3 cp "s3://$BUCKET/$PREFIX/fixc-tests.tar.gz" "\$STAGE/fixc-tests.tar.gz" --only-show-errors
echo "$sha  \$STAGE/fixc-tests.tar.gz" > "\$STAGE/manifest.sha256"
sha256sum -c "\$STAGE/manifest.sha256" || { echo "!! checksum FAILED" >&2; exit 1; }
mkdir -p "\$STAGE/repo"
tar xzf "\$STAGE/fixc-tests.tar.gz" -C "\$STAGE/repo"

aws ecr get-login-password --region us-east-1 \\
  | podman login --username AWS --password-stdin ${ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com >/dev/null

run() {
  podman run --rm --entrypoint="" -v "\$STAGE/repo":/fixc:Z -w /fixc "$IMAGE" "\$@"
}

echo
echo "=== FixC reconciler suite ==="
run python3.11 -m pytest pipeline/reconciler/test -q 2>&1 | tail -15
rec=\${PIPESTATUS[0]}
echo ">> reconciler suite exit code: \$rec"

echo
echo "=== FixC seams suite (submission order) ==="
run python3.11 -m pytest pipeline/test -q 2>&1 | tail -15
seam=\${PIPESTATUS[0]}
echo ">> seams suite exit code: \$seam"

echo
echo "=== entrypoint suite ==="
run python3.11 -m unittest discover -s pipeline/entrypoints/test -t . 2>&1 | tail -12
ep=\${PIPESTATUS[0]}
echo ">> entrypoint suite exit code: \$ep"

echo
echo "=== stages suite ==="
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
echo "=== connection helper + rapid_db parameterization ==="
run python3.11 -m unittest discover -s database/modules/utils/test -t . 2>&1 | tail -8
w3=\${PIPESTATUS[0]}
echo ">> database suite exit code: \$w3"

echo
if [ "\$rec" -eq 0 ] && [ "\$seam" -eq 0 ] && [ "\$ep" -eq 0 ] && [ "\$st" -eq 0 ] \\
   && [ "\$rt" -eq 0 ] && [ "\$sub" -eq 0 ] && [ "\$obs" -eq 0 ] \\
   && [ "\$reg" -eq 0 ] && [ "\$w3" -eq 0 ]; then
  echo "FIXC-UNITS-OK"
else
  echo "!! suites FAILED (rec=\$rec seam=\$seam ep=\$ep st=\$st rt=\$rt sub=\$sub obs=\$obs reg=\$reg db=\$w3)" >&2
  exit 1
fi
REMOTE
)

remote_b64=$(printf '%s' "$remote_script" | base64 | tr -d '\n')
params=$(mktemp "${TMPDIR:-/tmp}/fixc-params.XXXXXX")
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
echo "FIXC-VERIFICATION-OK"
