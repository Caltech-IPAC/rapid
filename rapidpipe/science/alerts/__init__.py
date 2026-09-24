"""The alerts stage's science: `dev`'s alert assembly as pure functions.

Source: `origin/dev`'s ``alerts/`` package (``param_registry.py``,
``produce.py``, ``providers.py``) and ``alerts/schema/00/04/``. The port
principle (lead, 2026-09-22): minimise differences to `dev`; improvements
come later. What changed is where the data comes from: `dev`'s
``AlertDataProvider`` queries RAPIDDB and stages S3 files itself; here the
stage reads the rows and files and passes plain records in.

- ``param_registry``: the schema's parameter registry, verbatim.
- ``records``: the normalized records (Source, ObjectRecord, ...).
- ``cutouts``: `dev`'s stamp extraction, on ``astropy.io.fits``.
- ``crossmatch``: KONA, reference-catalog and NED matching.
- ``assemble``: builders, alert assembly, the Avro container, BatchStats.
- ``schema/00/04/*.avsc``: `dev`'s schema files, verbatim, package data.

``fastavro`` is imported at module scope here (the pipeline image's conda
environment carries it); nothing outside this subpackage and the alerts
stage imports it. This subpackage imports nothing from ``rapidpipe.stages``,
``launch`` or ``cli``, and does no database I/O.
"""
