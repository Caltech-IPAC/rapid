"""Reconcile at publish: a difference job's founded objects against those founded since its read.

Foundation plan, amendment of 2026-10-24 (association in the difference
job), with the field rule (runbook part 12 step 1): a difference job
matches its sources against the objects of their fields and those fields'
neighbours as it read them, and founds an object for each source that
matched none, in the source's own field. Two epochs differenced at once
both see no object at a new source and both found one, in one field or
either side of a boundary. The publisher, inside its one transaction for
the job, takes the locks of every field the job's manifest names
(:func:`lock`: each field that gained an object or a membership and each
one's neighbours, transaction-scoped advisory locks on the run and field
taken in ascending field order, so two jobs founding either side of a
boundary share a lock and serialise, and two jobs never wait on each
other in a cycle), reads the objects of the run in those fields founded
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
"""

from __future__ import annotations

from rapidpipe.products.spatial import ra_offset, within


def lock(cur, run: int, fields: list[int]) -> None:
    """Serialise publication per (run, field) for each of ``fields``, ascending, until the transaction ends."""
    ordered = sorted({int(f) for f in fields})
    if ordered:
        cur.execute("; ".join(["SELECT pg_advisory_xact_lock(%s, %s)"] * len(ordered)),
                    [v for f in ordered for v in (int(run), f)])


def between(cur, job: int, run: int, catalog: dict, read_jobs: list[int]) -> list[tuple]:
    """``(job, aid, ra0, dec0)`` of the run's objects in the catalog's fields founded outside the read.

    A job publishes all its objects in one transaction, so a founding job
    the read saw had published every object it has; one founded since is
    one whose job the read did not see.
    """
    cur.execute("SELECT o.job, o.aid, o.ra0, o.dec0 FROM objects o JOIN jobs j ON j.id = o.job"
                " WHERE j.run = %s AND o.dataset = %s AND o.survey = %s AND o.field = ANY(%s)"
                " AND o.job <> %s AND NOT o.job = ANY(%s) ORDER BY o.job, o.aid",
                (run, catalog["dataset"], catalog["survey"], fields(catalog), job,
                 list(read_jobs)))
    return cur.fetchall()


def fields(catalog: dict) -> list[int]:
    """The manifest's locked fields; a manifest written before the field rule names its one ``field``."""
    return sorted(int(f) for f in catalog.get("fields", [catalog.get("field")]))


def attach(job: int, founded: list[dict], memberships: list[dict], others: list[tuple],
           radius: float) -> tuple[list[dict], list[dict], dict]:
    """Founded objects and memberships after attaching to ``others`` (``between``'s rows).

    A founded object within ``radius`` of any of ``others`` is dropped; each
    of its memberships goes to every such object whose position its source
    is within ``radius`` of, rebased to that object's origin. A membership
    that falls within none is dropped, as a sequential run would never
    have made it. Returns the kept founded rows, the memberships and the
    counts recorded on the job.
    """
    targets = {}
    for obj in founded:
        near = [o for o in others if within(obj["ra0"], obj["dec0"], o[2], o[3], radius)]
        if near:
            targets[obj["aid"]] = (obj, near)
    kept = [obj for obj in founded if obj["aid"] not in targets]
    out, seen, attached = [], set(), 0
    for m in memberships:
        if m["object_job"] != job or m["aid"] not in targets:
            out.append(m)
            seen.add((m["object_job"], m["aid"], m["source_job"], m["sid"]))
            continue
        obj, near = targets[m["aid"]]
        ra = (obj["ra0"] + m["dra"]) % 360.0
        dec = obj["dec0"] + m["ddec"]
        for owner, aid, ra0, dec0 in near:
            key = (owner, aid, m["source_job"], m["sid"])
            if key in seen or not within(ra, dec, ra0, dec0, radius):
                continue
            seen.add(key)
            attached += 1
            out.append({**m, "object_job": owner, "aid": aid,
                        "dra": m["dra"] + ra_offset(obj["ra0"], ra0),
                        "ddec": m["ddec"] + (obj["dec0"] - dec0)})
    return kept, out, {"objects_between": len(others), "reconciled": len(targets),
                       "attached_memberships": attached}
