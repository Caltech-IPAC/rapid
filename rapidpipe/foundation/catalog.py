"""Reconcile at publish: a difference job's founded objects against those founded since its read.

Foundation plan, amendment of 2026-10-24 (association in the difference
job), with the field rule (runbook part 12 step 1): a difference job
matches its sources against the objects of their fields and those fields'
neighbours as it read them, and founds an object for each source that
matched none, in the source's own field. Two epochs differenced at once
both see no object at a new source and both found one, in one field or
either side of a boundary. The publisher, inside its one transaction for
the job, takes the locks of every field the job's manifest names
(:func:`try_lock`: each field that gained an object or a membership and each
one's neighbours, transaction-scoped advisory locks on the run and field,
all taken or none, so two jobs founding either side of a boundary share a
lock and serialise; a job that finds one held rolls back and tries again,
never waiting on it while it holds a pool connection), reads the objects of the run in those fields founded
by jobs outside the job's recorded read (:func:`between`, those published
since the read), and :func:`attach` gives each founded object within the
match radius of one of them to that object instead: its memberships are
attached to the existing object, each one whose source is within the
radius of it, and the founded object is not inserted. Object ids never
change after publication.

A membership's sums are its source's offsets from the object's founding
position (:mod:`rapidpipe.science.statistics.sums`), so an attached
membership is rebased to the surviving object's origin: for an origin
shift ``d`` (the founded object's origin minus the surviving one's, RA
wrapped) each offset gains ``d``, which over an object's ``n`` attached
members is ``S' = S + n d`` and ``Q' = Q + 2 d S + n d²`` per axis.

`dev`'s alternative, tolerating the duplicates and merging them later
(``xmerges``), changes object ids after alerts have carried them; not
taken (rapid_docs ``decisions.md``).

The xsources' catalog, ``xobjects`` and ``xmemberships`` (`dev`'s
XAstroObjects and XMerges), is reconciled the same way, by the same
functions over its own tables and ids (:data:`CATALOGS`); a job that
publishes both takes the locks of both catalogs' fields at once, in one
ascending order.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right

from rapidpipe.products.spatial import ra_offset, within

#: The catalogs reconciled at publish, by objects table: (objects table, memberships table,
#: object id column, source id column).
CATALOGS = {"objects": ("objects", "memberships", "aid", "sid"),
            "xobjects": ("xobjects", "xmemberships", "xaid", "xsid")}


def try_lock(cur, run: int, fields: list[int]) -> bool:
    """Take the (run, field) lock of each of ``fields``, ascending, until the transaction ends, or none.

    One statement of ``pg_try_advisory_xact_lock``, which never waits: true
    when every lock is taken, false when another transaction holds any, and
    the caller then rolls back, releasing those it did take with its pool
    connection. A publisher that waited on the locks instead held a pooler
    server connection for the wait, and run 177's 800 waiters filled the
    pipeline's pool (publish-pool-wedge, 2026-11-08).
    """
    ordered = sorted({int(f) for f in fields})
    if not ordered:
        return True
    cur.execute("SELECT " + " AND ".join(["pg_try_advisory_xact_lock(%s, %s)"] * len(ordered)),
                [v for f in ordered for v in (int(run), f)])
    return bool(cur.fetchone()[0])


def between(cur, job: int, run: int, catalog: dict, read_jobs: list[int],
            table: str = "objects") -> list[tuple]:
    """``(job, id, ra0, dec0, field)`` of the run's objects in the catalog's fields founded outside the read.

    A job publishes all its objects in one transaction, so a founding job
    the read saw had published every object it has; one founded since is
    one whose job the read did not see. Read from those fields' children of
    ``table`` (``objects_in`` or ``xobjects_in`` of the run), never the parent.
    """
    ident = CATALOGS[table][2]
    cur.execute(f"SELECT o.job, o.{ident}, o.ra0, o.dec0, o.field FROM {table}_in(%s::integer[], %s) o"
                " WHERE o.dataset = %s AND o.survey = %s"
                f" AND o.job <> %s AND NOT o.job = ANY(%s) ORDER BY o.job, o.{ident}",
                (fields(catalog), run, catalog["dataset"], catalog["survey"], job,
                 list(read_jobs)))
    return cur.fetchall()


def fields(catalog: dict) -> list[int]:
    """The manifest's locked fields; a manifest written before the field rule names its one ``field``."""
    return sorted(int(f) for f in catalog.get("fields", [catalog.get("field")]))


def attach(job: int, founded: list[dict], memberships: list[dict], others: list[tuple],
           radius: float, ident: str = "aid", sid: str = "sid") -> tuple[list[dict], list[dict], dict]:
    """Founded objects and memberships after attaching to ``others`` (``between``'s rows).

    A founded object within ``radius`` of any of ``others`` is dropped; each
    of its memberships goes to every such object whose position its source
    is within ``radius`` of, rebased to that object's origin. A membership
    that falls within none is dropped, as a sequential run would never
    have made it. An attached membership takes the surviving object's field.
    Returns the kept founded rows, the memberships and the counts recorded
    on the job. ``ident`` and ``sid`` name the object and source id columns
    (``xaid`` and ``xsid`` for the xsources' catalog).
    """
    # Only an object within two radii in declination can be within one on the sky, so each
    # founded object tests that band of ``others`` (sorted by declination), in ``others``' order:
    # founded times others pairs took run 177's holder ten minutes under its locks.
    by_dec = sorted(range(len(others)), key=lambda i: others[i][3])
    decs = [others[i][3] for i in by_dec]
    targets = {}
    for obj in founded:
        band = by_dec[bisect_left(decs, obj["dec0"] - 2 * radius):
                      bisect_right(decs, obj["dec0"] + 2 * radius)]
        near = [others[i] for i in sorted(band)
                if within(obj["ra0"], obj["dec0"], others[i][2], others[i][3], radius)]
        if near:
            targets[obj[ident]] = (obj, near)
    kept = [obj for obj in founded if obj[ident] not in targets]
    out, seen, attached = [], set(), 0
    for m in memberships:
        if m["object_job"] != job or m[ident] not in targets:
            out.append(m)
            seen.add((m["object_job"], m[ident], m["source_job"], m[sid]))
            continue
        obj, near = targets[m[ident]]
        ra = (obj["ra0"] + m["dra"]) % 360.0
        dec = obj["dec0"] + m["ddec"]
        for owner, aid, ra0, dec0, field in near:
            key = (owner, aid, m["source_job"], m[sid])
            if key in seen or not within(ra, dec, ra0, dec0, radius):
                continue
            seen.add(key)
            attached += 1
            out.append({**m, "object_job": owner, ident: aid, "field": field,
                        "dra": m["dra"] + ra_offset(obj["ra0"], ra0),
                        "ddec": m["ddec"] + (obj["dec0"] - dec0)})
    return kept, out, {"objects_between": len(others), "reconciled": len(targets),
                       "attached_memberships": attached}
