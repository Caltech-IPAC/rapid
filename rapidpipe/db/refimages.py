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

- ``refimages``: ``fid`` from `filters` by the block's filter name in its
  RAPID spelling (``rapidpipe.products.refimage.rapid_filter_name``:
  ``F146`` and ``W146`` both find ``W146``); ``ppid`` from
  :data:`REFERENCE_RECIPE_PPIDS` (``awaicgen`` is `dev`'s pipeline 12,
  "Standard reference-image pipeline"); ``hp6``/``hp9`` from the block's
  ``ra_center``/``dec_center`` exactly as `dev` derives them
  (``hp.ang2pix`` NESTED at NSIDE 64 and 512,
  ``rapidpipe.science.spatial.healpix_indexes``); ``filename`` the
  primary member resolved against the output location; ``checksum`` the
  block's ``md5``. ``addRefImage`` allocates ``version`` -- the next
  number for (``field``, ``fid``, ``ppid``) across the table, legacy rows
  included, `dev`'s global counter kept -- takes ``svid`` from the latest
  `swversions` row (`dev`'s choice, kept), and inserts ``vbest`` 0. Then
  ``run``, ``attempt`` and ``instance`` (20260923-02-refimages-instance.sql)
  are set; ``attempt`` is the PRODUCING attempt, the manifest's own
  ``attempt`` (the `reference` attempt that made the product), per that
  migration's column meaning.
- ``refimmeta``: the block's measurements under `dev`'s names, with
  ``field``/``hp6``/``hp9``/``fid`` as above.
- ``refimimages``: one ``(rfid, rid)`` per constituent, ``rid`` from the
  `l2files` row whose ``instance`` is the constituent id.
- ``refimcatalogs``: ``rfid``, ``ppid``, ``field``, ``hp6``, ``hp9``,
  ``fid`` copied from the reference's `refimages` row (found by its
  instance, the key's ``reference``), ``cattype`` from ``catalog_type``.

Departures from `dev`, each deliberate:

- ``vbest`` stays 0: `dev` calls ``updateRefImage`` right after
  ``addRefImage`` to make the new row current; the rebuild never sets a
  legacy current flag at registration (products page, "Registration
  metadata"; promotion maintains it, step 3 ruling R5).
- A transaction-level advisory lock on (``field``, ``fid``, ``ppid``) is
  taken before ``addRefImage``: its ``max(version) + 1`` is a read then a
  write, and two registrations of one field and filter at once would
  otherwise both read the same maximum and one would fail on
  ``refimagespk`` (Codex plan review, 2026-09-24).
- ``refimmeta.npucatsources`` is null when no Photutils reference catalog
  was made (20260924-09 drops the column's NOT NULL); `dev` always makes
  one, so it never writes null.
- `dev` swallows a failed ``registerRefImImage``/``registerRefImMeta``
  ("skipping"), which is why production `rapid` has no `refimimages`
  table and no `refimmeta` rows today; the rebuild's trial database has
  all three. Here any failure fails the whole registration, and a
  constituent with no `l2files` row is an error (the run must register
  its admitted frames first).
- Replay compares. ``register_manifest`` checks an existing instance's
  identity and members only; here an instance already in `refimages`
  has its rows compared with the block (field, fid, checksum, the
  `refimmeta` measurements, the constituent set) and a catalog already in
  `refimcatalogs` its checksum. Identical is a no-op with no writes at
  all -- ``registerRefImCatalog`` is not called, since it rewrites an
  existing row, ``created`` included; different is an error.

This module imports ``rapidpipe.db``, ``rapidpipe.products`` and
``rapidpipe.science.spatial`` only, matching ``rapidpipe.db``'s package
contract (no ``rapidpipe.runs``, no stage module).
"""

from __future__ import annotations

import struct
from typing import Any

from rapidpipe.products.refimage import (
    REFERENCE_CATALOG_CATTYPES,
    ReferenceImageRegistration,
    rapid_filter_name,
    validate_reference_catalog_entry,
    validate_reference_image_entry,
)
from rapidpipe.science.spatial import healpix_indexes

#: Reference recipe to `pipelines` row: `awaicgen` is `dev`'s ppid 12,
#: "Standard reference-image pipeline" (rapidOpsPipelinesInserts.sql),
#: fixed with the `reference` stage (supervisor step 8, ruling R3), as
#: ``rapidpipe.db.diffimages.DIFFERENCER_PPIDS`` fixes the differencers'.
REFERENCE_RECIPE_PPIDS: dict[str, int] = {"awaicgen": 12}

#: `refimmeta` columns filled from the block's field of the same name, by
#: column type: ``real`` (compared after rounding to float4 on replay),
#: ``double precision`` and integer. The one whose block field is spelled
#: differently is in :data:`_META_RENAMED`.
_META_REAL = ("clmean", "clstddev", "gmedian", "datascale", "gmin", "gmax",
              "cov5percent", "medncov", "medpixunc", "fwhmmedpix", "fwhmminpix",
              "fwhmmaxpix")
_META_INT = ("nframes", "npixnan", "clnoutliers", "npucatsources")
#: `refimmeta` double-precision columns and the block fields they hold.
_META_DOUBLE = {"mjdobsmin": "mjdobs_min", "mjdobsmax": "mjdobs_max"}
#: Integer `refimmeta` columns whose block field keeps `dev`'s block
#: spelling: ``nsexcatsources`` -> ``nsxcatsources``.
_META_RENAMED = {"nsxcatsources": "nsexcatsources"}


def _as_real(value: float) -> float:
    """``value`` as PostgreSQL's ``real`` stores it (IEEE float4)."""
    return struct.unpack("f", struct.pack("f", value))[0]


def _primary_filename(entry: dict[str, Any], output_location: str) -> str:
    primary = next(m for m in entry["members"] if m["path"] == entry["primary"])
    return f"{output_location}/{primary['path']}"


def _fid(cur, filter_name: str) -> int:
    name = rapid_filter_name(filter_name)
    cur.execute("SELECT fid FROM filters WHERE filter = %s", (name,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown filter {filter_name!r} (looked up as {name!r})")
    return row[0]


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


def _replay_differences(cur, rfid: int, registration: ReferenceImageRegistration,
                        fid: int, rids: list[int]) -> list[str]:
    """What the existing rows of ``rfid`` hold that the block does not say."""
    differences = []
    cur.execute("SELECT field, fid, checksum FROM refimages WHERE rfid = %s", (rfid,))
    field, row_fid, checksum = cur.fetchone()
    for name, held, said in (("field", field, registration.field), ("fid", row_fid, fid),
                             ("checksum", checksum, registration.md5)):
        if held != said:
            differences.append(f"refimages.{name} {held!r} != {said!r}")

    columns = _META_REAL + _META_INT + tuple(_META_DOUBLE) + tuple(_META_RENAMED)
    cur.execute(f"SELECT {', '.join(columns)} FROM refimmeta WHERE rfid = %s", (rfid,))
    row = cur.fetchone()
    if row is None:
        differences.append("no refimmeta row")
    else:
        held = dict(zip(columns, row))
        said: dict[str, Any] = {c: _as_real(getattr(registration, c)) for c in _META_REAL}
        said.update({c: getattr(registration, c) for c in _META_INT})
        said.update({c: getattr(registration, f) for c, f in _META_DOUBLE.items()})
        said.update({c: getattr(registration, f) for c, f in _META_RENAMED.items()})
        # A real comes back in its shortest text form, parsed as a double:
        # round both sides to float4 before comparing.
        for column in _META_REAL:
            if held[column] is not None:
                held[column] = _as_real(held[column])
        for column in columns:
            if held[column] != said[column]:
                differences.append(f"refimmeta.{column} {held[column]!r} != {said[column]!r}")

    cur.execute("SELECT rid FROM refimimages WHERE rfid = %s", (rfid,))
    held_rids = sorted(r[0] for r in cur.fetchall())
    if held_rids != sorted(rids):
        differences.append(f"refimimages rids {held_rids} != {sorted(rids)}")
    return differences


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

    ``attempt_id`` is the producing attempt (the manifest's ``attempt``).
    Replaying an instance already registered with the same content writes
    nothing and returns its row. Raises :class:`ValueError` -- `register`
    maps it to InputRejected (65) -- for an invalid entry, an unknown
    filter or recipe, a constituent with no `l2files` row, or a replay
    whose block differs from the rows already written.
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
        fid = _fid(cur, registration.filter)
        rids = _constituent_rids(cur, registration.constituents)

        cur.execute("SELECT rfid FROM refimages WHERE instance = %s", (instance,))
        row = cur.fetchone()
        if row is not None:
            (rfid,) = row
            differences = _replay_differences(cur, rfid, registration, fid, rids)
            if differences:
                raise ValueError(
                    f"reference-image {instance!r} is already registered (rfid {rfid}) "
                    f"with different content: {'; '.join(differences)}")
            return rfid

        hp6, hp9 = healpix_indexes(registration.ra_center, registration.dec_center)
        field = registration.field

        # Serialise addRefImage's max(version) + 1 per (field, fid, ppid).
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"refimages:{field}:{fid}:{ppid}",))
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
             registration.nsexcatsources, registration.npucatsources))

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
    Replaying with the same checksum writes nothing. Raises
    :class:`ValueError` for an invalid entry, an unregistered reference,
    or a different catalog already registered for the same reference and
    type.
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
            SELECT rfcatid, checksum FROM refimcatalogs
            WHERE rfid = %s AND ppid = %s AND cattype = %s
            """,
            (rfid, ppid, cattype))
        row = cur.fetchone()
        if row is not None:
            rfcatid, existing_checksum = row
            if existing_checksum == registration.md5:
                return rfcatid
            raise ValueError(
                f"refimcatalogs already holds a different {registration.catalog_type} "
                f"catalog for reference {reference!r} (rfcatid {rfcatid}, checksum "
                f"{existing_checksum!r} != {registration.md5!r})")

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
