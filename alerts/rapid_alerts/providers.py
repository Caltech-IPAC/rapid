"""
How alert data gets loaded: the provider contract and its implementations.

The normalized records (Detection, ObjectRecord, ForcedPhot, Cutouts) are
the contract between storage backends and the alert builders in produce.py.
Providers translate their native column names into these canonical
attributes exactly once; everything downstream only sees these records, so
swapping the storage backend (database / file system / sqlite) means writing
one new AlertDataProvider subclass and nothing else.

Data flow for the database backend (DB table -> record -> schema record):

    sources + filters            -> Detection    -> diaSource
        one row per difference-image detection; the triggering source is
        looked up by sid, previous detections via the merges join below
    merges_<field>               -> (sid -> aid association only)
        per-field table linking detections (sid) to persistent objects (aid)
    astroobjects_<field>         -> ObjectRecord -> diaObject
        one row per persistent object, keyed by aid
    (forced photometry)          -> ForcedPhot   -> diaForcedSource
        not yet in the DB; produces FITS files only
    <cutout_dir>/{sid}_*.fits.gz -> Cutouts      -> alert cutout* fields

The Detection dataclass attribute names ARE the sources column names, so
Detection.from_row() maps a sources row directly; the only derived keys are
band (from filters.filter) and aid (from merges). Columns not declared on
Detection (e.g. sources.id, fid, npix) are silently dropped by from_row().
"""

import dataclasses
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Normalized records (the provider contract)
# ---------------------------------------------------------------------------

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
    def from_row(cls, row, strict=False):
        """Build from a dict, ignoring keys that are not Detection fields.

        With strict=True, every Detection field must be present as a key in
        row (except aid, which is derived from the merges association, not a
        storage column). This turns a renamed or dropped storage column into
        an immediate error instead of a silently-null alert field.
        """
        names = {f.name for f in dataclasses.fields(cls)}
        if strict:
            missing = names - set(row) - {"aid"}
            if missing:
                raise KeyError(
                    f"Detection row is missing expected columns: "
                    f"{sorted(missing)} (renamed or dropped in storage?)")
        return cls(**{key: value for key, value in row.items() if key in names})


@dataclass
class ObjectRecord:
    """Persistent astronomical object (DB `astroobjects_<field>` row equivalent).

    first_mjd / last_mjd / validity_mjd are filled in by assemble_alert()
    from the source history before the diaObject record is built.
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


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------

class AlertDataProvider(ABC):
    """Data-access interface for alert assembly.

    produce.assemble_alert() only ever calls these methods, so switching
    storage backends means writing a new subclass -- the schema registry
    and assembly logic do not change.
    """

    @abstractmethod
    def get_detection(self, sid) -> Detection:
        """Return the triggering detection. Raises ValueError if not found."""

    @abstractmethod
    def get_object_for_source(self, detection) -> Optional[ObjectRecord]:
        """Return the associated persistent object, or None if unassociated."""

    @abstractmethod
    def get_prv_detections(self, detection, obj,
                           window_days=365.25) -> List[Detection]:
        """Return prior detections of obj within window_days before the
        triggering detection, oldest first, excluding the trigger itself."""

    @abstractmethod
    def get_forced_photometry(self, detection, obj) -> List[ForcedPhot]:
        """Return forced-photometry history at the object position."""

    @abstractmethod
    def get_cutouts(self, detection) -> Cutouts:
        """Return image stamps for the detection (members None if missing)."""

    def iter_detections(self, job_or_visit):
        """Yield all Detections for one processing unit (batch alert
        production, as in roman_rapid_alerts). Optional per backend."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support batch iteration")


# ---------------------------------------------------------------------------
# Database backend
# ---------------------------------------------------------------------------

class DatabaseProvider(AlertDataProvider):
    """Pulls alert inputs from the RAPID operations database (RAPIDDB)."""

    def __init__(self, db, cutout_dir=None):
        """
        Args:
            db: RAPIDDB instance (rapid.database.modules.utils.rapid_db).
            cutout_dir: optional directory containing {sid}_{diff,sci,tmpl}.fits.gz.
        """
        self.db = db
        self.cutout_dir = cutout_dir

    def _query(self, sql, params):
        """Run one query and return rows as {column_name: value} dicts."""
        cur = self.db.conn.cursor()
        try:
            cur.execute(sql, params)
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
        finally:
            cur.close()

    def get_detection(self, sid):
        # sources row -> Detection, column names matching attribute names.
        # The filters join resolves the numeric fid to the band string
        # ("F158", ...).
        rows = self._query("""
            SELECT s.*, f.filter AS filter_name
            FROM sources s
            JOIN filters f ON s.fid = f.fid
            WHERE s.sid = %s
        """, (sid,))
        if not rows:
            raise ValueError(f"Source {sid} not found")
        row = rows[0]
        row["band"] = row.get("filter_name")
        # Detection.aid stays None here; assemble_alert() fills it in after
        # get_object_for_source() resolves the association. strict=True makes
        # a renamed/dropped sources column an error, not a null alert field.
        return Detection.from_row(row, strict=True)

    def get_object_for_source(self, detection):
        # The merges/astroobjects tables are partitioned by Roman field, so
        # the detection's field number selects which pair to query.
        field = int(detection.field)
        # merges_<field> links this sid to its persistent object (aid);
        # astroobjects_<field> supplies the object summary itself.
        rows = self._query(f"""
            SELECT m.aid, a.*
            FROM merges_{field} m
            JOIN astroobjects_{field} a ON m.aid = a.aid
            WHERE m.sid = %s
        """, (detection.sid,))
        if not rows:
            return None  # unassociated detection -> alert has no diaObject
        row = rows[0]
        # Only these four columns feed diaObject today. Available but unused:
        # meanra/meandec (mean position), stdevra/stdevdec (candidates for
        # the raErr/decErr stubs), flux0/meanflux/stdevflux, hp6/hp9.
        # ObjectRecord's first/last/validity MJDs are computed later by
        # assemble_alert() from the source history, not read from the DB.
        return ObjectRecord(
            aid=int(row["aid"]),
            ra0=float(row["ra0"]),
            dec0=float(row["dec0"]),
            nsources=int(row["nsources"]),
        )

    def get_prv_detections(self, detection, obj, window_days=365.25):
        # Same sources -> Detection mapping as get_detection(), but selecting
        # the object's other detections: merges_<field> gathers every sid
        # associated with this aid, minus the trigger itself, restricted to
        # the look-back window before the triggering detection. These become
        # the alert's prvDiaSources (oldest first).
        field = int(detection.field)
        rows = self._query(f"""
            SELECT s.*, f.filter AS filter_name
            FROM sources s
            JOIN merges_{field} m ON s.sid = m.sid
            JOIN filters f ON s.fid = f.fid
            WHERE m.aid = %s AND s.sid != %s
              AND s.mjdobs >= %s
            ORDER BY s.mjdobs
        """, (obj.aid, detection.sid, detection.mjdobs - window_days))
        detections = []
        for row in rows:
            row["band"] = row.get("filter_name")
            row["aid"] = obj.aid  # known from the join; save a lookup
            detections.append(Detection.from_row(row, strict=True))
        return detections

    def get_forced_photometry(self, detection, obj):
        # Forced photometry in RAPID produces FITS files, not DB records;
        # integration with alert packets is not yet implemented.
        logger.info("Forced photometry not yet available for alert assembly")
        return []

    def get_cutouts(self, detection):
        # Cutouts are not stored in the DB; they are pre-made files named
        # <cutout_dir>/{sid}_{diff|sci|tmpl}.fits.gz. Raw gzipped-FITS bytes
        # go into the alert's cutoutDifference/Science/Template fields as-is;
        # a missing file just leaves that cutout null.
        if self.cutout_dir is None:
            return Cutouts()

        def load(suffix):
            path = os.path.join(self.cutout_dir,
                                f"{detection.sid}_{suffix}.fits.gz")
            try:
                with open(path, "rb") as f:
                    return f.read()
            except OSError:
                return None

        return Cutouts(difference=load("diff"),
                       science=load("sci"),
                       template=load("tmpl"))


# ---------------------------------------------------------------------------
# Future file-system backend
#
# A FilesystemProvider would read pipeline products directly from disk. The
# logic to port lives in alerts/roman_rapid_alerts/generate_alerts.py on the
# add-alert-generation branch:
#
#     get_detection / iter_detections  <-  parse_sextractor() +
#                                          load_psf_catalog() + match_psf()
#                                          + FITS header parsing
#     get_object_for_source            <-  load_lc_tile() + match_lc()
#     get_prv_detections               <-  nested_lc_data unpacking in
#                                          build_prv_dia_sources()
#     get_cutouts                      <-  load_image() + extract_stamp()
#
# Note the flux calibration difference: that script converts SExtractor and
# light-curve fluxes to nJy via FILTER_ZP_EFF, while the database flow
# currently passes instrumental fluxfit through. Reconcile when porting.
# ---------------------------------------------------------------------------
