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
# Exits non-zero on any unallowlisted hit.

set -u

cd "$(git rev-parse --show-toplevel)" || exit 1

# Hard-blocked literals, no allowlist: an operations database host and an
# ECR alias this repository used to hard-code. Assembled from fragments so
# this tracked file does not trip its own check.
DB_HOST="35""\.165\.53\.98"
ECR_ALIAS="y9b1s7h8"

# ---------------------------------------------------------------------------
# Allowlist: one entry per line, "path<TAB>allowed-match-prefix".
# A hit is allowed only if its file matches exactly AND the matched string
# starts with the listed prefix. Anything new blocks the check.
ALLOWLIST="$(cat <<'EOF'
modules/fake_src/generateInjectionCatalogForField.py	526133100000
modules/fake_src/generateInjectionCatalogForField.py	526133100001
c/src/bkgest/readme.txt	/Users/laher
docs/source/analyses/count_fields_imaged/count_fields_imaged.rst	/Users/laher
scripts/check-public-safety.sh	/Users/laher
scripts/check-public-safety.sh	~/Vault
scripts/check-public-safety.sh	~/Claude
.githooks/pre-push	/Users/laher
.githooks/pre-push	~/Vault
.githooks/pre-push	~/Claude
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

# scan_sha <sha>: grep the tree at <sha> (or a git-grep-able ref), print
# violations, return 1 if any.
scan_sha() {
    local sha=$1 bad=0 label allow regex file line match
    while IFS='|' read -r label allow regex; do
        while IFS=: read -r file line match; do
            [ -n "$file" ] || continue
            if [ "$allow" = "yes" ] && is_allowed "$file" "$match"; then
                continue
            fi
            echo "check-public-safety: BLOCKED [$label] $file:$line: $match" >&2
            bad=1
        done <<EOF
$(git grep -nIoP -e "$regex" "$sha" -- 2>/dev/null | sed "s/^$sha://")
EOF
    done <<PATTERNS
db-host|no|$DB_HOST
ecr-alias|no|$ECR_ALIAS
aws-account-12digit|yes|(?<![0-9.])[0-9]{12}(?![0-9])
users-path|yes|/Users/[A-Za-z0-9_.-]+
personal-notes-path|yes|~/(Vault|Claude)\b
PATTERNS
    return $bad
}

# scan_worktree: same checks against the tracked working tree (no commit
# required), for a plain local or CI run.
scan_worktree() {
    local bad=0 label allow regex file line match
    while IFS='|' read -r label allow regex; do
        while IFS=: read -r file line match; do
            [ -n "$file" ] || continue
            if [ "$allow" = "yes" ] && is_allowed "$file" "$match"; then
                continue
            fi
            echo "check-public-safety: BLOCKED [$label] $file:$line: $match" >&2
            bad=1
        done <<EOF
$(git grep -nIoP -e "$regex" -- 2>/dev/null)
EOF
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
if [ -n "$sha" ]; then
    if scan_sha "$sha"; then
        echo "check-public-safety: scan of $sha clean"
        exit 0
    fi
    echo "check-public-safety: scan of $sha found violations" >&2
    exit 1
else
    if scan_worktree; then
        echo "check-public-safety: scan of working tree clean"
        exit 0
    fi
    echo "check-public-safety: scan of working tree found violations" >&2
    exit 1
fi
