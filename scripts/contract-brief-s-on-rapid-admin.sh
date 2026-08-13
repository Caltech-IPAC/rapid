#!/usr/bin/env bash
#
# Run brief S's acceptance on rapid-admin: the contract tier against a
# throwaway PostgreSQL built from the authoritative migration stream, then
# the stub tier.
#
# ONE PASS, NOT TWO. Brief R's runner (and every sibling before it) applied
# base then base+drafts, because `migrations-draft/` held unadopted
# migrations and the point was proving CI's skip-clean behaviour on the base
# stream. That is now obsolete for every brief: 044-052 were adopted
# verbatim into the authoritative `rapid_systems` stream on 2026-08-12
# (`migrations-draft/README.md`), `migrations-draft/` holds only notes, and
# `smdc` CI's own pinned revision already carries them. So there is no draft
# to apply, no draft to re-apply for idempotence, and no "does it skip
# cleanly without the draft" property left to demonstrate here — the
# authoritative stream alone now includes migration 044 (`submissions`),
# which is brief S's whole subject. The zero-skip gate stays: it is what
# makes "the criteria actually ran" provable regardless of how many passes
# get you there.
#
# Derived from `contract-brief-r-on-rapid-admin.sh`, which carries the two
# fixes this inherits and both are load-bearing here: the CRITERIA
# AGGREGATION fix (`crit_rc`, so a red criterion actually fails the run
# rather than being printed and dropped) and the ZERO-SKIP GATE (so a
# criterion that silently skipped cannot report as passing).
#
# The shape it inherits, and the comments that explain each choice: run ON
# the host, staged there by the caller, taking no AWS actions of its own,
# generating the throwaway cluster's password locally so it never enters an
# SSM command's content (command history is readable for ~30 days with no
# delete API).
#
# Usage: contract-brief-s-on-rapid-admin.sh <stage-dir> <image> <run-id>
#   stage-dir : holds `db-migrations/` (the stream), `repo/` (this repo)
#   image     : the PostgreSQL+Q3C image, digest-pinned
#   run-id    : namespaces the container and the scratch footprint
#
# TEARDOWN IS THIS SCRIPT'S OWN: the container is removed on exit, success or
# failure, by the trap. rapid-admin is a SHARED team host — nothing here ever
# touches an existing container, an existing database, or rapid-db.

set -u

STAGE_DIR=${1:?usage: contract-brief-s-on-rapid-admin.sh <stage-dir> <image> <run-id>}
IMAGE=${2:?usage: contract-brief-s-on-rapid-admin.sh <stage-dir> <image> <run-id>}
RUN_ID=${3:?usage: contract-brief-s-on-rapid-admin.sh <stage-dir> <image> <run-id>}

: "${HOME:=/root}"          # AWS-RunShellScript runs with no login environment
export HOME

CONTAINER="brief-s-pg-${RUN_ID}"
DBNAME=rapid
# A nonstandard port, distinct from every prior brief's (B 55432, C 55433,
# G 55434, D 55435, H 55436, E 55437, R 55438) so a leftover of any of those
# runs cannot collide with this one.
PGPORT_HOST=${PGPORT_HOST:-55439}
PGPW="brief-s-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"

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

echo "BRIEF-S-RUN-ID: $RUN_ID"
echo "BRIEF-S-IMAGE: $IMAGE"

podman rm -f "$CONTAINER" >/dev/null 2>&1 || true

echo ">> starting throwaway PostgreSQL as $CONTAINER on port $PGPORT_HOST"
if ! podman run -d --name "$CONTAINER" \
        -e POSTGRES_PASSWORD="$PGPW" \
        -e POSTGRES_DB=postgres \
        -p "127.0.0.1:${PGPORT_HOST}:5432" \
        "$IMAGE" >>"$LOG" 2>&1; then
    echo "BRIEF-S-CONTAINER: FAIL exit=1 (could not start)"
    tail -20 "$LOG"
    exit 1
fi
echo "BRIEF-S-CONTAINER: PASS exit=0 ($CONTAINER)"

cd "${STAGE_DIR}/repo" || exit 2

python3 -m venv "${STAGE_DIR}/venv" >>"$LOG" 2>&1 || {
    echo "BRIEF-S-VENV: FAIL exit=1"; tail -10 "$LOG"; exit 1; }
VPY="${STAGE_DIR}/venv/bin/python3"
"$VPY" -m pip install --quiet --upgrade pip >>"$LOG" 2>&1

if "$VPY" -m pip install --quiet -e '.[test]' >>"$LOG" 2>&1; then
    echo "BRIEF-S-PIP-INSTALL-E: PASS exit=0"
else
    echo "BRIEF-S-PIP-INSTALL-E: FAIL exit=1"
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
: "${RAPID_SW:=${STAGE_DIR}/repo}"
export RAPID_SW

# --- THE ONE PASS: the authoritative stream, which now includes 044 --------
echo "BRIEF-S-PASS: authoritative stream (044-055 already adopted)"
./scripts/run-contract-tests.sh "${STAGE_DIR}/db-migrations" \
    >"${STAGE_DIR}/pass.log" 2>&1
pass_rc=$?
grep -E '^BRIEF-B-(APPLY|SUITE|SCHEMA-MIGRATIONS)' "${STAGE_DIR}/pass.log" | head -5
pass_line=$(grep -E '^[0-9]+ (passed|failed)|passed|skipped' \
    "${STAGE_DIR}/contract-pytest.log" 2>/dev/null | tail -1)
echo "BRIEF-S-PASS-RESULT: ${pass_line:-<no summary line>}"

# ZERO SKIPS, ENFORCED RATHER THAN ASSUMED.
#
# Brief D's standard, restated here for one pass instead of two: "the
# recorded acceptance run executes with ZERO brief-specific skips, and the
# ledger states the skip count." `test_submission_protocol.py`'s tests
# stayed skipped for every prior brief's acceptance run (044 was a DRAFT
# then); this run is the first where they are expected to fire for real, and
# this gate is what turns that expectation into a checked fact rather than a
# claim. A skip here — the probe misfiring, a fixture bailing early, the
# stream somehow missing 044 — must fail the run, not report success with
# package S's entire subject silently unexercised.
pass_skips=$(grep -cE '^SKIPPED' "${STAGE_DIR}/pass.log" 2>/dev/null)
pass_skips=${pass_skips:-0}
echo "BRIEF-S-PASS-SKIPS: $pass_skips"
if [ "$pass_skips" -ne 0 ]; then
    echo "BRIEF-S-PASS-SKIPS: FAIL exit=1 ($pass_skips skip(s) in the"\
         "recorded acceptance run; the brief requires zero)"
    grep -E '^SKIPPED' "${STAGE_DIR}/pass.log" | head -20
    pass_rc=1
else
    echo "BRIEF-S-PASS-SKIPS: PASS exit=0 (zero skips, as the brief requires)"
fi

if [ "$pass_rc" -ne 0 ]; then
    echo "--- BRIEF-S-PASS failures ---"
    grep -E '^(FAILED|ERROR)' "${STAGE_DIR}/contract-pytest.log" | head -20
    grep -E '^E ' "${STAGE_DIR}/contract-pytest.log" | head -25
fi
echo "BRIEF-S-CONTRACT-SUITE: exit=$pass_rc"

# THE CRITERIA, NAMED INDIVIDUALLY. A suite-level "N passed" does not say
# WHICH tests ran, and the criteria are the deliverable — so each brief-S
# criterion is re-run with its own selection and its own verdict line,
# front-loaded where the 24KB truncation cannot reach it.
#
# The `-k` expressions are passed as ONE argument, not split on spaces: brief
# G's version of this loop word-split a multi-term `-k` expression into
# separate arguments, so pytest read a bare `or` as a file path and reported
# "no tests ran" with exit 4 — a criterion that looked selected and never
# ran. Hence four colon-separated fields (name, target, -k, -m) rather than a
# single args string, split by prefix/suffix removal so an empty middle field
# stays empty instead of collapsing and shifting the ones after it.
#
# S1's criteria (1-5) are STUB-tier — `poll_once`'s wiring needs no database,
# only FakeConnection/FakeBatch — so they carry `-m "not live"` rather than
# `contract`: this repo's `addopts` is `-m 'not contract and not live'`, so a
# bare marker-less selection still inherits that default and a stub-tier
# line with no explicit `-m` would silently collect nothing (exit 5) while
# looking selected. S2's criteria (6-11) are the same stub-tier file. 12 is
# the protocol invariant, also stub-tier (FakeBatch has no submit_job at
# all). 13 is contract-tier — durability needs a real second connection.
crit_rc=0
for spec in \
    "S1-RUNS-EACH-CYCLE:pipeline/reconciler/test/test_service.py:resolve_open_runs_once_per_cycle_via_the_batch_describer:not live" \
    "S1-RUNS-WITH-ZERO-OPEN:pipeline/reconciler/test/test_service.py:the_pass_runs_even_when_zero_attempts_are_open:not live" \
    "S1-RAISE-DOES-NOT-KILL:pipeline/reconciler/test/test_service.py:a_raising_describe_does_not_kill_the_cycle:not live" \
    "S1-PRE-044-DEGRADES:pipeline/reconciler/test/test_service.py:a_pre_044_database_degrades_quietly:not live" \
    "S1-OUTCOMES-IN-SUMMARY:pipeline/reconciler/test/test_service.py:resolution_outcomes_appear_in_the_summary:not live" \
    "S2-FOUND-WAITS-PAST-HORIZON:pipeline/reconciler/test/test_service.py:a_found_submission_waits_however_late_the_clock_is:not live" \
    "S2-LOST-SKIPS-THE_CLOCK:pipeline/reconciler/test/test_service.py:a_lost_submission_classifies_without_waiting_on_the_horizon:not live" \
    "S2-NO-ROW-UNCHANGED:pipeline/reconciler/test/test_service.py:no_submission_row_classifies_at_the_horizon_unchanged:not live" \
    "S2-OPEN-STILL-WAITS:pipeline/reconciler/test/test_service.py:an_open_submission_inside_the_horizon_still_waits:not live" \
    "S2-REDIRECT-PATH-FOUND:pipeline/reconciler/test/test_service.py:the_redirect_path_also_honours_a_found_submission:not live" \
    "S2-ATTEMPT-RAN-PRESERVED:pipeline/reconciler/test/test_service.py:the_attempt_ran_distinction_is_preserved_under_lost:not live" \
    "S2-FAILS-OPEN:pipeline/reconciler/test/test_service.py:a_raising_submission_lookup_falls_through_to_the_horizon:not live" \
    "S-NEVER-SUBMITS:pipeline/reconciler/test/test_service.py:never_calls_submit_job_reaching_this_path:not live" \
    "S-RESOLVE-OPEN-FIRST-COVERAGE:pipeline/contract/test_submission_protocol.py:resolve_open_resolves_every_open_row_in_one_pass:contract" \
    "S-RESOLVE-OPEN-ONE-FAILS:pipeline/contract/test_submission_protocol.py:resolve_open_one_rows_failure_does_not_stop_the_others:contract" \
    "S-LOOKUP-READS-LINKED-ROW:pipeline/contract/test_submission_protocol.py:submission_for_attempt_reads_the_linked_row:contract" \
    "S-LOOKUP-NONE-ON-NO-LINK:pipeline/contract/test_submission_protocol.py:submission_for_attempt_returns_none_with_no_link:contract" \
    "S-DURABLE-SECOND-CONN:pipeline/contract/test_submission_protocol.py:a_resolution_pass_is_visible_from_a_second_connection:contract" ; do
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
    echo "BRIEF-S-${name}: exit=$rc ${line}"
    if [ "$rc" -ne 0 ]; then
        # A FAILING CRITERION FAILS THE RUN — the fix brief R's own history
        # names: an earlier loop shape printed each criterion's exit code and
        # then dropped it, so a red criterion whose tests were outside the
        # main suite selection still reported BRIEF-*-OVERALL: PASS.
        crit_rc=1
        grep -E '^(FAILED|ERROR)' "${STAGE_DIR}/${name}.log" | head -10
        grep -E '^E ' "${STAGE_DIR}/${name}.log" | head -15
    fi
done
echo "BRIEF-S-CRITERIA: exit=$crit_rc"

# --- THE MUTATION CHECK ------------------------------------------------------
# A green criterion proves nothing until the test has been shown to go RED
# when the wiring it tests is removed (H round 2's standard). Criterion 6
# (a FOUND submission waits past the horizon) is package S's headline and
# gets its own mutation; criterion 2 (the pass runs with zero open attempts)
# is named in the brief as the single easiest way to ship this subtly broken
# and gets its own mutation too. Both need only the stub tier, so they run
# against the venv already built above.
"${STAGE_DIR}/repo/scripts/mutation-brief-s-on-rapid-admin.sh" "$STAGE_DIR" \
    >"${STAGE_DIR}/mutation.log" 2>&1
mutation_rc=$?
grep -E '^(MUTATION-|=== )' "${STAGE_DIR}/mutation.log"
if [ "$mutation_rc" -ne 0 ]; then
    echo "--- BRIEF-S-MUTATION detail ---"
    tail -25 "${STAGE_DIR}/mutation.log"
fi
echo "BRIEF-S-MUTATION: exit=$mutation_rc"

# --- The stubbed tier stays green, unchanged ---------------------------------
RAPID_SW="${STAGE_DIR}/repo" ./scripts/run-operational-tests.sh "$VPY" \
    >"${STAGE_DIR}/stub-tier.log" 2>&1
stub_rc=$?
if [ "$stub_rc" -ne 0 ]; then
    echo "--- BRIEF-S-STUB-TIER failures ---"
    grep -E '^FAIL |^ *FAIL: ' "${STAGE_DIR}/stub-tier.log" | head -25
    echo "--- BRIEF-S-STUB-TIER detail ---"
    for module in $(grep -E '^FAIL ' "${STAGE_DIR}/stub-tier.log" \
                    | awk '{print $2}' | head -6); do
        echo ">> $module"
        "$VPY" -m unittest "$module" 2>&1 | tail -25
    done
fi
tail -4 "${STAGE_DIR}/stub-tier.log"
echo "BRIEF-S-STUB-TIER: exit=$stub_rc"

# FIVE TERMS: contract suite, stub tier, criteria, mutation. No `reapply_rc`
# and no `entrypoint_rc` — S adds no migration (nothing to re-apply) and
# touches no entry point (nothing new to preflight); carrying either term
# forward from R would report on properties this package does not claim.
if [ "$pass_rc" -eq 0 ] && [ "$stub_rc" -eq 0 ] \
        && [ "$crit_rc" -eq 0 ] && [ "$mutation_rc" -eq 0 ]; then
    echo "BRIEF-S-OVERALL: PASS exit=0"
    exit 0
fi
echo "BRIEF-S-OVERALL: FAIL exit=1 (contract=$pass_rc stub=$stub_rc " \
     "criteria=$crit_rc mutation=$mutation_rc)"
exit 1
