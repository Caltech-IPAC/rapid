# RAPID
Repository for RAPID (***R***oman ***A***lerts ***P***romptly from ***I***mage ***D***ifferencing) project-infrastructure team

[![Documentation Status](https://readthedocs.org/projects/caltech-ipac-rapid/badge/?version=latest)](https://caltech-ipac-rapid.readthedocs.io/en/latest/)

## Repository rules

This repository is public and portable. Account identifiers, bucket names and hostnames are injected at deploy time and are never committed here. Bulk data artifacts (product listings, large generated files) are referenced, with the command to reproduce them, not committed. The design authority for this repository's boundaries and stage interfaces is the [specification](https://roman-rapid.readthedocs.io/en/latest/system/specification.html).

## Documentation

Install instructions and documentation are available on [ReadTheDocs](https://caltech-ipac-rapid.readthedocs.io/en/latest/)

## Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on how to get involved. All participants are expected to follow our [Code of Conduct](CODE_OF_CONDUCT.md).

Before your first push, run `git config core.hooksPath .githooks` to enable the pre-push hook that blocks account identifiers and personal paths from reaching this public repository; the same check runs in CI as a backstop.

## License

This project is licensed under the BSD 3-Clause License. See [LICENSE](LICENSE) for details.

## Acknowledgments

The RAPID project infrastructure team acknowledges NASA support under award 80NSSC24M0020 (program NNH22ZDA001N-ROMAN).
