"""A stand-in for kete: the names ``rapidpipe.science.alerts.kona`` calls, with no orbits.

The real kete fetches the MPC orbit table and SPICE kernels and integrates
n-body; this fake carries a day's "states" as ``(designation, ra, dec)``
predictions written as JSON where the states parquet would be, so a test
controls what is in the cone. The geometry kona does itself (km to AU, the
slide to the mid-time, the heliocentric re-centre) runs on this fake's
small vector type and is checked by the tests of ``observer_state``.

Install with ``monkeypatch.setattr(kona, "_kete", lambda: fakekete)``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone

AU_KM = 149597870.7


class constants:  # noqa: N801 - kete's name
    AU_KM = AU_KM


class Frames:
    Ecliptic = "Ecliptic"
    Equatorial = "Equatorial"


@dataclass
class Time:
    jd: float

    @classmethod
    def from_iso(cls, s: str) -> "Time":
        iso = s[:-1] if s.endswith("Z") else s
        dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
        epoch = datetime(1858, 11, 17, tzinfo=timezone.utc)
        return cls(2400000.5 + (dt - epoch).total_seconds() / 86400.0)

    @classmethod
    def from_mjd(cls, mjd: float, scaling: str = "tdb") -> "Time":
        return cls(2400000.5 + float(mjd))


class Vector:
    """A 3-vector that can also stand for a sky direction (``ra``, ``dec``)."""

    def __init__(self, xyz, frame=None, ra=None, dec=None):
        self.xyz = tuple(float(v) for v in xyz)
        self.frame = frame
        self._ra, self._dec = ra, dec

    @classmethod
    def from_ra_dec(cls, ra: float, dec: float, r=None) -> "Vector":
        return cls((0.0, 0.0, 0.0), ra=ra, dec=dec)

    def __sub__(self, other: "Vector") -> "Vector":
        return Vector([a - b for a, b in zip(self.xyz, other.xyz)], self.frame, self._ra, self._dec)

    @property
    def as_equatorial(self) -> "Vector":
        return self

    @property
    def ra(self) -> float:
        return self._ra

    @property
    def dec(self) -> float:
        return self._dec


@dataclass
class State:
    desig: str
    jd: object
    pos: Vector
    vel: Vector
    frame: object = None
    center_id: int = 10

    def change_center(self, center_id: int) -> "State":
        return State(self.desig, self.jd, self.pos, self.vel, self.frame, center_id)


class _Fov:
    def __init__(self, pointing: Vector, angle: float, observer: State) -> None:
        self.pointing, self.angle, self.observer = pointing, angle, observer


class fov:  # noqa: N801 - kete's module name
    ConeFOV = _Fov


class _Seen:
    def __init__(self, states, fov) -> None:
        self.states, self.fov = states, fov


def _sep_deg(ra1, dec1, ra2, dec2) -> float:
    a, b, c, d = map(math.radians, (ra1, dec1, ra2, dec2))
    return math.degrees(math.acos(max(-1.0, min(1.0, math.sin(b) * math.sin(d)
                                                 + math.cos(b) * math.cos(d) * math.cos(a - c)))))


def propagate_n_body(states, jd, **kwargs):
    return list(states)


def fov_state_check(obj_state, fovs, dt_limit=3.0, include_asteroids=False):
    """Each fov's states within its cone of the pointing; the state's ``pos`` carries the sky direction."""
    return [_Seen([s for s in obj_state
                   if _sep_deg(s.pos.ra, s.pos.dec, f.pointing.ra, f.pointing.dec) <= f.angle], f)
            for f in fovs]


class flux:  # noqa: N801 - kete's module name
    @staticmethod
    def hg_apparent_mag(sun2obj, sun2obs, h_mag, g_param):
        return h_mag + 5.0  # a stand-in: H plus a fixed distance term


class SimultaneousStates:
    """The day's states, kept as JSON ``[[desig, ra, dec], ...]`` where kete keeps parquet."""

    def __init__(self, states, fov=None) -> None:
        self.states = list(states)

    def save_parquet(self, filename: str) -> None:
        with open(filename, "w") as out:
            json.dump([[s.desig, s.pos.ra, s.pos.dec] for s in self.states], out)

    @classmethod
    def load_parquet(cls, filename: str) -> "SimultaneousStates":
        with open(filename) as stream:
            rows = json.load(stream)
        return cls([State(d, None, Vector((1.0, 0.0, 0.0), ra=ra, dec=dec), Vector((0.0, 0.0, 0.0)))
                    for d, ra, dec in rows])


class horizons:  # noqa: N801 - kete's module name
    @staticmethod
    def fetch_known_orbit_data():
        raise RuntimeError("the fake kete has no network: give ingest_day a table through the test")


#: The epoch the fake gives every orbit, inside kona.KERNEL_JD_RANGE; a table may carry its
#: own ``jd`` list to put an orbit outside it.
EPOCH_JD = 2461200.5


def _table_to_states(table):
    jds = getattr(table, "jd", None) or [EPOCH_JD] * len(table.desig)
    return [State(d, jd, Vector((1.0, 0.0, 0.0), ra=ra, dec=dec), Vector((0.0, 0.0, 0.0)))
            for d, jd, ra, dec in zip(table.desig, jds, table.ra, table.dec)]


class conversion:  # noqa: N801 - kete's module name, where 2.x and 3.x keep table_to_states
    table_to_states = staticmethod(_table_to_states)


class mpc:  # noqa: N801 - kete's module name; 1.x keeps table_to_states here
    table_to_states = staticmethod(_table_to_states)
