#!/usr/bin/env bash
#
# THE MUTATION CHECK (H round 2's standard): a green test proves nothing
# until it has been shown to go red when the thing it tests is removed.
#
# Six mutations, each reverting exactly one piece of R's wiring, each
# expected to turn its own test RED and nothing else. Every mutation is
# applied to a COPY of the file and reverted immediately after.

set -uo pipefail

STAGE_DIR=${1:?usage: mutation-r.sh <stage-dir>}
cd "${STAGE_DIR}/repo" || exit 2
VPY="${STAGE_DIR}/venv/bin/python3"

fail=0

# The integrity baseline, taken before anything is mutated.
sha256sum \
    pipeline/operator/registrar.py \
    pipeline/reconciler/main.py \
    pipeline/operator/service.py \
    pipeline/entrypoints/job.py \
    pipeline/publisher/service.py \
    > "${STAGE_DIR}/pre-mutation.sha256"

# run_mutation <name> <file> <sed-expr> <test-selection...>
run_mutation() {
    name=$1; file=$2; expr=$3; shift 3
    cp "$file" "${STAGE_DIR}/mutation.bak"
    sed -i "$expr" "$file"
    if diff -q "${STAGE_DIR}/mutation.bak" "$file" >/dev/null 2>&1; then
        # A MUTATION THAT CHANGED NOTHING IS A BROKEN CHECK, not a pass:
        # it would report "the test went red" for a file never edited.
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

echo "=== R1: remove the identity_repository argument from the LIVE registrar"
run_mutation "R1-WIRING" pipeline/operator/registrar.py \
    's/^                         identity_repository=identity_repository_for(conn))/                         )/' \
    pipeline/contract/test_live_registrar_identity.py -m contract

echo "=== R1: make the probe always report 048 present (breaks degradation)"
run_mutation "R1-PROBE" pipeline/operator/registrar.py \
    's/^        if not present:/        if False:/' \
    pipeline/contract/test_live_registrar_identity.py \
    -k degrades_to_legacy_only -m contract

echo "=== R2: remove each entry point's application-contract call"
run_mutation "R2-RECONCILER" pipeline/reconciler/main.py \
    's/^    verify_application_contract(ConnectionExecutor(conn).execute)/    pass/' \
    pipeline/contract/test_publisher_startup.py -k reconciler_preflights -m contract

run_mutation "R2-OPERATOR" pipeline/operator/service.py \
    's/^        identity = verify_application_contract(execute)/        identity = {"release_identity": "x"}/' \
    pipeline/contract/test_publisher_startup.py -k operator_preflights -m contract

run_mutation "R2-JOB" pipeline/entrypoints/job.py \
    's/^        verify_application_contract(execute.execute)/        pass/' \
    pipeline/contract/test_publisher_startup.py -k job_payload_preflights -m contract

run_mutation "R2-PUBLISHER" pipeline/publisher/service.py \
    's/^    identity = verify_application_contract(ConnectionExecutor(conn).execute)/    identity = {"release_identity": "x"}/' \
    pipeline/contract/test_publisher_startup.py -k publisher_preflights -m contract

# THE FILES MUST BE BYTE-IDENTICAL AFTERWARDS. A mutation check that left a
# file mutated would poison every later run in this stage dir — and would do
# it silently, since the next run would simply be testing different code.
#
# Compared against sha256sums taken BEFORE the first mutation, not against
# git: the staged tree is an extracted tarball with no repository in it, so
# `git status` there reports on whatever enclosing directory it finds, or
# nothing at all. A check that cannot fail is not a check.
echo "=== post-mutation file integrity"
if sha256sum -c "${STAGE_DIR}/pre-mutation.sha256" >"${STAGE_DIR}/integrity.log" 2>&1
then
    echo "MUTATION-CLEANUP: PASS exit=0 (all five files byte-identical)"
else
    echo "MUTATION-CLEANUP: FAIL exit=1 (a mutation was not reverted)"
    grep -v ': OK$' "${STAGE_DIR}/integrity.log" | head
    fail=1
fi

if [ "$fail" -eq 0 ]; then
    echo "MUTATION-OVERALL: PASS exit=0 (every mutation went red)"
    exit 0
fi
echo "MUTATION-OVERALL: FAIL exit=1"
exit 1
