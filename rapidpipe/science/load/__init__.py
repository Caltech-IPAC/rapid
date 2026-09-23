"""The load stage's science: catalog join and per-source derivations, ported from `dev`.

Source: `origin/dev`'s ``pipeline/loadPSFCatIntoDBSourcesTable.py``. The
port principle (lead, 2026-09-22): minimise differences to `dev`;
improvements come later. Each function keeps its `dev` counterpart's maths
and quirks; where a quirk looks wrong it is reproduced and recorded in the
port's ledger, not fixed.

- ``catalogs``: read a Photutils PSF-fit catalog and its finder catalog and
  inner-join them on ``id``; HEALPix level-6/9 indexes and the Roman
  tessellation id per source; `dev`'s fit-position rejection and CSV rows.

This subpackage imports nothing from ``rapidpipe.stages``, ``launch`` or
``cli`` (stage contract, dependency direction), and does no database I/O.
"""
