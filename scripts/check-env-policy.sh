#!/usr/bin/env bash
#
# check-env-policy.sh — the environment policy's three mechanical
# prohibitions, checked over the operational path.
#
# design/code-standards.md § Environment variables states rules a reader has
# to apply by hand. These three are the ones a grep can decide, so they are
# checked rather than trusted:
#
#   1. No operational-path code reads STARTREFIMMJDOBS/ENDREFIMMJDOBS. The
#      reference window is release content with a manifest override.
#   2. No operational-path code writes the environment for a downstream
#      reader. The one permitted write is CRDS's own contract interface,
#      which the policy carves out by name.
#   3. No operational-path code silently defaults an AWS region.
#
# SCOPE is the policy's own: `aws/`, `scripts/`, `sims/` and the standalone
# analysis scripts are outside it until promoted, and `c/`, `RuBR/`, `docs/`
# and `soc/` are not Python operational code at all. Test files are excluded
# from 2 and 3: a test that sets an environment variable to prove a
# regression fails is doing its job.
#
# Exits 0 if every check passes; prints each violation otherwise.

set -uo pipefail
cd "$(dirname "$0")/.."

# Prose is not code. Every rule below is about what a line DOES, and the
# files that implement these rules necessarily describe them too — the
# docstring on the region resolver quotes the very pattern it replaced.
#
# Only two things are dropped: comment lines, and lines belonging to a
# STANDALONE string expression (a docstring). A line that merely CONTAINS a
# string literal is kept, because almost every line of real code does — an
# earlier version skipped those and reported a clean tree while a probe
# violating two rules sat in it. The negative control at the bottom of this
# file is what caught that, and is why it is run rather than assumed.
code_only() {
    python3 - "$@" <<'PY'
import ast
import io
import sys
import tokenize

for path in sys.argv[1:]:
    with open(path, "rb") as handle:
        source = handle.read()

    skip = set()
    try:
        for tok in tokenize.tokenize(io.BytesIO(source).readline):
            if tok.type == tokenize.COMMENT:
                skip.update(range(tok.start[0], tok.end[0] + 1))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass

    # Docstrings and any other bare string statement: the whole statement is
    # prose, so the whole span goes.
    try:
        tree = ast.parse(source)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if (isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                skip.update(range(node.lineno, (node.end_lineno or
                                                node.lineno) + 1))

    for number, line in enumerate(source.decode("utf-8", "replace")
                                  .splitlines(), 1):
        if number not in skip:
            print(f"{path}:{number}:{line}")
PY
}

sources() {
    find $PATHS -name '*.py' -not -path '*/test/*' | sort
}

# The operational path: payload jobs, the orchestrator, supervised services,
# and anything they invoke.
PATHS="pipeline submission observability database/modules/utils modules alerts"

fail=0

report() {
    printf '\n== %s ==\n' "$1"
}

CODE=$(code_only $(sources))

check() {
    report "$1"
    hits=$(printf '%s\n' "$CODE" | grep -E "$2" | grep -vE "${3:-\$^}")
    if [ -n "$hits" ]; then
        echo "$hits"; fail=1
    else
        echo "none"
    fi
}

# --- 1. the retired reference-window variables ------------------------------
# The standalone analysis script is excluded BY NAME rather than by pattern:
# it is outside the operational path (it is invoked by nothing), its reads
# are its own arguments, and naming it here means promoting it into the
# pipeline makes this check fail until its window is converted.
check "STARTREFIMMJDOBS / ENDREFIMMJDOBS reads on the operational path" \
      'STARTREFIMMJDOBS|ENDREFIMMJDOBS' \
      'analyzeSciencePipelineProductsForDateTimeRangeWithRefImageWindow\.py'

# --- 2. environment writes for a downstream reader --------------------------
# Two exclusions, both named by the policy itself:
#
# CRDS_SERVER_URL (§ A) is the third-party contract carve-out — CRDS reads
# its own environment and RAPID does not get to choose otherwise. The
# fallback is explicit and logged where it is applied. CRDS_PATH is NOT in
# this list: it is read and logged but never written, because a compiled-in
# cache path is the shape this policy fails loud on elsewhere.
#
# JOBPROCDATE/MAKEREFIMAGESFLAG/STARTDATETIME/ENDDATETIME in the VPO are the
# policy's ONE named temporary exception: the orchestrator's environment
# interface to its four post-DB subprocesses, which expires when those
# become bulk-queue job types (O3). "No new environment transport may be
# added under it" — so this list is exhaustive, and a fifth variable fails
# this check.
check "os.environ writes on the operational path" \
      'os\.environ\[[^]]*\] *=|os\.environ\.update|os\.putenv' \
      'CRDS_SERVER_URL|JOBPROCDATE|MAKEREFIMAGESFLAG|STARTDATETIME|ENDDATETIME'

# --- 3. silent region defaults ----------------------------------------------
check "region reads with a compiled-in default" \
      'environ\.get\("AWS_(DEFAULT_)?REGION", *[^)]|getenv\("AWS_(DEFAULT_)?REGION", *[^)]'

check "hardcoded us-east-1 in operational Python" \
      '"us-east-1"|'"'"'us-east-1'"'"

# --- 4. the software root is never defaulted --------------------------------
check "RAPID_SW reads with a compiled-in default" \
      'get(env)?\("RAPID_SW", *["'"'"']'

# --- negative control -------------------------------------------------------
#
# A checker that cannot fail verifies nothing, and this one silently could
# not: its first version dropped every line containing a string literal,
# which is nearly every line of code, and reported a clean tree with a
# deliberate two-rule violation sitting in it. So the refusal path is
# exercised on every run, against a probe file this script writes and
# removes, rather than trusted.
report "negative control: the checks refuse a known violation"
probe_dir=$(mktemp -d)
trap 'rm -rf "$probe_dir"' EXIT
cat > "$probe_dir/probe.py" <<'PROBE'
"""A docstring mentioning AWS_DEFAULT_REGION, which must NOT trip anything."""
import os

region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
os.environ["RAPID_PROBE"] = "value"
root = os.environ.get("RAPID_SW", "/code")
start = os.getenv("STARTREFIMMJDOBS")
PROBE

probe_code=$(code_only "$probe_dir/probe.py")
caught=0
for pattern in \
    'STARTREFIMMJDOBS|ENDREFIMMJDOBS' \
    'os\.environ\[[^]]*\] *=|os\.environ\.update|os\.putenv' \
    'environ\.get\("AWS_(DEFAULT_)?REGION", *[^)]|getenv\("AWS_(DEFAULT_)?REGION", *[^)]' \
    '"us-east-1"|'"'"'us-east-1'"'" \
    'get(env)?\("RAPID_SW", *["'"'"']'
do
    if printf '%s\n' "$probe_code" | grep -qE "$pattern"; then
        caught=$((caught + 1))
    else
        echo "!! a check did not catch its probe: $pattern" >&2
    fi
done
# The docstring's mention must not be one of the five.
if printf '%s\n' "$probe_code" | grep -q 'A docstring mentioning'; then
    echo "!! prose is reaching the checks; comments would trip them" >&2
    caught=-1
fi
if [ "$caught" -eq 5 ]; then
    echo "all five checks refuse the probe, and prose does not reach them"
else
    echo "!! the negative control did not hold ($caught/5)" >&2
    fail=1
fi

echo
if [ "$fail" -eq 0 ]; then
    echo "ENV-POLICY-OK"
else
    echo "!! environment policy violations above" >&2
fi
exit "$fail"
