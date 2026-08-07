#!/usr/bin/env bash
#
# run-operational-tests-on-rapid-admin.sh — the operational-layer suite
# (scripts/run-operational-tests.sh) inside the pipeline image, on
# rapid-admin, via SSM.
#
# Why not on the laptop: psycopg2 and the rest of the pipeline's dependency
# set are not installed there and would not be the right ones if they were —
# the image is where that set actually lives — and team policy puts
# containers on rapid-admin, never the laptop and never rapid-rusholme
# (Ben, 2026-07-16). The existing per-module runners beside each test package
# each stage their own subtree; this one stages the whole repo, because
# run-operational-tests.sh spans 35 modules across six packages.
#
# Usage: run-operational-tests-on-rapid-admin.sh [image-ref]
# Exits 0 only if the suite prints RESULT: PASS with exit code 0.

set -euo pipefail
cd "$(dirname "$0")/.."   # repo root of the worktree

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
RUN_ID="op-tests-$(date -u +%Y%m%dT%H%M%SZ)"
PREFIX="db-migrations-staging/${RUN_ID}"

id=$(aws ssm describe-instance-information \
    --filters "Key=tag:Name,Values=rapid-admin" \
    --query 'InstanceInformationList[0].InstanceId' --output text)
[ -n "$id" ] && [ "$id" != "None" ] || { echo "!! rapid-admin not found" >&2; exit 1; }
echo ">> target: rapid-admin ($id)"
echo ">> image: $IMAGE"

tarball=$(mktemp "${TMPDIR:-/tmp}/op-tests.XXXXXX.tar.gz")
trap 'rm -f "$tarball"' EXIT
# Every package the 35 modules import, plus the release content they read
# and the runner itself. `sims/`, `RuBR/`, `docs/` and `c/` are excluded:
# nothing in the operational suite imports them.
# --no-xattrs: macOS tar otherwise stamps com.apple.provenance on every
# member, and GNU tar on the far side warns once per file.
tar --no-xattrs -czf "$tarball" \
    scripts/run-operational-tests.sh \
    cdf \
    database \
    modules \
    observability \
    pipeline \
    submission \
    alerts \
    pyproject.toml
echo ">> staged tarball: $(du -h "$tarball" | cut -f1)"

aws s3 cp "$tarball" "s3://$BUCKET/$PREFIX/op-tests.tar.gz" --only-show-errors
sha=$(shasum -a 256 "$tarball" | awk '{print $1}')
echo ">> sha256: $sha"

script=$(cat <<REMOTE
set -euo pipefail
STAGE=\$(mktemp -d)
trap 'rm -rf "\$STAGE"' EXIT
aws s3 cp "s3://$BUCKET/$PREFIX/op-tests.tar.gz" "\$STAGE/op-tests.tar.gz" --only-show-errors
echo "$sha  \$STAGE/op-tests.tar.gz" > "\$STAGE/manifest.sha256"
sha256sum -c "\$STAGE/manifest.sha256" || { echo "!! checksum FAILED" >&2; exit 1; }
mkdir -p "\$STAGE/repo"
# 2>/dev/null: macOS tar writes com.apple.provenance xattrs that GNU tar
# warns about once per file, which is thousands of lines of noise around the
# result this run exists to show.
tar xzf "\$STAGE/op-tests.tar.gz" -C "\$STAGE/repo" 2>/dev/null
chmod +x "\$STAGE/repo/scripts/run-operational-tests.sh"

aws ecr get-login-password --region us-east-1 \\
  | podman login --username AWS --password-stdin ${ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com

echo
echo "=== operational-layer suite (35 modules, one interpreter each) ==="
# RAPID_SW is set because the software root is fail-loud now (O1): the
# suite's science-config tests resolve release content through it, and
# nothing may default it to /code.
podman run --rm --entrypoint="" -v "\$STAGE/repo":/w:Z -w /w \\
  -e RAPID_SW=/w -e AWS_DEFAULT_REGION=us-east-1 "$IMAGE" \\
  ./scripts/run-operational-tests.sh python3.11 2>&1 | tail -60
rc=\${PIPESTATUS[0]}
echo ">> operational suite exit code: \$rc"
exit \$rc
REMOTE
)

b64=$(printf '%s' "$script" | base64 | tr -d '\n')
params=$(mktemp "${TMPDIR:-/tmp}/op-params.XXXXXX")
printf '{"commands":["echo %s | base64 -d | bash"]}' "$b64" > "$params"

echo
echo ">> running the operational suite on rapid-admin via SSM..."
cmd=$(aws ssm send-command --instance-ids "$id" \
    --document-name "AWS-RunShellScript" --timeout-seconds 1800 \
    --parameters "file://$params" \
    --query 'Command.CommandId' --output text)
rm -f "$params"
echo ">> command id: $cmd"

# Polled rather than `aws ssm wait`, which blocks one call for as long as the
# command takes and gives no sign of life meanwhile.
for _ in $(seq 60); do
    status=$(aws ssm get-command-invocation --command-id "$cmd" \
        --instance-id "$id" --query Status --output text 2>/dev/null || echo Pending)
    case "$status" in
        Success|Failed|Cancelled|TimedOut) break ;;
    esac
    sleep 15
done
echo ">> status: $status"

aws ssm get-command-invocation --command-id "$cmd" --instance-id "$id" \
    --query StandardOutputContent --output text
aws ssm get-command-invocation --command-id "$cmd" --instance-id "$id" \
    --query StandardErrorContent --output text >&2

[ "$status" = "Success" ] || { echo "!! operational suite FAILED" >&2; exit 1; }
echo "OPERATIONAL-TESTS-OK"
