# ported-from: alerts/schema/00/07 @ 883e3876
"""The packet stage's science: `dev`'s alert assembly as pure functions.

Source: `origin/dev`'s ``alerts/`` package (``param_registry.py``,
``produce.py``, ``providers.py``, ``forced_phot.py``) and
``alerts/schema/00/07/``. The port
principle (lead, 2026-09-22): minimise differences to `dev`; improvements
come later. What changed is where the data comes from: `dev`'s
``AlertDataProvider`` queries RAPIDDB and stages S3 files itself; here the
stage reads the rows and files and passes plain records in.

- ``param_registry``: the schema's parameter registry, verbatim.
- ``records``: the normalized records (Source, ObjectRecord, ...).
- ``cutouts``: `dev`'s stamp extraction, on ``astropy.io.fits``.
- ``crossmatch``: KONA, reference-catalog, NED and Gaia matching.
- ``forced``: `dev`'s forced PSF photometry at fixed positions.
- ``assemble``: builders, alert assembly, the Avro container, BatchStats.
- ``schema/00/07/*.avsc``: `dev`'s schema files, verbatim, package data (00.06 kept beside).

``fastavro`` is imported at module scope here (the pipeline image's conda
environment carries it); nothing outside this subpackage and the packet
stage imports it. This subpackage imports nothing from ``rapidpipe.stages``,
``launch`` or ``cli``, and does no database I/O.
"""
