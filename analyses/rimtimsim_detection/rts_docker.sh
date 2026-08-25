#!/bin/bash
# Run a rimtimsim_detection stage inside the RAPID pipeline container.
#
# Nothing here is specific to one person or one machine.  The three things that
# vary -- where the working directory lives, where the shared product cache lives,
# and where AWS credentials live -- are environment variables with sane defaults.
#
#   RTS_HOST_WORK    working directory on the host            (required)
#   RTS_HOST_CACHE   shared product cache on the host         (default: inside work)
#   RTS_HOST_CATALOGS  shared variable-delivery directory     (default: inside work)
#   RTS_HOST_AWS     AWS credential directory                 (default: $HOME/.aws)
#   RTS_IMAGE        container image                          (default: rapid_science_pipeline:1.0)
#   RTS_CACHE_POLICY keep | discard                           (default: from the config)
#   DOCK_NAME        container name; implies detached (-d)
#
# Examples:
#   RTS_HOST_WORK=/data/me/rts ./rts_docker.sh "python3.11 -m analyses.rimtimsim_detection.cli truth"
#
#   # share the existing 100 GB product cache instead of downloading another copy
#   RTS_HOST_WORK=/data/me/rts RTS_HOST_CACHE=/data/jj/work/rts_downselect/cache/img \
#     DOCK_NAME=mysweep ./rts_docker.sh "python3.11 -m analyses.rimtimsim_detection.cli sweep"
set -euo pipefail

if [ $# -eq 0 ]; then
    sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
fi

: "${RTS_HOST_WORK:?set RTS_HOST_WORK to the working directory on this host}"
RTS_HOST_AWS="${RTS_HOST_AWS:-$HOME/.aws}"
RTS_IMAGE="${RTS_IMAGE:-rapid_science_pipeline:1.0}"

# The repo root is two levels above this script, so the package is importable as
# analyses.rimtimsim_detection no matter where the clone lives.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

mkdir -p "$RTS_HOST_WORK"

MOUNTS=(-v "$RTS_HOST_WORK:/work" -v "$REPO:/rtscode:ro")
ENVS=(-e HOME=/work -e RTS_WORK=/work -e PYTHONPATH=/rtscode
      -e RTS_SEX=/code/c/bin/sex -e RTS_CDF=/code/cdf -e MPLCONFIGDIR=/work/.mpl)

# The container has no git binary, so resolve the revision out here and pass it in
# -- otherwise every containerised run records its provenance as "unknown".
if GIT_SHA="$(git -C "$REPO" rev-parse HEAD 2>/dev/null)" && [ -n "$GIT_SHA" ]; then
    if [ -n "$(git -C "$REPO" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
        GIT_SHA="$GIT_SHA-dirty"
    fi
    ENVS+=(-e "RTS_GIT_SHA=$GIT_SHA")
fi

# A shared cache is mounted separately and writable: fetches land in the shared
# copy so the next person does not re-download them.
if [ -n "${RTS_HOST_CACHE:-}" ]; then
    mkdir -p "$RTS_HOST_CACHE"
    MOUNTS+=(-v "$RTS_HOST_CACHE:/cache")
    ENVS+=(-e RTS_CACHE=/cache)
fi

if [ -n "${RTS_HOST_CATALOGS:-}" ]; then
    MOUNTS+=(-v "$RTS_HOST_CATALOGS:/catalogs:ro")
    ENVS+=(-e RTS_CATALOGS=/catalogs)
fi

if [ -d "$RTS_HOST_AWS" ]; then
    MOUNTS+=(-v "$RTS_HOST_AWS:/aws:ro")
    ENVS+=(-e AWS_SHARED_CREDENTIALS_FILE=/aws/credentials -e AWS_CONFIG_FILE=/aws/config)
fi

[ -n "${RTS_CACHE_POLICY:-}" ] && ENVS+=(-e "RTS_CACHE_POLICY=$RTS_CACHE_POLICY")

# Named runs go to the background; anonymous ones stay attached.
DETACH=()
if [ -n "${DOCK_NAME:-}" ]; then
    DETACH=(-d --name "$DOCK_NAME")
fi

# --user keeps everything the container writes owned by the caller rather than root.
exec docker run --rm "${DETACH[@]}" \
    --user "$(id -u):$(id -g)" \
    "${MOUNTS[@]}" "${ENVS[@]}" \
    --entrypoint bash "$RTS_IMAGE" -c "cd /work && $*"
