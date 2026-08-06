#!/usr/bin/env bash
# run-fixe-overlap-sql-on-rapid-db.sh — execute BOTH branches of the overlap
# query against the real PostgreSQL on rapid-db.
#
# Same shape and same reasons as FixD's schema probe beside it: the probe
# runs on RAPID-DB, not rapid-admin, because the pooler is on that host —
# port 6432 is pgbouncer and 5432 is not reachable off-host by design
# (pooler-only, one door). No credentials are passed in: boto3 inside the
# container picks up rapid-db-instance-role from IMDS and does the real
# Secrets Manager fetch.
#
# WHY A SERVER IS REQUIRED. Round-4 finding #3 is entirely in what the
# DATABASE makes of the query text: the "no exclusion" branch emitted
# `a.rid is not %s` and bound the string 'null' through it, so PostgreSQL
# received the invalid `a.rid IS NOT 'null'` and rejected the whole query.
# Every existing test of that path stubs the database method, and a mocked
# cursor accepts any string at all — including one that cannot parse. That
# is precisely how a query that could not run survived a green suite, so
# the fix is proven by executing it, not by asserting its shape (the shape
# is checked too, cheaply, in database/modules/utils/test/test_rapid_db.py).
#
# READ-ONLY, against the REAL l2files. An earlier shape built its own
# fixture schema; rapid_pipeline has no CREATE privilege on the database
# (correctly — least-privilege service role, and the first run of this
# probe is what established that), and the fixture was never the point.
# The deployed table with the deployed q3c extension is a better witness
# than a stand-in: it is the actual schema the query names. Both calls are
# SELECTs and the transaction is rolled back before closing.
#
# Usage: run-fixe-overlap-sql-on-rapid-db.sh [image-ref]
# Exits 0 only if every assertion in the probe passed.

set -euo pipefail
cd "$(dirname "$0")/../.."   # repo root of the worktree (submission/test/ is two deep)

: "${AWS_PROFILE:=rapid-admin}" ; export AWS_PROFILE
: "${AWS_DEFAULT_REGION:=us-east-1}" ; export AWS_DEFAULT_REGION

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
: "${RAPID_ACCOUNT:?set RAPID_ACCOUNT to the SMDC account id}"
[ "$ACCOUNT" = "$RAPID_ACCOUNT" ] \
  || { echo "!! wrong account: $ACCOUNT (expected $RAPID_ACCOUNT)" >&2; exit 1; }

IMAGE=${1:?pass the image ref to run the probe in}
BUCKET="rapid-build-artifacts-${ACCOUNT}"
RUN_ID="fixe-overlap-$(date -u +%Y%m%dT%H%M%SZ)"
PREFIX="db-migrations-staging/${RUN_ID}"

db_id=$(aws ssm describe-instance-information \
    --filters "Key=tag:Name,Values=rapid-db" \
    --query 'InstanceInformationList[0].InstanceId' --output text)
[ -n "$db_id" ] && [ "$db_id" != "None" ] \
  || { echo "!! rapid-db not found in SSM inventory" >&2; exit 1; }
echo ">> target: rapid-db ($db_id)"
echo ">> image: $IMAGE"

tarball=$(mktemp "${TMPDIR:-/tmp}/fixe-overlap.XXXXXX.tar.gz")
trap 'rm -f "$tarball"' EXIT
tar czf "$tarball" \
    cdf \
    database/modules/utils \
    modules \
    observability \
    pipeline \
    submission
echo ">> staged tarball: $(du -h "$tarball" | cut -f1)"

aws s3 cp "$tarball" "s3://$BUCKET/$PREFIX/fixe-overlap.tar.gz" --only-show-errors
sha=$(shasum -a 256 "$tarball" | awk '{print $1}')
echo ">> sha256: $sha"

remote_script=$(cat <<REMOTE
set -uo pipefail
STAGE=\$(mktemp -d)
trap 'rm -rf "\$STAGE"' EXIT
aws s3 cp "s3://$BUCKET/$PREFIX/fixe-overlap.tar.gz" "\$STAGE/fixe-overlap.tar.gz" --only-show-errors
echo "$sha  \$STAGE/fixe-overlap.tar.gz" > "\$STAGE/manifest.sha256"
sha256sum -c "\$STAGE/manifest.sha256" || { echo "!! checksum FAILED" >&2; exit 1; }
mkdir -p "\$STAGE/repo"
tar xzf "\$STAGE/fixe-overlap.tar.gz" -C "\$STAGE/repo"

aws ecr get-login-password --region us-east-1 \\
  | podman login --username AWS --password-stdin ${ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com >/dev/null

# The pooler is on this host. 6432 is pgbouncer; 5432 is not reachable
# off-host by design.
DBSERVER=127.0.0.1
DBPORT=6432
DBNAME=rapid
DBSECRET=rapid/db/service/pipeline
echo ">> DBSERVER=\$DBSERVER DBPORT=\$DBPORT DBNAME=\$DBNAME SECRET=\$DBSECRET"

podman run --rm --entrypoint="" -v "\$STAGE/repo":/fixe:Z -w /fixe \\
  -e DBSERVER="\$DBSERVER" -e DBPORT="\$DBPORT" -e DBNAME="\$DBNAME" \\
  -e RAPID_DB_SECRET_ID="\$DBSECRET" \\
  -e AWS_DEFAULT_REGION=us-east-1 \\
  -e RAPID_RECORDS_BUCKET=roman-rapid-records \\
  -e RAPID_DIAGNOSTICS_BUCKET=roman-rapid-diagnostics \\
  -e RAPID_RECORDS_PREFIX=attempts \\
  --network host \\
  "$IMAGE" python3.11 -m submission.test.live_fixe_overlap_sql 2>&1 | head -400
live=\${PIPESTATUS[0]}
echo ">> live probe exit code: \$live"
[ "\$live" -eq 0 ] || exit 1
echo "FIXE-OVERLAP-SQL-OK"
REMOTE
)

remote_b64=$(printf '%s' "$remote_script" | base64 | tr -d '\n')
params=$(mktemp "${TMPDIR:-/tmp}/fixe-overlap-params.XXXXXX")
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
echo "FIXE-OVERLAP-VERIFICATION-OK"
