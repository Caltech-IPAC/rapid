#!/usr/bin/env bash
#
# Run brief H's acceptance on rapid-admin: the contract tier against a
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
# Derived from `contract-brief-f-on-rapid-admin.sh` — F's, not G's or C's,
# specifically because F carries the two fixes the earlier runners lack and
# both are load-bearing here: the CRITERIA AGGREGATION fix (`crit_rc`, so a
# red criterion actually fails the run rather than being printed and dropped)
# and the PASS2 ZERO-SKIP GATE (so a criterion that silently skipped cannot
# report as passing). Brief E has ten criteria and several are stub-tier, which
# is exactly the shape that made G's verdict lie.
#
# The shape it inherits, and the comments that explain each choice: run ON the
# host, staged there by the caller, taking no AWS actions of its own,
# generating the throwaway cluster's password locally so it never enters an
# SSM command's content (command history is readable for ~30 days with no
# delete API).
#
# Usage: contract-brief-h-on-rapid-admin.sh <stage-dir> <image> <run-id>
#   stage-dir : holds `db-migrations/` (the stream), `repo/` (this repo)
#   image     : the PostgreSQL+Q3C image, digest-pinned
#   run-id    : namespaces the container and the scratch footprint
#
# TEARDOWN IS THIS SCRIPT'S OWN: the container is removed on exit, success or
# failure, by the trap. rapid-admin is a SHARED team host — nothing here ever
# touches an existing container, an existing database, or rapid-db.

set -u

STAGE_DIR=${1:?usage: contract-brief-h-on-rapid-admin.sh <stage-dir> <image> <run-id>}
IMAGE=${2:?usage: contract-brief-h-on-rapid-admin.sh <stage-dir> <image> <run-id>}
RUN_ID=${3:?usage: contract-brief-h-on-rapid-admin.sh <stage-dir> <image> <run-id>}

: "${HOME:=/root}"          # AWS-RunShellScript runs with no login environment
export HOME

CONTAINER="brief-h-pg-${RUN_ID}"
DBNAME=rapid
# A nonstandard port, distinct from brief B's 55432, C's 55433, G's 55434,
# D's 55435, F's 55436 and E's 55437 so a leftover of any of those runs cannot
# collide with this one.
PGPORT_HOST=${PGPORT_HOST:-55438}
PGPW="brief-h-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"

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

echo "BRIEF-H-RUN-ID: $RUN_ID"
echo "BRIEF-H-IMAGE: $IMAGE"

podman rm -f "$CONTAINER" >/dev/null 2>&1 || true

echo ">> starting throwaway PostgreSQL as $CONTAINER on port $PGPORT_HOST"
if ! podman run -d --name "$CONTAINER" \
        -e POSTGRES_PASSWORD="$PGPW" \
        -e POSTGRES_DB=postgres \
        -p "127.0.0.1:${PGPORT_HOST}:5432" \
        "$IMAGE" >>"$LOG" 2>&1; then
    echo "BRIEF-H-CONTAINER: FAIL exit=1 (could not start)"
    tail -20 "$LOG"
    exit 1
fi
echo "BRIEF-H-CONTAINER: PASS exit=0 ($CONTAINER)"

cd "${STAGE_DIR}/repo" || exit 2

python3 -m venv "${STAGE_DIR}/venv" >>"$LOG" 2>&1 || {
    echo "BRIEF-H-VENV: FAIL exit=1"; tail -10 "$LOG"; exit 1; }
VPY="${STAGE_DIR}/venv/bin/python3"
"$VPY" -m pip install --quiet --upgrade pip >>"$LOG" 2>&1

if "$VPY" -m pip install --quiet -e '.[test]' >>"$LOG" 2>&1; then
    echo "BRIEF-H-PIP-INSTALL-E: PASS exit=0"
else
    echo "BRIEF-H-PIP-INSTALL-E: FAIL exit=1"
    tail -25 "$LOG"
    exit 1
fi

# THE ENTRY POINTS ARE THE DELIVERABLE, SO THEIR INSTALLATION IS CHECKED —
# ALL FIVE (acceptance 10). A wheel that declares a console script but cannot
# execute it is exactly the "scaffold is not a deployment" defect.
#
# WHAT IS ASSERTED IS THAT THE SHIM RESOLVED AND THE MODULE IMPORTED — not
# that the command succeeded. This matches the workflow's own loop
# (`.github/workflows/contract-tests.yml`) exactly, and the match is
# deliberate: three of these five are long-running SERVICES with no `--help`
# at all, so they attempt to START and exit non-zero on their own missing
# environment (70, EXIT_START_FAILED, having logged what was absent). That is
# a correct entry point behaving correctly with no AWS region on this host.
#
# A FIRST VERSION OF THIS BLOCK GRADED ON THE EXIT CODE and reported
# `rapid-publisher: FAIL exit=70` — alongside `rapid-reconciler: FAIL exit=70`,
# which is a service that has shipped and run in production since brief B. Two
# identical results, one of them impossible to be a real defect: the check was
# wrong, not the entry points. An ImportError is the failure that matters here,
# because it is the one that means the wheel is broken.
#
# A FAILING ENTRY POINT STILL FAILS THE RUN (acceptance 10), on that criterion.
entrypoint_rc=0
for cmd in rapid-reconciler rapid-operator rapid-job rapidctl rapid-publisher
do
    if [ ! -x "${STAGE_DIR}/venv/bin/$cmd" ]; then
        entrypoint_rc=1
        echo "BRIEF-H-ENTRYPOINT-$cmd: FAIL exit=1 (not installed)"
        continue
    fi
    out=$("${STAGE_DIR}/venv/bin/$cmd" --help 2>&1)
    rc=$?
    printf '%s\n' "$out" >>"$LOG"
    if printf '%s\n' "$out" | grep -qi 'ModuleNotFoundError\|ImportError'; then
        entrypoint_rc=1
        echo "BRIEF-H-ENTRYPOINT-$cmd: FAIL exit=1 (import error)"
        printf '%s\n' "$out" | tail -15
    else
        echo "BRIEF-H-ENTRYPOINT-$cmd: PASS resolved exit=$rc (imported)"
    fi
done
echo "BRIEF-H-ENTRYPOINTS: exit=$entrypoint_rc"

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
echo "BRIEF-H-PASS1: base stream only (drafts absent — skips expected)"
./scripts/run-contract-tests.sh "${STAGE_DIR}/db-migrations" \
    >"${STAGE_DIR}/pass1.log" 2>&1
pass1_rc=$?
grep -E '^BRIEF-B-(APPLY|SUITE|SCHEMA-MIGRATIONS)' "${STAGE_DIR}/pass1.log" | head -5
pass1_line=$(grep -E '^[0-9]+ (passed|failed)|passed|skipped' \
    "${STAGE_DIR}/contract-pytest.log" 2>/dev/null | tail -1)
echo "BRIEF-H-PASS1-RESULT: ${pass1_line:-<no summary line>}"
# THE SKIP REASONS ARE THE EVIDENCE for "CI stays green", so they are
# re-run with `-rs` and echoed. A bare count cannot distinguish the intended
# draft-schema skips from a module that failed to import and skipped
# everything, and `-q` alone prints no skip lines at all to grep for — the
# first version of this block looked for `SKIPPED [n]` in a `-q` log and
# silently reported nothing.
echo "BRIEF-H-PASS1-SKIPS:"
"$VPY" -m pytest pipeline/contract -m contract -p no:cacheprovider \
    --no-header -q -rs >"${STAGE_DIR}/pass1-skips.log" 2>&1
skips_rc=$?
# THE RE-RUN'S OWN EXIT CODE IS REPORTED. This block re-invokes pytest to
# get `-rs` skip reasons, and an earlier version reported nothing at all
# when that invocation failed — a silent empty section that reads exactly
# like "there were no skips". The count and the exit code are printed
# unconditionally so an empty list is distinguishable from a failed re-run.
skip_lines=$(grep -cE '^SKIPPED' "${STAGE_DIR}/pass1-skips.log" 2>/dev/null)
echo "BRIEF-H-PASS1-SKIPS-RERUN: exit=$skips_rc lines=${skip_lines:-0}"
grep -E '^SKIPPED' "${STAGE_DIR}/pass1-skips.log" \
    | sed -E 's/^SKIPPED \[([0-9]+)\] .*: /[\1] /' \
    | sort | uniq -c | head -12
if [ "${skip_lines:-0}" -eq 0 ]; then
    echo "--- no SKIPPED lines; the re-run's own tail follows ---"
    tail -15 "${STAGE_DIR}/pass1-skips.log"
fi
if [ "$pass1_rc" -ne 0 ]; then
    echo "--- BRIEF-H-PASS1 failures ---"
    grep -E '^(FAILED|ERROR)' "${STAGE_DIR}/contract-pytest.log" | head -20
    grep -E '^E ' "${STAGE_DIR}/contract-pytest.log" | head -25
fi
echo "BRIEF-H-PASS1: exit=$pass1_rc"

# --- Apply the DRAFT migrations, in order ------------------------------------
# On top of the base stream pass 1 already built, which is the order they
# would land in as change requests. `ON_ERROR_STOP=1` so a draft that does not
# apply fails here rather than leaving a half-built schema.
echo ">> applying DRAFT migrations from migrations-draft/"
drafts_applied=0
for f in migrations-draft/0[0-9][0-9]-*.sql; do
    [ -e "$f" ] || { echo "BRIEF-H-DRAFTS: FAIL exit=2 (no draft files matched)"; exit 2; }
    fn=$(basename "$f")
    if psql -v ON_ERROR_STOP=1 -f "$f" >>"$LOG" 2>&1; then
        drafts_applied=$((drafts_applied + 1))
        echo "BRIEF-H-DRAFT-${fn%%-*}: PASS exit=0 ($fn)"
    else
        rc=$?
        echo "BRIEF-H-DRAFT-${fn%%-*}: FAIL exit=$rc ($fn)"
        grep -E 'ERROR|FATAL' "$LOG" | tail -20
        exit "$rc"
    fi
done
echo "BRIEF-H-DRAFTS: PASS exit=0 ($drafts_applied draft migrations applied)"

# IDEMPOTENCE OF E'S OWN DRAFT. The stream's convention is that a re-run
# converges rather than errors, and the brief requires the drafts to
# double-apply as a no-op. Re-applying proves 050's guards (CREATE TABLE IF
# NOT EXISTS, CREATE INDEX IF NOT EXISTS, CREATE OR REPLACE FUNCTION, the
# catalog-guarded CREATE ROLE, the ON CONFLICT DO NOTHING policy seed, and the
# DROP TRIGGER IF EXISTS before each CREATE TRIGGER) actually hold, which a
# single application cannot show.
#
# The glob is H's OWN numbers — 051 and 052. The earlier drafts were proved
# idempotent by their own briefs' runs; re-proving them here would attribute
# someone else's evidence to this one.
reapply_rc=0
for f in migrations-draft/05[12]-*.sql; do
    [ -e "$f" ] || continue
    fn=$(basename "$f")
    if psql -v ON_ERROR_STOP=1 -f "$f" >>"$LOG" 2>&1; then
        echo "BRIEF-H-DRAFT-${fn%%-*}-REAPPLY: PASS exit=0 (idempotent)"
    else
        rc=$?
        reapply_rc=$rc
        echo "BRIEF-H-DRAFT-${fn%%-*}-REAPPLY: FAIL exit=$rc (not idempotent)"
        grep -E 'ERROR|FATAL' "$LOG" | tail -10
    fi
done
echo "BRIEF-H-DRAFT-REAPPLY: exit=$reapply_rc"

# --- PASS 2: base + drafts, the full acceptance ------------------------------
# THE SUITE IS RUN DIRECTLY, NOT THROUGH `run-contract-tests.sh` A SECOND
# TIME: that script re-applies the whole stream first, and the stream is not
# idempotent across this boundary (DRAFT 045 replaces `work_units_state_ck`,
# so re-running 036 then fails on its own ADD CONSTRAINT). Pass 1 built the
# base schema; the drafts amended it; the schema is now exactly base+drafts.
echo "BRIEF-H-PASS2: base + drafts (the full acceptance)"
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
echo "BRIEF-H-PASS2-RESULT: ${pass2_line:-<no summary line>}"

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
echo "BRIEF-H-PASS2-SKIPS: $pass2_skips"
if [ "$pass2_skips" -ne 0 ]; then
    echo "BRIEF-H-PASS2-SKIPS: FAIL exit=1 ($pass2_skips skip(s) in the"\
         "recorded acceptance run; the brief requires zero)"
    grep -E '^SKIPPED' "${STAGE_DIR}/pass2.log" | head -20
    suite_rc=1
else
    echo "BRIEF-H-PASS2-SKIPS: PASS exit=0 (zero skips, as the brief requires)"
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
# The ten criteria are brief H's acceptance list, in its order. Criterion 3's
# only-route assertion and criterion 1's pure-digest half are stub-tier (no
# database), so they are selected WITHOUT the `-m contract` marker — the
# four-field spec exists for exactly that, and F's own comment records what
# happens when a stub-tier criterion is selected with a marker it does not
# carry ("no tests ran", exit 4, a criterion that looked selected and was
# never run).
#
# THE STUB-TIER LINES CARRY AN EXPLICIT `-m` OF THEIR OWN, and must. This
# repo's `[tool.pytest.ini_options]` sets `addopts = "-m 'not contract and not
# live'"` so a bare `pytest` never opens a database — which means a criterion
# passing NO marker still inherits that default expression, and the auto-marked
# `pipeline/contract/` tests are deselected by it. On this branch's second
# acceptance run three criteria reported exit 5 (no tests collected) for that
# reason while the files themselves were fine. So a stub-tier line says
# `-m "not live"`: it re-states the intent (skip the manual tier) and displaces
# the inherited default rather than fighting it.
#
# `alerts/test/` is ALSO outside `testpaths`, which lists pipeline,
# observability, submission and database. An explicit path argument overrides
# `testpaths`, so naming the file works — but only with the marker fixed too.
crit_rc=0
for spec in \
    "CRIT1-IDENTITY-DIGEST:alerts/test/test_identity.py::not live" \
    "CRIT1-IDENTITY-DB:pipeline/contract/test_alert_outbox_identity.py::contract" \
    "CRIT2-CONFIRM-ATOMICITY:pipeline/contract/test_alert_outbox_confirmation.py::contract" \
    "CRIT3-NO-BATCH-SENDS:pipeline/contract/test_alert_send_routes.py::contract" \
    "CRIT3-NO-BATCH-SENDS-STUB:pipeline/stages/test/test_alert_production.py:outbox or producer or no_send or oversize:not live" \
    "CRIT4-PUBLISHER-WIRE:pipeline/contract/test_publisher_contract.py:order or key or identical or ambiguous or refus or already_sent:contract" \
    "CRIT5-CRASH-WINDOWS:pipeline/contract/test_publisher_contract.py:crash or lease or reclaim or overlapping or atomic_claim:contract" \
    "CRIT6-DELIVERY-POLICY:pipeline/contract/test_publisher_contract.py:policy or unauthorized or revocation or authoriz:contract" \
    "CRIT7-GRANTS-IMMUTABILITY:pipeline/contract/test_alert_outbox_grants.py::contract" \
    "CRIT8-OVERSIZE-DROP:pipeline/stages/test/test_alert_production.py:oversize:not live" \
    "CRIT9-LEGACY-BASIS:pipeline/contract/test_alert_outbox_identity.py:legacy:contract" \
    "CRIT10-ENTRYPOINTS:pipeline/contract/test_publisher_startup.py::contract" ; do
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
    echo "BRIEF-H-${name}: exit=$rc ${line}"
    if [ "$rc" -ne 0 ]; then
        # A FAILING CRITERION FAILS THE RUN. Brief G's version of this loop
        # printed each criterion's exit code and then dropped it: the final
        # verdict was computed from the suite, stub and pass-1 codes alone.
        # That is safe only while every criterion's tests are also inside
        # the contract suite — and criteria 7 and 8 are stub-tier files, so
        # a red criterion reported BRIEF-H-OVERALL: PASS. Observed live on
        # this branch before this line existed.
        crit_rc=1
        grep -E '^(FAILED|ERROR)' "${STAGE_DIR}/${name}.log" | head -10
        grep -E '^E ' "${STAGE_DIR}/${name}.log" | head -15
    fi
done
echo "BRIEF-H-CRITERIA: exit=$crit_rc"

if [ "$suite_rc" -ne 0 ]; then
    echo "--- BRIEF-H-PASS2 failures ---"
    grep -E '^(FAILED|ERROR)' "${STAGE_DIR}/pass2.log" | head -20
    grep -E '^E ' "${STAGE_DIR}/pass2.log" | head -30
fi
echo "BRIEF-H-CONTRACT-SUITE: exit=$suite_rc"

# --- The stubbed tier stays green, unchanged ---------------------------------
RAPID_SW="${STAGE_DIR}/repo" ./scripts/run-operational-tests.sh "$VPY" \
    >"${STAGE_DIR}/stub-tier.log" 2>&1
stub_rc=$?
if [ "$stub_rc" -ne 0 ]; then
    echo "--- BRIEF-H-STUB-TIER failures ---"
    grep -E '^FAIL |^ *FAIL: ' "${STAGE_DIR}/stub-tier.log" | head -25
    # THE FAILING MODULES ARE RE-RUN INDIVIDUALLY FOR THEIR ACTUAL ERRORS.
    # `run-operational-tests.sh` prints a per-module PASS/FAIL table and
    # swallows the tracebacks, which is right for a green run and useless
    # for a red one: "FAIL submission.test.test_gathering" names the module
    # and nothing about what broke, so every diagnosis needed another whole
    # staging round trip. Re-running just the failures costs seconds and
    # puts the error in the same transcript as the verdict.
    echo "--- BRIEF-H-STUB-TIER detail ---"
    for module in $(grep -E '^FAIL ' "${STAGE_DIR}/stub-tier.log" \
                    | awk '{print $2}' | head -6); do
        echo ">> $module"
        "$VPY" -m unittest "$module" 2>&1 | tail -25
    done
fi
tail -4 "${STAGE_DIR}/stub-tier.log"
echo "BRIEF-H-STUB-TIER: exit=$stub_rc"

# SIX TERMS, NOT FIVE: `entrypoint_rc` joins F's list because acceptance 10
# names the entry points, and a check whose result cannot fail the run is a
# check that reports rather than gates.
if [ "$suite_rc" -eq 0 ] && [ "$stub_rc" -eq 0 ] && [ "$pass1_rc" -eq 0 ] \
        && [ "$crit_rc" -eq 0 ] && [ "$reapply_rc" -eq 0 ] \
        && [ "$entrypoint_rc" -eq 0 ]; then
    echo "BRIEF-H-OVERALL: PASS exit=0"
    exit 0
fi
echo "BRIEF-H-OVERALL: FAIL exit=1 (pass1=$pass1_rc contract=$suite_rc " \
     "stub=$stub_rc criteria=$crit_rc reapply=$reapply_rc " \
     "entrypoints=$entrypoint_rc)"
exit 1
