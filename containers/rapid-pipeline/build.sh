#!/usr/bin/env bash
# containers/rapid-pipeline/build.sh <git-ref> [--base <image-ref>]
#     [--tag <name>] [--install-requirements] [--allow-dirty]
#     [--python <path>]
#
# Builds the rapid-pipeline image locally from this repository's own
# source, for CI or a developer's own inspection. It never pushes and
# never names a registry -- publishing an image is rapid_systems' job
# (specification, "Repositories": "A `rapid` release supplies the source
# commit; `rapid_systems` builds and publishes the image and records the
# resulting digest").
#
# <git-ref> is a branch, tag or commit SHA to build from; `HEAD` builds
# the current checkout. Resolved to a full SHA with `git rev-parse` and
# recorded as the image's revision label, so the image is traceable back
# to an exact commit even when the working tree has since moved on.
#
# By default the working copy must be clean at the requested ref (release
# images come from a recorded commit, not an uncommitted edit) --
# --allow-dirty lifts that for a scratch build against the working tree
# as it stands, matching build.sh's own uncommitted-scratch-run allowance
# elsewhere in this project.
#
# Filtering: this script archives the requested commit into a temporary
# src/ build context, which the Containerfile copies to /code. The archive
# is an explicit include list (INCLUDE_PATHS below), not an exclude list,
# so a new top-level directory stays out of the image until named here:
#   rapidpipe/               -- the package itself.
#   cdf/                     -- SExtractor/SWarp configuration files the
#                               stages read at /code/cdf (settings
#                               `cfg_path`).
#   modules/zogy, modules/sfft
#                            -- py_zogy.py and sfft_rapid*.py, run as
#                               scripts from /code/modules/... (settings
#                               `zogy_code`, `sfft_code`).
#   modules/sip_tpv          -- imported by science/difference/resample.py.
#   database/modules/utils/roman_tessellation{,_db}.py
#                            -- imported by science/spatial.py and
#                               science/load/catalogs.py.
#   pyproject.toml, requirements.txt, LICENSE, README.md
#                            -- what `pip install --no-deps /code` reads.
# Left out, among others: docs/, c/ (replaced at runtime by the base
# image's rapid-* C-tool RPMs, rapid_systems/containers/rapid-base),
# tests/, scripts/, .github/, and database/migrations with
# apply-migrations.sh (the release migrate hook reads those from the
# tagged git tree, not from the image).
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

usage() {
  cat <<'EOF'
usage: build.sh <git-ref> [--base <image-ref>] [--tag <name>]
                 [--install-requirements] [--allow-dirty] [--python <path>]
                 [--engine podman|docker]

  <git-ref>                a branch, tag or commit SHA (or HEAD)
  --base <image-ref>       full base image reference (required)
  --tag <name>             image tag (default: rapid-pipeline:<short-sha>)
  --install-requirements   install requirements.txt into the base at build
                            time; only for a stand-in base with no science
                            stack (see .github/workflows/container.yml).
                            Never use for a release build.
  --allow-dirty            build the working tree as-is even if it has
                            uncommitted changes
  --python <path>          the base image's Python interpreter path
                            (default: /opt/rapid/conda/envs/rapid/bin/python)
  --engine podman|docker   container engine to build with (default: podman
                            if present, else docker). Pass explicitly when
                            a later step (e.g. `docker run` in CI) must see
                            the built image in a specific engine's store.
EOF
}

git_ref=""
base_image=""
tag=""
install_requirements=0
allow_dirty=0
python_path="/opt/rapid/conda/envs/rapid/bin/python"
engine=""

while [ $# -gt 0 ]; do
  case "$1" in
    --base) base_image="$2"; shift 2 ;;
    --tag) tag="$2"; shift 2 ;;
    --install-requirements) install_requirements=1; shift ;;
    --allow-dirty) allow_dirty=1; shift ;;
    --python) python_path="$2"; shift 2 ;;
    --engine) engine="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    -*)
      echo "build.sh: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      if [ -n "$git_ref" ]; then
        echo "build.sh: unexpected extra argument: $1" >&2
        usage >&2
        exit 2
      fi
      git_ref="$1"
      shift
      ;;
  esac
done

if [ -z "$git_ref" ]; then
  echo "build.sh: a git ref is required" >&2
  usage >&2
  exit 2
fi
if [ -z "$base_image" ]; then
  echo "build.sh: --base <image-ref> is required" >&2
  usage >&2
  exit 2
fi

source_sha="$(git -C "$repo_root" rev-parse "$git_ref")"

if [ "$allow_dirty" -eq 0 ]; then
  if ! git -C "$repo_root" diff --quiet -- && [ "$git_ref" = "HEAD" ]; then
    echo "build.sh: working tree has uncommitted changes; commit them or pass --allow-dirty" >&2
    exit 1
  fi
fi

if [ -z "$tag" ]; then
  tag="rapid-pipeline:${source_sha:0:7}"
fi

build_date="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

build_context="$script_dir"
src_dir="${build_context}/src"

cleanup() {
  rm -rf "$src_dir"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

INCLUDE_PATHS=(
  rapidpipe
  cdf
  modules/zogy
  modules/sfft
  modules/sip_tpv
  database/modules/utils/roman_tessellation.py
  database/modules/utils/roman_tessellation_db.py
  pyproject.toml
  requirements.txt
  LICENSE
  README.md
)

echo "build.sh: archiving ${source_sha:0:7} (${#INCLUDE_PATHS[@]} included paths) ..." >&2
rm -rf "$src_dir"
mkdir -p "$src_dir"
git -C "$repo_root" archive "$source_sha" -- "${INCLUDE_PATHS[@]}" \
  | tar -x -C "$src_dir"

if [ -n "$engine" ]; then
  command -v "$engine" >/dev/null 2>&1 || { echo "build.sh: engine not found: $engine" >&2; exit 1; }
elif command -v podman >/dev/null 2>&1; then
  engine="podman"
elif command -v docker >/dev/null 2>&1; then
  engine="docker"
else
  echo "build.sh: neither podman nor docker found" >&2
  exit 1
fi

echo "build.sh: building ${tag} with ${engine} (base ${base_image}, source ${source_sha:0:7}) ..." >&2
"$engine" build \
  --build-arg "RAPID_BASE_IMAGE=${base_image}" \
  --build-arg "RAPID_SOURCE_REVISION=${source_sha}" \
  --build-arg "RAPID_BUILD_DATE=${build_date}" \
  --build-arg "RAPID_PYTHON=${python_path}" \
  --build-arg "RAPID_INSTALL_REQUIREMENTS=${install_requirements}" \
  -f "${script_dir}/Containerfile" \
  -t "${tag}" \
  "${build_context}"

image_id="$("$engine" images --no-trunc --format '{{.ID}}' "${tag}" | head -n1)"

echo "build.sh: built ${tag} (${image_id})"
echo "TAG=${tag}"
echo "IMAGE_ID=${image_id}"
echo "SOURCE_REVISION=${source_sha}"
