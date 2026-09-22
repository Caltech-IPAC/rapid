# RAPID
Repository for RAPID (***R***oman ***A***lerts ***P***romptly from ***I***mage ***D***ifferencing) project-infrastructure team

[![Documentation Status](https://readthedocs.org/projects/caltech-ipac-rapid/badge/?version=latest)](https://caltech-ipac-rapid.readthedocs.io/en/latest/)

## Repository rules

This repository is public and portable. Account identifiers, bucket names and hostnames are injected at deploy time and are never committed here. Bulk data artifacts (product listings, large generated files) are referenced, with the command to reproduce them, not committed. The design authority for this repository's boundaries and stage interfaces is the [specification](https://roman-rapid.readthedocs.io/en/latest/system/specification.html).

### Package

The distribution built from this repository is `rapid-pipeline`; its import package is `rapidpipe`. `rapidpipe stage <name>` runs one stage against a declared unit of work, input manifest and output location. The stage contract that `rapidpipe` implements is documented at [the contract page](https://roman-rapid.readthedocs.io/en/latest/system/stage-contract.html). `rapidpipe.db` holds persistence (the connection module and the migrations applier) and `rapidpipe.runs` holds runs, units, attempts, product instances and promotion, per [the runs page](https://roman-rapid.readthedocs.io/en/latest/system/runs.html). `rapidpipe.db` reaches its database credentials through the `PG*` environment or an AWS Secrets Manager secret named by `RAPID_DB_SECRET_ID`, never through arguments or a file committed to this repository.

`rapidpipe.stages.admit` is the first stage: it reads a delivery manifest naming one delivered l2 image (a FITS file staged outside the pipeline, not yet a registered product), verifies the delivered bytes and FITS checksums, reads the header and WCS the products page's l2-image field list needs, copies the file into the attempt's output location, and publishes a manifest with a fresh product instance. `rapidpipe.stages.register` reads that manifest's registration block and writes the `l2files`/`l2filemeta` rows without opening the FITS file itself, registering the manifest's own product instance first and recording nothing twice on a replay. `rapidpipe.science.spatial` holds the pure spatial derivations both `register` and its tests need -- HEALPix indexes, the Roman tessellation tile id, and the exact tile-overlap footprint -- with no import of any stage, so it can be exercised without a database.

## Documentation

Install instructions and documentation are available on [ReadTheDocs](https://caltech-ipac-rapid.readthedocs.io/en/latest/)

## Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on how to get involved. All participants are expected to follow our [Code of Conduct](CODE_OF_CONDUCT.md).

Before your first push, run `git config core.hooksPath .githooks` to enable the pre-push hook that blocks account identifiers and personal paths from reaching this public repository; the same check runs in CI as a backstop.

## License

This project is licensed under the BSD 3-Clause License. See [LICENSE](LICENSE) for details.

## Acknowledgments

The RAPID project infrastructure team acknowledges NASA support under award 80NSSC24M0020 (program NNH22ZDA001N-ROMAN).
