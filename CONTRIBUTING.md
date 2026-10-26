# Contributing to RAPID

Guidelines for contributing to RAPID (Roman Alerts Promptly from Image Differencing).

## Code of Conduct

The project follows a [Code of Conduct](CODE_OF_CONDUCT.md), which all participants are expected to uphold.

## Reporting Issues

Use the GitHub Issues tracker to report bugs or request features. Search existing issues first to avoid duplicates. Bug reports should include:

- A clear, descriptive title
- Steps to reproduce the issue
- Expected vs. actual behavior
- Your environment (OS, Python version, relevant package versions)

## Submitting Changes

Keep each pull request (PR) focused on one feature or fix.

1. Fork the repository and create a feature branch from `rebuild`; pull requests target `rebuild`.
2. Make your changes in the feature branch.
   - Follow the project's existing style and conventions, including PEP 8 for Python code.
   - Include docstrings for public functions, classes, and modules.
   - Write meaningful commit messages.
   - Update documentation affected by your changes.
3. Write or update tests as appropriate.
4. Ensure all tests pass before submitting.
5. Submit a pull request describing what changed and why. Reference any related issues (e.g., "Fixes #42").

## Review and Acceptance

- A person opens a pull request to `rebuild` and merges it once every CI check is green. The pull request names the [rapid_docs](https://roman-rapid.readthedocs.io/en/latest/) page it implements or corrects, and a page that disagrees with the code is corrected first ([AGENTS.md](AGENTS.md), Design authority).
- One review by a named maintainer is required once the team names one. Until then, green CI and the doc-first rule are the gate.
- Coding agents merge their own pull requests on `rebuild` once CI is green ([AGENTS.md](AGENTS.md), Branches and pull requests).

Project maintainers may accept or reject any contribution at their discretion.

## Questions

For questions about contributing, open an issue on GitHub or consult the [documentation](https://roman-rapid.readthedocs.io/en/latest/).
