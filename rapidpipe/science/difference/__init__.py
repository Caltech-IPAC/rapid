"""The difference stage's science, one module per step, ported from `dev`.

Source: `origin/dev`'s ``pipeline/awsBatchSubmitJobs_runSingleSciencePipeline.py``
and the modules it calls (``pipeline/differenceImageSubs.py``,
``pipeline/zogyNoiseSubs.py``, ``pipeline/artifactRepairSubs.py``,
``pipeline/sfftCommandSubs.py``, ``modules/utils/rapid_pipeline_subs.py``).
The port principle (lead, 2026-09-22): minimise differences to `dev`;
improvements come later. Each function here keeps its `dev` counterpart's
maths, file conventions and quirks; where a quirk looks wrong it is
reproduced and recorded in the port's ledger, not fixed. The `dev` name of
each function is given in its docstring.

External tools (SExtractor, SWarp, bkgest, ZOGY's ``py_zogy.py``, SFFT)
are run by subprocess through :class:`~rapidpipe.science.difference.tools.ToolRunner`,
which tests replace with a fake. Every tool runs with the stage's work
directory as its current directory and is given the same bare file names
`dev` gives it, so file naming, the SFFT ``./`` prefix quirk and
SWarp's ``.head`` lookup behave as they do in `dev`.

The step modules, in `dev`'s science order:

- ``statistics``: clipped image statistics (``fits_data_statistics_with_clipping``).
- ``reformat``: reformat the delivered image and its simple-model uncertainty.
- ``sextractor``: SExtractor command line, catalog parsing, FWHM.
- ``resample``: SIP to PV, and SWarp of the reference bundle onto the science grid.
- ``background``: bkgest background subtraction of the science image.
- ``gainmatch``: gain matching and the reference-to-science offsets.
- ``repair``: NaN replacement and restoration, extreme-artifact repair.
- ``offsets``: the subpixel offset applied to the reference.
- ``psf``: PSF normalisation and transposition.
- ``zogy``: ZOGY's noise arguments and command line.
- ``masking``: coverage-map masking of difference images.
- ``uncertainty``: the difference-image uncertainty image.
- ``psfcat``: the Photutils PSF-fit catalog.
- ``sfft``: the SFFT command line (run as `dev` runs it; its science is not ported).
- ``naive``: the naive subtraction diagnostic.
- ``fitsops``: FITS image utilities several steps share (scale, keywords).

This subpackage imports nothing from ``rapidpipe.stages``, ``launch`` or
``cli`` (stage contract, dependency direction).
"""
