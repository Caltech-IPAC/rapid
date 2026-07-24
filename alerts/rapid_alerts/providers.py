"""How alert data gets loaded: one provider plus per-source reader functions.

The normalized records (:class:`Source`, :class:`ObjectRecord`,
:class:`ForcedPhot`, :class:`Cutouts`) are the contract between the raw data
and the alert builders in :mod:`rapid_alerts.produce`. The provider translates
native column names into these canonical attributes exactly once; everything
downstream only sees the records.

There is a single provider, :class:`AlertDataProvider`. It is not "database
only": the RAPID database is effectively a fast index over the pipeline job
directories, so the provider reads tabular data (detections, object
associations, history) from the DB and reads pixel/auxiliary products (cutout
FITS, and an auxiliary source catalog to cross-reference) from the job
directory those DB rows point at. Each distinct data source is a small
module-level reader function (:func:`load_fits_image`, :func:`extract_stamp`,
:func:`parse_catalog`, ...); adding a source means adding a function and one
call site, not a new class or subclass. A future no-DB flow would be an
alternate constructor on this same provider, not a separate class.

The :class:`Source` dataclass attribute names ARE the ``sources`` column
names, so :meth:`Source.from_row` maps a sources row directly; the only
derived keys are ``band`` (from ``filters.filter``) and ``aid`` (from
merges). Columns not declared on ``Source`` (e.g. ``sources.id``, ``fid``,
``npix``) are silently dropped by ``from_row``.

Notes
-----
Data flow (DB table / job-dir file -> record -> schema record)::

    sources + filters     -> Source        -> diaSource
        one row per difference-image detection; the triggering source is
        looked up by sid, previous detections via the merges join below
    merges_<field>        -> (sid -> aid association only)
        per-field table linking detections (sid) to persistent objects (aid)
    astroobjects_<field>  -> ObjectRecord   -> diaObject
        one row per persistent object, keyed by aid
    (forced photometry)   -> ForcedPhot     -> diaForcedSource
        not yet in the DB; produces FITS files only
    diffimages.filename   -> full chip images -> Cutouts
        diffimages.filename names one difference image in a pipeline job
        directory (s3://.../<date>/jid<N>/); the science and template images
        used for cutouts are the co-gridded products in that same directory
        (see CUTOUT_FILES / DIFF_FLAVORS). Stamps are cut around each source
        position by extract_stamp() and carry the parent image's WCS; the
        three images are staged and loaded once per chip and reused for
        every source.

Examples
--------
>>> from rapid_alerts.providers import AlertDataProvider
>>> provider = AlertDataProvider(db, diff_flavor="sfft")
>>> source = provider.get_detection(sid)
>>> cutouts = provider.get_cutouts(source)

Attributes
----------
PRV_WINDOW_DAYS : float
    Default look-back window (days) for previous detections of an object.
STAMP_HALF_WIDTH : int
    Half the cutout stamp side; stamps are ``2 * STAMP_HALF_WIDTH + 1`` px.
STAMP_FILL_VALUE : float
    Value for stamp pixels that fall outside the chip (edge clips).
DIFF_FLAVORS : dict
    Differencing algorithm ("sfft" / "zogy") -> difference-image basename.
CUTOUT_FILES : dict
    Cutout kind ("sci" / "ref") -> co-gridded product basename in the job dir.
WCS_CARD_PREFIXES : tuple of str
    FITS header-card prefixes copied from the parent image into each cutout.
CATALOG_FILE : str or None
    Basename of the auxiliary catalog to cross-reference against, or None
    while cross-referencing is not wired up.
"""

import dataclasses
import logging
import os
import tempfile
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import fitsio
import numpy as np

logger = logging.getLogger(__name__)

PRV_WINDOW_DAYS = 365.25  # default look-back window for previous detections


# ---------------------------------------------------------------------------
# Normalized records (the provider contract)
# ---------------------------------------------------------------------------

@dataclass
class Source:
    """One difference-image source detection (DB `sources` row equivalent)."""
    sid: int
    expid: int
    sca: int
    mjdobs: float
    ra: float
    dec: float
    xfit: float
    yfit: float
    band: str
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
    exptime: Optional[float] = None

    @property
    def snr(self):
        if self.fluxfit is not None and self.fluxerr:
            return self.fluxfit / self.fluxerr
        return None

    @classmethod
    def from_row(cls, row, strict=False):
        """Build from a dict, ignoring keys that are not Source fields.

        With strict=True, every Source field must be present as a key in
        row (except aid, which is derived from the merges association, not a
        storage column). This turns a renamed or dropped storage column into
        an immediate error instead of a silently-null alert field.
        """
        names = {f.name for f in dataclasses.fields(cls)}
        if strict:
            missing = names - set(row) - {"aid"}
            if missing:
                raise KeyError(
                    f"Source row is missing expected columns: "
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
    stdevra: float
    stdevdec: float
    nsources: int
    first_mjd: Optional[float] = None
    last_mjd: Optional[float] = None
    validity_mjd: float = 0.0

    # fields assemble_alert() fills in later; never storage columns
    FILLED_LATER = frozenset({"first_mjd", "last_mjd", "validity_mjd"})

    @classmethod
    def from_row(cls, row, strict=False):
        """Build from a dict, ignoring keys that are not ObjectRecord
        fields (a SELECT a.* row carries meanra, flux0, hp6, ...).

        With strict=True, every field except the FILLED_LATER ones must be
        present in row. This turns a renamed/dropped storage column -- or
        a column missing from a set-based prefetch SELECT list, the easy
        one to forget -- into an immediate error instead of a silently
        broken alert field. Used by both the batch and single-alert flows
        of get_object_for_source(), so a new column is added in exactly
        one place (here) plus the prefetch SELECT list.
        """
        names = {f.name for f in dataclasses.fields(cls)}
        if strict:
            missing = names - set(row) - cls.FILLED_LATER
            if missing:
                raise KeyError(
                    f"ObjectRecord row is missing expected columns: "
                    f"{sorted(missing)} (renamed or dropped in storage, or "
                    f"absent from a prefetch SELECT list?)")
        return cls(**{key: value for key, value in row.items()
                      if key in names})


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
    """Raw FITS bytes for the three image stamps (any may be missing).

    Each non-None member is a complete little FITS file: parse with
    fits.open(io.BytesIO(cutouts.difference)) or write it straight to
    disk for DS9."""
    difference: Optional[bytes] = None
    science: Optional[bytes] = None
    template: Optional[bytes] = None

    def __repr__(self):
        # the default dataclass repr would dump ~80 kB of raw bytes per
        # stamp into the terminal; summarize instead
        parts = (f"{f.name}=<FITS clip, {len(v)} bytes>" if v is not None
                 else f"{f.name}=None"
                 for f in dataclasses.fields(self)
                 for v in [getattr(self, f.name)])
        return f"Cutouts({', '.join(parts)})"


# ---------------------------------------------------------------------------
# Cutout stamps (backend-independent helpers: any provider that can get its
# hands on full images uses these to fill Cutouts)
# ---------------------------------------------------------------------------

STAMP_HALF_WIDTH = 64  # stamps are 2*64+1 = 129x129 pixels

# Value for stamp pixels that fall outside the chip (edge clips).
# TODO: when cutouts gain a mask/DQ HDU, flag these filled pixels there so
# consumers don't have to treat the fill value itself as meaningful.
STAMP_FILL_VALUE = 0.0

# The three cutout images all come from the difference image's pipeline job
# directory and share one pixel grid (the template is the mosaic already
# resampled and gain-matched onto the science grid), so a source's fitted
# pixel position is valid in all of them. The job runs both differencing
# algorithms; which one feeds cutoutDifference is the provider's
# diff_flavor argument.
DIFF_FLAVORS = {
    "sfft": "sfftdiffimage_masked.fits",
    "zogy": "zogy_diffimage_masked.fits",
}
CUTOUT_FILES = {
    "sci": "bkg_subbed_science_image.fits",
    "ref": "awaicgen_output_mosaic_image_resampled_gainmatched.fits",
}

# Header cards copied from the parent image into each cutout, so every clip
# is a self-describing FITS image with a valid WCS. CRPIX1/2 are shifted by
# the stamp's corner offset; everything else (including the PV/SIP
# distortion polynomials, which are defined relative to CRPIX) copies
# unchanged.
WCS_CARD_PREFIXES = (
    "CTYPE", "CUNIT", "CRVAL", "CRPIX", "CDELT", "CD1_", "CD2_",
    "PC1_", "PC2_", "PV1_", "PV2_",
    "A_", "B_", "AP_", "BP_",           # SIP polynomials and their ORDERs
    "RADESYS", "EQUINOX", "LONPOLE", "LATPOLE",
    "MJD-OBS", "BUNIT", "FILTER",
)


def load_fits_image(path):
    """Return (pixels, header) of a FITS image: the first HDU that has
    pixel data (primary for the pipeline products; Roman L2 cal files keep
    the pixels in a SCI extension). (None, None) if the file is missing,
    unreadable, or has no image HDU -- the alert then carries a null
    cutout."""
    if path is None:
        return None, None
    try:
        with fitsio.FITS(path) as hdus:
            for hdu in hdus:
                if hdu.get_exttype() == "IMAGE_HDU" and hdu.has_data():
                    return hdu.read(), hdu.read_header()
        logger.warning("No image HDU in %s", path)
    except Exception:
        logger.warning("Could not load image %s", path, exc_info=True)
    return None, None


def extract_stamp(image_data, x, y, header=None, half_width=STAMP_HALF_WIDTH):
    """Cut a square stamp centered on pixel (x, y) out of a full image and
    return it as the bytes of a small single-HDU FITS file (which is what
    the alert cutout params carry). With the parent image's header, the
    stamp carries the parent WCS with CRPIX shifted to the stamp frame
    (also valid for edge stamps: the shift is pure translation, on- or
    off-chip). Stamp pixels beyond the chip edge are set to
    STAMP_FILL_VALUE.

    Returns None if there is no image, or if the stamp would not overlap
    the chip at all.
    """
    #TODO: should we include edge stamps or not? What strategy?
    if image_data is None or x is None or y is None:
        return None
    # FITS pixel coordinates are 1-based; numpy indexing is 0-based
    col = int(round(x)) - 1
    row = int(round(y)) - 1
    nrows, ncols = image_data.shape
    top, bottom = row - half_width, row + half_width + 1
    left, right = col - half_width, col + half_width + 1
    # the part of the stamp window that actually lies on the chip
    ontop, onbottom = max(top, 0), min(bottom, nrows)
    onleft, onright = max(left, 0), min(right, ncols)
    if ontop >= onbottom or onleft >= onright:
        return None
    side = 2 * half_width + 1
    stamp = np.full((side, side), STAMP_FILL_VALUE, dtype=np.float32)
    stamp[ontop - top:onbottom - top, onleft - left:onright - left] = \
        image_data[ontop:onbottom, onleft:onright]

    cards = []
    if header is not None:
        for rec in header.records():
            if not str(rec["name"]).startswith(WCS_CARD_PREFIXES):
                continue
            card = {"name": rec["name"], "value": rec["value"],
                    "comment": rec.get("comment", "")}
            # 1-based parent pixel p lands at p - left (p - top) in the clip
            if rec["name"] == "CRPIX1":
                card["value"] = rec["value"] - left
            elif rec["name"] == "CRPIX2":
                card["value"] = rec["value"] - top
            cards.append(card)

    # cfitsio only writes to paths, not buffers; round-trip through a
    # temp file to get the clip bytes
    fd, tmp = tempfile.mkstemp(suffix=".fits")
    try:
        os.close(fd)
        fitsio.write(tmp, stamp, header=cards, clobber=True)
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        os.unlink(tmp)


# ---------------------------------------------------------------------------
# Per-source readers (auxiliary source catalog)
#
# "A different source is a function, not a subclass." Besides the cutout
# FITS, the pipeline job directory holds per-source catalogs carrying
# measurements the DB `sources` row does not (aperture flux, shape moments,
# elongation, position errors, ...). Cross-referencing pulls those onto a
# Source by position-matching, filling currently-stubbed diaSource params
# (apFlux, ixx/iyy/ixy, elong, raErr/decErr, ...).
#
# The catalog product is not settled yet -- candidates are the difference-
# image SExtractor catalog and the photutils psf-fit catalog -- so these
# readers stay product-agnostic: parse_catalog returns opaque (rows, index)
# and match_catalog_row does the nearest-neighbour lookup on them.
#
# Both are STUBS and the whole path is a no-op today: CATALOG_FILE is unset
# and cross-referencing is off by default. A reference implementation to port
# from is alerts/roman_rapid_alerts/generate_alerts.py on the
# add-alert-generation branch (parse_sextractor / load_psf_catalog /
# match_psf).
# ---------------------------------------------------------------------------

# Basename of the catalog to cross-reference against, beside the cutout FITS
# in the job dir. Unset until the product is chosen (e.g. a SExtractor
# ".txt" or a photutils psf-fit parquet); while None, _load_catalog()
# short-circuits and cross-referencing stays a no-op.
CATALOG_FILE = None


def parse_catalog(path):
    """STUB. Parse the auxiliary source catalog at `path` and return
    (rows, index) for position matching -- e.g. a table of catalog rows plus
    a spatial index (cKDTree) over their (x, y) pixel positions -- or
    (None, None) if the file is missing or empty. Port from
    generate_alerts.py:parse_sextractor / load_psf_catalog."""
    raise NotImplementedError(
        "catalog parsing not implemented yet; port from "
        "add-alert-generation:alerts/roman_rapid_alerts/generate_alerts.py")


def match_catalog_row(x, y, index, rows, radius=3.0):
    """STUB. Return the catalog row nearest to pixel (x, y) within `radius`
    pixels, or None if there is no match. Port from
    generate_alerts.py:match_psf."""
    raise NotImplementedError(
        "catalog position matching not implemented yet")


# ---------------------------------------------------------------------------
# The alert data provider
#
# One provider. The RAPID database is effectively a fast index over the
# pipeline job directories, so this reads tabular data (detections, object
# associations, history) from the DB and pixel/auxiliary products (cutout
# FITS, and -- opt-in -- the source catalog above) from the job directory
# those DB rows point at, via the module-level reader functions.
# ---------------------------------------------------------------------------

class AlertDataProvider:
    """Pulls alert inputs from the RAPID operations database (RAPIDDB).

    The one DB connection (held by RAPIDDB) persists for the provider's
    lifetime; each query uses a short-lived cursor.

    Two flows share the same get_* interface:
      - single-alert: each get_* call issues its own query
      - batch (per chip): iter_sources(pid) prefetches the whole chip's
        associations and histories with a few set-based queries, and the
        get_* calls below answer from that prefetch instead of querying
    assemble_alert() cannot tell the difference, by design.
    """

    def __init__(self, db, diff_flavor="sfft"):
        """
        Args:
            db: RAPIDDB instance (rapid.database.modules.utils.rapid_db).
            diff_flavor: which differencing algorithm's image feeds
                cutoutDifference ("sfft" or "zogy"). The detections
                themselves always come from the sources table regardless.
        """
        if diff_flavor not in DIFF_FLAVORS:
            raise ValueError(f"diff_flavor must be one of "
                             f"{sorted(DIFF_FLAVORS)}, not {diff_flavor!r}")
        self.db = db
        self.diff_flavor = diff_flavor
        # Per-chip prefetch state, filled by iter_sources(pid). While the
        # current chip matches source.pid, get_object_for_source() and
        # get_prv_detections() answer from these dicts.
        self._chip_pid = None
        self._chip_objects = {}       # sid -> astroobjects row dict
        self._chip_history = {}       # aid -> [Source, ...], oldest first
        self._chip_window_days = 0.0  # look-back window the prefetch covers
        # Full chip images for cutouts, loaded lazily by get_cutouts() and
        # held until a source from a different chip comes along. S3 files
        # are staged here before loading; constant product basenames mean
        # each chip's downloads replace the previous chip's files.
        self._images_pid = None
        self._images = {}             # "diff"|"sci"|"ref" -> (pixels, header)
        self._staging_dir = tempfile.mkdtemp(prefix="rapid_cutouts_")
        self._forced_phot_logged = False  # log the not-implemented note once
        # Auxiliary-catalog cross-reference state. The catalog is staged and
        # parsed once per pid -- same lifetime as _images. While CATALOG_FILE
        # is unset, _load_catalog() short-circuits before any query or stage,
        # so cross-referencing costs nothing beyond a cache check per source.
        self._catalog_pid = None
        self._catalog = (None, None)  # (rows, index) from parse_catalog

    def _query(self, sql, params=None):
        """Run one query and return rows as {column_name: value} dicts."""
        cur = self.db.conn.cursor()
        try:
            cur.execute(sql, params)
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
        finally:
            cur.close()

    def resolve_pid(self, expid, sca):
        """Map (exposure, SCA) to the difference-image processing ID to
        alert on.

        One pid is one processing of one (exposure, SCA), but the mapping
        back is not unique: reprocessing campaigns leave several
        diffimages rows per (expid, sca), each with vbest=1 (older
        campaigns' flags are not cleared). Take the newest such row -- in
        practice only the newest campaign's pid has sources loaded.

        TODO: remove this workaround once the database standards for
        vbest/reprocessing are settled; vbest > 0 should then identify
        exactly one row per (expid, sca).
        """
        rows = self._query("""
            SELECT pid FROM diffimages
            WHERE expid = %s AND sca = %s AND vbest > 0
            ORDER BY pid DESC
        """, (expid, sca))
        if not rows:
            raise ValueError(
                f"no vbest>0 difference image for expid={expid} sca={sca}")
        if len(rows) > 1:
            logger.warning(
                "expid=%s sca=%s has %d vbest>0 diffimages rows "
                "(reprocessing campaigns); using the newest, pid=%s",
                expid, sca, len(rows), rows[0]["pid"])
        return int(rows[0]["pid"])

    def get_detection(self, sid):
        # sources row -> Source, column names matching attribute names.
        # The filters join resolves the numeric fid to the band string
        # ("F158", ...).
        rows = self._query("""
            SELECT s.*, f.filter AS filter_name, e.exptime
            FROM sources s
            JOIN filters f ON s.fid = f.fid
            JOIN exposures e ON s.expid = e.expid
            WHERE s.sid = %s
        """, (sid,))
        if not rows:
            raise ValueError(f"Source {sid} not found")
        row = rows[0]
        row["band"] = row.get("filter_name")
        # Source.aid stays None here; assemble_alert() fills it in after
        # get_object_for_source() resolves the association. strict=True makes
        # a renamed/dropped sources column an error, not a null alert field.
        source = Source.from_row(row, strict=True)
        self._crossref(source)
        return source

    def iter_sources(self, pid):
        # One chip = one difference image = one diffimages.pid. Fetch every
        # detection on it with a single query, then prefetch the association
        # and history rows for all of them at once (a handful of set-based
        # queries instead of ~3 queries per alert).
        rows = self._query("""
            SELECT s.*, f.filter AS filter_name, e.exptime
            FROM sources s
            JOIN filters f ON s.fid = f.fid
            JOIN exposures e ON s.expid = e.expid
            WHERE s.pid = %s
            ORDER BY s.sid
        """, (pid,))
        sources = []
        for row in rows:
            row["band"] = row.get("filter_name")
            source = Source.from_row(row, strict=True)
            self._crossref(source)
            sources.append(source)
        self._prefetch_chip(pid, sources)
        yield from sources

    def _prefetch_chip(self, pid, sources, window_days=PRV_WINDOW_DAYS):
        """Load the object associations and detection histories for a whole
        chip into memory, so the per-source get_* calls don't hit the DB."""
        objects_by_sid = {}
        history_by_aid = {}
        # merges/astroobjects are partitioned by Roman field, and sources
        # near a field boundary can land in different partitions, so group
        # the sids by field first (usually a single group).
        for field in sorted({s.field for s in sources}):
            sids = [s.sid for s in sources if s.field == field]
            object_rows = self._query(f"""
                SELECT m.sid, a.aid, a.ra0, a.dec0,
                       a.stdevra, a.stdevdec, a.nsources
                FROM merges_{int(field)} m
                JOIN astroobjects_{int(field)} a ON m.aid = a.aid
                WHERE m.sid = ANY(%s)
            """, (sids,))
            for row in object_rows:
                objects_by_sid[row["sid"]] = row

            aids = sorted({row["aid"] for row in object_rows})
            if not aids:
                continue
            # All prior detections of every associated object, in one query.
            # The MJD cutoff uses the earliest trigger on the chip; each
            # get_prv_detections() call then tightens it to its own trigger.
            earliest_mjd = min(s.mjdobs for s in sources) - window_days
            history_rows = self._query(f"""
                SELECT m.aid AS object_aid, s.*, f.filter AS filter_name, e.exptime
                FROM sources s
                JOIN merges_{int(field)} m ON s.sid = m.sid
                JOIN filters f ON s.fid = f.fid
                JOIN exposures e ON s.expid = e.expid
                WHERE m.aid = ANY(%s) AND s.mjdobs >= %s
                ORDER BY s.mjdobs
            """, (aids, earliest_mjd))
            for row in history_rows:
                row["band"] = row.get("filter_name")
                row["aid"] = row["object_aid"]
                history_by_aid.setdefault(row["aid"], []).append(
                    Source.from_row(row, strict=True))

        self._chip_pid = pid
        self._chip_objects = objects_by_sid
        self._chip_history = history_by_aid
        self._chip_window_days = window_days

    def get_object_for_source(self, detection):
        # Batch flow: after iter_sources(pid), every association for the
        # chip is already in memory. An sid absent from the prefetch means
        # "no associated object" -- no fallback query needed.
        if self._chip_pid is not None and self._chip_pid == detection.pid:
            row = self._chip_objects.get(detection.sid)
            if row is None:
                return None
            # Build a fresh ObjectRecord each time: assemble_alert() fills
            # in the first/last/validity MJDs, and two sources on the same
            # chip may share an object.
            return ObjectRecord.from_row(row, strict=True)

        # Single-alert flow: query for just this sid.
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
        # from_row() keeps only the ObjectRecord columns. Available in the
        # a.* row but currently unused: meanra/meandec (mean position),
        # flux0/meanflux/stdevflux, hp6/hp9.
        return ObjectRecord.from_row(rows[0], strict=True)

    def get_prv_detections(self, detection, obj, window_days=PRV_WINDOW_DAYS):
        # Batch flow: filter this object's prefetched history down to this
        # trigger's window. Only usable when the prefetch covered at least
        # as long a look-back window as requested.
        if (self._chip_pid is not None and self._chip_pid == detection.pid
                and window_days <= self._chip_window_days):
            cutoff = detection.mjdobs - window_days
            return [s for s in self._chip_history.get(obj.aid, [])
                    if s.sid != detection.sid and s.mjdobs >= cutoff]

        # Single-alert flow: same sources -> Source mapping as
        # get_detection(), but selecting
        # the object's other detections: merges_<field> gathers every sid
        # associated with this aid, minus the trigger itself, restricted to
        # the look-back window before the triggering detection. These become
        # the alert's prvDiaSources (oldest first).
        field = int(detection.field)
        rows = self._query(f"""
            SELECT s.*, f.filter AS filter_name, e.exptime
            FROM sources s
            JOIN merges_{field} m ON s.sid = m.sid
            JOIN filters f ON s.fid = f.fid
            JOIN exposures e ON s.expid = e.expid
            WHERE m.aid = %s AND s.sid != %s
              AND s.mjdobs >= %s
            ORDER BY s.mjdobs
        """, (obj.aid, detection.sid, detection.mjdobs - window_days))
        detections = []
        for row in rows:
            row["band"] = row.get("filter_name")
            row["aid"] = obj.aid  # known from the join; save a lookup
            detections.append(Source.from_row(row, strict=True))
        return detections

    def get_forced_photometry(self, detection, obj):
        # Forced photometry in RAPID produces FITS files, not DB records;
        # integration with alert packets is not yet implemented. Log once
        # per provider, not once per source -- a batch run reaches here
        # tens of thousands of times.
        if not self._forced_phot_logged:
            logger.info(
                "Forced photometry not yet available for alert assembly")
            self._forced_phot_logged = True
        return []

    def get_cutouts(self, detection):
        # Cutouts are generated on the fly: stamps sliced out of the chip's
        # full difference/science/template images at the source position
        # (one shared pixel grid; see DIFF_FLAVORS/CUTOUT_FILES). The
        # images are loaded once per chip and reused for every source on
        # it, in both the batch and single-alert flows.
        images = self._chip_images(detection.pid)
        # sources.xfit/yfit are 0-based (photutils PSF-fit convention);
        # extract_stamp takes 1-based FITS pixel coordinates. Verified
        # against the DB: sources.ra/dec equals the difference image's
        # TPV WCS evaluated at exactly (xfit+1, yfit+1).
        x, y = detection.xfit + 1.0, detection.yfit + 1.0
        stamps = {}
        for key in ("diff", "sci", "ref"):
            pixels, header = images.get(key, (None, None))
            stamps[key] = extract_stamp(pixels, x, y, header=header)
        return Cutouts(difference=stamps["diff"], science=stamps["sci"],
                       template=stamps["ref"])

    def _stage(self, url):
        """Make one image available locally, downloading s3:// URLs into
        the staging directory (plain paths pass through). Returns the local
        path, or None if the download failed -- that image's cutouts are
        then null."""
        if not url.startswith("s3://"):
            return url
        parts = urlparse(url)
        local = os.path.join(self._staging_dir, os.path.basename(url))
        try:
            import boto3  # deferred so non-AWS providers/tests don't need it
            boto3.client("s3").download_file(parts.netloc,
                                             parts.path.lstrip("/"), local)
            return local
        except Exception:
            logger.warning("Could not stage %s", url, exc_info=True)
            return None

    def _chip_images(self, pid):
        """Return the chip's {"diff", "sci", "ref"} (pixels, header) pairs,
        staging and loading them on first use. diffimages.filename locates
        the job directory; the three cutout images are the co-gridded
        products in it (the DB's own diff filename is replaced by the
        diff_flavor one). A missing/unreadable file loads as (None, None),
        which extract_stamp() turns into a null cutout."""
        if self._images_pid == pid:
            return self._images

        rows = self._query(
            "SELECT filename FROM diffimages WHERE pid = %s", (pid,))
        if rows:
            job_dir = os.path.dirname(rows[0]["filename"])
            names = {"diff": DIFF_FLAVORS[self.diff_flavor],
                     "sci": CUTOUT_FILES["sci"], "ref": CUTOUT_FILES["ref"]}
            self._images = {
                key: load_fits_image(self._stage(f"{job_dir}/{name}"))
                for key, name in names.items()
            }
            self._check_grids_match(pid)
        else:
            logger.warning("No diffimages row for pid=%s; cutouts will be "
                           "null", pid)
            self._images = {}
        self._images_pid = pid
        return self._images

    def _check_grids_match(self, pid):
        """Cutout positions assume the three images share one pixel grid.
        Verify it from the loaded WCS headers and drop (null) any image on
        a different grid rather than emit a cutout of the wrong sky
        position. Tolerances allow the header-writing rounding differences
        between the products (~1e-10 deg)."""
        _, diff_header = self._images.get("diff", (None, None))
        if diff_header is None:
            return
        grid_cards = ("CRVAL1", "CRVAL2", "CRPIX1", "CRPIX2",
                      "CD1_1", "CD1_2", "CD2_1", "CD2_2")
        for key in ("sci", "ref"):
            _, header = self._images.get(key, (None, None))
            if header is None:
                continue
            for card in grid_cards:
                if np.isclose(header.get(card, np.nan),
                              diff_header.get(card, np.nan),
                              rtol=1e-6, atol=1e-8):
                    continue
                logger.warning(
                    "pid=%s: %s image grid differs from the difference "
                    "image (%s: %r vs %r); its cutouts will be null",
                    pid, key, card, header.get(card),
                    diff_header.get(card))
                self._images[key] = (None, None)
                break

    # -- Auxiliary-catalog cross-reference ---------------------------------

    def _load_catalog(self, pid):
        """(rows, index) for this chip's auxiliary source catalog, staged and
        parsed once per pid -- same lifetime as _images. Returns (None, None)
        when cross-referencing is not wired up yet (CATALOG_FILE unset) or the
        diffimages row / catalog file is missing, so _crossref() degrades to a
        no-op (the null-cutout ladder). While CATALOG_FILE is None this
        short-circuits before any query or stage."""
        if self._catalog_pid == pid:
            return self._catalog
        self._catalog = (None, None)
        self._catalog_pid = pid
        if CATALOG_FILE is None:
            return self._catalog
        rows = self._query(
            "SELECT filename FROM diffimages WHERE pid = %s", (pid,))
        if rows:
            job_dir = os.path.dirname(rows[0]["filename"])
            path = self._stage(f"{job_dir}/{CATALOG_FILE}")
            if path is not None:
                self._catalog = parse_catalog(path)
        else:
            logger.warning("No diffimages row for pid=%s; catalog "
                           "cross-reference skipped", pid)
        return self._catalog

    def _crossref(self, source):
        """Cross-reference `source` against the chip's auxiliary source
        catalog by position and pull extra per-source fields (apFlux, shape
        moments, elong, raErr/decErr, ...) onto it. A no-op when the catalog
        or a matching row is unavailable, so a source with no catalog
        counterpart still yields a valid (stub-null) alert.

        The attribute assignments are intentionally left as a TODO: they land
        in lockstep with (a) new optional fields on Source and (b) flipping
        the matching params to IMPLEMENTED in param_registry.py. See
        generate_alerts.py:build_alert for a field->column mapping and the
        FILTER_ZP_EFF nJy calibration.
        """
        rows, index = self._load_catalog(source.pid)
        if rows is None:
            return
        match = match_catalog_row(source.xfit, source.yfit, index, rows)
        if match is None:
            return
        # TODO: assign cross-referenced attributes onto `source` from the
        # matched catalog row, once Source gains those fields and the registry
        # marks them IMPLEMENTED. Until then the match is intentionally unused.


# ---------------------------------------------------------------------------
# No-DB (pure filesystem) flow
#
# There is deliberately no separate FilesystemProvider: the database and
# filesystem paths overlapped almost entirely (get_cutouts already reads job-
# dir FITS; the SExtractor/LC products live in that same directory). A no-DB
# run -- reading detections/objects straight from the pipeline products
# instead of the sources/astroobjects tables -- would be an alternate
# constructor on this one provider (e.g. AlertDataProvider.from_job_dir) that
# fills the same normalized records using the same module-level readers
# (load_fits_image, extract_stamp, parse_catalog, plus a light-curve tile
# reader to port from generate_alerts.py:load_lc_tile/match_lc).
#
# Calibration note: that script converts SExtractor and light-curve fluxes to
# nJy via FILTER_ZP_EFF, while this flow currently passes instrumental fluxfit
# through. Reconcile the calibration when porting.
# ---------------------------------------------------------------------------
