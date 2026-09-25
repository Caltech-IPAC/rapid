"""Persistence for the reference kinds: `refimages`, `refimmeta`, `refimimages`, `refimcatalogs`.

`dev` registers a reference image from ``registerCompletedJobsInDB.py``
(``database/modules/utils/rapid_db.py``): ``add_refimage`` (the
``addRefImage`` stored function), one ``register_refimimage`` per input
frame (``registerRefImImage``), ``register_refimcatalog`` per catalog
(``registerRefImCatalog``) and ``register_refimmeta``
(``registerRefImMeta``). All four functions are in the baseline
(20260921-01-baseline.sql) and the rebuild calls each of them unchanged,
as ``rapidpipe.db.psfs`` calls ``addPSF`` (supervisor step 8, ruling R7).

Column sources, where they are not the manifest itself:

- ``refimages``: ``fid`` from `filters` by the block's filter name;
  ``ppid`` from :data:`REFERENCE_RECIPE_PPIDS` (``awaicgen`` is `dev`'s
  pipeline 12, "Standard reference-image pipeline"); ``hp6``/``hp9`` from
  the block's ``ra_center``/``dec_center`` exactly as `dev` derives them
  (``hp.ang2pix`` NESTED at NSIDE 64 and 512,
  ``rapidpipe.science.spatial.healpix_indexes``); ``filename`` the
  primary member resolved against the output location; ``checksum`` the
  block's ``md5``. ``addRefImage`` allocates ``version`` -- the next
  number for (``field``, ``fid``, ``ppid``) across the table, legacy rows
  included -- takes ``svid`` from the latest `swversions` row, and inserts
  ``vbest`` 0. Then ``run``, ``attempt`` and ``instance``
  (20260923-02-refimages-instance.sql) are set, as psfs.py does.
- ``refimmeta``: the block's measurements under `dev`'s names, with
  ``field``/``hp6``/``hp9``/``fid`` as above.
- ``refimimages``: one ``(rfid, rid)`` per constituent, ``rid`` from the
  `l2files` row whose ``instance`` is the constituent id.
- ``refimcatalogs``: ``rfid``, ``field``, ``hp6``, ``hp9``, ``fid`` copied
  from the reference's `refimages` row (found by its instance, the key's
  ``reference``), ``ppid`` 12, ``cattype`` from ``catalog_type``.

Departures from `dev`, each deliberate:

- ``vbest`` stays 0: `dev` calls ``updateRefImage`` right after
  ``addRefImage`` to make the new row current; the rebuild never sets a
  legacy current flag at registration (products page, "Registration
  metadata"; promotion maintains it, step 3 ruling R5).
- ``refimmeta.npucatsources`` is NOT NULL in the baseline, but the block
  carries null when the Photutils reference catalog is off (ruling R6).
  `dev` always runs that catalog, so it never meets the case. Without a
  migration the row cannot say "not measured"; :data:`NPUCATSOURCES_WHEN_ABSENT`
  (0) is written instead, and the absence of a ``cattype`` 2
  `refimcatalogs` row is what records that no PSF catalog exists.
- `dev` swallows a failed ``registerRefImImage``/``registerRefImMeta``
  ("skipping"), which is why production `rapid` has no `refimimages`
  table and no `refimmeta` rows today; here any failure fails the whole
  registration, and a constituent with no `l2files` row is an error
  (the run must register its admitted frames first).
- ``registerRefImCatalog`` upserts on (``rfid``, ``ppid``, ``cattype``).
  Here an existing row with the same filename and checksum is a replay
  and writes nothing; one with different content is an error rather than
  a silent overwrite, because `refimcatalogs` has no instance column to
  say which instance a row came from.

This module imports ``rapidpipe.db``, ``rapidpipe.products`` and
``rapidpipe.science.spatial`` only, matching ``rapidpipe.db``'s package
contract (no ``rapidpipe.runs``, no stage module).
"""

from __future__ import annotations

from typing import Any

from rapidpipe.products.refimage import (
    REFERENCE_CATALOG_CATTYPES,
    validate_reference_catalog_entry,
    validate_reference_image_entry,
)
from rapidpipe.science.spatial import healpix_indexes

#: Reference recipe to `pipelines` row: `awaicgen` is `dev`'s ppid 12,
#: "Standard reference-image pipeline" (rapidOpsPipelinesInserts.sql),
#: fixed with the `reference` stage (supervisor step 8, ruling R3), as
#: ``rapidpipe.db.diffimages.DIFFERENCER_PPIDS`` fixes the differencers'.
REFERENCE_RECIPE_PPIDS: dict[str, int] = {"awaicgen": 12}

#: What ``refimmeta.npucatsources`` (NOT NULL) receives when the block's
#: ``npucatsources`` is null, i.e. no Photutils reference catalog was made
#: (module docstring, "Departures").
NPUCATSOURCES_WHEN_ABSENT = 0


def _primary_filename(entry: dict[str, Any], output_location: str) -> str:
    primary = next(m for m in entry["members"] if m["path"] == entry["primary"])
    return f"{output_location}/{primary['path']}"


def _constituent_rids(cur, constituents: list[str]) -> list[int]:
    rids = []
    for constituent in constituents:
        cur.execute("SELECT rid FROM l2files WHERE instance = %s", (constituent,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(
                f"reference constituent {constituent!r} has no l2files row; the run "
                "must register its admitted l2-image instances before the reference")
        rids.append(row[0])
    return rids


def register_reference_image(
    conn,
    *,
    entry: dict[str, Any],
    run_id: str,
    attempt_id: str,
    output_location: str,
) -> int:
    """Write the `refimages`, `refimmeta` and `refimimages` rows for one
    reference-image entry, in the caller's transaction; return its rfid.

    Replaying an instance already registered writes nothing and returns
    its row. Raises :class:`ValueError` -- `register` maps it to
    InputRejected (65) -- for an invalid entry, an unknown filter or
    recipe, or a constituent with no `l2files` row.
    """
    registration = validate_reference_image_entry(entry)
    key = entry["key"]
    instance = entry["instance"]
    ppid = REFERENCE_RECIPE_PPIDS.get(key["recipe"])
    if ppid is None:
        raise ValueError(
            f"reference recipe {key['recipe']!r} has no pipelines row to register under; "
            f"known: {sorted(REFERENCE_RECIPE_PPIDS)}")
    filename = _primary_filename(entry, output_location)

    with conn.cursor() as cur:
        cur.execute("SELECT rfid FROM refimages WHERE instance = %s", (instance,))
        row = cur.fetchone()
        if row is not None:
            return row[0]

        cur.execute("SELECT fid FROM filters WHERE filter = %s", (registration.filter,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"unknown filter {registration.filter!r}")
        (fid,) = row

        rids = _constituent_rids(cur, registration.constituents)
        hp6, hp9 = healpix_indexes(registration.ra_center, registration.dec_center)
        field = registration.field

        cur.execute(
            """
            SELECT * FROM addRefImage(
                cast(%s as integer), cast(%s as integer), cast(%s as integer),
                cast(%s as smallint), cast(%s as smallint), cast(%s as integer),
                cast(%s as character varying(255)), cast(%s as character varying(32)),
                cast(%s as smallint)
            ) AS (rfid integer, version smallint)
            """,
            (field, hp6, hp9, fid, ppid, registration.infobits, filename,
             registration.md5, registration.status))
        rfid, _version = cur.fetchone()

        cur.execute(
            "UPDATE refimages SET run = %s, attempt = %s, instance = %s WHERE rfid = %s",
            (run_id, attempt_id, instance, rfid))

        npucatsources = registration.npucatsources
        if npucatsources is None:
            npucatsources = NPUCATSOURCES_WHEN_ABSENT
        cur.execute(
            """
            SELECT registerRefImMeta(
                cast(%s as integer), cast(%s as smallint), cast(%s as integer),
                cast(%s as integer), cast(%s as integer), cast(%s as smallint),
                cast(%s as double precision), cast(%s as double precision),
                cast(%s as integer), cast(%s as real), cast(%s as real),
                cast(%s as integer), cast(%s as real), cast(%s as real),
                cast(%s as real), cast(%s as real), cast(%s as real),
                cast(%s as real), cast(%s as real), cast(%s as real),
                cast(%s as real), cast(%s as real), cast(%s as integer),
                cast(%s as integer)
            )
            """,
            (rfid, fid, field, hp6, hp9, registration.nframes,
             registration.mjdobs_min, registration.mjdobs_max,
             registration.npixnan, registration.clmean, registration.clstddev,
             registration.clnoutliers, registration.gmedian, registration.datascale,
             registration.gmin, registration.gmax, registration.cov5percent,
             registration.medncov, registration.medpixunc, registration.fwhmmedpix,
             registration.fwhmminpix, registration.fwhmmaxpix,
             registration.nsexcatsources, npucatsources))

        for rid in rids:
            cur.execute(
                "SELECT registerRefImImage(cast(%s as integer), cast(%s as integer))",
                (rfid, rid))
        return rfid


def register_reference_catalog(
    conn,
    *,
    entry: dict[str, Any],
    output_location: str,
) -> int:
    """Write the `refimcatalogs` row for one reference-catalog entry, in the
    caller's transaction; return its rfcatid.

    The reference is found by its instance (the key's ``reference``): one
    registered earlier in the same manifest, or already in `refimages`.
    Replaying writes nothing. Raises :class:`ValueError` for an invalid
    entry, an unregistered reference, or a different catalog already
    registered for the same reference and type.
    """
    registration = validate_reference_catalog_entry(entry)
    reference = entry["key"]["reference"]
    cattype = REFERENCE_CATALOG_CATTYPES[registration.catalog_type]
    filename = _primary_filename(entry, output_location)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT rfid, ppid, field, hp6, hp9, fid FROM refimages WHERE instance = %s",
            (reference,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(
                f"no refimages row for reference instance {reference!r}; register the "
                "reference-image before its catalog")
        rfid, ppid, field, hp6, hp9, fid = row

        cur.execute(
            """
            SELECT rfcatid, filename, checksum FROM refimcatalogs
            WHERE rfid = %s AND ppid = %s AND cattype = %s
            """,
            (rfid, ppid, cattype))
        row = cur.fetchone()
        if row is not None:
            rfcatid, existing_filename, existing_checksum = row
            if (existing_filename, existing_checksum) == (filename, registration.md5):
                return rfcatid
            raise ValueError(
                f"refimcatalogs already holds a different {registration.catalog_type} "
                f"catalog for reference {reference!r} (rfcatid {rfcatid}, "
                f"{existing_filename!r})")

        cur.execute(
            """
            SELECT * FROM registerRefImCatalog(
                cast(%s as integer), cast(%s as smallint), cast(%s as smallint),
                cast(%s as integer), cast(%s as integer), cast(%s as integer),
                cast(%s as smallint), cast(%s as character varying(255)),
                cast(%s as character varying(32)), cast(%s as smallint)
            ) AS (rfcatid integer, svid smallint)
            """,
            (rfid, ppid, cattype, field, hp6, hp9, fid, filename,
             registration.md5, registration.status))
        rfcatid, _svid = cur.fetchone()
        return rfcatid
