# Contributing to RAPID

Thank you for your interest in contributing to RAPID (Roman Alerts Promptly from Image Differencing). This document provides guidelines for contributing to the project.

## Code of Conduct

This project adheres to a [Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold this code.

## How to Contribute

### Reporting Issues

- Use the GitHub Issues tracker to report bugs or request features.
- Before opening a new issue, search existing issues to avoid duplicates.
- When reporting a bug, include:
  - A clear, descriptive title
  - Steps to reproduce the issue
  - Expected vs. actual behavior
  - Your environment (OS, Python version, relevant package versions)

### Submitting Changes

1. Fork the repository and create a feature branch from `main`.
2. Make your changes in the feature branch.
3. Write or update tests as appropriate.
4. Ensure all tests pass before submitting.
5. Submit a pull request with a clear description of the changes.

Changes intended for the deployed SMDC environment branch from `smdc`
instead — see below.

### The `smdc` branch

RAPID runs on the NASA Science Mission Directorate Cloud (SMDC), and the
branch that environment actually runs is `smdc`, not `main`. Every
pipeline image built for the account is built from an `smdc` commit, and
six consumers — two AWS Batch job definitions and three long-running
services — are pinned to the digest that build produced.

The practical consequence for a contributor: **a change merged to `main`
does not reach the deployed environment.** If your change is meant to run
on SMDC, branch from `smdc` and open your pull request against `smdc`.
The two branches converge at cutover, when `smdc` becomes the project's
development line; until then, `smdc` is what runs and carries commits
`main` does not.

`contract-tests.yml` is the workflow that runs on `smdc`, on push to any
branch, so you can see it run without opening a pull request. It is a
test gate only: it builds and pushes no image.

### Deployment

Merging to `smdc` does not deploy. Deployment is a separate, deliberate
act performed by someone with access to the SMDC account: the pipeline
image is rebuilt from your commit and all six consumers are repinned to
the resulting digest. Until that happens, the environment keeps running
the previously pinned image, and the repository and the environment
disagree by design.

Nothing in this repository performs that deployment or can. The procedure
lives in the infrastructure repository (see below), and repinning fewer
than all six consumers is a known failure mode with its own check — a
2026-08-14 incident left the Batch job definitions five days stale on a
pre-fix digest while the services were current, and Batch silently ran
the old code.

If your change needs to reach the environment on a particular timescale,
say so in the pull request. It will not happen automatically.

### The `rapid_systems` checkout the contract tier needs

The contract test tier runs the suite against a real PostgreSQL built
from the authoritative database migrations, and those migrations do not
live in this repository — they live in `rapid_systems`, which is a
separate, **private** repository under the `IPAC-SW` organization.

So the contract tier needs a local checkout of `rapid_systems` and
therefore access to that organization, which is granted per person rather
than being public. The stub tier needs neither, which is why it is the
default and the tier CI and `git push` gate on. If you do not have
`rapid_systems` access, the stub tier is the tier you can run, and a
change that genuinely needs contract coverage should say so in the pull
request so someone with access runs it.

`rapid_systems` is also where the deployment procedure, the operational
runbooks, and the campaign documentation live. See
[`pipeline/contract/README.md`](pipeline/contract/README.md) for exactly
what the contract tier expects.

### Running the Tests

The suite has three tiers — stub, contract, and live — described in full in
[`pipeline/contract/README.md`](pipeline/contract/README.md). For most
changes, the stub tier is what you need:

    pip install -e '.[test]'
    RAPID_SW="$PWD" scripts/run-operational-tests.sh

It needs no database and no network — psycopg2/boto3 are stubbed into
`sys.modules` — and is what CI and `git push` both gate on. `RAPID_SW` is
read fail-loud with no compiled-in default (the science configuration is
release content, not something to guess from the working directory); for
a checkout-rooted run, point it at the checkout.

If your change touches SQL, the migration stream, or anything the stub
tier's fakes cannot faithfully model, it likely belongs in the contract
tier instead, which runs the same suite against a real PostgreSQL built
from the authoritative `rapid_systems` migrations. See
[`pipeline/contract/README.md`](pipeline/contract/README.md) for what it
needs (a live Postgres with Q3C, `PGHOST`/`PGPORT`/etc., `RAPID_SW`, and a
private `rapid_systems` checkout) and how to run it.

### Pull Request Guidelines

- Keep pull requests focused — one feature or fix per PR.
- Include a clear description of what the PR does and why.
- Reference any related issues (e.g., "Fixes #42").
- Ensure your code follows the existing style and conventions of the project.
- Update documentation if your changes affect it.

### Coding Standards

- Follow PEP 8 for Python code.
- Include docstrings for public functions, classes, and modules.
- Write meaningful commit messages.

## Review and Acceptance

Please note that all contributions are subject to review. The project maintainers reserve the right to accept or reject any contribution at their discretion.

## Questions

If you have questions about contributing, please open an issue on GitHub or consult the [documentation](https://caltech-ipac-rapid.readthedocs.io/en/latest/).
