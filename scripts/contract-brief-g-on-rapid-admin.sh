#!/usr/bin/env bash
#
# Run brief G's acceptance on rapid-admin: the contract tier against a
# throwaway PostgreSQL built from the authoritative migration stream PLUS this
# branch's DRAFT migrations, then the stub tier.
#
# This is the ONLY venue where the draft-schema tests actually execute. CI
# builds its database from the authoritative stream alone, which does not
# contain `migrations-draft/`, so the operator-contract tests skip there by
# design — that is what keeps smdc CI green while 047 is pending as a
# rapid_systems change request. Here, base + drafts are applied in order and
# those tests run.
#
# Derived from `contract-brief-c-on-rapid-admin.sh`, which established the
# shape and whose comments explain each choice: run ON the host, staged there
# by the caller, taking no AWS actions of its own, generating the throwaway
# cluster's password locally so it never enters an SSM command's content
# (command history is readable for ~30 days with no delete API).
#
# Usage: contract-brief-g-on-rapid-admin.sh <stage-dir> <image> <run-id>
#   stage-dir : holds `db-migrations/` (the stream), `repo/` (this repo)
#   image     : the PostgreSQL+Q3C image, digest-pinned
#   run-id    : namespaces the container and the scratch footprint
#
# TEARDOWN IS THIS SCRIPT'S OWN: the container is removed on exit, success or
# failure, by the trap. rapid-admin is a SHARED team host — nothing here ever
# touches an existing container, an existing database, or rapid-db.

set -u

STAGE_DIR=${1:?usage: contract-brief-g-on-rapid-admin.sh <stage-dir> <image> <run-id>}
IMAGE=${2:?usage: contract-brief-g-on-rapid-admin.sh <stage-dir> <image> <run-id>}
RUN_ID=${3:?usage: contract-brief-g-on-rapid-admin.sh <stage-dir> <image> <run-id>}

: "${HOME:=/root}"          # AWS-RunShellScript runs with no login environment
export HOME

CONTAINER="brief-g-pg-${RUN_ID}"
DBNAME=rapid
# A nonstandard port, distinct from brief B's 55432 and brief C's 55433 so a
# leftover of either run cannot collide with this one.
PGPORT_HOST=${PGPORT_HOST:-55434}
PGPW="brief-g-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"

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

echo "BRIEF-G-RUN-ID: $RUN_ID"
echo "BRIEF-G-IMAGE: $IMAGE"

podman rm -f "$CONTAINER" >/dev/null 2>&1 || true

echo ">> starting throwaway PostgreSQL as $CONTAINER on port $PGPORT_HOST"
if ! podman run -d --name "$CONTAINER" \
        -e POSTGRES_PASSWORD="$PGPW" \
        -e POSTGRES_DB=postgres \
        -p "127.0.0.1:${PGPORT_HOST}:5432" \
        "$IMAGE" >>"$LOG" 2>&1; then
    echo "BRIEF-G-CONTAINER: FAIL exit=1 (could not start)"
    tail -20 "$LOG"
    exit 1
fi
echo "BRIEF-G-CONTAINER: PASS exit=0 ($CONTAINER)"

cd "${STAGE_DIR}/repo" || exit 2

python3 -m venv "${STAGE_DIR}/venv" >>"$LOG" 2>&1 || {
    echo "BRIEF-G-VENV: FAIL exit=1"; tail -10 "$LOG"; exit 1; }
VPY="${STAGE_DIR}/venv/bin/python3"
"$VPY" -m pip install --quiet --upgrade pip >>"$LOG" 2>&1

if "$VPY" -m pip install --quiet -e '.[test]' >>"$LOG" 2>&1; then
    echo "BRIEF-G-PIP-INSTALL-E: PASS exit=0"
else
    echo "BRIEF-G-PIP-INSTALL-E: FAIL exit=1"
    tail -25 "$LOG"
    exit 1
fi

# THE ENTRY POINT IS THE DELIVERABLE, SO ITS INSTALLATION IS CHECKED.
# `rapidctl` is a new `[project.scripts]` entry; a wheel that declares it but
# cannot execute it is exactly the "scaffold is not a deployment" defect, and
# `--help` is the cheapest call that proves the console script exists, the
# module imports, and the subcommand tree parses.
if "${STAGE_DIR}/venv/bin/rapidctl" --help >>"$LOG" 2>&1; then
    echo "BRIEF-G-RAPIDCTL-ENTRYPOINT: PASS exit=0"
else
    rc=$?
    echo "BRIEF-G-RAPIDCTL-ENTRYPOINT: FAIL exit=$rc"
    tail -25 "$LOG"
fi

export CONTRACT_PYTHON="$VPY"
export PGHOST=127.0.0.1
export PGPORT="$PGPORT_HOST"
export PGUSER=postgres
export PGPASSWORD="$PGPW"
export PGDATABASE="$DBNAME"
export CONTRACT_LOG="${STAGE_DIR}/contract-pytest.log"

# --- PASS 1: the authoritative stream ALONE, drafts absent -------------------
# The CI-equivalent run, done FIRST and deliberately: it demonstrates that the
# draft-schema tests SKIP cleanly rather than fail when their schema is
# absent, which is the property that keeps smdc CI green. A run that only ever
# saw base+drafts could not tell a clean skip from a test that never existed.
echo "BRIEF-G-PASS1: base stream only (drafts absent — skips expected)"
./scripts/run-contract-tests.sh "${STAGE_DIR}/db-migrations" \
    >"${STAGE_DIR}/pass1.log" 2>&1
pass1_rc=$?
grep -E '^BRIEF-B-(APPLY|SUITE|SCHEMA-MIGRATIONS)' "${STAGE_DIR}/pass1.log" | head -5
pass1_line=$(grep -E '^[0-9]+ (passed|failed)|passed|skipped' \
    "${STAGE_DIR}/contract-pytest.log" 2>/dev/null | tail -1)
echo "BRIEF-G-PASS1-RESULT: ${pass1_line:-<no summary line>}"
# THE SKIP REASONS ARE THE EVIDENCE for "CI stays green", so they are
# re-run with `-rs` and echoed. A bare count cannot distinguish the intended
# draft-schema skips from a module that failed to import and skipped
# everything, and `-q` alone prints no skip lines at all to grep for — the
# first version of this block looked for `SKIPPED [n]` in a `-q` log and
# silently reported nothing.
echo "BRIEF-G-PASS1-SKIPS:"
"$VPY" -m pytest pipeline/contract -m contract -p no:cacheprovider \
    --no-header -q -rs >"${STAGE_DIR}/pass1-skips.log" 2>&1
grep -E '^SKIPPED' "${STAGE_DIR}/pass1-skips.log" \
    | sed -E 's/^SKIPPED \[([0-9]+)\] .*: /[\1] /' \
    | sort | uniq -c | head -10
if [ "$pass1_rc" -ne 0 ]; then
    echo "--- BRIEF-G-PASS1 failures ---"
    grep -E '^(FAILED|ERROR)' "${STAGE_DIR}/contract-pytest.log" | head -20
    grep -E '^E ' "${STAGE_DIR}/contract-pytest.log" | head -25
fi
echo "BRIEF-G-PASS1: exit=$pass1_rc"

# --- Apply the DRAFT migrations, in order ------------------------------------
# On top of the base stream pass 1 already built, which is the order they
# would land in as change requests. `ON_ERROR_STOP=1` so a draft that does not
# apply fails here rather than leaving a half-built schema.
echo ">> applying DRAFT migrations from migrations-draft/"
drafts_applied=0
for f in migrations-draft/0[0-9][0-9]-*.sql; do
    [ -e "$f" ] || { echo "BRIEF-G-DRAFTS: FAIL exit=2 (no draft files matched)"; exit 2; }
    fn=$(basename "$f")
    if psql -v ON_ERROR_STOP=1 -f "$f" >>"$LOG" 2>&1; then
        drafts_applied=$((drafts_applied + 1))
        echo "BRIEF-G-DRAFT-${fn%%-*}: PASS exit=0 ($fn)"
    else
        rc=$?
        echo "BRIEF-G-DRAFT-${fn%%-*}: FAIL exit=$rc ($fn)"
        grep -E 'ERROR|FATAL' "$LOG" | tail -20
        exit "$rc"
    fi
done
echo "BRIEF-G-DRAFTS: PASS exit=0 ($drafts_applied draft migrations applied)"

# IDEMPOTENCE OF 047 SPECIFICALLY. The stream's own convention is that a
# re-run converges rather than errors, and 047 is the file this brief adds —
# re-applying it here proves its guards (ADD COLUMN IF NOT EXISTS, CREATE
# UNIQUE INDEX IF NOT EXISTS, CREATE OR REPLACE FUNCTION) actually hold,
# which a single application cannot show.
if psql -v ON_ERROR_STOP=1 -f migrations-draft/047-idempotency-and-expected-state.sql \
        >>"$LOG" 2>&1; then
    echo "BRIEF-G-DRAFT-047-REAPPLY: PASS exit=0 (idempotent)"
else
    rc=$?
    echo "BRIEF-G-DRAFT-047-REAPPLY: FAIL exit=$rc (not idempotent)"
    grep -E 'ERROR|FATAL' "$LOG" | tail -10
fi

# --- PASS 2: base + drafts, the full acceptance ------------------------------
# THE SUITE IS RUN DIRECTLY, NOT THROUGH `run-contract-tests.sh` A SECOND
# TIME: that script re-applies the whole stream first, and the stream is not
# idempotent across this boundary (DRAFT 045 replaces `work_units_state_ck`,
# so re-running 036 then fails on its own ADD CONSTRAINT). Pass 1 built the
# base schema; the drafts amended it; the schema is now exactly base+drafts.
echo "BRIEF-G-PASS2: base + drafts (the full acceptance)"
export CONTRACT_LOG="${STAGE_DIR}/contract-pytest-pass2.log"
: > "$CONTRACT_LOG"
: "${RAPID_SW:=${STAGE_DIR}/repo}"
export RAPID_SW
"$VPY" -m pytest pipeline/contract -m contract -p no:cacheprovider \
    --no-header -q >"${STAGE_DIR}/pass2.log" 2>&1
suite_rc=$?
cp "${STAGE_DIR}/pass2.log" "$CONTRACT_LOG"
pass2_line=$(grep -E 'passed|failed|error' \
    "${STAGE_DIR}/pass2.log" 2>/dev/null | tail -1)
echo "BRIEF-G-PASS2-RESULT: ${pass2_line:-<no summary line>}"

# THE FIVE CRITERIA, NAMED INDIVIDUALLY. A suite-level "N passed" does not
# say WHICH tests ran, and the criteria are the deliverable — so each brief-G
# module is re-run with `-v` selection and its own verdict line, front-loaded
# where the 24KB truncation cannot reach it.
#
# The `-k` expression for criterion 3 is passed as ONE argument, not split
# on spaces: the first version of this loop word-split `-k break_glass or
# unreconciled` into three arguments, so pytest read `or` as a file path and
# reported "no tests ran" with exit 4 — a criterion that looked selected and
# was never run. Hence the explicit third field per spec rather than a
# single args string.
for spec in \
    "CRIT1-2:pipeline/contract/test_operator_mutations.py:" \
    "CRIT3:pipeline/contract/test_operator_mutations.py:clear_the_row or unreconciled_view or unopened_event" \
    "CRIT4:pipeline/contract/test_wrapped_operator_tools.py:" \
    "CRIT5:pipeline/contract/test_repositories.py:" \
    "GRANTS:pipeline/contract/test_operator_grants.py:" ; do
    name=${spec%%:*}
    rest=${spec#*:}
    target=${rest%%:*}
    kexpr=${rest#*:}
    if [ -n "$kexpr" ]; then
        "$VPY" -m pytest "$target" -k "$kexpr" -m contract \
            -p no:cacheprovider --no-header -q \
            >"${STAGE_DIR}/${name}.log" 2>&1
    else
        "$VPY" -m pytest "$target" -m contract \
            -p no:cacheprovider --no-header -q \
            >"${STAGE_DIR}/${name}.log" 2>&1
    fi
    rc=$?
    line=$(grep -E 'passed|failed|error|no tests ran' \
        "${STAGE_DIR}/${name}.log" 2>/dev/null | tail -1)
    echo "BRIEF-G-${name}: exit=$rc ${line}"
    if [ "$rc" -ne 0 ]; then
        grep -E '^(FAILED|ERROR)' "${STAGE_DIR}/${name}.log" | head -10
        grep -E '^E ' "${STAGE_DIR}/${name}.log" | head -15
    fi
done

if [ "$suite_rc" -ne 0 ]; then
    echo "--- BRIEF-G-PASS2 failures ---"
    grep -E '^(FAILED|ERROR)' "${STAGE_DIR}/pass2.log" | head -20
    grep -E '^E ' "${STAGE_DIR}/pass2.log" | head -30
fi
echo "BRIEF-G-CONTRACT-SUITE: exit=$suite_rc"

# --- The stubbed tier stays green, unchanged ---------------------------------
RAPID_SW="${STAGE_DIR}/repo" ./scripts/run-operational-tests.sh "$VPY" \
    >"${STAGE_DIR}/stub-tier.log" 2>&1
stub_rc=$?
if [ "$stub_rc" -ne 0 ]; then
    echo "--- BRIEF-G-STUB-TIER failures ---"
    grep -E '^FAIL |^ *FAIL: ' "${STAGE_DIR}/stub-tier.log" | head -25
fi
tail -4 "${STAGE_DIR}/stub-tier.log"
echo "BRIEF-G-STUB-TIER: exit=$stub_rc"

if [ "$suite_rc" -eq 0 ] && [ "$stub_rc" -eq 0 ] && [ "$pass1_rc" -eq 0 ]; then
    echo "BRIEF-G-OVERALL: PASS exit=0"
    exit 0
fi
echo "BRIEF-G-OVERALL: FAIL exit=1 (pass1=$pass1_rc contract=$suite_rc stub=$stub_rc)"
exit 1
