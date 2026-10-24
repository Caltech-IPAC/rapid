#!/bin/bash
#
# check-public-safety.sh: scan a tree for identifiers that must not reach
# this public repository (specification, "Repositories": account
# identifiers, bucket names tied to an account and hostnames are injected
# at deploy time, never committed).
#
# Shared scan logic for two callers:
#   - .githooks/pre-push, which scans the tree of each pushed commit
#   - this script, run directly (locally or in CI) against a working tree
#
# Checks, by pattern:
#   1. any 12-digit AWS-account-like number     -> block unless allowlisted
#   2. /Users/<name> personal paths              -> block unless allowlisted
#   3. ~/Vault, ~/Claude personal note paths     -> block unless allowlisted
#   4. the RAPID operations database host IP     -> hard block, no allowlist
#   5. the public ECR alias this repo formerly hard-coded -> hard block
#
# Usage:
#   scripts/check-public-safety.sh                # scan the tracked working tree
#   scripts/check-public-safety.sh <sha-or-ref>    # scan the tree at a commit
#
# Exits 1 on any unallowlisted hit, 2 when git grep itself fails.

set -u

cd "$(git rev-parse --show-toplevel)" || exit 1

# Hard-blocked literals, no allowlist: an operations database host and an
# ECR alias this repository used to hard-code. Assembled from fragments so
# this tracked file does not trip its own check.
DB_HOST="35""\.165\.53\.98"
ECR_ALIAS="y9b1""s7h8"

# ---------------------------------------------------------------------------
# Allowlist: one entry per line, "path<TAB>allowed-match-prefix".
# A hit is allowed only if its file matches exactly AND the matched string
# starts with the listed prefix. Anything new blocks the check.
ALLOWLIST="$(cat <<'EOF'
c/src/bkgest/readme.txt	/Users/laher
docs/source/analyses/count_fields_imaged/count_fields_imaged.rst	/Users/laher
scripts/check-public-safety.sh	/Users/laher
scripts/check-public-safety.sh	~/Vault
scripts/check-public-safety.sh	~/Claude
.githooks/pre-push	/Users/laher
.githooks/pre-push	~/Vault
.githooks/pre-push	~/Claude
docs/foundation-smdc-step.md	634500218910
scripts/check-public-safety.sh	634500218910
EOF
)"

is_allowed() {  # $1 = file path, $2 = matched string
    local path pat
    while IFS=$'\t' read -r path pat; do
        [ -n "$path" ] || continue
        if [ "$1" = "$path" ]; then
            case "$2" in "$pat"*) return 0 ;; esac
        fi
    done <<EOF
$ALLOWLIST
EOF
    return 1
}

# scan [<sha>]: grep the tree at <sha> (a git-grep-able ref), or the
# tracked working tree without one, print violations, return 1 if any and 2
# if git grep itself failed (status above 1: a bad ref, no PCRE), so a
# broken scan never reads as clean.
scan() {
    local sha=${1:-} bad=0 label allow regex entry file line match hits status
    while IFS='|' read -r label allow regex; do
        hits=$(git grep -nIoP -e "$regex" ${sha:+"$sha"} --)
        status=$?
        if [ "$status" -gt 1 ]; then
            echo "check-public-safety: git grep failed (exit $status) for [$label]" >&2
            return 2
        fi
        while IFS= read -r entry; do
            [ -n "$entry" ] || continue
            [ -z "$sha" ] || entry=${entry#"$sha:"}
            IFS=: read -r file line match <<<"$entry"
            if [ "$allow" = "yes" ] && is_allowed "$file" "$match"; then
                continue
            fi
            echo "check-public-safety: BLOCKED [$label] $file:$line: $match" >&2
            bad=1
        done <<HITS
$hits
HITS
    done <<PATTERNS
db-host|no|$DB_HOST
ecr-alias|no|$ECR_ALIAS
aws-account-12digit|yes|(?<![0-9.])[0-9]{12}(?![0-9])
users-path|yes|/Users/[A-Za-z0-9_.-]+
personal-notes-path|yes|~/(Vault|Claude)\b
PATTERNS
    return $bad
}

sha="${1:-}"
scan "$sha"
status=$?
what="${sha:-working tree}"
case $status in
    0) echo "check-public-safety: scan of $what clean"; exit 0 ;;
    1) echo "check-public-safety: scan of $what found violations" >&2; exit 1 ;;
    *) echo "check-public-safety: scan of $what could not run" >&2; exit 2 ;;
esac
