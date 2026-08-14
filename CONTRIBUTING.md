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
