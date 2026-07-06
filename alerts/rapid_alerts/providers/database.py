"""
Provider backed by the RAPID operations database (RAPIDDB).

Ports the SQL that previously lived inline in produce_alert.py. All
column-name translation to the canonical records.py attributes happens
here and nowhere else.

Data flow (DB table -> normalized record -> schema record):

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

import logging
import os

from .base import AlertDataProvider
from ..records import Detection, ObjectRecord, Cutouts

logger = logging.getLogger(__name__)


class DatabaseProvider(AlertDataProvider):

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
        # sources row -> Detection, column names matching attribute names
        # (sid, expid, sca, mjdobs, ra, dec, xfit, yfit, xerr, yerr, fluxfit,
        # fluxerr, flags, field, hp6, hp9, pid, isdiffpos, qfit, cfit, redchi,
        # npixfit, sharpness, roundness1, roundness2, peak). The filters join
        # resolves the numeric fid to the band string ("F158", ...).
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
        # Detection.aid stays None here; assemble.py fills it in after
        # get_object_for_source() resolves the association.
        return Detection.from_row(row)

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
        # assemble.py from the source history, not read from the DB.
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
            detections.append(Detection.from_row(row))
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
