# ported-from: none
"""An object's statistics as sufficient sums, updated per epoch without reading history.

Foundation plan, the amendment of 2026-10-24: each membership carries its
source's offsets from the object's founding position and its flux, and an
object's count and sums and sums of squares of RA, Dec and flux are the
sums over its official memberships (``objects_official``), additive and so
order-free; the light-curve statistics of
:mod:`rapidpipe.science.statistics.lightcurve` follow from the sums alone.

The position sums are of offsets from the object's founding position
``(ra0, dec0)``, in degrees, the RA offset wrapped into [-180, 180): the
spread of an object's positions is about 1e-5 degrees and its RA about
268, so sums of raw degrees would lose the variance to cancellation
(``sumra2 / n - mean²`` subtracts two numbers near 7e4 to find one near
1e-10). The flux sums are of ``fluxfit`` itself.

From the sums: ``meanra = ra0 + sumra / n``, ``meandec = dec0 + sumdec / n``,
``meanflux = sumflux / n``, and each standard deviation is the population
one, ``sqrt(sum2 / n - (sum / n)²)``, as ``np.std`` computes it; RA's is
scaled by ``cos(meandec)`` as ``compute_radec_statistics`` scales by each
member's ``cos(dec)``. The means agree with `dev`'s unit-vector mean and
the standard deviations with ``np.std`` to floating point for objects a
match radius across; RA's spread to the relative size of the members'
Dec spread times ``tan(dec)`` (one cosine for all members, not one each).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from rapidpipe.products.spatial import ra_offset

__all__ = ["Sums", "ra_offset"]


@dataclass
class Sums:
    """One object's founding position and sufficient sums (``objects`` columns)."""

    ra0: float
    dec0: float
    nsources: int = 0
    sumra: float = 0.0
    sumra2: float = 0.0
    sumdec: float = 0.0
    sumdec2: float = 0.0
    sumflux: float = 0.0
    sumflux2: float = 0.0

    def add(self, ra: float, dec: float, flux: float, sign: int = 1) -> None:
        """Add one member (``sign = -1`` removes one)."""
        dra, ddec = ra_offset(ra, self.ra0), dec - self.dec0
        self.nsources += sign
        self.sumra += sign * dra
        self.sumra2 += sign * dra * dra
        self.sumdec += sign * ddec
        self.sumdec2 += sign * ddec * ddec
        self.sumflux += sign * flux
        self.sumflux2 += sign * flux * flux

    def statistics(self) -> dict[str, float]:
        """``meanra, stdevra, meandec, stdevdec, meanflux, stdevflux`` (NaN with no members)."""
        n = self.nsources
        if n <= 0:
            return dict.fromkeys(("meanra", "stdevra", "meandec", "stdevdec",
                                  "meanflux", "stdevflux"), math.nan)

        def mean_std(total: float, total2: float) -> tuple[float, float]:
            mean = total / n
            return mean, math.sqrt(max(total2 / n - mean * mean, 0.0))

        mra, sra = mean_std(self.sumra, self.sumra2)
        mdec, sdec = mean_std(self.sumdec, self.sumdec2)
        mflux, sflux = mean_std(self.sumflux, self.sumflux2)
        meandec = self.dec0 + mdec
        return {"meanra": (self.ra0 + mra) % 360.0, "stdevra": sra * math.cos(math.radians(meandec)),
                "meandec": meandec, "stdevdec": sdec, "meanflux": mflux, "stdevflux": sflux}
