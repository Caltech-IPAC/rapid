# ported-from: modules/solarsystem/rapid_kona.py @ 0e7113b7
"""Known Object Name Association (KONA): known solar-system objects predicted on a chip.

`dev`'s ``modules/solarsystem/rapid_kona.py`` (Joseph Masiero, from Manaswi
Kondapally's prototype; Emily Everetts' fixes of 2026-09-29, 0e7113b7)
runs kete over an L2 ASDF file: the MPC orbit catalog propagated by n-body
to the exposure's mid-time, the observatory placed from the file's
``meta.ephemeris`` (barycentric position and velocity, re-centred on the
Sun because kete's fields of view want a heliocentric observer), a cone
field of view, and the objects in it with a predicted V magnitude from the
IAU HG model. The port splits that into the two steps the rebuild runs
(Emily, 2026-11-12):

- :func:`ingest_day`, once a day, anywhere with network: the orbits fetched
  from the MPC and propagated to the day's noon, saved as kete's states
  parquet with the H and G parameters beside it and a record, under
  ``<catalogs prefix>/ss/<yyyymmdd>/``. The ``ss`` catalog is registered
  once per dataset at the ``ss/`` root (an import job with the ``ss``
  scope); every later day is found by date.
- :class:`SsReader` and :func:`predict`, in the packet job: the newest day
  at or before the trigger's observation, the states moved the remaining
  hours to the mid-time, the cone check on the trigger chip. The observer
  comes from the L2 file's primary header, where the intake writes the
  whole ASDF ``meta`` tree as ``HIERARCH`` cards (:func:`l2_primary_header`
  reads only the header, so a gzipped L2 in S3 costs its first blocks).

Departures from `dev`, on the packet page: the cone is centred on the
difference image's footprint centre (`dev`: ``meta.wcsinfo.ra_ref``, the
SCA's reference point, which the rebuild does not keep) with `dev`'s 0.15
degree radius; the H and G table is read from the day's copy, never
fetched in the packet job; nothing is written back to the ASDF (`dev`
disabled that too). kete is imported on first use, so the alerts package
loads without it.

TODO (Emily, 2026-11-12): return to IPAC's kete 1.x line (Caltech-IPAC/kete,
the line `dev` runs and the one carrying Joe's MPC-epoch TT fix, 1.1.4)
once it is installable in the pipeline image: as of 2026-11-12 its wheels
stop at CPython 3.13 and its ``requires-python`` is ``<3.14`` while the
image is on 3.14, so ``requirements.txt`` pins kete 3.3.0 (stable-ABI
wheels). The port runs on either line: :func:`_table_to_states` is the one
call that moved (``mpc.table_to_states`` became ``conversion.table_to_states``
in 2.0.0); every other call has the same signature on both. A day's
states parquet is read by the kete that wrote it, so switching lines means
ingesting from that day on, not re-reading old days.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import pyarrow as pa
import pyarrow.parquet as pq
from astropy.io import fits

from rapidpipe.science.catalogs.store import Store

#: IAU HG model default slope parameter, used when the orbit table has no G (`dev`).
DEFAULT_G_PARAM = 0.15

#: `dev`'s cone radius about the chip, degrees: one SCA is ~0.125 degrees across.
CONE_RADIUS_DEG = 0.15

#: The Julian dates kete's core planetary kernels (de440s) cover, 1849-12-26 to 2150-01-22. An
#: orbit whose epoch falls outside (a few hundred ancient comets in the MPC table, "-146 P1"
#: and the like) cannot be propagated ("SPK files are missing information ... at JD ...",
#: observed on kete 3.3.0 at JD 2390520.5) and is left out of a day's states, counted in
#: its record.
KERNEL_JD_RANGE = (2396758.5, 2506352.5)

#: A day's files under ``ss/<yyyymmdd>/``: kete's propagated states, the H and G parameters by
#: designation, and the record.
STATES_NAME = "states.parquet"
HG_NAME = "hg.parquet"
SS_INFO = "ss.json"
SS_RELEASE = "rolling"      # the ``catalogs`` row's version: one registration, every day under it

#: The ``HIERARCH`` cards the intake writes from the ASDF ``meta`` tree, as this module reads them.
MID_TIME_CARD = "meta exposure mid_time"
EPHEMERIS_CARDS = {"time": "meta ephemeris time", "frame": "meta ephemeris ephemeris_reference_frame",
                   "x": "meta ephemeris spatial_x", "y": "meta ephemeris spatial_y",
                   "z": "meta ephemeris spatial_z", "vx": "meta ephemeris velocity_x",
                   "vy": "meta ephemeris velocity_y", "vz": "meta ephemeris velocity_z"}

_DAY = re.compile(r"^\d{8}$")


def _kete():
    """kete, imported on first use."""
    import kete
    return kete


def _table_to_states(k, orbits):
    """kete's orbit table to states: ``conversion.table_to_states`` (2.x, 3.x) or ``mpc.table_to_states`` (1.x)."""
    conversion = getattr(k, "conversion", None)
    if conversion is not None and hasattr(conversion, "table_to_states"):
        return conversion.table_to_states(orbits)
    return k.mpc.table_to_states(orbits)


# ---------------------------------------------------------------------------
# The observer: the L2 file's ephemeris
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Ephemeris:
    """The observatory's state as the L2 file carries it, and the exposure mid-time.

    ``position`` in km and ``velocity`` in km/s at ``time`` (MJD, UTC) in
    ``frame`` (``Ecliptic`` or anything else, read as equatorial, as
    `dev`); ``mid_time`` the exposure mid-point as an ISO string.
    """

    mid_time: str
    time: float
    frame: str
    position: tuple[float, float, float]
    velocity: tuple[float, float, float]


def ephemeris_from_header(header: fits.Header) -> Ephemeris:
    """The :class:`Ephemeris` of an L2 primary header; ``ValueError`` names a missing card."""
    missing = [c for c in (MID_TIME_CARD, *EPHEMERIS_CARDS.values()) if c not in header]
    if missing:
        raise ValueError(f"the L2 header has no {', '.join(missing)}: no observatory ephemeris")
    get = {k: header[c] for k, c in EPHEMERIS_CARDS.items()}
    return Ephemeris(mid_time=str(header[MID_TIME_CARD]), time=float(get["time"]), frame=str(get["frame"]),
                     position=(float(get["x"]), float(get["y"]), float(get["z"])),
                     velocity=(float(get["vx"]), float(get["vy"]), float(get["vz"])))


def _open_url(url: str):
    """A binary stream over a ``file://`` or ``s3://`` URL, read from its start."""
    parsed = urlparse(url)
    if parsed.scheme == "file":
        return open(unquote(parsed.path), "rb")
    if parsed.scheme == "s3" and parsed.netloc:
        from rapidpipe.products import storage
        return storage.s3_client().get_object(Bucket=parsed.netloc, Key=parsed.path.lstrip("/"))["Body"]
    raise ValueError(f"{url!r} is neither a file:// nor an s3://bucket/ URL")


def l2_primary_header(url: str) -> fits.Header:
    """The primary header of the FITS file ``url`` names, reading only the blocks that hold it.

    A ``.gz`` is decompressed as it streams, so a gzipped L2 in S3 is read
    up to its END card and no further.
    """
    raw = _open_url(url)
    stream = gzip.GzipFile(fileobj=raw) if url.endswith(".gz") else raw
    blocks = []
    try:
        while True:
            block = stream.read(2880)
            if len(block) < 2880:
                raise ValueError(f"{url}: no END card in the primary header")
            blocks.append(block)
            if any(block[i:i + 80].rstrip() == b"END" for i in range(0, 2880, 80)):
                break
    finally:
        stream.close()
        raw.close()
    return fits.Header.fromstring(b"".join(blocks).decode("ascii"))


def observer_state(eph: Ephemeris) -> tuple[Any, Any]:
    """`dev`'s observer: the kete ``State`` of the observatory at the exposure mid-time, heliocentric, and the time.

    The barycentric km and km/s become AU and AU/day, the position slides
    along the velocity from the ephemeris time to the mid-time (the
    instant the objects are propagated to), the state is built on the
    barycentre (NAIF 0) and re-centred on the Sun (NAIF 10).
    """
    k = _kete()
    au = k.constants.AU_KM
    mid = eph.mid_time if eph.mid_time.endswith("Z") else eph.mid_time + "Z"   # trailing Z: UTC
    image_time = k.Time.from_iso(mid)
    eph_time = k.Time.from_mjd(eph.time, scaling="utc")
    dt_days = image_time.jd - eph_time.jd
    vel = [v / au * 86400.0 for v in eph.velocity]
    pos = [p / au + v * dt_days for p, v in zip(eph.position, vel)]
    frame = k.Frames.Ecliptic if eph.frame == "Ecliptic" else k.Frames.Equatorial
    state = k.State("Roman", image_time, k.Vector(pos, frame=frame), k.Vector(vel, frame=frame), center_id=0)
    return state.change_center(10), image_time


# ---------------------------------------------------------------------------
# A day's copy: the propagated orbits
# ---------------------------------------------------------------------------

def day_of(mjd: float) -> str:
    """The UT date of an MJD as ``yyyymmdd``."""
    return (datetime(1858, 11, 17, tzinfo=timezone.utc) + timedelta(days=float(mjd))).strftime("%Y%m%d")


def noon_jd(day: str) -> float:
    """The Julian date of ``yyyymmdd`` at 12:00 UTC, the epoch a day's states are propagated to."""
    date = datetime.strptime(day, "%Y%m%d").replace(tzinfo=timezone.utc)
    return 2400000.5 + (date - datetime(1858, 11, 17, tzinfo=timezone.utc)).days + 0.5


def ingest_day(dest: str, day: str, cache_dir: str | None = None) -> dict:
    """Write ``<dest>/<day>/``: the MPC orbits propagated to the day's noon, their H and G, the record.

    `dev`'s ``kona(mpc_save=..., median_jd=...)``. ``dest`` is the ``ss``
    root (``s3://bucket/ss`` or a directory). A day already written is
    left as it is and its record returned: the catalogs bucket denies
    deletes and seals releases create-once, and a packet's read of a day
    must stay reconstructible. ``cache_dir`` points kete's download cache
    (SPICE kernels, the orbit table) somewhere other than ``~/.kete``.
    Returns the record.
    """
    if not _DAY.match(day):
        raise ValueError(f"day {day!r} is not yyyymmdd")
    store = Store(_root(dest))
    if store.exists(f"{day}/{SS_INFO}"):
        return json.loads(store.read_text(f"{day}/{SS_INFO}"))
    if cache_dir:                    # before the import: kete fetches its core kernels at import
        os.makedirs(cache_dir, exist_ok=True)
        os.environ["KETE_CACHE_DIR"] = cache_dir
    k = _kete()
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    orbits = k.horizons.fetch_known_orbit_data()
    states = _table_to_states(k, orbits)
    lo, hi = KERNEL_JD_RANGE
    placeable = [s for s in states if lo <= float(s.jd) <= hi]
    epoch = noon_jd(day)
    moved = k.propagate_n_body(placeable, epoch)
    with tempfile.TemporaryDirectory(prefix="kona-") as tmp:
        local = Path(tmp) / STATES_NAME
        k.SimultaneousStates(moved).save_parquet(str(local))
        # Conditional creates: the catalogs bucket is sealed create-once (its policy denies an
        # unconditional put), and a day is written once by design.
        store.put_bytes(f"{day}/{STATES_NAME}", local.read_bytes(), create_once=True)
        hg = pa.table({"desig": pa.array([str(d) for d in orbits.desig]),
                       "h": pa.array([float(h) for h in orbits.H], pa.float64()),
                       "g": pa.array([float(g) for g in orbits.G], pa.float64())})
        pq.write_table(hg, Path(tmp) / HG_NAME)
        store.put_bytes(f"{day}/{HG_NAME}", (Path(tmp) / HG_NAME).read_bytes(), create_once=True)
    record = {"release": day, "epoch_jd": epoch, "n_objects": len(moved),
              "n_dropped_epoch": len(states) - len(placeable), "fetched_at": fetched_at,
              "tool": "rapidpipe.science.alerts.kona", "complete": True}
    store.write_text(f"{day}/{SS_INFO}", json.dumps(record, indent=1), create_once=True)
    return record


def _root(location: str) -> str:
    """The store root a location names (``file://`` as its directory)."""
    if location.startswith("file://"):
        return unquote(urlparse(location).path)
    return location


def days_of(store: Store) -> list[str]:
    """The days under the root with a record, oldest first."""
    days = {p.split("/", 1)[0] for p in store.list_files("")
            if p.count("/") == 1 and p.endswith("/" + SS_INFO) and _DAY.match(p.split("/", 1)[0])}
    return sorted(days)


def newest_day(days: list[str], mjd: float) -> str | None:
    """The newest of ``days`` at or before the UT date of ``mjd``, or None."""
    wanted = day_of(mjd)
    before = [d for d in days if d <= wanted]
    return before[-1] if before else None


# ---------------------------------------------------------------------------
# The per-exposure step
# ---------------------------------------------------------------------------

def predict(states: list, hg: dict[str, tuple[float, float]], eph: Ephemeris, ra: float, dec: float,
            radius_deg: float = CONE_RADIUS_DEG) -> dict[str, tuple[float, float, float | None]]:
    """`dev`'s per-image loop: ``{designation: (ra, dec, vmag)}`` of the objects in the cone.

    ``states`` are a day's propagated states, moved by n-body to the
    exposure mid-time; the cone of ``radius_deg`` about ``(ra, dec)`` is
    checked from the observer :func:`observer_state` places. ``vmag`` is
    the HG model's apparent V, None without an H or beyond the model's
    phase range.
    """
    k = _kete()
    observer, image_time = observer_state(eph)
    fov = k.fov.ConeFOV(k.Vector.from_ra_dec(float(ra), float(dec)), float(radius_deg), observer)
    current = k.propagate_n_body(states, image_time.jd)
    visible = k.fov_state_check(current, [fov])
    found: dict[str, tuple[float, float, float | None]] = {}
    for seen in visible:
        for state in seen.states:
            vec = (state.pos - seen.fov.observer.pos).as_equatorial
            h_mag, g_param = hg.get(str(state.desig), (math.nan, math.nan))
            if math.isnan(g_param):
                g_param = DEFAULT_G_PARAM
            vmag = None
            if not math.isnan(h_mag):
                vmag = k.flux.hg_apparent_mag(sun2obj=state.pos, sun2obs=seen.fov.observer.pos,
                                              h_mag=h_mag, g_param=g_param)
                if not math.isfinite(vmag):
                    vmag = None
            found[str(state.desig)] = (float(vec.ra), float(vec.dec), vmag)
    return found


class SsReader:
    """The ``ss`` catalog at its root: the days it holds and one day's states on request.

    ``release`` is :data:`SS_RELEASE`; the day read is the packet
    manifest's record. A root with no day raises at open, so the match
    is recorded as not run.
    """

    def __init__(self, location: str) -> None:
        self.root = _root(location)
        self.store = Store(self.root)
        self.days = days_of(self.store)
        if not self.days:
            raise ValueError(f"{location}: no day with {SS_INFO} under the ss root")
        self.release = SS_RELEASE
        self.n_reads = 0

    def day_for(self, mjd: float) -> str:
        """The day to read for an observation at ``mjd``; raises when every day is later."""
        day = newest_day(self.days, mjd)
        if day is None:
            raise ValueError(f"no ss day at or before {day_of(mjd)} (oldest is {self.days[0]})")
        return day

    def load(self, day: str) -> tuple[list, dict[str, tuple[float, float]], dict]:
        """A day's states, its H and G by designation, and its record."""
        k = _kete()
        record = json.loads(self.store.read_text(f"{day}/{SS_INFO}"))
        with tempfile.TemporaryDirectory(prefix="kona-") as tmp:
            local = Path(tmp) / STATES_NAME
            local.write_bytes(self._read_bytes(f"{day}/{STATES_NAME}"))
            states = k.SimultaneousStates.load_parquet(str(local)).states
            hg_local = Path(tmp) / HG_NAME
            hg_local.write_bytes(self._read_bytes(f"{day}/{HG_NAME}"))
            table = pq.read_table(hg_local).to_pydict()
        hg = {d: (h, g) for d, h, g in zip(table["desig"], table["h"], table["g"])}
        self.n_reads += 1
        return states, hg, record

    def _read_bytes(self, rel: str) -> bytes:
        with self.store.fs.open_input_file(self.store.fs_path(rel)) as stream:
            return stream.read()


def read_days(location: str) -> list[str]:
    """The days an ``ss`` root holds (the import stage's check of a copy)."""
    return days_of(Store(_root(location)))


__all__ = ["CONE_RADIUS_DEG", "DEFAULT_G_PARAM", "Ephemeris", "HG_NAME", "SS_INFO", "SS_RELEASE",
           "STATES_NAME", "SsReader", "day_of", "days_of", "ephemeris_from_header", "ingest_day",
           "l2_primary_header", "newest_day", "noon_jd", "observer_state", "predict", "read_days"]
