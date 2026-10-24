# ported-from: pipeline/crossMatchSources.py @ c740f3e3
"""The in-memory matcher: `dev`'s two crossmatch passes over rows held in memory.

Foundation plan v5, "Stages read, compute in memory": the branch's
crossmatch matched exposure k in SQL against the objects it had itself
written for exposure k-1 inside one open transaction; with outputs going
to Parquet there is nothing for q3c to query mid-job, so the match runs
here with the same semantics (``LEDGER-associate-code.md`` 2):

- pass 1, the field's own sources, exposure by exposure in ascending MJD
  (:func:`~rapidpipe.science.crossmatch.catalog.expid_order`): a source
  matches **every** object within ``match_radius``, not the nearest, so a
  source may join several objects; a source that matches none founds a
  new object whose id is ``radec_index`` of its position
  (:func:`~rapidpipe.science.crossmatch.catalog.new_object_id`), and two
  such sources at one packed position share that one object. Objects
  founded by an exposure are visible to the next exposure, not to the
  rest of their own (the branch copied them in after each exposure);
- pass 2, the neighbouring fields' sources against the finished catalog,
  memberships only (inside the field's inclusion cone when the field has
  eight neighbours, as `dev` does).

"Within the radius" is q3c's ``q3c_join`` test, a strict inequality on
the haversine of the separation,
``sin²(Δdec/2) + cos dec1 cos dec2 sin²(Δra/2) < sin²(r/2)``, evaluated in
the same order so a pair on the boundary falls the same side. Candidates
come from a grid of unit vectors whose cell is the radius's chord, so the
27 cells about a source hold every object it can match.

The one change from the branch, named: the catalog is a dictionary
grown in memory instead of the per-field table the branch's SQL queried.
Since the plan's amendment of 2026-10-24 it runs inside the difference
job, one epoch against the field's objects as the job read them
(:func:`associate_epoch`); the publisher reconciles founders that two jobs
in flight at once both made (``rapidpipe.foundation.catalog``).
Parity with the SQL matcher on the field is ``tests/db/test_associate_parity.py``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field as dataclass_field
from typing import Iterable

from rapidpipe.products.spatial import ra_offset, within
from rapidpipe.science.crossmatch.catalog import expid_order, new_object_id

__all__ = ["Association", "Catalog", "EpochAssociation", "Source", "associate", "associate_epoch", "within"]


@dataclass(frozen=True)
class Source:
    """One accepted source: its identity ``(source_job, sid)`` and what matching reads."""

    source_job: int
    sid: int
    expid: int
    mjdobs: float
    field: int
    ra: float
    dec: float


def _unit(ra: float, dec: float) -> tuple[float, float, float]:
    r, d = math.radians(ra), math.radians(dec)
    return math.cos(d) * math.cos(r), math.cos(d) * math.sin(r), math.sin(d)


@dataclass
class Catalog:
    """The field's objects by id with their founding positions, indexed for radius queries."""

    radius: float
    positions: dict[int, tuple[float, float]] = dataclass_field(default_factory=dict)
    _cells: dict[tuple[int, int, int], list[int]] = dataclass_field(
        default_factory=lambda: defaultdict(list))

    def __post_init__(self) -> None:
        self._cell = 2 * math.sin(math.radians(self.radius) / 2)
        given, self.positions = self.positions, {}
        for aid, (ra, dec) in given.items():
            self.add(aid, ra, dec)

    def _key(self, ra: float, dec: float) -> tuple[int, int, int]:
        return tuple(math.floor(c / self._cell) for c in _unit(ra, dec))

    def add(self, aid: int, ra: float, dec: float) -> None:
        if aid in self.positions:
            return
        self.positions[aid] = (ra, dec)
        self._cells[self._key(ra, dec)].append(aid)

    def matches(self, ra: float, dec: float) -> list[int]:
        """Every object within the radius of ``(ra, dec)``, in ascending id."""
        i, j, k = self._key(ra, dec)
        found = []
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for dk in (-1, 0, 1):
                    for aid in self._cells.get((i + di, j + dj, k + dk), ()):
                        ra0, dec0 = self.positions[aid]
                        if within(ra, dec, ra0, dec0, self.radius):
                            found.append(aid)
        return sorted(found)


@dataclass
class Association:
    """What one epoch's matching added: new objects and this epoch's memberships."""

    new_objects: dict[int, tuple[float, float]]
    memberships: list[tuple[int, int, int]]  # (aid, source_job, sid)
    exposures: int
    pass1_memberships: int
    pass2_memberships: int


def associate(catalog: Catalog, field_sources: Iterable[Source],
              neighbour_sources: Iterable[Source],
              cone: tuple[float, float, float] | None = None) -> Association:
    """Run pass 1 over ``field_sources`` and pass 2 over ``neighbour_sources``, growing ``catalog``.

    ``cone`` is ``(ra, dec, radius)``, the field's inclusion cone, applied
    to the neighbours' sources when given (`dev`: when the field has eight
    neighbours). Sources are taken in ``(source_job, sid)`` order within
    an exposure, so which of two coincident founders gives the object its
    position is reproducible (the branch's ``ON CONFLICT DO NOTHING`` kept
    whichever row came first).
    """
    by_expid: dict[int, list[Source]] = defaultdict(list)
    pairs = []
    for source in sorted(field_sources, key=lambda s: (s.source_job, s.sid)):
        by_expid[source.expid].append(source)
        pairs.append((source.expid, source.mjdobs))
    new_objects: dict[int, tuple[float, float]] = {}
    memberships: list[tuple[int, int, int]] = []
    order = expid_order(pairs)
    for expid in order:
        founded: dict[int, tuple[float, float]] = {}
        for source in by_expid[expid]:
            aids = catalog.matches(source.ra, source.dec)
            if not aids:
                aid = new_object_id(source.ra, source.dec)
                founded.setdefault(aid, (source.ra, source.dec))
                aids = [aid]
            memberships.extend((aid, source.source_job, source.sid) for aid in aids)
        for aid, (ra, dec) in founded.items():
            catalog.add(aid, ra, dec)
        new_objects.update(founded)
    pass1 = len(memberships)
    for source in sorted(neighbour_sources, key=lambda s: (s.source_job, s.sid)):
        if cone is not None and not within(source.ra, source.dec, *cone):
            continue
        memberships.extend((aid, source.source_job, source.sid)
                           for aid in catalog.matches(source.ra, source.dec))
    return Association(new_objects=new_objects, memberships=memberships, exposures=len(order),
                       pass1_memberships=pass1, pass2_memberships=len(memberships) - pass1)



@dataclass
class EpochAssociation:
    """One difference job's association: the objects it founded and its memberships.

    ``founded`` rows are ``aid, ra0, dec0``; ``memberships`` rows are
    ``object_job, aid, source_job, sid, dra, ddec, flux``: the object (its
    founding job and id), the source, and the source's offsets from the
    object's founding position (RA wrapped, degrees) and its ``fluxfit``,
    the membership's share of the object's sufficient sums
    (:mod:`rapidpipe.science.statistics.sums`).
    """

    founded: list[dict]
    memberships: list[dict]
    counts: dict


def associate_epoch(objects: dict[int, tuple[int, float, float]], sources: Iterable[dict], *,
                    job: int, field: int,
                    cone: tuple[float, float, float] | None, radius: float) -> EpochAssociation:
    """Match one epoch's accepted ``sources`` against the field's ``objects`` as read.

    ``objects`` maps ``aid`` to ``(founding job, ra0, dec0)``: the field's
    objects of the run when the job read them. ``sources`` are dicts with
    ``sid, expid, mjdobs, field, ra, dec, fluxfit``, the job's own (its id
    ``job`` is their source set), those in ``field`` matched by pass 1 and
    founding objects, those in its neighbours by pass 2 (inside ``cone``
    when given), as :func:`associate` runs them; a founded object's job is
    ``job``.
    """
    sources = list(sources)
    catalog = Catalog(radius, {aid: (ra0, dec0) for aid, (_, ra0, dec0) in objects.items()})
    as_source = [Source(job, s["sid"], s["expid"], s["mjdobs"], s["field"], s["ra"], s["dec"])
                 for s in sources]
    result = associate(catalog, [s for s in as_source if s.field == field],
                       [s for s in as_source if s.field != field], cone)
    origin = {aid: (owner, ra0, dec0) for aid, (owner, ra0, dec0) in objects.items()}
    origin.update({aid: (job, ra, dec) for aid, (ra, dec) in result.new_objects.items()})
    by_sid = {s["sid"]: s for s in sources}
    memberships = []
    for aid, source_job, sid in sorted(result.memberships):
        owner, ra0, dec0 = origin[aid]
        source = by_sid[sid]
        memberships.append({"object_job": owner, "aid": aid, "source_job": source_job, "sid": sid,
                            "dra": ra_offset(source["ra"], ra0), "ddec": source["dec"] - dec0,
                            "flux": float(source["fluxfit"])})
    founded = [{"aid": aid, "ra0": ra, "dec0": dec}
               for aid, (ra, dec) in sorted(result.new_objects.items())]
    return EpochAssociation(founded=founded, memberships=memberships, counts={
        "sources": len(sources), "objects_read": len(objects), "founded": len(founded),
        "pass1_memberships": result.pass1_memberships,
        "pass2_memberships": result.pass2_memberships})
