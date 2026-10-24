#!/usr/bin/env bash
#
# check_run.sh: run one run's three checks and print one verdict line.
#
#   scripts/check_run.sh --run <id> [--settings F] [--inspect DIR] [--scas LIST]
#
# First inspect_run.py --run <id> --out DIR (default $TMPDIR/inspect-run-<id>),
# so the run's quality numbers and gallery are read before the checks' minutes;
# then validate_run.py --run <id> (with --scas LIST when given: a second
# loop's pending jobs of other SCAs under the same run are not counted); read_packets.py over the run's bound packet
# sets, eight processes at a time; consume_packets.py --run <id> (with
# --settings F when given). Each one's output goes to stderr as it runs;
# the last line on stdout is
#
#   Check run <id>: CLEAN|FAIL validate <p>/<n> <s> reader <clean>/<sets> <s> consumer <seen>/<expected> <s>
#
# and the exit status is non-zero when any check fails. The database and
# the topic are named as for every rapidpipe command; PY names the Python
# (default python3).

set -uo pipefail
PY=${PY:-python3}
RUN= SETTINGS=() INSPECT= SCAS=()
while [ $# -gt 0 ]; do
  case $1 in
    --run) RUN=$2; shift 2 ;;
    --settings) SETTINGS=(--settings "$2"); shift 2 ;;
    --inspect) INSPECT=$2; shift 2 ;;
    --scas) SCAS=(--scas "$2"); shift 2 ;;
    *) echo "usage: $0 --run <id> [--settings F] [--inspect DIR] [--scas LIST]" >&2; exit 64 ;;
  esac
done
[ -n "$RUN" ] || { echo "usage: $0 --run <id> [--settings F] [--inspect DIR] [--scas LIST]" >&2; exit 64; }
INSPECT=${INSPECT:-${TMPDIR:-/tmp}/inspect-run-$RUN}
cd "$(dirname "$0")/.." || exit 1
export PYTHONPATH=$PWD${PYTHONPATH:+:$PYTHONPATH}
fail=0

"$PY" scripts/inspect_run.py --run "$RUN" --out "$INSPECT" >&2 || fail=1

s=$SECONDS
v=$("$PY" scripts/validate_run.py --run "$RUN" "${SCAS[@]}" | tee -a /dev/stderr | tail -n 1) || fail=1
validate="validate $(grep -o '[0-9]*/[0-9]*' <<<"$v") $((SECONDS - s)) s"

s=$SECONDS
sets=$("$PY" -c 'import sys
from rapidpipe.db.connection import connect
with connect(application_name="check_run") as c, c.cursor() as cur:
    cur.execute("SELECT DISTINCT job FROM packets_official WHERE run = %s ORDER BY job", (int(sys.argv[1]),))
    print(" ".join(f"--job={r[0]}" for r in cur.fetchall()))' "$RUN") || fail=1
n=$(wc -w <<<"$sets")
r=$(xargs -r -n $(( (n + 7) / 8 > 0 ? (n + 7) / 8 : 1 )) -P 8 "$PY" scripts/read_packets.py <<<"$sets" | tee -a /dev/stderr) || fail=1
clean=$(grep -c ' | 0 |$' <<<"$r")
[ "$clean" -eq "$n" ] || fail=1
reader="reader $clean/$n $((SECONDS - s)) s"

s=$SECONDS
c=$("$PY" scripts/consume_packets.py --run "$RUN" "${SETTINGS[@]}" | tee -a /dev/stderr | grep '^Consumer check') || fail=1
consumer="consumer $(sed -n 's/.*expected \([0-9]*\) seen \([0-9]*\).*/\2\/\1/p' <<<"$c") $((SECONDS - s)) s"

echo "Check run $RUN: $( ((fail)) && echo FAIL || echo CLEAN) $validate $reader $consumer"
exit $fail
