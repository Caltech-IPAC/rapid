#!/usr/bin/env bash
#
# rapidctl on rapid-admin, with the container's AWS identity chained into
# rapid-orchestrator-role -- the sanctioned way to run `run register` (and
# anything else that reads an attempt's terminal record) from the admin host.
#
# WHY THE ROLE CHAIN IS NEEDED. Registration reads each attempt's terminal
# closure record from s3://roman-rapid-records. The admin host's own instance
# role (rapid-admin-instance-role) has no s3:GetObject there -- it returns
# AccessDenied, observed live 2026-09-11 -- while rapid-orchestrator-role is
# allowed, and the admin role already holds sts:AssumeRole on it via policy
# rapid-admin-assume-orchestrator. So this is the intended path and NOT a
# privilege widening: no IAM change is required or made here. Registration
# running as the orchestrator is also what the pipeline itself does; the admin
# host is borrowing the pipeline's identity to do the pipeline's work.
#
# WHY BOTH PGUSER AND THE ROLE CHAIN. They are two different identities doing
# two different jobs, and both are required. `pipeline.operatorctl.session.
# operator_session` connects as the PERSONAL login (PGUSER) and assumes an
# operate tier from it, so the mutation audit ledger keeps naming the human --
# `derived.write_mutation_audit` records `session_user`, which `SET ROLE` does
# not change. The AWS role chain is only about S3 read access. Supplying the
# chain without PGUSER fails with "no login role: set PGUSER to the personal
# login role granted rapid_operator".
#
# CREDENTIALS NEVER REACH A COMMAND LINE. Both the database password and the
# assumed-role keys are written to a mode-0600 env file consumed by podman's
# --env-file, and the file is removed on every exit path. Nothing is passed as
# an argument, put in the environment of a logged command, or sent in SSM
# command content -- an SSM document's parameters are retained and readable,
# which is why this is a host-side script rather than an inline command.
#
# USAGE, from rapid-admin:
#
#     scripts/rapidctl-as-orchestrator.sh run register --name <run> [--apply]
#     scripts/rapidctl-as-orchestrator.sh run compare <run-a> <run-b>
#
# All arguments are passed through to `python3 -m pipeline.operatorctl.main`.
# Set RAPID_SRC to run a working tree instead of the image's baked code (the
# same override `bulk_run.rst`'s "Running a working tree" section describes);
# unset, the image's own code runs.
#
# Set PODMAN_ROOT to use an alternate podman store root. This exists because
# the production store can be left holding a layer podman cannot delete --
# "invalid cross-device link" on a rename into its own tempdirs, which is how
# xfs project quotas report a refused cross-project move -- and in that state
# every new `podman run` from it fails while already-running containers keep
# working. An isolated root is a non-destructive way past it that leaves the
# production store and the running services untouched (observed on rapid-admin
# 2026-09-12; the store itself needs repair separately).

set -uo pipefail

export AWS_REGION=${AWS_REGION:-us-east-1}
export AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-us-east-1}

# The role name is stable; the ACCOUNT is resolved at runtime from STS rather
# than written here. The repo's pre-push hook refuses a committed 12-digit
# account id, and rightly: an account number in a source file is both an
# identifier this repo does not publish and a value that silently wrongs any
# other account the script is run in. Asking STS also means the script can
# only ever target the account it is actually authenticated to.
ORCHESTRATOR_ROLE_NAME=${ORCHESTRATOR_ROLE_NAME:-rapid-orchestrator-role}
JOB_DEFINITION=${JOB_DEFINITION:-rapid-pipeline-science}

# The personal login. `~/.rapid-pg.env` is the host's own operator credential
# file; PGUSER/PGPASSWORD already in the environment win, so a caller can
# supply them another way.
if [ -z "${PGUSER:-}" ] && [ -r "$HOME/.rapid-pg.env" ]; then
    # shellcheck disable=SC1090,SC1091
    . "$HOME/.rapid-pg.env"
fi
if [ -z "${PGUSER:-}" ]; then
    echo "rapidctl-as-orchestrator: PGUSER is not set and ~/.rapid-pg.env is" \
         "not readable; the operator session needs the personal login" >&2
    exit 78
fi

DIGEST=$(aws batch describe-job-definitions \
    --job-definition-name "$JOB_DEFINITION" --status ACTIVE \
    --query 'jobDefinitions[0].containerProperties.image' --output text) || exit 75
if [ -z "$DIGEST" ] || [ "$DIGEST" = "None" ]; then
    echo "rapidctl-as-orchestrator: could not resolve the active image for" \
         "job definition $JOB_DEFINITION" >&2
    exit 75
fi

# The pipeline parameter tree is the source for the database coordinates, the
# same tree the pipeline itself reads -- never defaults compiled in here.
tree=$(aws ssm get-parameters-by-path --path /rapid/pipeline --recursive \
    --query 'Parameters[].[Name,Value]' --output text) || exit 75
param() { echo "$tree" | awk -v key="$1" '$1 == key { print $2 }'; }

if [ -z "${ORCHESTRATOR_ROLE_ARN:-}" ]; then
    account=$(aws sts get-caller-identity --query Account --output text) || exit 77
    if [ -z "$account" ] || [ "$account" = "None" ]; then
        echo "rapidctl-as-orchestrator: could not resolve the AWS account" \
             "from STS; is a profile or instance role available?" >&2
        exit 77
    fi
    ORCHESTRATOR_ROLE_ARN="arn:aws:iam::${account}:role/${ORCHESTRATOR_ROLE_NAME}"
fi

creds=$(aws sts assume-role --role-arn "$ORCHESTRATOR_ROLE_ARN" \
    --role-session-name "rapidctl-$(date -u +%Y%m%dT%H%M%SZ)" \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
    --output text) || exit 77

envfile=$(mktemp "${TMPDIR:-/tmp}/rapidctl-orch-env.XXXXXX") || exit 74
chmod 600 "$envfile"
trap 'rm -f "$envfile"' EXIT
trap 'rm -f "$envfile"; exit 130' INT TERM

{
    echo "PGUSER=${PGUSER}"
    echo "PGPASSWORD=${PGPASSWORD:-}"
    # DBUSER/DBPASS AS WELL AS PGUSER/PGPASSWORD, because the two halves of
    # the codebase read different variables for the same login. The operator
    # session (`pipeline.operatorctl.session`) reads PGUSER/PGPASSWORD; the
    # product layer (`database.modules.utils.rapid_db`) reads DBUSER/DBPASS,
    # or a Secrets Manager secret named by RAPID_DB_SECRET_ID. Subcommands
    # that reach the product layer -- `run start`, and anything that opens a
    # RAPIDDB handle of its own rather than borrowing the session's
    # connection -- refuse with "environment variable(s) not set: DBUSER,
    # DBPASS" when only the PG* pair is supplied.
    echo "DBUSER=${PGUSER}"
    echo "DBPASS=${PGPASSWORD:-}"
    echo "DBSERVER=$(param /rapid/pipeline/db/server)"
    echo "DBPORT=$(param /rapid/pipeline/db/port)"
    echo "DBNAME=$(param /rapid/pipeline/db/name)"
    echo "RAPID_MANIFEST_BUCKET=$(param /rapid/pipeline/s3/products-bucket)"
    echo "RAPID_IMAGE_DIGEST=${DIGEST##*@}"
    echo "AWS_REGION=${AWS_REGION}"
    echo "AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION}"
    echo "AWS_ACCESS_KEY_ID=$(echo "$creds" | awk '{print $1}')"
    echo "AWS_SECRET_ACCESS_KEY=$(echo "$creds" | awk '{print $2}')"
    echo "AWS_SESSION_TOKEN=$(echo "$creds" | awk '{print $3}')"
    [ -n "${RAPID_RELEASE_IDENTITY:-}" ] && \
        echo "RAPID_RELEASE_IDENTITY=${RAPID_RELEASE_IDENTITY}"
    [ -n "${RAPID_OPERATOR_TIER:-}" ] && \
        echo "RAPID_OPERATOR_TIER=${RAPID_OPERATOR_TIER}"
    [ -n "${RAPID_SRC:-}" ] && echo "PYTHONPATH=/code"
} > "$envfile"

mounts=()
if [ -n "${RAPID_SRC:-}" ]; then
    # `z` relabels for SELinux; without it the container cannot read the
    # bind mount at all (Permission denied opening the mounted path).
    mounts+=(-v "${RAPID_SRC}:/code:ro,z" -w /code)
fi

store=()
if [ -n "${PODMAN_ROOT:-}" ]; then
    store+=(--root "${PODMAN_ROOT}")
fi

sudo podman "${store[@]}" run --rm --network host --env-file "$envfile" \
    "${mounts[@]}" --entrypoint python3.11 "$DIGEST" \
    -m pipeline.operatorctl.main "$@"
rc=$?
echo "RAPIDCTL_RC=${rc}"
exit "$rc"
