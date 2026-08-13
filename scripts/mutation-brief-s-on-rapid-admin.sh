#!/usr/bin/env bash
#
# THE MUTATION CHECK (H round 2's standard): a green test proves nothing
# until it has been shown to go red when the thing it tests is removed.
#
# Two mutations, each reverting exactly one piece of S's wiring, each
# expected to turn its own test RED and nothing else. Every mutation is
# applied to a COPY of the file and reverted immediately after.
#
# Criterion 6 is package S's headline (a FOUND submission must win over the
# clock, however late) and criterion 2 is the placement the brief calls out
# by name as "the single easiest way to get this package subtly wrong" — the
# resolution pass must run BEFORE `poll_once`'s `if not rows:` early return.
# Both get their own mutation rather than trusting the suite's green as
# proof, per the brief's §5.

set -uo pipefail

STAGE_DIR=${1:?usage: mutation-brief-s.sh <stage-dir>}
cd "${STAGE_DIR}/repo" || exit 2
VPY="${STAGE_DIR}/venv/bin/python3"

fail=0

# The integrity baseline, taken before anything is mutated.
sha256sum pipeline/reconciler/service.py > "${STAGE_DIR}/pre-mutation.sha256"

# run_mutation <name> <file> <sed-expr> <test-selection...>
run_mutation() {
    name=$1; file=$2; expr=$3; shift 3
    cp "$file" "${STAGE_DIR}/mutation.bak"
    sed -i "$expr" "$file"
    if diff -q "${STAGE_DIR}/mutation.bak" "$file" >/dev/null 2>&1; then
        # A MUTATION THAT CHANGED NOTHING IS A BROKEN CHECK, not a pass: it
        # would report "the test went red" for a file never edited.
        echo "MUTATION-${name}: BROKEN exit=2 (the sed matched nothing)"
        cp "${STAGE_DIR}/mutation.bak" "$file"
        fail=1
        return
    fi
    "$VPY" -m pytest "$@" -p no:cacheprovider --no-header -q \
        >"${STAGE_DIR}/mutation-${name}.log" 2>&1
    rc=$?
    cp "${STAGE_DIR}/mutation.bak" "$file"
    line=$(grep -E 'passed|failed|error|no tests ran' \
        "${STAGE_DIR}/mutation-${name}.log" | tail -1)
    if [ "$rc" -ne 0 ]; then
        echo "MUTATION-${name}: PASS exit=$rc (test went RED as required) ${line}"
    else
        echo "MUTATION-${name}: FAIL exit=0 (test stayed GREEN — it does not"\
             "actually test the wiring) ${line}"
        fail=1
    fi
}

echo "=== S2-C6: make the clock decide again (revert the FOUND branch)"
run_mutation "S2-C6-FOUND-BRANCH" pipeline/reconciler/service.py \
    "s/if classification == submission_protocol.FOUND:/if classification == '__mutated_out__':/" \
    pipeline/reconciler/test/test_service.py \
    -k a_found_submission_waits_however_late_the_clock_is -m "not live"

echo "=== S1-C2: move the resolution pass below the early return"
run_mutation "S1-C2-PASS-PLACEMENT" pipeline/reconciler/service.py \
    '/self\._resolve_submissions(summary)/d' \
    pipeline/reconciler/test/test_service.py \
    -k the_pass_runs_even_when_zero_attempts_are_open -m "not live"

# THE FILE MUST BE BYTE-IDENTICAL AFTERWARDS. A mutation check that left the
# file mutated would poison every later run in this stage dir — and would do
# it silently, since the next run would simply be testing different code.
echo "=== post-mutation file integrity"
if sha256sum -c "${STAGE_DIR}/pre-mutation.sha256" >"${STAGE_DIR}/integrity.log" 2>&1
then
    echo "MUTATION-CLEANUP: PASS exit=0 (service.py byte-identical)"
else
    echo "MUTATION-CLEANUP: FAIL exit=1 (the mutation was not reverted)"
    grep -v ': OK$' "${STAGE_DIR}/integrity.log" | head
    fail=1
fi

if [ "$fail" -eq 0 ]; then
    echo "MUTATION-OVERALL: PASS exit=0 (every mutation went red)"
    exit 0
fi
echo "MUTATION-OVERALL: FAIL exit=1"
exit 1
