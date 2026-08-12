#!/usr/bin/env bash
#
# Run brief F's acceptance on rapid-admin: the contract tier against a
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
# Usage: contract-brief-f-on-rapid-admin.sh <stage-dir> <image> <run-id>
#   stage-dir : holds `db-migrations/` (the stream), `repo/` (this repo)
#   image     : the PostgreSQL+Q3C image, digest-pinned
#   run-id    : namespaces the container and the scratch footprint
#
# TEARDOWN IS THIS SCRIPT'S OWN: the container is removed on exit, success or
# failure, by the trap. rapid-admin is a SHARED team host — nothing here ever
# touches an existing container, an existing database, or rapid-db.

set -u

STAGE_DIR=${1:?usage: contract-brief-f-on-rapid-admin.sh <stage-dir> <image> <run-id>}
IMAGE=${2:?usage: contract-brief-f-on-rapid-admin.sh <stage-dir> <image> <run-id>}
RUN_ID=${3:?usage: contract-brief-f-on-rapid-admin.sh <stage-dir> <image> <run-id>}

: "${HOME:=/root}"          # AWS-RunShellScript runs with no login environment
export HOME

CONTAINER="brief-f-pg-${RUN_ID}"
DBNAME=rapid
# A nonstandard port, distinct from brief B's 55432, C's 55433, G's 55434 and
# D's 55435 so a leftover of any of those runs cannot collide with this one.
PGPORT_HOST=${PGPORT_HOST:-55436}
PGPW="brief-f-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"

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

echo "BRIEF-F-RUN-ID: $RUN_ID"
echo "BRIEF-F-IMAGE: $IMAGE"

podman rm -f "$CONTAINER" >/dev/null 2>&1 || true

echo ">> starting throwaway PostgreSQL as $CONTAINER on port $PGPORT_HOST"
if ! podman run -d --name "$CONTAINER" \
        -e POSTGRES_PASSWORD="$PGPW" \
        -e POSTGRES_DB=postgres \
        -p "127.0.0.1:${PGPORT_HOST}:5432" \
        "$IMAGE" >>"$LOG" 2>&1; then
    echo "BRIEF-F-CONTAINER: FAIL exit=1 (could not start)"
    tail -20 "$LOG"
    exit 1
fi
echo "BRIEF-F-CONTAINER: PASS exit=0 ($CONTAINER)"

cd "${STAGE_DIR}/repo" || exit 2

python3 -m venv "${STAGE_DIR}/venv" >>"$LOG" 2>&1 || {
    echo "BRIEF-F-VENV: FAIL exit=1"; tail -10 "$LOG"; exit 1; }
VPY="${STAGE_DIR}/venv/bin/python3"
"$VPY" -m pip install --quiet --upgrade pip >>"$LOG" 2>&1

if "$VPY" -m pip install --quiet -e '.[test]' >>"$LOG" 2>&1; then
    echo "BRIEF-F-PIP-INSTALL-E: PASS exit=0"
else
    echo "BRIEF-F-PIP-INSTALL-E: FAIL exit=1"
    tail -25 "$LOG"
    exit 1
fi

# THE ENTRY POINT IS THE DELIVERABLE, SO ITS INSTALLATION IS CHECKED.
# `rapidctl` is a new `[project.scripts]` entry; a wheel that declares it but
# cannot execute it is exactly the "scaffold is not a deployment" defect, and
# `--help` is the cheapest call that proves the console script exists, the
# module imports, and the subcommand tree parses.
if "${STAGE_DIR}/venv/bin/rapidctl" --help >>"$LOG" 2>&1; then
    echo "BRIEF-F-RAPIDCTL-ENTRYPOINT: PASS exit=0"
else
    rc=$?
    echo "BRIEF-F-RAPIDCTL-ENTRYPOINT: FAIL exit=$rc"
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
echo "BRIEF-F-PASS1: base stream only (drafts absent — skips expected)"
./scripts/run-contract-tests.sh "${STAGE_DIR}/db-migrations" \
    >"${STAGE_DIR}/pass1.log" 2>&1
pass1_rc=$?
grep -E '^BRIEF-B-(APPLY|SUITE|SCHEMA-MIGRATIONS)' "${STAGE_DIR}/pass1.log" | head -5
pass1_line=$(grep -E '^[0-9]+ (passed|failed)|passed|skipped' \
    "${STAGE_DIR}/contract-pytest.log" 2>/dev/null | tail -1)
echo "BRIEF-F-PASS1-RESULT: ${pass1_line:-<no summary line>}"
# THE SKIP REASONS ARE THE EVIDENCE for "CI stays green", so they are
# re-run with `-rs` and echoed. A bare count cannot distinguish the intended
# draft-schema skips from a module that failed to import and skipped
# everything, and `-q` alone prints no skip lines at all to grep for — the
# first version of this block looked for `SKIPPED [n]` in a `-q` log and
# silently reported nothing.
echo "BRIEF-F-PASS1-SKIPS:"
"$VPY" -m pytest pipeline/contract -m contract -p no:cacheprovider \
    --no-header -q -rs >"${STAGE_DIR}/pass1-skips.log" 2>&1
skips_rc=$?
# THE RE-RUN'S OWN EXIT CODE IS REPORTED. This block re-invokes pytest to
# get `-rs` skip reasons, and an earlier version reported nothing at all
# when that invocation failed — a silent empty section that reads exactly
# like "there were no skips". The count and the exit code are printed
# unconditionally so an empty list is distinguishable from a failed re-run.
skip_lines=$(grep -cE '^SKIPPED' "${STAGE_DIR}/pass1-skips.log" 2>/dev/null)
echo "BRIEF-F-PASS1-SKIPS-RERUN: exit=$skips_rc lines=${skip_lines:-0}"
grep -E '^SKIPPED' "${STAGE_DIR}/pass1-skips.log" \
    | sed -E 's/^SKIPPED \[([0-9]+)\] .*: /[\1] /' \
    | sort | uniq -c | head -12
if [ "${skip_lines:-0}" -eq 0 ]; then
    echo "--- no SKIPPED lines; the re-run's own tail follows ---"
    tail -15 "${STAGE_DIR}/pass1-skips.log"
fi
if [ "$pass1_rc" -ne 0 ]; then
    echo "--- BRIEF-F-PASS1 failures ---"
    grep -E '^(FAILED|ERROR)' "${STAGE_DIR}/contract-pytest.log" | head -20
    grep -E '^E ' "${STAGE_DIR}/contract-pytest.log" | head -25
fi
echo "BRIEF-F-PASS1: exit=$pass1_rc"

# --- Apply the DRAFT migrations, in order ------------------------------------
# On top of the base stream pass 1 already built, which is the order they
# would land in as change requests. `ON_ERROR_STOP=1` so a draft that does not
# apply fails here rather than leaving a half-built schema.
echo ">> applying DRAFT migrations from migrations-draft/"
drafts_applied=0
for f in migrations-draft/0[0-9][0-9]-*.sql; do
    [ -e "$f" ] || { echo "BRIEF-F-DRAFTS: FAIL exit=2 (no draft files matched)"; exit 2; }
    fn=$(basename "$f")
    if psql -v ON_ERROR_STOP=1 -f "$f" >>"$LOG" 2>&1; then
        drafts_applied=$((drafts_applied + 1))
        echo "BRIEF-F-DRAFT-${fn%%-*}: PASS exit=0 ($fn)"
    else
        rc=$?
        echo "BRIEF-F-DRAFT-${fn%%-*}: FAIL exit=$rc ($fn)"
        grep -E 'ERROR|FATAL' "$LOG" | tail -20
        exit "$rc"
    fi
done
echo "BRIEF-F-DRAFTS: PASS exit=0 ($drafts_applied draft migrations applied)"

# IDEMPOTENCE OF F'S OWN DRAFT. The stream's convention is that a re-run
# converges rather than errors, and brief F's acceptance requires the drafts
# to be "applied twice, with no-op second application". Re-applying proves
# 049's guards (CREATE TABLE IF NOT EXISTS, CREATE UNIQUE INDEX IF NOT
# EXISTS, CREATE OR REPLACE FUNCTION, the ON CONFLICT DO NOTHING seed rows)
# actually hold, which a single application cannot show.
#
# The glob is F's OWN numbers — 049 onward. D's 048 and the earlier drafts
# were proved idempotent by their own briefs' runs; re-proving them here
# would attribute someone else's evidence to this one.
reapply_rc=0
for f in migrations-draft/049-*.sql migrations-draft/05[0-9]-*.sql; do
    [ -e "$f" ] || continue
    fn=$(basename "$f")
    if psql -v ON_ERROR_STOP=1 -f "$f" >>"$LOG" 2>&1; then
        echo "BRIEF-F-DRAFT-${fn%%-*}-REAPPLY: PASS exit=0 (idempotent)"
    else
        rc=$?
        reapply_rc=$rc
        echo "BRIEF-F-DRAFT-${fn%%-*}-REAPPLY: FAIL exit=$rc (not idempotent)"
        grep -E 'ERROR|FATAL' "$LOG" | tail -10
    fi
done
echo "BRIEF-F-DRAFT-REAPPLY: exit=$reapply_rc"

# --- PASS 2: base + drafts, the full acceptance ------------------------------
# THE SUITE IS RUN DIRECTLY, NOT THROUGH `run-contract-tests.sh` A SECOND
# TIME: that script re-applies the whole stream first, and the stream is not
# idempotent across this boundary (DRAFT 045 replaces `work_units_state_ck`,
# so re-running 036 then fails on its own ADD CONSTRAINT). Pass 1 built the
# base schema; the drafts amended it; the schema is now exactly base+drafts.
echo "BRIEF-F-PASS2: base + drafts (the full acceptance)"
export CONTRACT_LOG="${STAGE_DIR}/contract-pytest-pass2.log"
: > "$CONTRACT_LOG"
: "${RAPID_SW:=${STAGE_DIR}/repo}"
export RAPID_SW
# `-rs` so the skip REASONS are in the log, not just a count in the summary
# line: a nonzero skip count has to name which tests skipped for the failure
# below to be actionable.
"$VPY" -m pytest pipeline/contract -m contract -p no:cacheprovider \
    --no-header -q -rs >"${STAGE_DIR}/pass2.log" 2>&1
suite_rc=$?
cp "${STAGE_DIR}/pass2.log" "$CONTRACT_LOG"
pass2_line=$(grep -E 'passed|failed|error' \
    "${STAGE_DIR}/pass2.log" 2>/dev/null | tail -1)
echo "BRIEF-F-PASS2-RESULT: ${pass2_line:-<no summary line>}"

# ZERO SKIPS IN PASS 2, ENFORCED RATHER THAN ASSUMED.
#
# Brief D: "the recorded acceptance run executes with ZERO D-specific skips,
# and the ledger states the skip count." This block is what makes that a
# gate instead of a claim. Until it existed, PASS 2's verdict was grepped
# for `passed|failed|error` only — so a run where a draft-schema test
# skipped (because 048 had not applied, or a probe was misspelled, or a
# fixture bailed early) reported success with the criterion silently
# unexercised. That is the "no false cleans" failure mode exactly: the
# acceptance would have looked identical whether the D tests ran or not.
#
# PASS 1 is where skips are EXPECTED (drafts absent, that is the CI-green
# property being demonstrated). PASS 2 is the recorded acceptance run and
# must have none.
pass2_skips=$(grep -cE '^SKIPPED' "${STAGE_DIR}/pass2.log" 2>/dev/null)
pass2_skips=${pass2_skips:-0}
echo "BRIEF-F-PASS2-SKIPS: $pass2_skips"
if [ "$pass2_skips" -ne 0 ]; then
    echo "BRIEF-F-PASS2-SKIPS: FAIL exit=1 ($pass2_skips skip(s) in the"\
         "recorded acceptance run; the brief requires zero)"
    grep -E '^SKIPPED' "${STAGE_DIR}/pass2.log" | head -20
    suite_rc=1
else
    echo "BRIEF-F-PASS2-SKIPS: PASS exit=0 (zero skips, as the brief requires)"
fi

# THE CRITERIA, NAMED INDIVIDUALLY. A suite-level "N passed" does not say
# WHICH tests ran, and the criteria are the deliverable — so each brief-D
# criterion is re-run with its own selection and its own verdict line,
# front-loaded where the 24KB truncation cannot reach it.
#
# The `-k` expressions are passed as ONE argument, not split on spaces: an
# earlier version of this loop (brief G's) word-split `-k break_glass or
# unreconciled` into three arguments, so pytest read `or` as a file path and
# reported "no tests ran" with exit 4 — a criterion that looked selected and
# was never run. Hence the explicit third field per spec rather than a
# single args string.
#
# Criteria 7 and 8 are STUB-tier tests (manifest wire format and typed
# payload construction need no database), so they are selected here without
# the `-m contract` marker the database-tier lines carry; the loop passes
# the marker per spec for that reason.
crit_rc=0
for spec in \
    "CRIT1-ORDERED-CLAIMING:pipeline/contract/test_association_claim_order.py::contract" \
    "CRIT1-ORDERED-CLAIMING-STUB:submission/test/test_gathering.py:ordering or watermark or claims or owed or exhausted or canonical:" \
    "CRIT2-ATOMICITY:pipeline/contract/test_association_watermark.py:atomicity or rolled_back or commit_together or backwards or no_op:contract" \
    "CRIT3-STALE-RETRY:pipeline/contract/test_association_watermark.py:stale or unique_index:contract" \
    "CRIT4-LANE-LEASE:pipeline/contract/test_association_watermark.py:lease or lock_order:contract" \
    "CRIT5-SET-ISOLATION:pipeline/contract/test_association_watermark.py:isolation or clone_family or live_prompt or live_set_lookup:contract" \
    "CRIT6-RADEC-IDEMPOTENCY:pipeline/contract/test_association_watermark.py:radec:contract" \
    "CRIT7-WITHIN-JOB-ORDER:pipeline/contract/test_association_watermark.py:canonical_detection or temporal_column or python_and_sql:contract" \
    "FIX2-GATE-AGREEMENT:pipeline/contract/test_association_work_inventory.py:agree or boundary or vbest_flip:contract" \
    "FIX3-REAL-SQL:pipeline/contract/test_association_work_inventory.py:executes or accepted_pair or schema_probe or query_failure:contract" ; do
    # Four colon-separated fields: name, target, -k expression, -m marker.
    # Split by successive prefix/suffix removal rather than by IFS word
    # splitting, because an empty middle field must stay empty rather than
    # collapsing and shifting the ones after it.
    name=${spec%%:*}
    rest=${spec#*:}
    target=${rest%%:*}
    rest=${rest#*:}
    kexpr=${rest%%:*}
    marker=${rest#*:}

    set -- "$target"
    [ -n "$kexpr" ] && set -- "$@" -k "$kexpr"
    [ -n "$marker" ] && set -- "$@" -m "$marker"
    "$VPY" -m pytest "$@" -p no:cacheprovider --no-header -q \
        >"${STAGE_DIR}/${name}.log" 2>&1
    rc=$?
    line=$(grep -E 'passed|failed|error|no tests ran' \
        "${STAGE_DIR}/${name}.log" 2>/dev/null | tail -1)
    echo "BRIEF-F-${name}: exit=$rc ${line}"
    if [ "$rc" -ne 0 ]; then
        # A FAILING CRITERION FAILS THE RUN. Brief G's version of this loop
        # printed each criterion's exit code and then dropped it: the final
        # verdict was computed from the suite, stub and pass-1 codes alone.
        # That is safe only while every criterion's tests are also inside
        # the contract suite — and criteria 7 and 8 are stub-tier files, so
        # a red criterion reported BRIEF-F-OVERALL: PASS. Observed live on
        # this branch before this line existed.
        crit_rc=1
        grep -E '^(FAILED|ERROR)' "${STAGE_DIR}/${name}.log" | head -10
        grep -E '^E ' "${STAGE_DIR}/${name}.log" | head -15
    fi
done
echo "BRIEF-F-CRITERIA: exit=$crit_rc"

if [ "$suite_rc" -ne 0 ]; then
    echo "--- BRIEF-F-PASS2 failures ---"
    grep -E '^(FAILED|ERROR)' "${STAGE_DIR}/pass2.log" | head -20
    grep -E '^E ' "${STAGE_DIR}/pass2.log" | head -30
fi
echo "BRIEF-F-CONTRACT-SUITE: exit=$suite_rc"

# --- The stubbed tier stays green, unchanged ---------------------------------
RAPID_SW="${STAGE_DIR}/repo" ./scripts/run-operational-tests.sh "$VPY" \
    >"${STAGE_DIR}/stub-tier.log" 2>&1
stub_rc=$?
if [ "$stub_rc" -ne 0 ]; then
    echo "--- BRIEF-F-STUB-TIER failures ---"
    grep -E '^FAIL |^ *FAIL: ' "${STAGE_DIR}/stub-tier.log" | head -25
    # THE FAILING MODULES ARE RE-RUN INDIVIDUALLY FOR THEIR ACTUAL ERRORS.
    # `run-operational-tests.sh` prints a per-module PASS/FAIL table and
    # swallows the tracebacks, which is right for a green run and useless
    # for a red one: "FAIL submission.test.test_gathering" names the module
    # and nothing about what broke, so every diagnosis needed another whole
    # staging round trip. Re-running just the failures costs seconds and
    # puts the error in the same transcript as the verdict.
    echo "--- BRIEF-F-STUB-TIER detail ---"
    for module in $(grep -E '^FAIL ' "${STAGE_DIR}/stub-tier.log" \
                    | awk '{print $2}' | head -6); do
        echo ">> $module"
        "$VPY" -m unittest "$module" 2>&1 | tail -25
    done
fi
tail -4 "${STAGE_DIR}/stub-tier.log"
echo "BRIEF-F-STUB-TIER: exit=$stub_rc"

if [ "$suite_rc" -eq 0 ] && [ "$stub_rc" -eq 0 ] && [ "$pass1_rc" -eq 0 ] \
        && [ "$crit_rc" -eq 0 ] && [ "$reapply_rc" -eq 0 ]; then
    echo "BRIEF-F-OVERALL: PASS exit=0"
    exit 0
fi
echo "BRIEF-F-OVERALL: FAIL exit=1 (pass1=$pass1_rc contract=$suite_rc " \
     "stub=$stub_rc criteria=$crit_rc reapply=$reapply_rc)"
exit 1
