#!/usr/bin/env bash
# apply-migrations.sh [--dry-run|--status] [migrations-dir]
#
# Applies database/migrations/*.sql (or the given directory) to a
# PostgreSQL database in filename order, tracking what has been applied in
# a schema_migrations table so a re-run only applies what is new. See
# database/migrations/README.md for the rules this implements.
#
# Reads connection parameters from the standard PG* environment variables
# ONLY: PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD. Nothing
# account-specific, no secret-store lookups, no other environment
# variables consulted.
#
# Portable to bash 3.2 (macOS's /bin/bash) and to Ubuntu's bash: no
# arrays with negative indices, no mapfile, no associative arrays.
set -euo pipefail

usage() {
  echo "usage: $0 [--dry-run|--status] [migrations-dir]" >&2
  exit 2
}

mode=apply
dir=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) mode=dry-run ;;
    --status) mode=status ;;
    -h|--help) usage ;;
    -*) echo "!! unrecognized option: $arg" >&2; usage ;;
    *)
      if [ -n "$dir" ]; then
        echo "!! unexpected extra argument: $arg" >&2
        usage
      fi
      dir="$arg"
      ;;
  esac
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
: "${dir:=$script_dir/migrations}"

if [ ! -d "$dir" ]; then
  echo "!! migrations directory not found: $dir" >&2
  exit 1
fi

: "${PGHOST:?PGHOST must be set}"
: "${PGPORT:?PGPORT must be set}"
: "${PGDATABASE:?PGDATABASE must be set}"
: "${PGUSER:?PGUSER must be set}"
: "${PGPASSWORD:?PGPASSWORD must be set}"
export PGHOST PGPORT PGDATABASE PGUSER PGPASSWORD

psql_q() {
  # Quiet, tuples-only, unaligned; stops on first error.
  psql -X -q -v ON_ERROR_STOP=1 -tA "$@"
}

sha256_of() {
  # macOS ships shasum, not sha256sum; Ubuntu ships sha256sum, not
  # necessarily shasum. Try both rather than assume the platform.
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    echo "!! neither sha256sum nor shasum is available" >&2
    return 1
  fi
}

ensure_tracking_table() {
  psql_q -c "
    CREATE TABLE IF NOT EXISTS schema_migrations (
      filename   text PRIMARY KEY,
      sha256     text NOT NULL,
      applied_at timestamptz NOT NULL DEFAULT now()
    );
  " >/dev/null
}

recorded_sha256_for() {
  # Empty output means not recorded. -tA already gives a bare value or
  # nothing; no row is simply empty stdout.
  psql_q -c "SELECT sha256 FROM schema_migrations WHERE filename = '$1';"
}

# Collect migration files in filename order. No arrays-with-negative-index
# or mapfile use, for bash 3.2 portability.
files=$(find "$dir" -maxdepth 1 -type f -name '[0-9][0-9][0-9]-*.sql' | sort)

if [ -z "$files" ]; then
  echo "!! no NNN-*.sql files found in $dir" >&2
  exit 1
fi

ensure_tracking_table

if [ "$mode" = "status" ]; then
  echo "applied:"
  psql_q -c "SELECT '  ' || filename || '  (' || applied_at || ')' FROM schema_migrations ORDER BY filename;"
  echo "pending:"
  any_pending=0
  printf '%s\n' "$files" | while IFS= read -r f; do
    fn=$(basename "$f")
    recorded=$(recorded_sha256_for "$fn")
    if [ -z "$recorded" ]; then
      echo "  $fn"
    fi
  done
  exit 0
fi

exit_status=0
printf '%s\n' "$files" | while IFS= read -r f; do
  fn=$(basename "$f")
  current_sha=$(sha256_of "$f")
  recorded_sha=$(recorded_sha256_for "$fn")

  if [ -n "$recorded_sha" ]; then
    if [ "$recorded_sha" != "$current_sha" ]; then
      echo "!! $fn was already applied with sha256 $recorded_sha but now hashes to $current_sha — an applied migration must never be edited; refusing to proceed" >&2
      exit 1
    fi
    if [ "$mode" = "apply" ]; then
      echo ">> $fn already applied, skipping"
    else
      echo ">> $fn already applied (dry run)"
    fi
    continue
  fi

  if [ "$mode" = "dry-run" ]; then
    echo ">> would apply $fn"
    continue
  fi

  echo ">> applying $fn"
  # Explicit exit-status check, not reliance on `set -e` propagating out of
  # this loop: the loop body runs inside a `while` that is itself piped
  # from `printf` and wrapped in `|| exit_status=$?` below, and bash
  # suspends `-e` for every command that is part of such a conditional
  # list -- including commands inside a loop that IS that list's last
  # element. A failing psql here would otherwise fall through silently to
  # the "applied" echo and the next iteration (found live: a deliberately
  # broken CREATE INDEX printed ">> applied ... " and left
  # schema_migrations with zero rows, exit 0 throughout).
  if ! psql -X -v ON_ERROR_STOP=1 --single-transaction -f "$f" \
    -c "INSERT INTO schema_migrations (filename, sha256) VALUES ('$fn', '$current_sha');"; then
    echo "!! applying $fn failed — see the psql error above; nothing from this file was committed (--single-transaction)" >&2
    exit 1
  fi
  echo ">> applied $fn (sha256 $current_sha)"
done || exit_status=$?

exit "$exit_status"
