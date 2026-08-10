#!/usr/bin/env bash
#
# Run the contract tier on rapid-admin, against a throwaway PostgreSQL built
# from the authoritative migration stream.
#
# This is the ACCEPTANCE-EQUIVALENT of the CI job (brief B: "If the repo secret
# is not yet provisioned, the same run demonstrated on rapid-admin via SSM is
# acceptance-equivalent"). It runs the SAME `scripts/run-contract-tests.sh`
# against the SAME migration stream at the SAME pinned revision; only the host
# and port differ, which is exactly what the location-parameterized fixture
# was built to make irrelevant.
#
# Runs ON the host, staged there by the caller. It takes no AWS actions of its
# own beyond the optional transcript upload, so it needs no role and reads no
# secret — the throwaway cluster's superuser password is generated here and
# never leaves the host, and never enters an SSM command's content (SSM
# command history is readable for ~30 days with no delete API).
#
# Usage: contract-on-rapid-admin.sh <stage-dir> <image> <run-id>
#   stage-dir : holds `db-migrations/` (the stream) and `repo/` (this repo)
#   image     : the PostgreSQL+Q3C image, digest-pinned
#   run-id    : namespaces the container and the scratch footprint
#
# TEARDOWN IS THIS SCRIPT'S OWN: the container is removed on exit, success or
# failure, by the trap. rapid-admin is a SHARED team host — nothing here ever
# touches an existing container, an existing database, or rapid-db.

set -u

STAGE_DIR=${1:?usage: contract-on-rapid-admin.sh <stage-dir> <image> <run-id>}
IMAGE=${2:?usage: contract-on-rapid-admin.sh <stage-dir> <image> <run-id>}
RUN_ID=${3:?usage: contract-on-rapid-admin.sh <stage-dir> <image> <run-id>}

: "${HOME:=/root}"          # AWS-RunShellScript runs with no login environment
export HOME

CONTAINER="brief-b-pg-${RUN_ID}"
DBNAME=rapid
# A nonstandard port: rapid-admin is shared, and 5432 may belong to something
# else. Derived from the run id so two runs cannot collide on it either.
PGPORT_HOST=${PGPORT_HOST:-55432}
PGPW="brief-b-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"

LOG="${STAGE_DIR}/contract-transcript.log"
: > "$LOG"

cleanup() {
    rc=$?
    echo ">> cleanup: removing throwaway container $CONTAINER"
    podman rm -f "$CONTAINER" >/dev/null 2>&1 || true
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

echo "BRIEF-B-RUN-ID: $RUN_ID"
echo "BRIEF-B-IMAGE: $IMAGE"

# Never inherit a stale container of our own name.
podman rm -f "$CONTAINER" >/dev/null 2>&1 || true

echo ">> starting throwaway PostgreSQL as $CONTAINER on port $PGPORT_HOST"
if ! podman run -d --name "$CONTAINER" \
        -e POSTGRES_PASSWORD="$PGPW" \
        -e POSTGRES_DB=postgres \
        -p "127.0.0.1:${PGPORT_HOST}:5432" \
        "$IMAGE" >>"$LOG" 2>&1; then
    echo "BRIEF-B-CONTAINER: FAIL exit=1 (could not start)"
    tail -20 "$LOG"
    exit 1
fi
echo "BRIEF-B-CONTAINER: PASS exit=0 ($CONTAINER)"

# --- the suite, through the SAME script CI runs -----------------------------
# psql and python run on the HOST against the container's published port, so
# `run-contract-tests.sh` is byte-identical to the CI invocation. Nothing here
# reaches into the container to do work the CI path does differently.
cd "${STAGE_DIR}/repo" || exit 2

export PGHOST=127.0.0.1
export PGPORT="$PGPORT_HOST"
export PGUSER=postgres
export PGPASSWORD="$PGPW"
export PGDATABASE="$DBNAME"
export CONTRACT_LOG="${STAGE_DIR}/contract-pytest.log"

./scripts/run-contract-tests.sh "${STAGE_DIR}/db-migrations"
suite_rc=$?
echo "BRIEF-B-CONTRACT-SUITE: exit=$suite_rc"

# --- the stubbed tier must stay green, unchanged ----------------------------
# Required outcome 3. Run on the same host, in the same checkout, right after
# the contract tier — a packaging change that broke the stub tier's per-module
# interpreter isolation would show up here and nowhere else.
./scripts/run-operational-tests.sh >"${STAGE_DIR}/stub-tier.log" 2>&1
stub_rc=$?
tail -4 "${STAGE_DIR}/stub-tier.log"
echo "BRIEF-B-STUB-TIER: exit=$stub_rc"

if [ "$suite_rc" -eq 0 ] && [ "$stub_rc" -eq 0 ]; then
    echo "BRIEF-B-OVERALL: PASS exit=0"
    exit 0
fi
echo "BRIEF-B-OVERALL: FAIL exit=1 (contract=$suite_rc stub=$stub_rc)"
exit 1
