# ported-from: pipeline/crossMatchSources.py, pipeline/computeStatisticsForAstroObjects.py, pipeline/pruneNotBestMerges.py @ c740f3e3
"""``associate``: one field's object set for one epoch, matched in memory.

Foundation plan v5, Part 2: one stage in place of the branch's crossmatch,
statistics, prune and maintain. It runs as ``rapidpipe job local --stage
associate`` under :class:`rapidpipe.foundation.local.JobContext`: it reads
what its job's recorded inputs name in one short transaction, matches with
:func:`rapidpipe.science.crossmatch.memory.associate`, writes
``objects.parquet``, ``matches.parquet`` and ``manifest.json`` and returns;
``job local`` publishes.

Scope: ``{"field": <tessellation id>}``. Recorded inputs:

- ``input`` rows of product ``sources``: the epoch's source sets (the
  field's and its neighbours'; on the branch the neighbour pass read the
  same sets' sources in the neighbouring fields). Sources are accepted
  with ``flags`` equal to ``[associate] source_flags`` once the
  informational bits are cleared
  (:data:`rapidpipe.products.diffimage.INFORMATIONAL_SOURCE_FLAGS`, the
  bright footprint: a flagged source is associated) and ``fluxfit /
  fluxerr >= [associate] min_snr``. Pass 1 takes those in the field, pass 2 those in
  its neighbours (:func:`rapidpipe.science.spatial.field_neighbours`);
- at most one ``input`` row of product ``objects``: the predecessor object
  set (none for a field's first epoch), whose objects and sums this set
  carries forward and whose objects the matching starts from;
- ``excludes`` rows: source sets no longer official, whose members in the
  predecessor's chain are subtracted from the sums. A source set some set
  of the chain already excludes was subtracted then and is not again.

Outputs, declared in the manifest: ``objects.parquet`` (table
``objects``: every object of the field with its founding position and
sufficient sums, :mod:`rapidpipe.science.statistics.sums`) and
``matches.parquet`` (table ``memberships``: this epoch's memberships only,
``aid, source_job, sid``; the members view follows the predecessor chain).
The job's ``expects`` row names the field's open object binding, or none.

Gone with the port, as the plan gives them up: the branch's per-field
tables and their ``CLUSTER`` (``maintain``), the advisory lock, the
``done_check`` reuse, and prune's ``vbest`` rule (exclusions replace it).
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq

from rapidpipe.foundation.local import JobContext
from rapidpipe.products.diffimage import INFORMATIONAL_SOURCE_FLAGS
from rapidpipe.science.crossmatch.catalog import field_inclusion_cone
from rapidpipe.science.crossmatch.memory import Catalog, Source, associate
from rapidpipe.science.spatial import field_neighbours
from rapidpipe.science.statistics.sums import Sums

#: `dev`'s pass 2 uses the inclusion cone only for a tile with this many neighbours.
CONE_NEIGHBOURS = 8

OBJECTS_SCHEMA = pa.schema([
    ("aid", pa.int64()), ("dataset", pa.string()), ("survey", pa.string()),
    ("field", pa.int32()), ("ra0", pa.float64()), ("dec0", pa.float64()),
    ("nsources", pa.int32()),
    *((name, pa.float64()) for name in ("sumra", "sumra2", "sumdec", "sumdec2",
                                        "sumflux", "sumflux2")),
])
MATCHES_SCHEMA = pa.schema([("aid", pa.int64()), ("source_job", pa.int64()), ("sid", pa.int64())])

SOURCES_SQL = (
    "SELECT job, sid, expid, mjdobs, field, ra, dec, fluxfit FROM sources"
    " WHERE job = ANY(%s) AND field = ANY(%s) AND flags & ~%s = %s"
    " AND fluxerr > 0 AND fluxfit / fluxerr >= %s")

OBJECTS_SQL = (
    "SELECT aid, ra0, dec0, nsources, sumra, sumra2, sumdec, sumdec2, sumflux, sumflux2"
    " FROM objects WHERE job = %s AND dataset = %s AND survey = %s AND field = %s")

#: The predecessor's chain of object sets (itself first), through each set's `objects` input.
CHAIN_SQL = (
    "WITH RECURSIVE chain (job) AS ("
    " SELECT %s::bigint"
    " UNION ALL SELECT i.source_job FROM chain c JOIN job_inputs i ON i.job = c.job"
    " AND i.kind = 'input' AND i.product = 'objects' WHERE i.source_job IS NOT NULL)"
    " SELECT job FROM chain")

#: The members of the chain's sets from one source set, with what the sums read.
EXCLUDED_MEMBERS_SQL = (
    "SELECT m.aid, s.ra, s.dec, s.fluxfit FROM memberships m"
    " JOIN sources s ON s.job = m.source_job AND s.sid = m.sid"
    " WHERE m.job = ANY(%s) AND m.source_job = %s")


def _inputs(job: JobContext) -> tuple[list[int], int | None, list[int]]:
    source_sets = sorted(i["source_job"] for i in job.inputs
                         if i["kind"] == "input" and i["product"] == "sources")
    predecessors = [i["source_job"] for i in job.inputs
                    if i["kind"] == "input" and i["product"] == "objects"]
    excluded = sorted(i["source_job"] for i in job.inputs if i["kind"] == "excludes")
    if not source_sets:
        raise ValueError(f"job {job.job}: no source set among its inputs")
    if len(predecessors) > 1:
        raise ValueError(f"job {job.job}: {len(predecessors)} predecessor object sets; at most one")
    return source_sets, (predecessors[0] if predecessors else None), excluded


def _read(job: JobContext, field: int, neighbours: list[int], source_sets: list[int],
          predecessor: int | None, excluded: list[int]):
    """Everything the job reads, in one short transaction."""
    settings = job.settings["associate"]
    with job.connect() as conn, conn.cursor() as cur:
        cur.execute(SOURCES_SQL, (source_sets, [field, *neighbours], INFORMATIONAL_SOURCE_FLAGS,
                                  int(settings["source_flags"]), float(settings["min_snr"])))
        rows = cur.fetchall()
        sources = [Source(*r[:7]) for r in rows]
        fluxes = {(r[0], r[1]): r[7] for r in rows}
        objects: dict[int, Sums] = {}
        subtract: list[tuple] = []
        if predecessor is not None:
            cur.execute(OBJECTS_SQL, (predecessor, job.dataset, job.survey, field))
            for aid, *values in cur.fetchall():
                objects[aid] = Sums(*values)
            cur.execute(CHAIN_SQL, (predecessor,))
            chain = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT DISTINCT source_job FROM job_inputs"
                        " WHERE job = ANY(%s) AND kind = 'excludes'", (chain,))
            already = {r[0] for r in cur.fetchall()}
            for source_job in excluded:
                if source_job not in already:
                    cur.execute(EXCLUDED_MEMBERS_SQL, (chain, source_job))
                    subtract.extend(cur.fetchall())
        conn.rollback()
    return sources, fluxes, objects, subtract


def run(job: JobContext) -> None:
    field = int(job.scope["field"])
    radius = float(job.settings["associate"]["match_radius"])
    source_sets, predecessor, excluded = _inputs(job)
    neighbours = field_neighbours(field)
    sources, fluxes, objects, subtract = _read(job, field, neighbours, source_sets,
                                               predecessor, excluded)

    for aid, ra, dec, flux in subtract:
        objects[aid].add(ra, dec, flux, sign=-1)
    catalog = Catalog(radius, {aid: (s.ra0, s.dec0) for aid, s in objects.items()})
    cone = field_inclusion_cone(field, radius) if len(neighbours) == CONE_NEIGHBOURS else None
    result = associate(catalog, [s for s in sources if s.field == field],
                       [s for s in sources if s.field != field], cone)
    for aid, (ra, dec) in result.new_objects.items():
        objects[aid] = Sums(ra, dec)
    position = {(s.source_job, s.sid): (s.ra, s.dec) for s in sources}
    for aid, source_job, sid in result.memberships:
        objects[aid].add(*position[(source_job, sid)], fluxes[(source_job, sid)])

    aids = sorted(objects)
    columns = {"aid": aids, "dataset": [job.dataset] * len(aids),
               "survey": [job.survey] * len(aids), "field": [field] * len(aids)}
    for name in OBJECTS_SCHEMA.names[4:]:
        columns[name] = [getattr(objects[aid], name) for aid in aids]
    pq.write_table(pa.table(columns, schema=OBJECTS_SCHEMA), job.path / "objects.parquet")
    memberships = sorted(result.memberships)
    pq.write_table(pa.table({name: [m[i] for m in memberships]
                             for i, name in enumerate(MATCHES_SCHEMA.names)},
                            schema=MATCHES_SCHEMA), job.path / "matches.parquet")
    job.write_manifest([("objects", "objects.parquet"), ("memberships", "matches.parquet")],
                       associate={"sources": len(sources), "exposures": result.exposures,
                                  "new_objects": len(result.new_objects),
                                  "pass1_memberships": result.pass1_memberships,
                                  "pass2_memberships": result.pass2_memberships,
                                  "excluded_members": len(subtract)})
