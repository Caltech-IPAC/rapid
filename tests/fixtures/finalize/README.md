# The finalize stage's fixture

The stage-contract fixture for `finalize` (rapid_docs `system/stage-contract.md`,
"Local execution"). Run it with:

```
make stage-finalize                                # anywhere
rapidpipe selftest --stage finalize [--real-tools] # the same fixture, in the pipeline image
```

`run_fixture.py` prepares an isolated directory, runs
`python -m rapidpipe.stages.finalize` there as a subprocess with fresh
run and attempt ids (the invocation Batch uses), and checks the exit
code, the manifest and the products. It exits 0 when every check passes.
The stage runs no external tool and touches no database, so the fixture
is identical with `--real-tools` and without.

## What is here

| File | Holds |
|---|---|
| `run_fixture.py` | prepare, run, check |
| `README.md` | this file |

The fixture *data* -- `settings.toml` (an overlay restating ZOGY's
`[pipelines]` row) and `expected.json` (the input parameters and the
expected stamp) -- lives once, under the packaged
`rapidpipe/selftest/fixtures/finalize/`, the copy `rapidpipe selftest`
reads inside the pipeline image.

### Inputs

Not committed as files: `rapidpipe/selftest/support/fakefinalize.py`
(`build_difference_output`) writes a difference attempt's output location
at prepare time, deterministically (a fixed random seed), in the shape
`rapidpipe.stages.difference` publishes:

- the ZOGY bundle under `work/`: `difference`, `uncertainty` and
  `significance` as 64x64 float32 FITS images with a TAN WCS, and a 9x9
  `psf`;
- the SExtractor catalogs (role `catalog`) and the Photutils catalogs
  (roles `catalog`, `finder`, `residual` as a small FITS image, and
  `parquet` as synthetic bytes) for both signs;
- the attempt's execution record, `exec/<attempt>.json`, with a source
  revision and an image digest;
- `manifest.json` (stage `difference`): one `difference-image` entry with
  a registration block that passes `validate_difference_entry`, and four
  `source-catalog` entries keyed to it, ZOGY by default (the stage's
  `[finalize] differencer`). Only `expected.json` and
  `settings.toml` are package data; every FITS and catalog byte is
  generated here; `inputs.products` names the l2
  and reference instances.

### Database seed

None. `finalize` declares no database access. `register` records the
finalized manifest through the same difference-image path as a
difference manifest (`tests/db/test_register_difference.py`).

## What is checked

All exact; nothing here is floating-point science.

| Check | Why |
|---|---|
| one `difference-image` entry and every input `source-catalog` entry (four here; finalize passes through 0..n), every instance a new ULID, none reused from the input | the ruling: new instances, same kinds |
| difference-image key, primary path and roles unchanged; block validates | same logical key |
| registration = the input's, `md5` recomputed, plus `finalized_from` (input instance) and `revision` 2 | the products page: the manifest records the input instance and the output revision |
| registration `md5` = MD5 of the stamped file | `diffimages.checksum` |
| stamped file opens with `fits.open(checksum=True)` with no checksum warning; `CHECKSUM` and `DATASUM` present | `dev`'s `writeto(..., checksum=True)` |
| every stamp keyword present with its full comment; fixed values from `expected.json`; `RPRUN`, `RPATTMPT`, `RPINST`, `RPOUTLOC` equal to the invocation's; `RPFSETHS` equal to finalize's own settings hash and `RPSETHSH` to the key's original one; `DATE` ISO to the second | the keyword table |
| the input header's own keywords kept; pixels and dtype unchanged | only the header is stamped |
| every other member byte-identical (SHA-256) to its input | copied, not rewritten |
| each catalog's key names the finalized instance, `copied_from` names its input, members identical | catalogs follow the new instance |
| `inputs.products` names only the difference manifest's own l2 and reference instances; `inputs.manifest` references the difference manifest; no execution notes | ruling option (b): only registered instances are dependencies |

Provenance is otherwise checked for shape only, by the shared runner: the
manifest's run, unit and attempt are the invocation's, the execution
record carries a SHA-256 settings hash, and every member's size and
SHA-256 match the file on disk.
