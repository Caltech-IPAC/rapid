# ported-from: none
"""The external catalogs behind the alert cross-matches, as copies the pipeline owns.

`dev`'s ``alerts/catalog_store.py`` and ``alerts/catalog_readers.py``
(Emily Everetts, 2026-09 and 2026-10), ported whole on 2026-10-29
(rapid_docs ``decision-gaia-mirror``):

- :mod:`store`: a :class:`~rapidpipe.science.catalogs.store.Store` over
  ``s3://bucket/prefix`` or a local directory, and the builders that fill
  it without staging anything on a disk: the NED object directory
  (``mirror``, ``verify``, ``repartition``, ``manifest``), NED-LVS
  (``ingest-lvs``) and Gaia DR3 (``ingest-gaia``, ``gaia-from-table``).
- :mod:`readers`: the cone readers over those copies, one per catalog
  (``Hp6NedReader``, ``LvsReader``, ``GaiaReader``), each returning the
  column arrays its matcher takes.

The pipeline reads a copy through a ``catalogs`` product whose location is
the copy's prefix; the builders run once per release through
``python -m rapidpipe.science.catalogs.store``. This subpackage imports nothing from
``rapidpipe.stages``, ``launch`` or ``cli``, and does no database I/O.
"""
