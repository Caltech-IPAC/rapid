#!/usr/bin/env bash
#
# brief-f-acceptance-on-rapid-admin.sh — stage this branch and the
# authoritative migration stream to rapid-admin, and run brief F's acceptance
# there via SSM.
#
# The laptop edits files and drives git; nothing runs locally (Ben,
# 2026-08-10). This script stages and drives; every line of actual execution
# happens on rapid-admin, in a scratch footprint that is torn down with it.
#
# Derived from `brief-g-acceptance-on-rapid-admin.sh`, itself derived from
# brief C's, which ran this exact staging path to a green acceptance — the
# only differences are the run-id prefix, the sentinel prefix, and the inner
# runner it invokes. Kept as a sibling rather than parameterized into one
# script because each brief's acceptance is a record of what was run for that
# brief, and a shared script edited in place stops being that record for the
# earlier ones.
#
# Follows `run-operational-tests-on-rapid-admin.sh`'s proven shape exactly:
# S3-staged tarball with a sha256 pinned INSIDE the SSM command (integrity
# never delegated to the bucket), base64 script delivery so nothing needs
# escaping, `get-command-invocation` polled rather than `aws ssm wait` (which
# gives up after ~100s and grades a healthy long command as failed), and
# front-loaded grep-able sentinels because StandardOutputContent is
# tail-truncated at ~24KB.
#
# Usage: brief-f-acceptance-on-rapid-admin.sh [postgres-image-ref]

set -euo pipefail
cd "$(dirname "$0")/.."   # repo root of the worktree

: "${AWS_PROFILE:=rapid-admin}" ; export AWS_PROFILE
: "${AWS_DEFAULT_REGION:=us-east-1}" ; export AWS_DEFAULT_REGION

# The account is derived at runtime, never written down: this repo is PUBLIC
# (Caltech-IPAC/rapid) and .githooks/pre-push hard-blocks the SMDC account
# number with no allowlist.
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
: "${RAPID_ACCOUNT:?set RAPID_ACCOUNT to the SMDC account id}"
[ "$ACCOUNT" = "$RAPID_ACCOUNT" ] \
  || { echo "!! wrong account: $ACCOUNT (expected $RAPID_ACCOUNT)" >&2; exit 1; }

# PostgreSQL 18 + Q3C, the fleet's own image — `003-q3c-and-partition-grant
# .sql` does CREATE EXTENSION q3c, so the stream simply does not apply to an
# image without it. Digest-pinned.
IMAGE=${1:-${ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/rapid-postgres@sha256:ae911258c5ec314d9e0b02946fbc97c6c05c8cb12ecca5ff4b5d6b47295d3420}

# The sibling checkout, READ-ONLY. This worktree lives at
# `<rapid-parent>/rapid-wt-f`, so rapid_systems is one level up beside it —
# the same place the repo-isolation rule expects it and the only thing this
# script ever reads from that repo.
MIGRATIONS_SRC=${MIGRATIONS_SRC:-../rapid_systems/cloudformation/db-migrations}
[ -d "$MIGRATIONS_SRC" ] || { echo "!! no migration stream at $MIGRATIONS_SRC" >&2; exit 1; }
STREAM_REV=$(git -C "$MIGRATIONS_SRC" rev-parse HEAD)

BUCKET="rapid-build-artifacts-${ACCOUNT}"
RUN_ID="brief-f-$(date -u +%Y%m%dT%H%M%SZ)"
PREFIX="db-migrations-staging/${RUN_ID}"

id=$(aws ssm describe-instance-information \
    --filters "Key=tag:Name,Values=rapid-admin" \
    --query 'InstanceInformationList[0].InstanceId' --output text)
[ -n "$id" ] && [ "$id" != "None" ] || { echo "!! rapid-admin not found" >&2; exit 1; }
echo ">> target: rapid-admin ($id)"
echo ">> image: $IMAGE"
echo ">> rapid_systems rev: $STREAM_REV"
echo ">> run id: $RUN_ID"

tarball=$(mktemp "${TMPDIR:-/tmp}/brief-f.XXXXXX.tar.gz")
trap 'rm -f "$tarball"' EXIT

# The repo subset the suites need, plus this branch's drafts. Same exclusions
# as the operational runner (`sims/`, `RuBR/`, `c/` are not imported by either
# tier), with `migrations-draft/` added — the whole point of this run.
# `--exclude='._*'` as well as `--no-xattrs`: on this macOS `tar`, the flag
# stops the xattrs being ARCHIVED but AppleDouble `._<name>` sidecars already
# present on disk are ordinary files and ship anyway. They land beside the
# real sources with a `.py` suffix and binary content, so any tool that walks
# the tree by extension trips over them — brief F's fields-reader scanner did
# exactly that and reported 128 unparseable "sources".
tar --no-xattrs --exclude='._*' -czf "$tarball" \
    scripts \
    cdf \
    database \
    modules \
    observability \
    pipeline \
    submission \
    alerts \
    aws \
    migrations-draft \
    pyproject.toml
echo ">> repo tarball: $(du -h "$tarball" | cut -f1)"

stream=$(mktemp "${TMPDIR:-/tmp}/stream.XXXXXX.tar.gz")
trap 'rm -f "$tarball" "$stream"' EXIT
tar --no-xattrs -czf "$stream" -C "$MIGRATIONS_SRC" .
echo ">> stream tarball: $(du -h "$stream" | cut -f1)"

aws s3 cp "$tarball" "s3://$BUCKET/$PREFIX/repo.tar.gz" --only-show-errors
aws s3 cp "$stream" "s3://$BUCKET/$PREFIX/stream.tar.gz" --only-show-errors
repo_sha=$(shasum -a 256 "$tarball" | awk '{print $1}')
stream_sha=$(shasum -a 256 "$stream" | awk '{print $1}')
echo ">> repo sha256:   $repo_sha"
echo ">> stream sha256: $stream_sha"

# The scratch footprint is one directory under /var/tmp, named by run id, and
# removed by this script's own trap on the far side — success or failure.
script=$(cat <<REMOTE
set -uo pipefail
: "\${HOME:=/root}" ; export HOME
STAGE="/var/tmp/${RUN_ID}"
rm -rf "\$STAGE"; mkdir -p "\$STAGE/repo" "\$STAGE/db-migrations"
trap 'rm -rf "\$STAGE"' EXIT

aws s3 cp "s3://$BUCKET/$PREFIX/repo.tar.gz" "\$STAGE/repo.tar.gz" --only-show-errors
aws s3 cp "s3://$BUCKET/$PREFIX/stream.tar.gz" "\$STAGE/stream.tar.gz" --only-show-errors
printf '%s  %s\n' "$repo_sha" "\$STAGE/repo.tar.gz" > "\$STAGE/manifest.sha256"
printf '%s  %s\n' "$stream_sha" "\$STAGE/stream.tar.gz" >> "\$STAGE/manifest.sha256"
sha256sum -c "\$STAGE/manifest.sha256" || { echo "BRIEF-F-CHECKSUM: FAIL exit=1"; exit 1; }
echo "BRIEF-F-CHECKSUM: PASS exit=0"

tar xzf "\$STAGE/repo.tar.gz" -C "\$STAGE/repo" 2>/dev/null
tar xzf "\$STAGE/stream.tar.gz" -C "\$STAGE/db-migrations" 2>/dev/null
chmod +x "\$STAGE/repo/scripts/"*.sh
echo "BRIEF-F-STREAM-REV: $STREAM_REV"
echo "BRIEF-F-MIGRATIONS: \$(ls \$STAGE/db-migrations/0[0-9][0-9]-*.sql | wc -l) stream, \$(ls \$STAGE/repo/migrations-draft/0[0-9][0-9]-*.sql | wc -l) draft"

aws ecr get-login-password --region us-east-1 \\
  | podman login --username AWS --password-stdin ${ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com >/dev/null 2>&1
podman pull "$IMAGE" >/dev/null 2>&1 || { echo "BRIEF-F-PULL: FAIL exit=1"; exit 1; }
echo "BRIEF-F-PULL: PASS exit=0"

"\$STAGE/repo/scripts/contract-brief-f-on-rapid-admin.sh" "\$STAGE" "$IMAGE" "${RUN_ID}"
rc=\$?
echo "BRIEF-F-RUNNER: exit=\$rc"
exit \$rc
REMOTE
)

b64=$(printf '%s' "$script" | base64 | tr -d '\n')
params=$(mktemp "${TMPDIR:-/tmp}/brief-f-params.XXXXXX")
printf '{"commands":["echo %s | base64 -d | bash"]}' "$b64" > "$params"

echo
echo ">> running brief F acceptance on rapid-admin via SSM..."
cmd=$(aws ssm send-command --instance-ids "$id" \
    --document-name "AWS-RunShellScript" --timeout-seconds 3600 \
    --parameters "file://$params" \
    --query 'Command.CommandId' --output text)
rm -f "$params"
echo ">> command id: $cmd"
echo "$cmd" > "${TMPDIR:-/tmp}/brief-f-last-command-id"

# Polled, never `aws ssm wait command-executed`: that gives up after ~100s
# (20 polls x 5s) and returns with the command still InProgress, so a caller
# reading Status then grades a healthy long command as failed.
status=Pending
for _ in $(seq 120); do
    status=$(aws ssm get-command-invocation --command-id "$cmd" \
        --instance-id "$id" --query Status --output text 2>/dev/null || echo Pending)
    case "$status" in
        Success|Failed|Cancelled|TimedOut) break ;;
    esac
    sleep 20
done
echo ">> status: $status"

aws ssm get-command-invocation --command-id "$cmd" --instance-id "$id" \
    --query StandardOutputContent --output text
aws ssm get-command-invocation --command-id "$cmd" --instance-id "$id" \
    --query StandardErrorContent --output text >&2

# The S3 staging prefix is removed on success; a failed run keeps it, because
# the payload is what a diagnosis would need.
if [ "$status" = "Success" ]; then
    aws s3 rm "s3://$BUCKET/$PREFIX" --recursive --only-show-errors
    echo ">> staging prefix removed: s3://$BUCKET/$PREFIX"
    echo "BRIEF-F-ACCEPTANCE-OK"
    exit 0
fi
echo "!! brief F acceptance FAILED (status $status)" >&2
echo ">> staging prefix RETAINED for diagnosis: s3://$BUCKET/$PREFIX" >&2
exit 1
