#!/usr/bin/env bash
#
# Run brief C's acceptance on rapid-admin: the contract tier against a
# throwaway PostgreSQL built from the authoritative migration stream PLUS this
# branch's DRAFT migrations, then the stub tier.
#
# This is the ONLY venue where the draft-schema tests actually execute. CI
# builds its database from the authoritative stream alone, which does not
# contain `migrations-draft/`, so the C1 and C3-cancellation tests skip there
# by design — that is what keeps smdc CI green while the drafts are pending as
# rapid_systems change requests. Here, base + drafts are applied in order and
# those tests run.
#
# Derived from `contract-on-rapid-admin.sh` (brief B), which established the
# shape: run ON the host, staged there by the caller, taking no AWS actions of
# its own, generating the throwaway cluster's password locally so it never
# enters an SSM command's content (command history is readable for ~30 days
# with no delete API).
#
# Usage: contract-brief-c-on-rapid-admin.sh <stage-dir> <image> <run-id>
#   stage-dir : holds `db-migrations/` (the stream), `repo/` (this repo)
#   image     : the PostgreSQL+Q3C image, digest-pinned
#   run-id    : namespaces the container and the scratch footprint
#
# TEARDOWN IS THIS SCRIPT'S OWN: the container is removed on exit, success or
# failure, by the trap. rapid-admin is a SHARED team host — nothing here ever
# touches an existing container, an existing database, or rapid-db.

set -u

STAGE_DIR=${1:?usage: contract-brief-c-on-rapid-admin.sh <stage-dir> <image> <run-id>}
IMAGE=${2:?usage: contract-brief-c-on-rapid-admin.sh <stage-dir> <image> <run-id>}
RUN_ID=${3:?usage: contract-brief-c-on-rapid-admin.sh <stage-dir> <image> <run-id>}

: "${HOME:=/root}"          # AWS-RunShellScript runs with no login environment
export HOME

CONTAINER="brief-c-pg-${RUN_ID}"
DBNAME=rapid
# A nonstandard port: rapid-admin is shared and 5432 may belong to something
# else. Distinct from brief B's 55432 so a leftover of that run cannot collide.
PGPORT_HOST=${PGPORT_HOST:-55433}
PGPW="brief-c-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"

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

echo "BRIEF-C-RUN-ID: $RUN_ID"
echo "BRIEF-C-IMAGE: $IMAGE"

podman rm -f "$CONTAINER" >/dev/null 2>&1 || true

echo ">> starting throwaway PostgreSQL as $CONTAINER on port $PGPORT_HOST"
if ! podman run -d --name "$CONTAINER" \
        -e POSTGRES_PASSWORD="$PGPW" \
        -e POSTGRES_DB=postgres \
        -p "127.0.0.1:${PGPORT_HOST}:5432" \
        "$IMAGE" >>"$LOG" 2>&1; then
    echo "BRIEF-C-CONTAINER: FAIL exit=1 (could not start)"
    tail -20 "$LOG"
    exit 1
fi
echo "BRIEF-C-CONTAINER: PASS exit=0 ($CONTAINER)"

cd "${STAGE_DIR}/repo" || exit 2

# The package into a scratch venv — rapid-admin's system python3 carries
# neither pytest nor psycopg2, and `pip install` into a shared host's system
# interpreter is a mutation of everyone else's environment.
python3 -m venv "${STAGE_DIR}/venv" >>"$LOG" 2>&1 || {
    echo "BRIEF-C-VENV: FAIL exit=1"; tail -10 "$LOG"; exit 1; }
VPY="${STAGE_DIR}/venv/bin/python3"
"$VPY" -m pip install --quiet --upgrade pip >>"$LOG" 2>&1

if "$VPY" -m pip install --quiet -e '.[test]' >>"$LOG" 2>&1; then
    echo "BRIEF-C-PIP-INSTALL-E: PASS exit=0"
else
    echo "BRIEF-C-PIP-INSTALL-E: FAIL exit=1"
    tail -25 "$LOG"
    exit 1
fi

export CONTRACT_PYTHON="$VPY"
export PGHOST=127.0.0.1
export PGPORT="$PGPORT_HOST"
export PGUSER=postgres
export PGPASSWORD="$PGPW"
export PGDATABASE="$DBNAME"
export CONTRACT_LOG="${STAGE_DIR}/contract-pytest.log"

# --- PASS 1: the authoritative stream ALONE, drafts absent -------------------
# This is the CI-equivalent run, and it is done FIRST and deliberately: it
# demonstrates that the draft-schema tests SKIP cleanly rather than fail when
# their schema is absent, which is the property that keeps smdc CI green. A
# run that only ever saw base+drafts could not tell a clean skip from a test
# that never existed.
echo "BRIEF-C-PASS1: base stream only (drafts absent — skips expected)"
./scripts/run-contract-tests.sh "${STAGE_DIR}/db-migrations" \
    >"${STAGE_DIR}/pass1.log" 2>&1
pass1_rc=$?
grep -E '^BRIEF-B-(APPLY|SUITE|SCHEMA-MIGRATIONS)' "${STAGE_DIR}/pass1.log" | head -5
# The skip count is the evidence, so it is echoed as its own sentinel rather
# than left inside a 24KB-truncatable transcript.
pass1_line=$(grep -E '^[0-9]+ (passed|failed)|passed|skipped' \
    "${STAGE_DIR}/contract-pytest.log" 2>/dev/null | tail -1)
echo "BRIEF-C-PASS1-RESULT: ${pass1_line:-<no summary line>}"
echo "BRIEF-C-PASS1: exit=$pass1_rc"

# --- Apply the DRAFT migrations, in order ------------------------------------
# Applied on top of the base stream the pass above already built, which is the
# order they would land in as change requests. `ON_ERROR_STOP=1` so a draft
# that does not apply fails here rather than leaving a half-built schema for
# the suite to produce confusing results against.
echo ">> applying DRAFT migrations from migrations-draft/"
drafts_applied=0
for f in migrations-draft/0[0-9][0-9]-*.sql; do
    [ -e "$f" ] || { echo "BRIEF-C-DRAFTS: FAIL exit=2 (no draft files matched)"; exit 2; }
    fn=$(basename "$f")
    if psql -v ON_ERROR_STOP=1 -f "$f" >>"$LOG" 2>&1; then
        drafts_applied=$((drafts_applied + 1))
        echo "BRIEF-C-DRAFT-${fn%%-*}: PASS exit=0 ($fn)"
    else
        rc=$?
        echo "BRIEF-C-DRAFT-${fn%%-*}: FAIL exit=$rc ($fn)"
        grep -E 'ERROR|FATAL' "$LOG" | tail -20
        exit "$rc"
    fi
done
echo "BRIEF-C-DRAFTS: PASS exit=0 ($drafts_applied draft migrations applied)"

# --- PASS 2: base + drafts, the full acceptance ------------------------------
# `run-contract-tests.sh` re-applies the base stream; every file in it is
# idempotent (IF NOT EXISTS / DO $$ guards), so a second application converges
# rather than errors — which is the stream's own design property, exercised
# here rather than assumed.
echo "BRIEF-C-PASS2: base + drafts (the full acceptance)"
export CONTRACT_LOG="${STAGE_DIR}/contract-pytest-pass2.log"
./scripts/run-contract-tests.sh "${STAGE_DIR}/db-migrations" \
    >"${STAGE_DIR}/pass2.log" 2>&1
suite_rc=$?
grep -E '^BRIEF-B-(APPLY|SUITE)' "${STAGE_DIR}/pass2.log" | head -4
pass2_line=$(grep -E 'passed|failed' \
    "${STAGE_DIR}/contract-pytest-pass2.log" 2>/dev/null | tail -1)
echo "BRIEF-C-PASS2-RESULT: ${pass2_line:-<no summary line>}"
if [ "$suite_rc" -ne 0 ]; then
    grep -E '^(FAILED|ERROR)' "${STAGE_DIR}/contract-pytest-pass2.log" | head -20
fi
echo "BRIEF-C-CONTRACT-SUITE: exit=$suite_rc"

# --- The stubbed tier stays green, unchanged ---------------------------------
# RAPID_SW is the installed software root, read fail-loud by the
# science-configuration and definition loaders (check-env-policy.sh forbids a
# compiled-in default).
RAPID_SW="${STAGE_DIR}/repo" ./scripts/run-operational-tests.sh "$VPY" \
    >"${STAGE_DIR}/stub-tier.log" 2>&1
stub_rc=$?
tail -4 "${STAGE_DIR}/stub-tier.log"
echo "BRIEF-C-STUB-TIER: exit=$stub_rc"

if [ "$suite_rc" -eq 0 ] && [ "$stub_rc" -eq 0 ] && [ "$pass1_rc" -eq 0 ]; then
    echo "BRIEF-C-OVERALL: PASS exit=0"
    exit 0
fi
echo "BRIEF-C-OVERALL: FAIL exit=1 (pass1=$pass1_rc contract=$suite_rc stub=$stub_rc)"
exit 1
