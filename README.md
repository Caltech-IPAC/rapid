# RAPID
Repository for RAPID (***R***oman ***A***lerts ***P***romptly from ***I***mage ***D***ifferencing) project-infrastructure team

[![Documentation Status](https://readthedocs.org/projects/caltech-ipac-rapid/badge/?version=latest)](https://caltech-ipac-rapid.readthedocs.io/en/latest/)

## Repository rules

This repository is public and portable. Account identifiers, bucket names and hostnames are injected at deploy time and are never committed here. Bulk data artifacts (product listings, large generated files) are referenced, with the command to reproduce them, not committed. The design authority for this repository's boundaries and stage interfaces is the [specification](https://roman-rapid.readthedocs.io/en/latest/system/specification.html).

### Package

The distribution built from this repository is `rapid-pipeline`; its import package is `rapidpipe`. `rapidpipe stage <name>` runs one stage against a declared unit of work, input manifest and output location. The stage contract that `rapidpipe` implements is documented at [the contract page](https://roman-rapid.readthedocs.io/en/latest/system/stage-contract.html). `rapidpipe.db` holds persistence (the connection module and the migrations applier) and `rapidpipe.runs` holds runs, units, attempts, product instances and promotion, per [the runs page](https://roman-rapid.readthedocs.io/en/latest/system/runs.html). `rapidpipe.db` reaches its database credentials through the `PG*` environment or an AWS Secrets Manager secret named by `RAPID_DB_SECRET_ID`, never through arguments or a file committed to this repository.

`rapidpipe.stages.admit` is the first stage: it reads a delivery manifest naming one delivered l2 image (a FITS file staged outside the pipeline, not yet a registered product), verifies the delivered bytes and FITS checksums, reads the header and WCS the products page's l2-image field list needs, copies the file into the attempt's output location, and publishes a manifest with a fresh product instance. `rapidpipe.stages.register` reads that manifest's registration block and writes the `l2files`/`l2filemeta` rows without opening the FITS file itself, registering the manifest's own product instance first and recording nothing twice on a replay. `rapidpipe.science.spatial` holds the pure spatial derivations both `register` and its tests need -- HEALPix indexes, the Roman tessellation tile id, and the exact tile-overlap footprint -- with no import of any stage, so it can be exercised without a database.

`rapidpipe run create/list/show/local` operate on `rapidpipe.runs.repository`: `create` records a new run and prints its id, `list` and `show` inspect runs, units and attempts, and `local` runs one stage attempt as a subprocess on the current machine, through `rapidpipe.runs.local`, allocating its own attempt id and an exclusive output location without needing Batch. `promote`, `delete` and `run` for Batch remain placeholders.

#### Running a stage locally

```
run_id=$(rapidpipe run create --kind scratch --purpose "local smoke test" --stages admit,register)
rapidpipe run local "$run_id" admit --unit e20260821001234/SCA07 \
    --inputs /path/to/delivery --outputs-root /tmp/rapid-local
rapidpipe run local "$run_id" register --unit e20260821001234-reg/SCA07 \
    --inputs /tmp/rapid-local/runs/"$run_id"/admit/e20260821001234/SCA07/<admit-attempt-id> \
    --outputs-root /tmp/rapid-local
rapidpipe run show "$run_id"
```

`register`'s `--inputs` is admit's own output location (printed by `run local admit` as `outputs=...`), since `register` reads the producing attempt's completion manifest rather than the original delivery.

### Container

[`containers/rapid-pipeline`](containers/rapid-pipeline) holds the recipe that builds `rapidpipe` into a runnable image over a base environment supplying its dependencies. Which base image that is and where the built image is published are decided and owned outside this repository, in `rapid_systems`. See [`containers/README.md`](containers/README.md) for how to build the recipe locally.

## Documentation

Install instructions and documentation are available on [ReadTheDocs](https://caltech-ipac-rapid.readthedocs.io/en/latest/)

## Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on how to get involved. All participants are expected to follow our [Code of Conduct](CODE_OF_CONDUCT.md).

Before your first push, run `git config core.hooksPath .githooks` to enable the pre-push hook that blocks account identifiers and personal paths from reaching this public repository; the same check runs in CI as a backstop.

## License

This project is licensed under the BSD 3-Clause License. See [LICENSE](LICENSE) for details.

## Acknowledgments

The RAPID project infrastructure team acknowledges NASA support under award 80NSSC24M0020 (program NNH22ZDA001N-ROMAN).
