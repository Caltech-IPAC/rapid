#!/usr/bin/env bash
#
# The contract tier: apply the authoritative migration stream to an empty
# PostgreSQL and run the PostgreSQL-backed suite against it (rule 23).
#
# Usage:
#   scripts/run-contract-tests.sh <migrations-dir> [pytest-args...]
#
# The migrations directory is `rapid_systems/cloudformation/db-migrations/`,
# fetched by the caller at a PINNED REVISION — never vendored into this repo
# and never edited (brief B: "consumed as an artifact fetched from
# rapid_systems pinned by revision — do not fork or vendor edited copies").
# This script reads it and nothing else from that side.
#
# THE DATABASE TARGET IS THE ENVIRONMENT'S, not this script's: PGHOST, PGPORT,
# PGUSER, PGPASSWORD, PGDATABASE. That is what makes one script serve both
# venues — a GitHub Actions service container and a podman container on
# rapid-admin differ in host and port and in nothing else observable. Any
# host-standing-up logic belongs to the caller, deliberately: this script
# assumes a reachable empty database and would be wrong to assume more.
#
# SENTINELS. Every verdict line is prefixed `BRIEF-B-` and front-loaded,
# because this runs on rapid-admin through SSM whose StandardOutputContent is
# tail-truncated at ~24KB (aws-ssm-operations: "a verdict that scrolled off is
# a verdict nobody read"). The stream's own NOTICE chatter alone exceeds that,
# so the full transcript goes to a file and only signal reaches stdout.

set -u

MIGRATIONS_DIR=${1:?usage: run-contract-tests.sh <migrations-dir> [pytest-args...]}
shift || true

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT" || exit 2

: "${PGHOST:=127.0.0.1}"
: "${PGPORT:=5432}"
: "${PGUSER:=postgres}"
: "${PGDATABASE:=rapid}"
export PGHOST PGPORT PGUSER PGDATABASE
[ -n "${PGPASSWORD:-}" ] && export PGPASSWORD

# The installed software root, read fail-loud by the science-configuration and
# definition loaders (no compiled-in default — `check-env-policy.sh` enforces
# it). The definitions this suite's fixture and preflight tests read are
# release content shipped under it, so a run rooted at this checkout says so
# rather than letting a caller's ambient value decide.
: "${RAPID_SW:=$REPO_ROOT}"
export RAPID_SW

LOG=${CONTRACT_LOG:-/tmp/brief-b-contract.log}
: > "$LOG"

echo "BRIEF-B-TARGET: host=$PGHOST port=$PGPORT db=$PGDATABASE user=$PGUSER"
echo "BRIEF-B-MIGRATIONS-DIR: $MIGRATIONS_DIR"
echo "BRIEF-B-TRANSCRIPT: $LOG"

if [ ! -d "$MIGRATIONS_DIR" ]; then
    echo "BRIEF-B-APPLY: FAIL exit=2 (no such migrations directory)"
    exit 2
fi

# --- wait for the database to accept connections ----------------------------
# The caller starts the container; this bounded wait is what turns "started"
# into "accepting queries", which are not the same instant and whose gap is
# the most common flaky-CI cause in a suite like this.
ready=0
for _ in $(seq 60); do
    if psql -v ON_ERROR_STOP=1 -d postgres -c 'SELECT 1' >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 2
done
if [ "$ready" -ne 1 ]; then
    echo "BRIEF-B-READY: FAIL exit=1 (database never accepted connections)"
    exit 1
fi
echo "BRIEF-B-READY: PASS exit=0"

# --- the database the stream requires ---------------------------------------
# `002-grants.sql` hardcodes `GRANT CONNECT ON DATABASE rapid`, so the stream
# only applies to a database of that name. Created here if absent rather than
# assumed, so a bare postgres container is a valid starting point.
if ! psql -d postgres -tAc \
     "SELECT 1 FROM pg_database WHERE datname='$PGDATABASE'" \
     2>>"$LOG" | grep -q 1; then
    if psql -v ON_ERROR_STOP=1 -d postgres -c "CREATE DATABASE $PGDATABASE" \
         >>"$LOG" 2>&1; then
        echo "BRIEF-B-CREATEDB: PASS exit=0 ($PGDATABASE)"
    else
        echo "BRIEF-B-CREATEDB: FAIL exit=$? ($PGDATABASE)"
        tail -20 "$LOG"
        exit 1
    fi
else
    echo "BRIEF-B-CREATEDB: SKIP exit=0 ($PGDATABASE already exists)"
fi

# --- apply the full stream, in filename order -------------------------------
# EVERY file, 000 through head — not a subset. The point is that the schema
# under test is the one the applier produces, so a migration depending on
# state a later one removes is caught here rather than in production.
#
# `schema_migrations` is recorded by the APPLIER, never by the migration files
# themselves (each file's own trailer says so), so this loop records each row
# exactly as `apply-db-migrations.sh` does. Without that the table exists and
# stays empty, and the startup preflight this suite tests would have nothing
# to read.
applied=0
for f in "$MIGRATIONS_DIR"/0[0-9][0-9]-*.sql; do
    [ -e "$f" ] || { echo "BRIEF-B-APPLY: FAIL exit=2 (no migration files matched)"; exit 2; }
    fn=$(basename "$f")
    if psql -v ON_ERROR_STOP=1 -f "$f" >>"$LOG" 2>&1; then
        applied=$((applied + 1))
    else
        rc=$?
        echo "BRIEF-B-APPLY: FAIL exit=$rc (failed on $fn after $applied file(s))"
        grep -E 'ERROR|FATAL' "$LOG" | tail -20
        exit "$rc"
    fi
    psql -v ON_ERROR_STOP=1 -c \
        "INSERT INTO schema_migrations (filename) VALUES ('$fn')
         ON CONFLICT DO NOTHING" >>"$LOG" 2>&1
done
echo "BRIEF-B-APPLY: PASS exit=0 ($applied migrations applied and recorded)"

recorded=$(psql -tAc "SELECT count(*) FROM schema_migrations" 2>>"$LOG")
echo "BRIEF-B-SCHEMA-MIGRATIONS: $recorded rows recorded"

# --- the contract suite ------------------------------------------------------
# `-m contract` selects the tier explicitly, overriding the default
# `-m 'not contract and not live'` in pyproject.toml — the contract tier is
# opt-in precisely so a bare `pytest` never tries to open a database.
#
# CONTRACT_PYTHON names the interpreter the suite runs under. It exists
# because rapid-admin's system python3 has neither pytest nor psycopg2 and
# must not be `pip install`ed into — a shared host's system interpreter is
# everyone's. The rapid-admin runner points this at a scratch venv; CI leaves
# it unset and gets the job's own python, which is already the installed one.
PY=${CONTRACT_PYTHON:-python3}
echo "BRIEF-B-PYTHON: $($PY -c 'import sys; print(sys.executable, sys.version.split()[0])')"
echo "BRIEF-B-SUITE: starting"
"$PY" -m pytest pipeline/contract -m contract -p no:cacheprovider \
    --no-header -q "$@" 2>&1 | tee -a "$LOG" | tail -40
suite_rc=${PIPESTATUS[0]}

if [ "$suite_rc" -eq 0 ]; then
    echo "BRIEF-B-SUITE: PASS exit=0"
else
    echo "BRIEF-B-SUITE: FAIL exit=$suite_rc"
    grep -E '^(FAILED|ERROR)' "$LOG" | head -30
fi
exit "$suite_rc"
