"""Persistence for the difference-image kind: `diffimages`/`diffimmeta` rows.

Implements the products page's difference-image field list
(https://roman-rapid.readthedocs.io/en/latest/system/products.html,
"Registration metadata"): every column has one source -- a manifest
value, a lookup on an immutable parent instance, a deterministic
derivation, or an allocation -- and nothing substitutes zero for an
unavailable measurement. One call writes one row pair, for one registered
differencer's instance; `register` calls it once per difference-image
entry in a manifest.

Sources, column by column, where they are not the manifest itself:

- ``rid``, ``expid``, ``sca``, ``field``, ``fid``, ``jd``: the l2 instance's
  `l2files` row (``jd`` from its ``mjdobs``), as `dev`'s ``addDiffImage``
  copies them.
- ``rfid``: the reference instance's `refimages` row
  (20260923-01-refimages-instance.sql); for a reference registered by
  `dev`, which has no instance, the legacy rfid the manifest carries.
- ``ppid``: :data:`DIFFERENCER_PPIDS`, the fixed differencer-to-pipeline
  map. ZOGY registers as in `dev`, under the science pipeline's row (15).
- ``hp6``/``hp9`` on both tables: from the difference image's centre.
- ``version``: the next number for (``rid``, ``ppid``) within the run.
- ``svid``: the `swversions` row for the run's code revision, made on
  first use.
- ``vbest`` 0 and ``status`` 0: never current at registration.

This module imports ``rapidpipe.db``, ``rapidpipe.products`` and
``rapidpipe.science.spatial`` only, matching ``rapidpipe.db``'s package
contract (no ``rapidpipe.runs``, no stage module).
"""

from __future__ import annotations

import json
from typing import Any

from rapidpipe.products.diffimage import validate_difference_entry
from rapidpipe.science.spatial import healpix_indexes

#: The differencer-to-`pipelines` map, fixed with the difference stage
#: (products page: "the name-to-row mapping is fixed with the `difference`
#: stage"). ZOGY is the science pipeline's row, as `dev` registers it
#: (``[SCI_IMAGE] ppid = 15``). SFFT has no `pipelines` row yet: its
#: registration is designed in and off by default, and a manifest carrying
#: an SFFT instance is refused here until the lead assigns one.
DIFFERENCER_PPIDS: dict[str, int] = {"zogy": 15}

#: `swversions.cvstag` and `.release` widths (baseline schema).
_CVSTAG_WIDTH = 30
_RELEASE_WIDTH = 15


def _find_instance_pid(cur, instance: str) -> int | None:
    cur.execute("SELECT pid FROM diffimages WHERE instance = %s", (instance,))
    row = cur.fetchone()
    return row[0] if row is not None else None


def _l2_row(cur, l2_instance: str) -> tuple:
    cur.execute(
        "SELECT rid, expid, sca, field, fid, mjdobs FROM l2files WHERE instance = %s",
        (l2_instance,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"no l2files row for l2 instance {l2_instance!r}")
    return row


def _rfid(cur, reference_instance: str, legacy_rfid: int | None) -> int:
    cur.execute("SELECT rfid FROM refimages WHERE instance = %s", (reference_instance,))
    row = cur.fetchone()
    if row is not None:
        return row[0]
    if legacy_rfid is None:
        raise ValueError(
            f"no refimages row for reference instance {reference_instance!r}, and the "
            "manifest carries no legacy reference_rfid")
    cur.execute("SELECT rfid FROM refimages WHERE rfid = %s", (legacy_rfid,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"no refimages row with legacy rfid {legacy_rfid}")
    return row[0]


def _svid(cur, run_id: str) -> int:
    """The `swversions` row for the run's code revision, made on first use."""
    cur.execute("SELECT code_revision FROM runs WHERE id = %s", (run_id,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"run {run_id!r} does not exist")
    (revision,) = row
    cvstag = revision[:_CVSTAG_WIDTH]
    cur.execute(
        "SELECT svid FROM swversions WHERE cvstag = %s ORDER BY svid LIMIT 1", (cvstag,))
    row = cur.fetchone()
    if row is not None:
        return row[0]
    cur.execute(
        """
        INSERT INTO swversions (cvstag, installed, comment, release)
        VALUES (%s, now(), %s, %s)
        RETURNING svid
        """,
        (cvstag, f"rapidpipe code revision {revision}", revision[:_RELEASE_WIDTH]),
    )
    return cur.fetchone()[0]


def register_difference_image(
    conn,
    *,
    entry: dict[str, Any],
    run_id: str,
    attempt_id: str,
    output_location: str,
) -> int:
    """Write the `diffimages`/`diffimmeta` rows for one difference-image entry.

    Runs inside the caller's transaction (no commit or rollback here).
    Returns the `pid` of the (possibly pre-existing) `diffimages` row:
    replaying a manifest whose instance is already registered writes
    nothing (runs page, "Instances").

    Raises :class:`ValueError` -- `register` maps it to InputRejected --
    for an entry that fails the products page's checks, an unknown
    differencer, or a parent (l2 image, reference) that is not registered.
    """
    registration = validate_difference_entry(entry)
    key = entry["key"]
    instance = entry["instance"]
    differencer = key["differencer"]
    if differencer not in DIFFERENCER_PPIDS:
        raise ValueError(
            f"differencer {differencer!r} has no pipelines row to register under; "
            f"known: {sorted(DIFFERENCER_PPIDS)}")
    ppid = DIFFERENCER_PPIDS[differencer]

    with conn.cursor() as cur:
        existing = _find_instance_pid(cur, instance)
        if existing is not None:
            return existing

        rid, expid, sca, field, fid, mjdobs = _l2_row(cur, key["l2"])
        rfid = _rfid(cur, key["reference"], registration.reference_rfid)
        svid = _svid(cur, run_id)

        cur.execute(
            """
            SELECT coalesce(max(version), 0) + 1 FROM diffimages
            WHERE rid = %s AND ppid = %s AND run = %s
            """,
            (rid, ppid, run_id))
        (version,) = cur.fetchone()

        centre_ra = registration.centre["ra"]
        centre_dec = registration.centre["dec"]
        hp6, hp9 = healpix_indexes(centre_ra, centre_dec)
        corners = registration.corners

        primary = next(m for m in entry["members"] if m["path"] == entry["primary"])
        filename = f"{output_location}/{primary['path']}"

        columns = {
            "rid": rid,
            "expid": expid,
            "sca": sca,
            "ppid": ppid,
            "version": version,
            "vbest": 0,
            "rfid": rfid,
            "field": field,
            "hp6": hp6,
            "hp9": hp9,
            "fid": fid,
            "jd": mjdobs + 2400000.5,
            "ra0": centre_ra,
            "dec0": centre_dec,
            "ra1": corners[0][0], "dec1": corners[0][1],
            "ra2": corners[1][0], "dec2": corners[1][1],
            "ra3": corners[2][0], "dec3": corners[2][1],
            "ra4": corners[3][0], "dec4": corners[3][1],
            "infobitssci": registration.catalog_outcome_bits,
            "infobitsref": registration.infobits_reference,
            "filename": filename,
            "checksum": registration.md5,
            "status": 0,
            "svid": svid,
            "run": run_id,
            "attempt": attempt_id,
            "instance": instance,
        }
        names = list(columns)
        cur.execute(
            f"""
            INSERT INTO diffimages ({", ".join(names)})
            VALUES ({", ".join(["%s"] * len(names))})
            RETURNING pid
            """,
            [columns[name] for name in names],
        )
        (pid,) = cur.fetchone()

        residual = registration.registration_residual
        cur.execute(
            """
            INSERT INTO diffimmeta (
                pid, nsexcatsources, scalefacref, dxrmsfin, dyrmsfin,
                dxmedianfin, dymedianfin, field, hp6, hp9, fid, sca,
                source_counts, run, attempt, instance
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                pid,
                registration.source_counts["sextractor"]["positive"],
                registration.reference_scale_factor,
                residual["x_rms"], residual["y_rms"],
                residual["x_median"], residual["y_median"],
                field, hp6, hp9, fid, sca,
                json.dumps(registration.source_counts),
                run_id, attempt_id, instance,
            ),
        )
        return pid
