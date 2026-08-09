#!/usr/bin/env bash
#
# The operational-layer unit suite: the payload/submission/reconciler modules
# the Batch payload co-design owns. Science-stage and RuBR tests are excluded —
# they need simulation data and third-party stacks this suite does not.
#
# Usage: scripts/run-operational-tests.sh [python-interpreter]
#
# Exits nonzero if any module fails, and prints a per-module PASS/FAIL table.

set -u

PY="${1:-python3}"
cd "$(dirname "$0")/.." || exit 2

MODULES="
observability.test.test_attempts
observability.test.test_intervals
observability.test.test_registration
observability.test.test_submission_integration
pipeline.entrypoints.test.test_job
pipeline.reconciler.test.test_closure
pipeline.reconciler.test.test_horizons
pipeline.reconciler.test.test_main
pipeline.reconciler.test.test_retention
pipeline.reconciler.test.test_scheduler
pipeline.reconciler.test.test_service
pipeline.registration.test.test_consumer
pipeline.registration.test.test_products
pipeline.runtime.test.test_boundaries
pipeline.runtime.test.test_environment
pipeline.runtime.test.test_errors
pipeline.runtime.test.test_logging_setup
pipeline.runtime.test.test_ownership
pipeline.runtime.test.test_process
pipeline.runtime.test.test_science_config
pipeline.runtime.test.test_stages
pipeline.runtime.test.test_termination
pipeline.runtime.test.test_workdir
pipeline.stages.test.test_context
pipeline.stages.test.test_post_db
pipeline.stages.test.test_publishing
pipeline.stages.test.test_sequences
pipeline.test.test_mosaic_geometry
pipeline.test.test_operator
pipeline.test.test_seams
pipeline.test.test_vpo_phases
submission.test.test_batching
submission.test.test_gathering
submission.test.test_manifest
submission.test.test_manifest_vocabulary
submission.test.test_routes
submission.test.test_startup
submission.test.test_submit
"

# Each module runs in its OWN interpreter. That is not tidiness: several of
# these modules install third-party stubs (boto3, psycopg2) into sys.modules at
# import time, so collecting them into one process lets one module's stub be
# imported by the next and produces failures that belong to neither. Run them
# together and ~77 tests fail on stub bleed alone.
rc=0
total=0
for module in ${MODULES}; do
    # Some modules are pytest-native (bare functions, fixtures) and cannot be
    # loaded by the unittest loader; run each under unittest first and fall
    # back to pytest on a loader error rather than maintaining two lists.
    out="$("${PY}" -m unittest "${module}" 2>&1)"
    status=$?
    if printf '%s\n' "${out}" | grep -qE '_FailedTest|NO TESTS RAN'; then
        path="$(printf '%s\n' "${module}" | tr '.' '/').py"
        out="$("${PY}" -m pytest "${path}" -q 2>&1)"
        status=$?
        ran="$(printf '%s\n' "${out}" | grep -E '[0-9]+ (passed|failed)' | tail -1)"
        count="$(printf '%s\n' "${out}" | grep -oE '[0-9]+ passed' | head -1 | awk '{print $1}')"
        if [ -z "${count}" ]; then
            count=0
        fi
        total=$((total + count))
        if [ ${status} -eq 0 ]; then
            printf 'PASS  %-52s %s\n' "${module}" "${ran}"
        else
            rc=1
            printf 'FAIL  %-52s %s\n' "${module}" "${ran}"
            printf '%s\n' "${out}" | grep -E '^FAILED|^ERROR' | sed 's/^/        /'
        fi
        continue
    fi
    ran="$(printf '%s\n' "${out}" | grep -E '^Ran [0-9]+ test' | head -1)"
    count="$(printf '%s\n' "${ran}" | awk '{print $2}')"
    if [ -z "${count}" ]; then
        count=0
    fi
    total=$((total + count))
    if [ ${status} -eq 0 ]; then
        printf 'PASS  %-52s %s\n' "${module}" "${ran}"
    else
        rc=1
        printf 'FAIL  %-52s %s\n' "${module}" "${ran}"
        printf '%s\n' "${out}" | grep -E '^(FAIL|ERROR):' | sed 's/^/        /'
    fi
done

printf '\n%s tests across %s modules\n' "${total}" "$(printf '%s\n' ${MODULES} | grep -c .)"
if [ ${rc} -eq 0 ]; then
    printf 'RESULT: PASS\n'
else
    printf 'RESULT: FAIL\n'
fi
exit ${rc}
