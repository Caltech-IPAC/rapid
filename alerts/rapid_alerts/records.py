"""
Normalized in-memory records passed between providers and builders.

Providers (database, file system, ...) translate their native column names
into these canonical attributes exactly once; the builders in build.py and
the getters in fields.py only ever see these records, so they are identical
for every backend. Attribute names follow the RAPID database columns.
"""

import dataclasses
from dataclasses import dataclass
from typing import Optional


@dataclass
class Detection:
    """One difference-image source detection (DB `sources` row equivalent)."""
    sid: int
    expid: int
    sca: int
    mjdobs: float
    ra: float
    dec: float
    xfit: float
    yfit: float
    band: Optional[str] = None
    aid: Optional[int] = None        # associated object; set once known
    xerr: Optional[float] = None
    yerr: Optional[float] = None
    fluxfit: Optional[float] = None
    fluxerr: Optional[float] = None
    flags: int = 0
    field: int = 0
    hp6: int = 0
    hp9: int = 0
    pid: int = 0
    isdiffpos: bool = True
    qfit: Optional[float] = None
    cfit: Optional[float] = None
    redchi: Optional[float] = None
    npixfit: Optional[int] = None
    sharpness: Optional[float] = None
    roundness1: Optional[float] = None
    roundness2: Optional[float] = None
    peak: Optional[float] = None

    @property
    def snr(self):
        if self.fluxfit is not None and self.fluxerr:
            return self.fluxfit / self.fluxerr
        return None

    @classmethod
    def from_row(cls, row):
        """Build from a dict, ignoring keys that are not Detection fields."""
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in row.items() if k in names})


@dataclass
class ObjectRecord:
    """Persistent astronomical object (DB `astroobjects_<field>` row equivalent).

    first_mjd / last_mjd / validity_mjd are filled in by the assembler from
    the source history before the diaObject record is built.
    """
    aid: int
    ra0: float
    dec0: float
    nsources: int
    first_mjd: Optional[float] = None
    last_mjd: Optional[float] = None
    validity_mjd: float = 0.0


@dataclass
class ForcedPhot:
    """One forced-photometry measurement at an object position."""
    forced_id: int
    aid: int
    expid: int
    sca: int
    ra: float
    dec: float
    mjdobs: float
    time_processed: float
    band: Optional[str] = None
    flux: Optional[float] = None
    fluxerr: Optional[float] = None


@dataclass
class Cutouts:
    """Raw FITS bytes for the three image stamps (any may be missing)."""
    difference: Optional[bytes] = None
    science: Optional[bytes] = None
    template: Optional[bytes] = None
