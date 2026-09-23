# The difference stage's fixture

The stage-contract fixture for `difference` (rapid_docs `system/stage-contract.md`,
"Local execution"). Run it with:

```
make stage-difference                              # fake tools, anywhere
make stage-difference DIFFERENCE_TOOLS=real        # real tools, in the pipeline image
```

`run_fixture.py` prepares an isolated directory, runs
`python -m rapidpipe.stages.difference` there as a subprocess with fresh
run and attempt ids (the invocation Batch uses), and checks the exit
code, the manifest and the products. It exits 0 when every check passes.

## What is here

| File | Holds |
|---|---|
| `run_fixture.py` | prepare, run, check |
| `settings.toml` | the settings overlay: the clipped-statistics seed, so numbers repeat |
| `expected.json` | the input parameters, the expected products per tool set, the tolerances |

### Inputs

Not committed as files: `run_fixture.py` writes them with
`tests/unit/fakedifftools.build_input_set` from the parameters in
`expected.json` (`inputs`), deterministically (a fixed random seed). They
are minimal and synthetic:

- the l2 image: a 64x64 float32 image in HDU 1 of a gzipped FITS file,
  `dev`'s socsims layout, with EXPTIME, ZPTMAG and a TAN-SIP WCS; four
  stars and one transient; its `l2-image` entry carries a registration
  block as `admit` writes one;
- the reference bundle on the reformatted science grid (65x65, DN/s): the
  four stars without the transient, a coverage map with an uncovered
  three-column strip, a flat uncertainty image, and its SExtractor catalog
  with the reference parameter file's columns;
- two 9x9 Gaussian PSFs, science (unnormalised, as delivered) and reference.

### Database seed

None. `difference` declares no database access. How `register` records
this stage's manifest is tested against PostgreSQL in
`tests/db/test_register_difference.py`, and the stage's run through the
local runner (`rapidpipe run local`) in `tests/db/test_difference_run_local.py`.

## Expected products and tolerances

With the fake tools (`tests/unit/fakedifftools.py`) the stage's own logic
runs for real -- reformat, gain matching, masking, NaN handling, the
catalog blocks, the manifest -- while SExtractor, SWarp, bkgest, ZOGY,
SFFT, photutils and the SIP-to-PV converter are replaced by stand-ins
that write files of the right shape. The expected values in `fake` are
what that run produces; they catch a change in the stage's own logic, not
in the tools' science.

| Check | Tolerance | Why |
|---|---|---|
| exit code, entry counts, bundle roles, detection role, info bits, catalog-outcome mask, source counts, NaN count, image shape | exact | integers and names |
| centre and corners = the l2 instance's | 1e-9 deg absolute | copied, as `dev` copies the science image's |
| scale factor, registration residual | 1e-9 relative / absolute | the zero-point fallback (too few sources to match on 64x64) and 0.0, both exact in principle; the tolerance absorbs float formatting only |
| difference image sum, max, min, standard deviation over finite pixels | 1e-5 relative, 1e-6 absolute | float32 images; allows numpy and astropy version differences in summation order |

Provenance is checked for shape only: the manifest's run, unit and
attempt are the invocation's, instance ids are ULIDs, the execution
record carries a SHA-256 settings hash, and every member's size and
SHA-256 match the file on disk.

With the real tools, `real` holds only what the fixture's construction
fixes (entry counts within a range, roles, the l2 instance's info bits,
ZOGY's astrometric inputs, the coverage mask's NaN count). **The real-tool
run of this fixture and the IMSS comparison on fixed inputs are the
lead's gate before operational use** (stage contract, "Local execution":
"differences and tolerances approved by the lead before operational
use"); neither has been run. After the first real-tool run, its measured
values and their tolerances belong in `real`.
