"""The reference stage's science: `dev`'s reference-image pipeline (ppid 12).

`dev`: ``pipeline/referenceImageSubs.py`` (``generateReferenceImage``,
``generateSExtractorReferenceImageCatalog``,
``addKeywordsToReferenceImageHeader``, ``compute_cov5percent``) and the
helpers it calls in ``modules/utils/rapid_pipeline_subs.py``
(``build_awaicgen_command_line_args``, ``get_reference_image_zeropoint``,
``convert_mjd_to_jd``). SExtractor's command line, catalog parsing and the
clipped statistics are the difference package's ports of the same `dev`
functions, reused rather than copied.

- :mod:`.prep` -- per-frame reformat, DN/s, zero-point scaling and the
  simple-model uncertainty (verbatim arithmetic), filter names.
- :mod:`.awaicgen` -- the mosaic geometry and awaicgen's command line.
- :mod:`.catalog` -- SExtractor on the mosaic and the FWHM statistics.
- :mod:`.measure` -- ``cov5percent`` and the refimmeta measurements.
- :mod:`.header` -- the header stamp.
- :mod:`.identity` -- the selection digest and the logical key.

Imports nothing from ``rapidpipe`` outside ``rapidpipe.science`` (stage
contract, dependency direction).
"""
