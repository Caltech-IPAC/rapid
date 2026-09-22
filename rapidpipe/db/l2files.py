"""Persistence for the l2-image kind: writing `l2files`/`l2filemeta` rows.

Implements the products page's "Registration metadata" l2-image field
list (https://roman-rapid.readthedocs.io/en/latest/system/products.html):
every target column has exactly one source -- a manifest value, a lookup
on an immutable parent instance, a deterministic derivation, or a
database allocation or default -- and nothing substitutes zero for an
unavailable measurement.

This module imports ``rapidpipe.db`` (its own package: :mod:`rapidpipe.db.
ids`) and ``rapidpipe.science.spatial``, and nothing else in
``rapidpipe`` -- never ``rapidpipe.runs`` or any stage module, matching
``rapidpipe.db``'s package contract of providing persistence only
(``rapidpipe/db/connection.py``'s module docstring).
"""

from __future__ import annotations

from typing import Any

from rapidpipe.db.ids import new_ulid
from rapidpipe.products.l2image import L2ImageRegistration
from rapidpipe.science.spatial import (
    healpix_indexes,
    overlapping_fields,
    tessellation_field,
    unit_vector,
)

#: Sparse SIP coefficient grid the `l2files` table carries as individual
#: columns (baseline schema, `database/migrations/20260921-01-baseline.sql`):
#: order 5, but missing the `_0_0` term on each axis (SIP has no constant
#: term). Matches `L2ImageRegistration.a`/`.b`'s `"i_j"` keys exactly.
_SIP_COEFFICIENTS = (
    "0_1", "0_2", "0_3", "0_4", "0_5",
    "1_0", "1_1", "1_2", "1_3", "1_4",
    "2_0", "2_1", "2_2", "2_3",
    "3_0", "3_1", "3_2",
    "4_0", "4_1",
    "5_0",
)


def _sip_columns(prefix: str, coeffs: dict[str, float]) -> dict[str, float | None]:
    """Map a registration's sparse SIP dict onto the table's named columns.

    NULL (never zero) where the registration's dict has no entry for that
    term -- the products page's "nothing substitutes zero for an
    unavailable measurement" rule applies to SIP terms as much as to any
    other optional field.
    """
    return {f"{prefix}_{ij}": coeffs.get(ij) for ij in _SIP_COEFFICIENTS}


def _find_instance_rid(cur, instance: str) -> int | None:
    cur.execute("SELECT rid FROM l2files WHERE instance = %s", (instance,))
    row = cur.fetchone()
    return row[0] if row is not None else None


def _find_or_create_exposure(
    cur, *, external_id: str, dateobs: str, mjdobs: float, exptime: float,
    fid: int, infobits: int, ra_targ: float, dec_targ: float,
) -> int:
    """Find the `exposures` row for `external_id`, or create it.

    On create: `dateobs`, `mjdobs`, `exptime`, `fid`, `infobits` come from
    the registration; `field`/`hp6`/`hp9` are derived from `ra_targ`/
    `dec_targ` (products page, l2-image field list: the `exposures`
    indexes are for RA_TARG/DEC_TARG, unlike `l2files`, whose `field`/
    `hp6`/`hp9` are for the image centre). `status` 1. On find: `expid` is
    reused without updating any column -- the products page's lookup-vs-
    allocation split treats an existing exposure as immutable here.
    """
    cur.execute("SELECT expid FROM exposures WHERE external_id = %s", (external_id,))
    row = cur.fetchone()
    if row is not None:
        return row[0]

    hp6, hp9 = healpix_indexes(ra_targ, dec_targ)
    field = tessellation_field(ra_targ, dec_targ)
    cur.execute(
        """
        INSERT INTO exposures (
            external_id, dateobs, field, hp6, hp9, fid, exptime, mjdobs,
            status, infobits
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING expid
        """,
        (external_id, dateobs, field, hp6, hp9, fid, exptime, mjdobs, 1, infobits),
    )
    (expid,) = cur.fetchone()
    return expid


def register_l2_image(
    conn,
    *,
    entry: dict[str, Any],
    run_id: str,
    attempt_id: str,
    output_location: str,
) -> int:
    """Write the `l2files`/`l2filemeta` rows for one l2-image manifest entry.

    Runs inside the caller's transaction (no commit/rollback here, matching
    every ``rapidpipe.runs.repository`` function's contract). Returns the
    `rid` of the (possibly pre-existing) `l2files` row.

    Idempotent: if an `l2files` row already carries this entry's instance
    id, its `rid` is returned and nothing is written (runs page,
    "Instances": "Replaying an identical manifest is a no-op").

    Raises :class:`ValueError` -- which the stage maps to `InputRejected`
    -- for an unknown filter or a non-integer delivered version; both are
    manifest-content problems, not database or programming errors.
    """
    registration = L2ImageRegistration.from_dict(entry["registration"])
    registration.validate()
    instance = entry["instance"]

    with conn.cursor() as cur:
        existing_rid = _find_instance_rid(cur, instance)
        if existing_rid is not None:
            return existing_rid

        cur.execute("SELECT fid FROM filters WHERE filter = %s", (registration.filter,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"unknown filter {registration.filter!r}")
        (fid,) = row

        try:
            version = int(registration.delivered_version)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"delivered_version {registration.delivered_version!r} is not "
                "an integer") from exc

        centre_ra = registration.centre["ra"]
        centre_dec = registration.centre["dec"]
        field, (hp6, hp9) = (
            tessellation_field(centre_ra, centre_dec),
            healpix_indexes(centre_ra, centre_dec),
        )
        x, y, z = unit_vector(centre_ra, centre_dec)

        overlapfields = overlapping_fields(
            registration.crval1, registration.crval2,
            registration.crpix1, registration.crpix2,
            registration.cd11, registration.cd12,
            registration.cd21, registration.cd22,
            registration.naxis1, registration.naxis2,
            field=field,
        )

        expid = _find_or_create_exposure(
            cur,
            external_id=registration.exposure_id,
            dateobs=registration.dateobs,
            mjdobs=registration.mjdobs,
            exptime=registration.exptime,
            fid=fid,
            infobits=registration.infobits,
            ra_targ=registration.ra_targ,
            dec_targ=registration.dec_targ,
        )

        primary_member = next(
            m for m in entry["members"] if m["path"] == entry["primary"])
        filename = f"{output_location}/{primary_member['path']}"

        a_columns = _sip_columns("a", registration.a)
        b_columns = _sip_columns("b", registration.b)

        columns = {
            "expid": expid,
            "sca": registration.detector,
            "version": version,
            "vbest": 0,
            "field": field,
            "hp6": hp6,
            "hp9": hp9,
            "fid": fid,
            "dateobs": registration.dateobs,
            "mjdobs": registration.mjdobs,
            "exptime": registration.exptime,
            "infobits": registration.infobits,
            "filename": filename,
            "checksum": registration.md5,
            "status": registration.status,
            "crval1": registration.crval1,
            "crval2": registration.crval2,
            "crpix1": registration.crpix1,
            "crpix2": registration.crpix2,
            "cd11": registration.cd11,
            "cd12": registration.cd12,
            "cd21": registration.cd21,
            "cd22": registration.cd22,
            "ctype1": registration.ctype1,
            "ctype2": registration.ctype2,
            "cunit1": registration.cunit1,
            "cunit2": registration.cunit2,
            "a_order": registration.a_order,
            "b_order": registration.b_order,
            "equinox": registration.equinox,
            "ra": registration.ra_targ,
            "dec": registration.dec_targ,
            "paobsy": registration.pa_obsy,
            "pafpa": registration.pa_fpa,
            "zptmag": registration.zptmag,
            "skymean": registration.skymean,
            "overlapfields": overlapfields,
            "run": run_id,
            "attempt": attempt_id,
            "instance": instance,
            **a_columns,
            **b_columns,
        }

        column_names = list(columns)
        placeholders = ", ".join(["%s"] * len(column_names))
        cur.execute(
            f"""
            INSERT INTO l2files ({", ".join(column_names)})
            VALUES ({placeholders})
            RETURNING rid
            """,
            [columns[name] for name in column_names],
        )
        (rid,) = cur.fetchone()

        corners = registration.corners
        cur.execute(
            """
            INSERT INTO l2filemeta (
                rid, ra0, dec0, ra1, dec1, ra2, dec2, ra3, dec3, ra4, dec4,
                x, y, z, hp6, hp9, fid, sca, mjdobs, run, attempt, instance
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                rid, centre_ra, centre_dec,
                corners[0][0], corners[0][1],
                corners[1][0], corners[1][1],
                corners[2][0], corners[2][1],
                corners[3][0], corners[3][1],
                x, y, z, hp6, hp9, fid, registration.detector,
                registration.mjdobs, run_id, attempt_id, instance,
            ),
        )

        return rid
