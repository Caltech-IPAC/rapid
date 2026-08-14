#!/usr/bin/env bash
#
# The operational-layer unit suite: the stub tier, one interpreter per test
# module. Science-simulation (`sims/`) and RuBR are excluded — they need
# simulation data and third-party stacks this suite does not; everything
# else `pyproject.toml`'s `testpaths` declares as stub-tier is in scope.
#
# Usage: scripts/run-operational-tests.sh [python-interpreter]
#
# Exits nonzero if any module fails, and prints a per-module PASS/FAIL table.
#
# MODULE LIST: DISCOVERED, NOT HAND-MAINTAINED (D5, required outcome 3).
# Until now this script carried its own list of ~48 dotted module names,
# updated by hand whenever a test file was added, moved, or renamed --
# exactly the kind of list that silently falls behind (found while
# retiring it: 15 existing stub-tier files it had never been updated to
# include, among them database/modules/utils/test/test_rapid_db.py and
# every alerts/test/test_*.py file). The list is now `pytest`'s own
# collection: `pytest --collect-only -q`, against the SAME `testpaths` and
# `addopts = "-m 'not contract and not live'"` pyproject.toml declares, so
# discovery and the default `pytest` invocation can never disagree about
# what the stub tier contains. `--import-mode=importlib` matches
# `addopts` (pyproject.toml, D5) -- required for
# `database/modules/utils/test/` to collect at all (see that file's own
# comment: it is the one `test/` package in the tree with an `__init__.py`
# whose parents deliberately have none, and prepend-mode import cannot
# build a unique dotted path through them).
set -u

PY="${1:-python3}"
cd "$(dirname "$0")/.." || exit 2

MODULES="$(
    "${PY}" -m pytest --collect-only -q --import-mode=importlib 2>/dev/null \
        | grep '::' \
        | sed 's/::.*//' \
        | sort -u \
        | sed -e 's/\.py$//' -e 's#/#.#g'
)"
if [ -z "${MODULES}" ]; then
    echo "!! discovery found no stub-tier modules -- pytest collection failed" >&2
    "${PY}" -m pytest --collect-only -q --import-mode=importlib >&2
    exit 2
fi

# Each module runs in its OWN interpreter. That is not tidiness: several of
# these modules install third-party stubs (boto3, psycopg2) into sys.modules at
# import time, so collecting them into one process lets one module's stub be
# imported by the next and produces failures that belong to neither. Run them
# together and ~77 tests fail on stub bleed alone. Discovery replaced the
# hand-maintained NAME LIST only; this per-module subprocess isolation is the
# property that made the original script trustworthy and is unchanged.
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
        out="$("${PY}" -m pytest "${path}" -q --import-mode=importlib 2>&1)"
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
