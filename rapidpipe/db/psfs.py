"""Persistence for the `psf` kind: one `psfs` row per instance, as `dev` adds it.

`dev` registers a PSF with ``add_psf`` then ``update_psf``
(``database/modules/utils/rapid_db.py``), the ``addPSF`` and ``updatePSF``
stored functions (20260921-01-baseline.sql). ``addPSF`` allocates the
version -- the next number for (``fid``, ``sca``) across the table -- and
inserts the row with ``vbest`` 0; ``updatePSF`` then makes it the current
row (``vbest`` 1, demoting the previous one). The rebuild calls ``addPSF``
itself, unchanged, then attaches the run model; it does not call
``updatePSF``, because legacy current flags are never set at registration
(products page, "Registration metadata"; promotion maintains them).

Column sources: ``fid`` by the key's filter name in `filters`; ``sca`` the
key's detector; ``filename`` the primary member resolved against the
output location; ``checksum`` and ``status`` from the registration block;
``run``, ``attempt``, ``instance`` (20260923-07-psfs-run-columns.sql) from
the enclosing manifest.

This module imports ``rapidpipe.db`` and ``rapidpipe.products`` only,
matching ``rapidpipe.db``'s package contract.
"""

from __future__ import annotations

from typing import Any

from rapidpipe.products.psf import psf_detector, validate_psf_entry


def register_psf(
    conn,
    *,
    entry: dict[str, Any],
    run_id: str,
    attempt_id: str,
    output_location: str,
) -> int:
    """Write the `psfs` row for one psf entry, in the caller's transaction; return its psfid.

    Replaying an instance already registered writes nothing and returns its
    row. Raises :class:`ValueError` for an invalid entry or an unknown filter.
    """
    registration = validate_psf_entry(entry)
    key = entry["key"]
    instance = entry["instance"]
    sca = psf_detector(key)
    primary = entry["members"][0]
    filename = f"{output_location}/{primary['path']}"

    with conn.cursor() as cur:
        cur.execute("SELECT psfid FROM psfs WHERE instance = %s", (instance,))
        row = cur.fetchone()
        if row is not None:
            return row[0]

        cur.execute("SELECT fid FROM filters WHERE filter = %s", (key["filter"],))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"unknown filter {key['filter']!r}")
        (fid,) = row

        cur.execute(
            """
            SELECT * FROM addPSF(
                cast(%s as smallint), cast(%s as smallint),
                cast(%s as character varying(255)), cast(%s as character varying(32)),
                cast(%s as smallint)
            ) AS (psfid integer, version smallint)
            """,
            (fid, sca, filename, registration.md5, registration.status))
        psfid, _version = cur.fetchone()

        cur.execute(
            "UPDATE psfs SET run = %s, attempt = %s, instance = %s WHERE psfid = %s",
            (run_id, attempt_id, instance, psfid))
        return psfid
