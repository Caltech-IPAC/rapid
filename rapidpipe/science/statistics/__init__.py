"""The statistics stage's science: per-object position and flux statistics, ported from `dev`.

Source: `origin/dev`'s ``pipeline/computeStatisticsForAstroObjects.py`` and
``modules/utils/rapid_pipeline_subs.py`` (``compute_radec_statistics``).
The port principle (lead, 2026-09-22): minimise differences to `dev`;
improvements come later.

- ``lightcurve``: `dev`'s ``compute_radec_statistics`` verbatim, the
  per-object statistics `dev` computes from an object's sources (mean
  vector position, per-axis spread, flux mean and standard deviation,
  source count), and the ``astroobjectsmeta`` CSV line.

This subpackage imports nothing from ``rapidpipe.stages``, ``launch`` or
``cli`` (stage contract, dependency direction), and does no database I/O.
"""
