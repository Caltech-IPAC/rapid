"""
Provider backed by the RAPID operations database (RAPIDDB).

Ports the SQL that previously lived inline in produce_alert.py. All
column-name translation to the canonical records.py attributes happens
here and nowhere else.
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
        cur = self.db.conn.cursor()
        try:
            cur.execute(sql, params)
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
        finally:
            cur.close()

    def get_detection(self, sid):
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
        return Detection.from_row(row)

    def get_object_for_source(self, detection):
        field = int(detection.field)
        rows = self._query(f"""
            SELECT m.aid, a.*
            FROM merges_{field} m
            JOIN astroobjects_{field} a ON m.aid = a.aid
            WHERE m.sid = %s
        """, (detection.sid,))
        if not rows:
            return None
        row = rows[0]
        return ObjectRecord(
            aid=int(row["aid"]),
            ra0=float(row["ra0"]),
            dec0=float(row["dec0"]),
            nsources=int(row["nsources"]),
        )

    def get_prv_detections(self, detection, obj, window_days=365.25):
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
            row["aid"] = obj.aid
            detections.append(Detection.from_row(row))
        return detections

    def get_forced_photometry(self, detection, obj):
        # Forced photometry in RAPID produces FITS files, not DB records;
        # integration with alert packets is not yet implemented.
        logger.info("Forced photometry not yet available for alert assembly")
        return []

    def get_cutouts(self, detection):
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
