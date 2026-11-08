#!/usr/bin/env bash
# podrun.sh CPU CODE -- ARGS: python in the pipeline image, its threads pinned to CPU by taskset (rootless
# podman has no cpuset controller), the job definition's 24 GiB, the working directory $XRB_HOST_ROOT at
# /t1. CODE 1 mounts $XRB_CODE (the branch) at /exp ahead of the image's rapidpipe; RAPID_EXP_* pass through.
# Needs XRB_IMAGE, XRB_HOST_ROOT and AWS credentials the host's AWS config resolves (mounted read-only).
set -euo pipefail
cpu=$1; code=$2; shift 2
extra=()
if [ "$code" = 1 ]; then extra=(-v "$XRB_CODE":/exp:ro -e PYTHONPATH=/exp); fi
for v in $(compgen -e | grep "^RAPID_EXP_" || true); do extra+=(-e "$v=${!v}"); done
exec podman run --rm --network host --security-opt label=disable --memory 24576m \
  -v "$XRB_HOST_ROOT":/t1 -v "$HOME/.aws":/aws:ro \
  -e AWS_CONFIG_FILE=/aws/config -e AWS_PROFILE -e AWS_DEFAULT_REGION -e RAPID_PARAMETER_PATH \
  -e TMPDIR=/t1/tmp "${extra[@]}" -w /t1 --entrypoint /usr/bin/taskset \
  "$XRB_IMAGE" -c "$cpu" /opt/rapid/conda/envs/rapid/bin/python3 "$@"
