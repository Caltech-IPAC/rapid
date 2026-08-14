# RAPID
Repository for RAPID (***R***oman ***A***lerts ***P***romptly from ***I***mage ***D***ifferencing) project-infrastructure team

[![Documentation Status](https://readthedocs.org/projects/caltech-ipac-rapid/badge/?version=latest)](https://caltech-ipac-rapid.readthedocs.io/en/latest/)

## Documentation

Install instructions and documentation are available on [ReadTheDocs](https://caltech-ipac-rapid.readthedocs.io/en/latest/)

## Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on how to get involved. All participants are expected to follow our [Code of Conduct](CODE_OF_CONDUCT.md).

### Git hooks

`.githooks/pre-push` blocks internal identifiers (SMDC account ID, internal
hostnames, AWS-account-shaped numbers, personal `/Users/<name>` paths) from
reaching the public remote. Install it once per clone:

    git config core.hooksPath .githooks

### Running the tests

    pip install -e '.[test]'
    RAPID_SW="$PWD" scripts/run-operational-tests.sh

runs the default (stub) tier, no database required. See
[`pipeline/contract/README.md`](pipeline/contract/README.md) for the
PostgreSQL-backed contract tier.

## License

This project is licensed under the BSD 3-Clause License. See [LICENSE](LICENSE) for details.

## Acknowledgments

The RAPID project infrastructure team acknowledges NASA support under award 80NSSC24M0020 (program NNH22ZDA001N-ROMAN).
