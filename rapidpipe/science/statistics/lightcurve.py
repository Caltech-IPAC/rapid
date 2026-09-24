"""`dev`'s per-object light-curve statistics (computeStatisticsForAstroObjects.py).

For each object (``aid``) of a field, `dev` gathers the RA, Dec and
``fluxfit`` of every source the object's ``merges`` rows name (the UNION
ALL query, L448-460, accumulated into three ``defaultdict(list)`` keyed by
``aid``), then per object (L574-590):

- ``compute_radec_statistics(ras, decs)`` (``rapid_pipeline_subs.py``
  L2735-2785, ported verbatim below): the mean position from the mean of
  the sources' unit vectors, so the 0/360 RA wrap and the poles are
  handled; per-axis standard deviations by small-angle projection, RA's
  scaled by cos(Dec); and the RMS angular spread, which `dev` only logs
  (``astroobjectsmeta`` has no column for it);
- ``meanflux = np.mean(fluxes)``, ``stdevflux = np.std(fluxes)``
  (population standard deviation, so one source gives 0.0, not NaN);
- ``nsources = len(ras)``;

and writes one CSV line, ``",".join(str(v) for v in (aid, meanra, stdra,
meandec, stddec, meanflux, stdflux, nsources))``, in
``astroobjectsmeta``'s column order. The rebuild appends the three
run-model columns (``run``, ``attempt``, ``result_set``) to each line, as
every rebuild writer does.

One difference from `dev`, recorded on the stage's page: `dev` iterates
``list(best_aids)``, a set, so its line order is arbitrary; the rebuild
writes objects in ascending ``aid`` order, which changes nothing in the
table but makes the file reproducible.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np


def compute_radec_statistics(ra_deg, dec_deg):

    """Compute mean and stddev of (RA, Dec) positions, handling poles and 0/360 wrap.

    Parameters
    ----------
    ra_deg, dec_deg : array-like
        Right ascension and declination in degrees.

    Returns
    -------
    mean_ra, mean_dec, stddev_ra, stddev_dec, sky_position_spread : float
        All values in degrees. stddev_ra is corrected for cos(dec).
    """
    ra = np.deg2rad(np.asarray(ra_deg, dtype=float))
    dec = np.deg2rad(np.asarray(dec_deg, dtype=float))

    # Convert to unit vectors on the sphere
    cos_dec = np.cos(dec)
    x = cos_dec * np.cos(ra)
    y = cos_dec * np.sin(ra)
    z = np.sin(dec)

    # Mean Cartesian position
    xm, ym, zm = x.mean(), y.mean(), z.mean()
    r = np.sqrt(xm**2 + ym**2 + zm**2)

    # Mean RA/Dec from mean vector
    mean_dec = np.rad2deg(np.arcsin(np.clip(zm / r, -1, 1)))
    mean_ra = np.rad2deg(np.arctan2(ym, xm)) % 360.0

    # Angular separation of each point from the mean position
    # (dot product clamped for numerical safety)
    xn, yn, zn = xm / r, ym / r, zm / r
    dot = np.clip(x * xn + y * yn + z * zn, -1, 1)
    ang_sep = np.arccos(dot)

    # Sky position spread: RMS angular distance from mean (degrees)
    sky_position_spread = np.rad2deg(np.sqrt(np.mean(ang_sep**2)))

    # Per-axis standard deviations via small-angle projection
    # Delta-Dec
    ddec = dec - np.deg2rad(mean_dec)
    stddev_dec = np.rad2deg(np.std(ddec))

    # Delta-RA: shortest signed difference, scaled by cos(dec)
    dra = np.arctan2(np.sin(ra - np.deg2rad(mean_ra)),
                     np.cos(ra - np.deg2rad(mean_ra)))
    stddev_ra = np.rad2deg(np.std(dra * np.cos(dec)))

    return mean_ra, mean_dec, stddev_ra, stddev_dec, sky_position_spread


@dataclass(frozen=True)
class ObjectStatistics:
    """One ``astroobjectsmeta`` row's `dev` columns, in `dev`'s order, plus the logged spread."""

    aid: int
    meanra: float
    stdevra: float
    meandec: float
    stdevdec: float
    meanflux: float
    stdevflux: float
    nsources: int
    sky_position_spread: float

    def dev_values(self) -> tuple:
        """``(aid, meanra, stdevra, meandec, stdevdec, meanflux, stdevflux, nsources)``."""
        return (self.aid, self.meanra, self.stdevra, self.meandec, self.stdevdec,
                self.meanflux, self.stdevflux, self.nsources)


def object_statistics(aid: int, ras: Sequence[float], decs: Sequence[float],
                      fluxes: Sequence[float]) -> ObjectStatistics:
    """`dev`'s per-aid computation (computeStatisticsForAstroObjects.py L574-582)."""
    nsources = len(ras)
    meanra, meandec, stdra, stddec, sky_position_spread = compute_radec_statistics(ras, decs)
    meanflux = np.mean(fluxes)
    stdflux = np.std(fluxes)
    return ObjectStatistics(aid=aid, meanra=meanra, stdevra=stdra, meandec=meandec,
                            stdevdec=stddec, meanflux=meanflux, stdevflux=stdflux,
                            nsources=nsources, sky_position_spread=sky_position_spread)


@dataclass
class Accumulated:
    """`dev`'s three per-aid lists, and how many repeated (aid, sid) pairs were dropped."""

    ras: dict[int, list[float]]
    decs: dict[int, list[float]]
    fluxes: dict[int, list[float]]
    repeated_pairs: int = 0

    def aids(self) -> list[int]:
        return sorted(self.ras)


def accumulate(records: Iterable[Sequence]) -> Accumulated:
    """Group ``(aid, sid, ra, dec, fluxfit)`` records by ``aid``, as `dev` does (L486-492).

    `dev`'s records are ``(aid, ra, dec, fluxfit)`` from one ``merges`` table
    whose ``(aid, sid)`` pairs `dev`'s ``pruneRedundantMerges`` has made
    unique. The rebuild's membership is a base-plus-delta chain of sets, so
    the same pair could in principle appear under two sets of the chain;
    each ``(aid, sid)`` pair counts once, and the repeats are counted.
    """
    ras: dict[int, list[float]] = defaultdict(list)
    decs: dict[int, list[float]] = defaultdict(list)
    fluxes: dict[int, list[float]] = defaultdict(list)
    seen: set[tuple[int, int]] = set()
    repeated = 0
    for aid, sid, ra, dec, flux in records:
        pair = (int(aid), int(sid))
        if pair in seen:
            repeated += 1
            continue
        seen.add(pair)
        ras[pair[0]].append(ra)
        decs[pair[0]].append(dec)
        fluxes[pair[0]].append(flux)
    return Accumulated(ras=dict(ras), decs=dict(decs), fluxes=dict(fluxes),
                       repeated_pairs=repeated)


def csv_line(stats: ObjectStatistics, run_columns: Sequence[str]) -> str:
    """`dev`'s CSV line for one object, with the run columns appended; ends in a newline."""
    return ",".join(str(v) for v in (*stats.dev_values(), *run_columns)) + "\n"
