#!/usr/bin/env bash
# scripts/dev.sh: work on a branch in a container of the team's pipeline image,
# on a RAPID workstation (or any host with podman or docker and AWS access).
#
#   scripts/dev.sh image latest            The newest rebuild-* tag in the registry.
#   scripts/dev.sh image pull [TAG]        Log in to the registry, pull TAG (default: the
#                                          newest), remember it as this checkout's image.
#   scripts/dev.sh image rev [IMAGE]       The commit the image was built from (what a Batch
#                                          job runs) and the branches that hold it, beside
#                                          this checkout's (what shell and run run).
#   scripts/dev.sh sync [REF]              Fetch origin and check this checkout out at REF (a
#                                          branch, tracking origin's; or a commit, detached);
#                                          refuses a dirty working tree. `rapid dev` runs it
#                                          first with the laptop's own commit.
#   scripts/dev.sh db start|stop|reset|status
#                                          A throwaway PostgreSQL 18 with database/schema.sql
#                                          applied, in the pod the containers share. `start`
#                                          keeps one already running (its runs stay); `stop`
#                                          deletes it, data and all; `reset` is both.
#   scripts/dev.sh shell [--target local|deployed] [--image IMAGE]
#                                          An interactive bash session in a container of the
#                                          image: this checkout at /code ahead of the package
#                                          installed in the image (PYTHONPATH), so the
#                                          checkout's code runs on the image's dependencies;
#                                          work/ at /work, rapidpipe on PATH.
#                                          The target is what the container runs against: the
#                                          throwaway database here (local, the default; started
#                                          if it is not) or the deployed one (deployed).
#   scripts/dev.sh run [--target local|deployed] [--image IMAGE] -- CMD [ARGS...]
#                                          Start the container, run one command, exit, e.g.
#                                          `run -- python -m pytest tests/unit -q` or
#                                          `run --target deployed -- rapidpipe run show 7`.
#
# The local target (the default) talks to the throwaway database through PG*
# and writes products under work/out; the deployed target sets
# RAPID_PARAMETER_PATH to the rebuild tree, so the container reaches the
# deployed database under the host's instance role (rapid_systems
# docs/workstation-submission.md), and takes RAPIDPIPE_OUTPUTS_ROOT from the
# caller's environment. A run against the deployed database is a real run:
# use a dataset of your own.
#
# Nothing here is account-specific: the registry is derived from the caller's
# AWS identity, as the README's Getting started does. The pulled image's
# reference is kept in .rapid-dev/image (git-ignored).
set -euo pipefail

# Inside a container dev.sh started (it sets RAPID_DEV_TARGET there), none of
# this works: the engine and git are on the workstation.
if [ -n "${RAPID_DEV_TARGET:-}" ] && [ "${1:-}" != "--help" ] && [ "${1:-}" != "-h" ]; then
  echo "dev.sh: this is a dev.sh container (target $RAPID_DEV_TARGET); its commands run on an EC2 or laptop, not in here. Exit the container first." >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$ROOT/.rapid-dev"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
REPO="${RAPID_DEV_REPOSITORY:-rapid-pipeline}"
TAG_PREFIX="${RAPID_DEV_TAG_PREFIX:-rebuild-}"
POD="${RAPID_DEV_POD:-rapid-dev}"
DB_CONTAINER="$POD-db"
DB_PASSWORD="${RAPID_DEV_DB_PASSWORD:-local}"
PG_IMAGE="${RAPID_DEV_PG_IMAGE:-docker.io/library/postgres:18}"

die() { echo "dev.sh: $*" >&2; exit 1; }

engine() {
  if [ -n "${RAPID_DEV_ENGINE:-}" ]; then echo "$RAPID_DEV_ENGINE"
  elif command -v podman >/dev/null; then echo podman
  elif command -v docker >/dev/null; then echo docker
  else die "neither podman nor docker is on PATH"; fi
}

registry() {
  local account
  account=$(aws sts get-caller-identity --query Account --output text) \
    || die "no AWS identity (on a workstation the instance role; elsewhere run aws sso login)"
  echo "$account.dkr.ecr.$REGION.amazonaws.com"
}

# ---------------------------------------------------------------------------
# image
# ---------------------------------------------------------------------------

image_latest() {
  local tag
  tag=$(aws ecr describe-images --region "$REGION" --repository-name "$REPO" --filter tagStatus=TAGGED \
    --query "reverse(sort_by(imageDetails[?imageTags[?starts_with(@, '$TAG_PREFIX')]], &imagePushedAt))[0].imageTags[?starts_with(@, '$TAG_PREFIX')] | [0]" \
    --output text)
  case "$tag" in ""|None) die "no $REPO:$TAG_PREFIX* image in the registry" ;; esac
  echo "$tag"
}

image_pull() {
  local tag="${1:-}" reg eng image
  [ -n "$tag" ] || tag=$(image_latest)
  reg=$(registry); eng=$(engine)
  aws ecr get-login-password --region "$REGION" | "$eng" login --username AWS --password-stdin "$reg" >&2
  image="$reg/$REPO:$tag"
  "$eng" pull "$image" >&2
  mkdir -p "$STATE"
  echo "$image" > "$STATE/image"
  echo "$image"
}

image_current() {  # the image to use: --image, $RAPID_DEV_IMAGE, or the last pull
  if [ -n "${1:-}" ]; then echo "$1"
  elif [ -n "${RAPID_DEV_IMAGE:-}" ]; then echo "$RAPID_DEV_IMAGE"
  elif [ -f "$STATE/image" ]; then cat "$STATE/image"
  else die "no image: run 'scripts/dev.sh image pull' first, or pass --image"; fi
}

image_rev() {
  local image sha
  image=$(image_current "${1:-}")
  sha=$("$(engine)" image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image") \
    || die "cannot inspect $image (is it pulled?)"
  [ -n "$sha" ] || die "$image carries no revision label"
  echo "image:    $image"
  echo "commit:   $sha  (the package installed in the image: what a Batch job runs)"
  if git -C "$ROOT" cat-file -e "$sha^{commit}" 2>/dev/null; then
    echo "subject:  $(git -C "$ROOT" log -1 --format='%s (%ad)' --date=short "$sha")"
    echo "branches: $(git -C "$ROOT" branch -r --contains "$sha" --format='%(refname:short)' | tr '\n' ' ')"
  else
    echo "branches: (commit not in this checkout; git fetch and retry)"
  fi
  echo "checkout: $(git -C "$ROOT" rev-parse --short HEAD) $(git -C "$ROOT" branch --show-current || true)  (mounted at /code: what shell and run run)"
}

cmd_image() {
  case "${1:-}" in
    latest) image_latest ;;
    pull) image_pull "${2:-}" ;;
    rev) image_rev "${2:-}" ;;
    *) die "usage: image latest | pull [TAG] | rev [IMAGE]" ;;
  esac
}

# ---------------------------------------------------------------------------
# sync: this checkout at a pushed ref
# ---------------------------------------------------------------------------

cmd_sync() {
  local ref="${1:-}" dirty
  [ -n "$ref" ] || die "usage: sync REF   (a branch or a commit on origin)"
  dirty=$(git -C "$ROOT" status --porcelain --untracked-files=no)
  if [ -n "$dirty" ]; then
    echo "$dirty" >&2
    die "the working tree at $ROOT has uncommitted changes (above); commit or stash them before sync"
  fi
  git -C "$ROOT" fetch --quiet --prune origin || die "git fetch origin failed in $ROOT"
  if git -C "$ROOT" show-ref --verify --quiet "refs/remotes/origin/$ref"; then
    git -C "$ROOT" checkout --quiet -B "$ref" "origin/$ref"
    echo "sync: $ROOT at branch $ref ($(git -C "$ROOT" rev-parse --short HEAD)), tracking origin/$ref" >&2
  elif git -C "$ROOT" cat-file -e "$ref^{commit}" 2>/dev/null \
       && git -C "$ROOT" branch -r --contains "$ref" --format='%(refname)' | grep -q .; then
    git -C "$ROOT" checkout --quiet --detach "$ref"
    echo "sync: $ROOT at commit $(git -C "$ROOT" rev-parse --short HEAD), detached" >&2
  else
    die "'$ref' is not on origin: you forgot to push? (git push from the checkout that has it, then retry)"
  fi
}

# ---------------------------------------------------------------------------
# db: a throwaway PostgreSQL in the pod
# ---------------------------------------------------------------------------

# The containers share a network namespace: a podman pod (the fleet), where the
# database is 127.0.0.1, or a docker user network (a laptop), where it is the
# database container's name.
net_ensure() {
  local eng; eng=$(engine)
  if [ "$eng" = podman ]; then
    "$eng" pod exists "$POD" 2>/dev/null || "$eng" pod create --name "$POD" >/dev/null
  else
    "$eng" network inspect "$POD" >/dev/null 2>&1 || "$eng" network create "$POD" >/dev/null
  fi
}

net_args() { if [ "$(engine)" = podman ]; then echo "--pod=$POD"; else echo "--network=$POD"; fi; }
db_host() { if [ "$(engine)" = podman ]; then echo 127.0.0.1; else echo "$DB_CONTAINER"; fi; }

db_running() {
  local eng; eng=$(engine)
  if [ "$eng" = podman ]; then "$eng" container exists "$DB_CONTAINER" 2>/dev/null
  else [ -n "$("$eng" ps -q --filter "name=^${DB_CONTAINER}$")" ]; fi
}

db_start() {
  local eng; eng=$(engine)
  net_ensure
  if db_running; then echo "db: $DB_CONTAINER already running" >&2; return 0; fi
  "$eng" run -d "$(net_args)" --name "$DB_CONTAINER" -e POSTGRES_PASSWORD="$DB_PASSWORD" \
    -e POSTGRES_DB=rapid "$PG_IMAGE" >/dev/null
  until "$eng" exec "$DB_CONTAINER" pg_isready -h 127.0.0.1 -U postgres -d rapid >/dev/null 2>&1; do sleep 1; done
  "$eng" exec -i "$DB_CONTAINER" psql -U postgres -d rapid -X -q -v ON_ERROR_STOP=1 < "$ROOT/database/schema.sql"
  echo "db: $DB_CONTAINER up, schema applied ($(net_args), host $(db_host), port 5432, user postgres, db rapid)" >&2
}

db_stop() {
  local eng; eng=$(engine)
  db_running && "$eng" rm -f "$DB_CONTAINER" >/dev/null
  echo "db: stopped" >&2
}

db_status() {
  if db_running && "$(engine)" exec "$DB_CONTAINER" pg_isready -h 127.0.0.1 -U postgres -d rapid >/dev/null 2>&1; then
    echo "db: up ($(net_args), host $(db_host))"
  else
    echo "db: down"; return 1
  fi
}

cmd_db() {
  case "${1:-}" in
    start) db_start ;;
    stop) db_stop ;;
    reset) db_stop; db_start ;;
    status) db_status ;;
    *) die "usage: db start | stop | reset | status" ;;
  esac
}

# ---------------------------------------------------------------------------
# shell / run
# ---------------------------------------------------------------------------

#: Put rapidpipe and the image's Python environment on PATH inside the
#: container, then hand over to the command. The image names its interpreter
#: only in the entrypoint script (the Containerfile's RAPID_PYTHON), so the
#: environment's bin directory is read from there.
ENTRY='py=$(sed -n "s/^exec \"\(.*\)\" -m rapidpipe.*/\1/p" /usr/local/bin/rapidpipe-entrypoint);
[ -n "$py" ] && export PATH="$(dirname "$py"):$PATH";
ln -sf /usr/local/bin/rapidpipe-entrypoint /usr/local/bin/rapidpipe; cd /code; exec "$@"'

container_args() {  # target -> the engine's run arguments, one per line
  local target="$1"
  mkdir -p "$ROOT/work/out"
  printf '%s\n' --rm -v "$ROOT:/code:Z" -v "$ROOT/work:/work:Z" -w /code \
    -e PYTHONPATH=/code -e RAPIDPIPE_LOG_LEVEL="${RAPIDPIPE_LOG_LEVEL:-INFO}" \
    -e RAPID_DEV_TARGET="$target"
  case "$target" in
    local)
      db_running || db_start >&2   # keeps a running one and its runs; `db reset` clears it
      printf '%s\n' "$(net_args)" -e PGHOST="$(db_host)" -e PGPORT=5432 -e PGDATABASE=rapid \
        -e PGUSER=postgres -e PGPASSWORD="$DB_PASSWORD" \
        -e RAPIDPIPE_OUTPUTS_ROOT="${RAPIDPIPE_OUTPUTS_ROOT:-file:///work/out}"
      ;;
    deployed)
      [ -n "${RAPIDPIPE_OUTPUTS_ROOT:-}" ] || die "the deployed target: export RAPIDPIPE_OUTPUTS_ROOT (an s3:// scratch prefix) first"
      printf '%s\n' -e RAPID_PARAMETER_PATH="${RAPID_PARAMETER_PATH:-/rapid/rebuild}" \
        -e RAPIDPIPE_OUTPUTS_ROOT="$RAPIDPIPE_OUTPUTS_ROOT"
      for v in RAPIDPIPE_BATCH_JOB_QUEUE AWS_DEFAULT_REGION; do
        [ -n "${!v:-}" ] && printf '%s\n' -e "$v=${!v}"
      done
      ;;
    *) die "unknown target '$target' (local or deployed)" ;;
  esac
}

parse_target() {  # sets TARGET, IMAGE, REST from the argument list
  TARGET=local; IMAGE_ARG=""; REST=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --target) TARGET="${2:-}"; [ -n "$TARGET" ] || die "--target needs a value (local or deployed)"; shift 2 ;;
      --target=*) TARGET="${1#--target=}"; shift ;;
      --image) IMAGE_ARG="${2:-}"; [ -n "$IMAGE_ARG" ] || die "--image needs a value"; shift 2 ;;
      --) shift; REST=("$@"); break ;;
      -*) die "unknown option $1" ;;
      *) REST=("$@"); break ;;
    esac
  done
  case "$TARGET" in local|deployed) ;; *) die "unknown target '$TARGET' (local or deployed)" ;; esac
  IMAGE=$(image_current "$IMAGE_ARG")
}

cmd_shell() {
  parse_target "$@"
  local args=()
  while IFS= read -r line; do args+=("$line"); done < <(container_args "$TARGET")
  echo "dev.sh: target $TARGET, $IMAGE, checkout at /code" >&2
  exec "$(engine)" run -it "${args[@]}" --entrypoint /bin/sh "$IMAGE" -c "$ENTRY" sh /bin/bash
}

cmd_run() {
  parse_target "$@"
  [ ${#REST[@]} -gt 0 ] || die "usage: run [--target local|deployed] [--image IMAGE] -- CMD [ARGS...]"
  local args=()
  while IFS= read -r line; do args+=("$line"); done < <(container_args "$TARGET")
  exec "$(engine)" run "${args[@]}" --entrypoint /bin/sh "$IMAGE" -c "$ENTRY" sh "${REST[@]}"
}

# ---------------------------------------------------------------------------

case "${1:-}" in
  image) shift; cmd_image "$@" ;;
  sync) shift; cmd_sync "$@" ;;
  db) shift; cmd_db "$@" ;;
  shell) shift; cmd_shell "$@" ;;
  run) shift; cmd_run "$@" ;;
  -h|--help|help|"") sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//' ;;
  *) die "unknown command '$1' (image, sync, db, shell, run; --help)" ;;
esac
