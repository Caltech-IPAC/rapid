"""The crossmatch stage's science: exposure ordering, CSV lines, the pass-2 cone.

Source: `origin/dev`'s ``pipeline/crossMatchSources.py``. The port
principle (lead, 2026-09-22): minimise differences to `dev`. Each helper
keeps its `dev` counterpart's arithmetic and text format.

- ``catalog``: `dev`'s ascending-MJD exposure order (stage 1), the
  ``merges``/``astroobjects`` CSV lines it bulk-copies, and the stage-2
  inclusion cone around a field's centre.

This subpackage imports nothing from ``rapidpipe.stages``, ``launch`` or
``cli`` (stage contract, dependency direction), and does no database I/O.
"""
