#!/usr/bin/env bash
#
# science-drift.sh: report which dev-pipeline commits have not yet been
# ported into the rebuild's science/stage modules, and how the two ports
# of the science settings .ini have drifted from dev's current copy.
#
# Every module under rapidpipe/science and rapidpipe/stages, plus
# rapidpipe/settings/difference.toml and reference.toml, carries a
# "# ported-from: ..." header (line 1) naming the dev path(s) it was
# copied from and the dev commit it was pinned at, or "# ported-from:
# none" for rebuild-only code (see AGENTS.md). This script reads those
# headers straight out of the working tree -- it is not a second copy
# of the pin map -- so a header edited in a later pull request is
# picked up automatically, with nothing here to keep in sync.
#
# It never fails the build: every step degrades to a report line rather
# than a nonzero exit, and the script always exits 0 (the CI workflow
# also wraps the call in `|| true`). Run it from anywhere inside the
# repository; it finds the repository root itself.
#
# Usage:
#   scripts/science-drift.sh [--dev-ref <ref>] [--out <file>] [--watch-file <file>]
#
#   --dev-ref <ref>     the dev-pipeline ref to diff against (default origin/dev)
#   --out <file>        write the markdown report here instead of stdout
#   --watch-file <file> the watch list to read (default
#                       scripts/science-drift-watch.txt); a relative
#                       path is resolved against the repository root,
#                       like the default, so a probe passes an absolute
#                       path to a temporary copy to test a bogus or
#                       non-ancestor entry without committing one
#
# A failed history read (git log for a pin, git show for a settings
# .ini) is reported as "read failed" and a Pin error, never silently as
# "no change": SCIENCE_DRIFT_FORCE_FAIL=<dev path> is a testing-only
# hook (never set in CI) that substitutes an all-zero revision for that
# one path's pin, forcing a real git failure to exercise this path.
#
# bash 3.2 compatible (macOS ships bash 3.2 as /bin/bash; this also runs
# under the newer bash on the GitHub Actions Ubuntu runner): no
# associative arrays, no mapfile/readarray, no ${x,,}, and positional
# parameters (`set --`) are used instead of an array that could be
# empty (an empty array expansion under `set -u` is unsafe on bash
# 3.2). grep/sed calls use only switches both BSD (macOS) and GNU
# (Linux) support: no `-P`, no `\b`.

set -uo pipefail

dev_ref="origin/dev"
out_file=""
watch_file="scripts/science-drift-watch.txt"

while [ $# -gt 0 ]; do
  case "${1:-}" in
    --dev-ref)
      dev_ref="${2:-origin/dev}"
      shift 2 2>/dev/null || shift "$#"
      ;;
    --out)
      out_file="${2:-}"
      shift 2 2>/dev/null || shift "$#"
      ;;
    --watch-file)
      watch_file="${2:-scripts/science-drift-watch.txt}"
      shift 2 2>/dev/null || shift "$#"
      ;;
    *)
      shift
      ;;
  esac
done

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "${repo_root:-}" ]; then
  echo "science-drift.sh: not inside a git repository" >&2
  exit 0
fi
cd "$repo_root" 2>/dev/null || exit 0

if [ -n "${out_file:-}" ]; then
  exec > "$out_file"
fi

ref_sha="$(git rev-parse --short=8 "$dev_ref" 2>/dev/null || true)"
if [ -z "${ref_sha:-}" ]; then
  echo "# Science drift report"
  echo
  echo "Dev ref \`$dev_ref\` could not be resolved (not fetched, or does not exist)."
  exit 0
fi

have_python3=0
command -v python3 >/dev/null 2>&1 && have_python3=1

tmpdir="$(mktemp -d 2>/dev/null || true)"
if [ -z "${tmpdir:-}" ]; then
  echo "# Science drift report"
  echo
  echo "Could not create a scratch directory; nothing to report."
  exit 0
fi

triples_file="$tmpdir/triples.tsv"
none_file="$tmpdir/none.txt"
errors_file="$tmpdir/errors.txt"
groups_file="$tmpdir/groups.tsv"
valid_groups_file="$tmpdir/valid_groups.tsv"
commit_rows="$tmpdir/commit_rows.tsv"
by_source_out="$tmpdir/by_source.md"
watch_out="$tmpdir/watched.md"
settings_out="$tmpdir/settings.md"
dev_commits_out="$tmpdir/dev_commits.md"
: > "$triples_file"
: > "$none_file"
: > "$errors_file"
: > "$commit_rows"
: > "$by_source_out"
: > "$watch_out"
: > "$settings_out"
: > "$dev_commits_out"

# A well-formed header, line 1 of "# ported-from: <dev path>[, <dev
# path>...] @ <8-hex commit>" or "# ported-from: none" (the same test
# tests/unit/test_ported_from.py checks).
header_re='^# ported-from: (none|[A-Za-z0-9_./-]+(, [A-Za-z0-9_./-]+)* @ [0-9a-f]{8})$'

# ---------------------------------------------------------------------
# Read every header out of the tree.
# ---------------------------------------------------------------------

all_files="$tmpdir/all_files.txt"
{
  find rapidpipe/science rapidpipe/stages -name '*.py' 2>/dev/null
  [ -f rapidpipe/settings/difference.toml ] && echo rapidpipe/settings/difference.toml
  [ -f rapidpipe/settings/reference.toml ] && echo rapidpipe/settings/reference.toml
} | sort > "$all_files"

while IFS= read -r f; do
  [ -z "${f:-}" ] && continue

  loose_line="$(head -n 5 "$f" 2>/dev/null | grep -E '^# ported-from:' | head -n 1)"
  if [ -z "${loose_line:-}" ]; then
    case "$f" in
      *.py) echo "$f: no ported-from header in the first 5 lines" >> "$errors_file" ;;
    esac
    continue
  fi
  if ! printf '%s\n' "$loose_line" | grep -Eq "$header_re"; then
    echo "$f: malformed ported-from header: $loose_line" >> "$errors_file"
    continue
  fi

  content="${loose_line#\# ported-from: }"
  if [ "$content" = "none" ]; then
    echo "$f" >> "$none_file"
    continue
  fi

  pin="${content##* @ }"
  paths_part="${content% @ *}"

  old_ifs="$IFS"
  IFS=','
  set -f
  set -- $paths_part
  set +f
  IFS="$old_ifs"
  for raw in "$@"; do
    p="$(printf '%s' "$raw" | sed -e 's/^ *//' -e 's/ *$//')"
    [ -z "${p:-}" ] && continue
    printf '%s\t%s\t%s\n' "$f" "$p" "$pin" >> "$triples_file"
  done
done < "$all_files"

cut -f2,3 "$triples_file" | sort -u > "$groups_file"

# ---------------------------------------------------------------------
# Emit one "By source" / "Watched dev paths" block: the header, the
# caller-supplied detail (rebuild files, or a watch reason), and the
# commit log or "no change". A failed `git log` (bad revision, a
# missing object) is never read as "no change": its exit status is
# captured, "read failed" is printed in the block, and a Pin error is
# recorded, so a broken read is always visible.
# ---------------------------------------------------------------------

emit_group_report() {
  local target="$1" path="$2" pin="$3" suffix="$4" detail="$5" tag="$6"
  local log_pin="$pin"
  local log_out log_rc pairs pairs_rc

  if [ "${SCIENCE_DRIFT_FORCE_FAIL:-}" = "$path" ]; then
    log_pin="0000000000000000000000000000000000000000"
  fi

  {
    echo "### \`$path\` @ \`$pin\`$suffix"
    echo
    [ -n "${detail:-}" ] && printf '%s\n' "$detail"
    echo
    log_out="$(git log --oneline --abbrev=8 "$log_pin..$ref_sha" -- "$path" 2>/dev/null)"
    log_rc=$?
    if [ "$log_rc" -ne 0 ]; then
      echo "read failed"
    elif [ -z "${log_out:-}" ]; then
      echo "no change"
    else
      printf '%s\n' "$log_out" | sed 's/^/- /'
    fi
    echo
  } >> "$target"

  if [ "$log_rc" -ne 0 ]; then
    echo "history read failed: $path @ $pin" >> "$errors_file"
    return 0
  fi

  pairs="$(git log --abbrev=8 --date=short --pretty=format:'%h	%ad	%s' "$log_pin..$ref_sha" -- "$path" 2>/dev/null)"
  pairs_rc=$?
  if [ "$pairs_rc" -ne 0 ] || [ -z "${pairs:-}" ]; then
    return 0
  fi
  printf '%s\n' "$pairs" |
    while IFS="$(printf '\t')" read -r sha date subject; do
      [ -z "${sha:-}" ] && continue
      printf '%s\t%s\t%s\t%s\t%s\n' "$sha" "$date" "$subject" "$path" "$tag" >> "$commit_rows"
    done
  return 0
}

# ---------------------------------------------------------------------
# Validate each (dev path, pin) group and run its log.
# ---------------------------------------------------------------------

: > "$valid_groups_file"
while IFS="$(printf '\t')" read -r path pin; do
  [ -z "${path:-}" ] && continue

  kind="$(git cat-file -t "$pin" 2>/dev/null || true)"
  if [ "${kind:-}" != "commit" ]; then
    echo "$path @ $pin: unknown commit" >> "$errors_file"
    continue
  fi
  if ! git merge-base --is-ancestor "$pin" "$ref_sha" 2>/dev/null; then
    echo "$path @ $pin: pin is not an ancestor of $dev_ref ($ref_sha)" >> "$errors_file"
    continue
  fi
  if ! git cat-file -e "${pin}:${path}" 2>/dev/null; then
    echo "$path @ $pin: path absent at the pin" >> "$errors_file"
    continue
  fi
  printf '%s\t%s\n' "$path" "$pin" >> "$valid_groups_file"
done < "$groups_file"

while IFS="$(printf '\t')" read -r path pin; do
  [ -z "${path:-}" ] && continue
  detail="Rebuild files:
$(awk -F'\t' -v p="$path" -v c="$pin" '$2==p && $3==c {print "- `" $1 "`"}' "$triples_file")"
  emit_group_report "$by_source_out" "$path" "$pin" "" "$detail" "normal"
done < "$valid_groups_file"

# ---------------------------------------------------------------------
# The watch list: dev paths not copied, watched for drift. A watched
# path need not exist at its pin (several were created later); that is
# not a Pin error. It does get the same validation a ported pin gets
# (commit exists, is an ancestor of the dev ref), plus a check the
# watch list itself does not need: the watched path must exist at the
# dev ref right now, or watching it is pointless. A line that is not
# "<path> @ <8-hex>" once its trailing "# reason" is stripped is a Pin
# error too, never silently skipped (a blank or comment-only line is
# not a line at all, so it is not an error).
# ---------------------------------------------------------------------

watch_path="$watch_file"
if [ -f "$watch_path" ]; then
  while IFS= read -r wline || [ -n "${wline:-}" ]; do
    case "${wline:-}" in
      ''|'#'*) continue ;;
    esac
    entry="${wline%%#*}"
    reason="${wline#*#}"
    [ "$reason" = "$wline" ] && reason=""
    entry="$(printf '%s' "$entry" | sed -e 's/^ *//' -e 's/ *$//')"
    reason="$(printf '%s' "$reason" | sed -e 's/^ *//' -e 's/ *$//')"
    [ -z "${entry:-}" ] && continue

    malformed=0
    case "$entry" in
      *' @ '*)
        wpath="${entry%% @ *}"
        wpin="${entry##* @ }"
        ;;
      *)
        malformed=1
        ;;
    esac
    if [ "$malformed" = "0" ]; then
      if [ -z "${wpath:-}" ] || [ -z "${wpin:-}" ]; then
        malformed=1
      elif ! printf '%s' "$wpin" | grep -Eq '^[0-9a-f]{8}$'; then
        malformed=1
      fi
    fi
    if [ "$malformed" = "1" ]; then
      echo "malformed watch-list line: $wline" >> "$errors_file"
      continue
    fi

    kind="$(git cat-file -t "$wpin" 2>/dev/null || true)"
    if [ "${kind:-}" != "commit" ]; then
      echo "$wpath @ $wpin (watched): unknown commit" >> "$errors_file"
      continue
    fi
    if ! git merge-base --is-ancestor "$wpin" "$ref_sha" 2>/dev/null; then
      echo "$wpath @ $wpin (watched): pin is not an ancestor of $dev_ref ($ref_sha)" >> "$errors_file"
      continue
    fi
    if ! git cat-file -e "${ref_sha}:${wpath}" 2>/dev/null; then
      echo "$wpath @ $wpin (watched): watched path absent at $ref_sha" >> "$errors_file"
      continue
    fi

    detail=""
    [ -n "${reason:-}" ] && detail="Reason: $reason"
    emit_group_report "$watch_out" "$wpath" "$wpin" " (watched)" "$detail" "watch"
  done < "$watch_path"
fi

# ---------------------------------------------------------------------
# De-duplicated "Dev commits not in the rebuild": walk the ref's own
# history (already newest-first) and keep only the commits our groups
# and watch list found, so the merge across differently-pinned ranges
# comes out in the ref's real order rather than a re-sorted guess.
# ---------------------------------------------------------------------

cut -f1 "$commit_rows" | sort -u > "$tmpdir/shas.txt"
if [ -s "$tmpdir/shas.txt" ]; then
  git log --abbrev=8 --pretty=format:'%h' "$ref_sha" 2>/dev/null | \
    awk -v setfile="$tmpdir/shas.txt" '
      BEGIN { while ((getline line < setfile) > 0) want[line] = 1 }
      ($0 in want)' > "$tmpdir/ordered_shas.txt"
else
  : > "$tmpdir/ordered_shas.txt"
fi

while IFS= read -r sha; do
  [ -z "${sha:-}" ] && continue
  rows="$(awk -F'\t' -v s="$sha" '$1 == s' "$commit_rows")"
  [ -z "${rows:-}" ] && continue
  info="$(printf '%s\n' "$rows" | awk -F'\t' '
    {
      date = $2; subject = $3
      if (!seen[$4]++) { paths = (paths == "" ? $4 : paths ", " $4) }
      if ($5 == "normal") normal = 1
    }
    END { printf "%s\t%s\t%s\t%d\n", date, subject, paths, normal + 0 }
  ')"
  IFS="$(printf '\t')" read -r date subject paths normal <<< "$info"
  tag=""
  [ "${normal:-0}" = "0" ] && tag=" (watched)"
  printf -- "- \`%s\` %s %s%s -- %s\n" "$sha" "${date:-}" "${subject:-}" "$tag" "${paths:-}" >> "$dev_commits_out"
done < "$tmpdir/ordered_shas.txt"

# ---------------------------------------------------------------------
# Settings drift: for each settings toml that carries a pin, diff its
# dev .ini's keys section by section between the pin and the dev ref.
# ---------------------------------------------------------------------

run_ini_diff() {
  local pin="$1"
  local ref="$2"
  local path="$3"
  python3 - "$pin" "$ref" "$path" <<'PYEOF'
import subprocess
import sys
import configparser

pin, ref, path = sys.argv[1], sys.argv[2], sys.argv[3]


def read_ini(rev, path):
    result = subprocess.run(
        ["git", "show", "%s:%s" % (rev, path)],
        capture_output=True, text=True)
    if result.returncode != 0:
        return None
    cp = configparser.RawConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    try:
        cp.read_string(result.stdout)
    except configparser.Error:
        return None
    return cp


old = read_ini(pin, path)
new = read_ini(ref, path)
if old is None or new is None:
    # The exact sentinel science-drift.sh checks for: a failed `git
    # show` (bad revision, or content that will not parse as an .ini)
    # must read as a read failure, never silently as "no key changes".
    print("READ_FAILED")
    sys.exit(0)

sections = sorted(set(old.sections()) | set(new.sections()))
any_diff = False
for section in sections:
    old_keys = dict(old.items(section)) if old.has_section(section) else {}
    new_keys = dict(new.items(section)) if new.has_section(section) else {}
    added = sorted(set(new_keys) - set(old_keys))
    removed = sorted(set(old_keys) - set(new_keys))
    changed = sorted(k for k in (set(old_keys) & set(new_keys))
                      if old_keys[k] != new_keys[k])
    if not (added or removed or changed):
        continue
    any_diff = True
    print("- section `[%s]`:" % section)
    for k in changed:
        print("  - changed `%s`: `%s` -> `%s`" % (k, old_keys[k], new_keys[k]))
    for k in added:
        print("  - added `%s` = `%s`" % (k, new_keys[k]))
    for k in removed:
        print("  - removed `%s` (was `%s`)" % (k, old_keys[k]))

if not any_diff:
    print("(no key changes)")
PYEOF
}

awk -F'\t' '$1 ~ /rapidpipe\/settings\/.*\.toml$/' "$triples_file" > "$tmpdir/toml_triples.tsv"
if [ ! -s "$tmpdir/toml_triples.tsv" ]; then
  echo "(no settings tomls carry a pin)" >> "$settings_out"
elif [ "$have_python3" != "1" ]; then
  echo "(python3 not found on this host; settings drift skipped)" >> "$settings_out"
else
  while IFS="$(printf '\t')" read -r f path pin; do
    [ -z "${f:-}" ] && continue
    diff_pin="$pin"
    if [ "${SCIENCE_DRIFT_FORCE_FAIL:-}" = "$path" ]; then
      diff_pin="0000000000000000000000000000000000000000"
    fi
    diff_out="$(run_ini_diff "$diff_pin" "$ref_sha" "$path")"
    {
      echo "### \`$f\` (pinned to \`$path\` @ \`$pin\`)"
      echo
      if [ "$diff_out" = "READ_FAILED" ]; then
        echo "read failed"
      else
        printf '%s\n' "$diff_out"
      fi
      echo
    } >> "$settings_out"
    if [ "$diff_out" = "READ_FAILED" ]; then
      echo "history read failed: $path @ $pin" >> "$errors_file"
    fi
  done < "$tmpdir/toml_triples.tsv"
fi

# ---------------------------------------------------------------------
# Counts and assembly.
# ---------------------------------------------------------------------

py_pinned_count=$(awk -F'\t' '$1 ~ /\.py$/ {print $1}' "$triples_file" | sort -u | wc -l | tr -d ' ')
py_none_count=$(grep -c '\.py$' "$none_file" 2>/dev/null || true)
py_none_count="${py_none_count:-0}"
toml_pinned_count=$(awk -F'\t' '$1 ~ /\.toml$/ {print $1}' "$triples_file" | sort -u | wc -l | tr -d ' ')
group_count=$(wc -l < "$groups_file" | tr -d ' ')

echo "# Science drift report"
echo
echo "Dev ref: \`$dev_ref\` resolved to \`$ref_sha\`."
echo
echo "Pin counts: $py_pinned_count \`.py\` files pinned, $py_none_count \`.py\` files"
echo "\`none\`, $toml_pinned_count settings tomls pinned, $group_count distinct"
echo "(dev path, pin) groups."
echo
echo "## Dev commits not in the rebuild"
echo
if [ -s "$dev_commits_out" ]; then
  cat "$dev_commits_out"
else
  echo "(none)"
fi
echo
echo "## By source"
echo
if [ -s "$by_source_out" ]; then
  cat "$by_source_out"
else
  echo "(no pinned dev paths found)"
fi
echo "## Watched dev paths"
echo
if [ -s "$watch_out" ]; then
  cat "$watch_out"
else
  echo "(no watch list found, or it is empty)"
fi
echo "## Settings drift"
echo
cat "$settings_out"
echo
echo "## Rebuild-only files"
echo
if [ -s "$none_file" ]; then
  sort "$none_file" | sed 's#.*#- `&`#'
else
  echo "(none)"
fi
echo
echo "## Pin errors"
echo
if [ -s "$errors_file" ]; then
  # -u: an ini pin is validated both as an ordinary "By source" group
  # (git log) and again for its settings-drift diff (git show); a pin
  # that fails both legitimately produces the same message twice.
  sort -u "$errors_file" | sed 's/^/- /'
else
  echo "(none)"
fi

rm -rf "$tmpdir" 2>/dev/null || true

exit 0
