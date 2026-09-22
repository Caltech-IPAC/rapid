# Containers

`rapid-pipeline/` holds the recipe for the pipeline application image:
`Containerfile`, `build.sh` and the `import-gate.py` helper it bakes in.

## Building locally

```
containers/rapid-pipeline/build.sh <git-ref> --base <base-image-ref>
```

`<git-ref>` is any branch, tag or commit SHA (or `HEAD`); the build fails
on a dirty working tree unless `--allow-dirty` is passed. Run
`containers/rapid-pipeline/build.sh --help` for the full option list.

## What the base must provide

`--base` names a UBI10-compatible image carrying the `rapid-pipeline` RPM
group's C tools and the `rapid-python` RPM's locked conda environment
under `/opt/rapid`, already satisfying `requirements.txt`. This recipe
never names a registry, account or digest -- the caller always supplies
the full base image reference.

## Where publishing lives

Resolving which base image to build against, publishing the built image
and recording its digest are `rapid_systems`' job, not this repository's
(specification, "Repositories"). `.github/workflows/container.yml` proves
this recipe's mechanics on CI against a public stand-in base; it does not
publish anything.
