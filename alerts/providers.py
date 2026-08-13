"""
File:     providers.py
Author:   Emily Everetts
Date:     07/2026

Normalized data provider and per-source reader functions.

Normalized records (:class:`Source`, :class:`ObjectRecord`, :class:`ForcedPhot`,
:class:`Cutouts`) are used to fill the alert builders in :mod:`alerts.produce`.
Provider translates native column names into canonical alert attributes.

Notes
-----
Data flow (DB table / job-dir file -> record -> schema record)::

    sources + filters in DB -> Source object -> diaSource schema
        The triggering source is looked up by sid
    merges_<field>        -> (sid -> aid association)
        per-field table linking detections (sid) to persistent objects (aid)
    astroobjects_<field>  -> ObjectRecord   -> diaObject
        one row per persistent object, keyed by aid
    diffimages.filename   -> full chip images -> Cutouts
        diff, science and template images are from the pipeline job directory
        (s3://.../<date>/jid<N>/). Stamps are cut around each source
        position by extract_stamp().
        Images are staged and loaded once per chip.

Examples
--------
>>> from alerts.providers import AlertDataProvider
>>> provider = AlertDataProvider(db)
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
CUTOUT_FILES : dict
    Cutout kind ("sci" / "ref") -> co-gridded product basename in the job dir.
WCS_CARD_PREFIXES : tuple of str
    FITS header-card prefixes copied from the parent image into each cutout.
REF_MATCH_RADIUS_ARCSEC : float
    Maximum separation for reference-catalog matches.
REF_MATCH_NMAX : int
    Maximum reference-catalog matches kept per class (stars / galaxies).
"""

import dataclasses
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator, Sequence, TypeAlias
from urllib.parse import urlparse

import fitsio
import numpy as np

if TYPE_CHECKING:
    from fitsio.header import FITSHDR

logger = logging.getLogger(__name__)

PRV_WINDOW_DAYS = 365.25  # default look-back window for previous detections

# Cutout-image staging: backoff between attempts is
# STAGE_BACKOFF_BASE_S * 2**(attempt-1). The attempt count is the `retries`
# argument of _stage(), not a constant.
STAGE_BACKOFF_BASE_S = 1.0


class CutoutStagingError(RuntimeError):
    """A cutout source image could not be staged or loaded for a chip.

    Raised (loudly) rather than silently producing null cutouts: a
    persistent S3 failure or a missing/unreadable product aborts the whole
    chip so it can be retried or reprocessed, instead of shipping tens of
    thousands of cutout-less alerts. Because the three images load once per
    chip, one such failure would otherwise degrade every source on it.
    """

#: For pylance checking
LoadedImage: TypeAlias = "tuple[np.ndarray | None, FITSHDR | None]"


# ---------------------------------------------------------------------------
# Normalized records (the provider contract)
# ---------------------------------------------------------------------------

@dataclass
class Source:
    """One difference-image source detection (DB ``sources`` row equivalent).
    """
    sid: int
    expid: int
    sca: int
    mjdobs: float
    ra: float
    dec: float
    xfit: float
    yfit: float
    band: str
    aid: int | None = None        # associated object; set once known
    # THE CATALOGUE'S OWN KEY, exposed for alert identity (brief E). Together
    # with `pid` and `isdiffpos` this is migration 041's conflict identity for
    # `sources` — `id` is a PER-FILE ordinal, unique only within one difference
    # image and one sign, which is why it never travels alone.
    #
    # Distinct from `sid`, and that distinction is the reason this field
    # exists. `sid` is DB-GENERATED at catalog load (`pipeline/stages/
    # post_db.py`'s COPY column list does not carry it), so it is
    # realization-local: reload the same catalogue and every detection gets a
    # new one. An alert identity built on `sid` would change for data that did
    # not change, which is what `alerts/identity.py` refuses.
    #
    # Placed among the defaulted fields rather than beside `sid` where it
    # belongs by meaning: `Source` is built positionally by a good many test
    # doubles, and a non-defaulted field inserted second would break every one
    # of them (and a dataclass cannot put a defaulted field before a required
    # one at all). `from_row` fills it by NAME from the `sources` row, where
    # `SELECT s.*` has always returned it, so the production path is unaffected
    # by where it sits.
    id: int | None = None
    xerr: float | None = None
    yerr: float | None = None
    fluxfit: float | None = None
    fluxerr: float | None = None
    flags: int = 0
    field: int = 0
    hp6: int = 0
    hp9: int = 0
    pid: int = 0
    isdiffpos: bool = True
    qfit: float | None = None
    cfit: float | None = None
    redchi: float | None = None
    npixfit: int | None = None
    sharpness: float | None = None
    roundness1: float | None = None
    roundness2: float | None = None
    peak: float | None = None
    exptime: float | None = None
    # solar-system candidate flag, set by get_ss_matches(); None = KONA
    # association not run for this detection (e.g. all prv sources)
    is_ss_candidate: bool | None = None

    @property
    def snr(self) -> float | None:
        """float or None: signal-to-noise ratio, fluxfit / fluxerr.

        None when the flux is missing or the uncertainty is zero/missing.
        """
        if self.fluxfit is not None and self.fluxerr:
            return self.fluxfit / self.fluxerr
        return None

    @classmethod
    def from_row(cls, row: dict[str, Any], strict: bool = False) -> "Source":
        """Build a Source from a dict, ignoring keys that are not fields.

        Parameters
        ----------
        row : dict
            A ``sources`` row (column name -> value), typically with the
            derived ``band`` key already added.
        strict : bool, optional
            If True, every Source field must be present as a key in `row`
            (except `aid` and `is_ss_candidate`, which are filled after
            construction). Errors on dropped columns instead of null.

            `id` IS in the strict set, deliberately. Every caller reads
            `SELECT s.*` from `sources`, so the column is always there; making
            it strict means a future query that narrows its column list fails
            HERE, naming `id`, rather than silently producing alert packets
            whose identity component is None — which the identity module would
            then refuse one layer further away from the cause.

        Returns
        -------
        Source

        Raises
        ------
        KeyError
            In strict mode, if an expected column is missing from `row`.
        """
        names = {f.name for f in dataclasses.fields(cls)}
        if strict:
            missing = names - set(row) - {"aid", "is_ss_candidate"}
            if missing:
                raise KeyError(
                    f"Source row is missing expected columns: "
                    f"{sorted(missing)} (renamed or dropped in storage?)")
        return cls(**{key: value for key, value in row.items() if key in names})


@dataclass
class ObjectRecord:
    """Persistent astronomical object (DB ``astroobjects_<field>`` row equivalent).
    """
    aid: int
    ra0: float
    dec0: float
    # stdevra/stdevdec are statistics' product (astroobjectsmeta_<field>)
    # and map to the schema's NULLABLE raErr/decErr: None until statistics
    # has run for the field. nsources maps to non-nullable nDiaSources and
    # is never None — the provider falls back to the merges association
    # count when the meta row is absent.
    stdevra: float | None
    stdevdec: float | None
    nsources: int
    first_mjd: float | None = None
    last_mjd: float | None = None
    validity_mjd: float = 0.0

    # fields assemble_alert() fills in later; never storage columns
    FILLED_LATER = frozenset({"first_mjd", "last_mjd", "validity_mjd"})

    @classmethod
    def from_row(cls, row: dict[str, Any],
                 strict: bool = False) -> "ObjectRecord":
        """Build an ObjectRecord from a dict, ignoring non-field keys.

        Parameters
        ----------
        row : dict
            An ``astroobjects_<field>`` row (column name -> value).
        strict : bool, optional
            If True, every field must be present as a key in `row`
            Errors on dropped columns instead of null.

        Returns
        -------
        ObjectRecord

        Raises
        ------
        KeyError
            In strict mode, if an expected column is missing from `row`.
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
    """One forced-photometry measurement at an object position.

    Staged for the diaForcedSource record; no provider fills it yet
    (RAPID forced photometry writes lightcurve files, not DB rows).
    """
    forced_id: int
    aid: int
    expid: int
    sca: int
    ra: float
    dec: float
    mjdobs: float
    time_processed: float
    band: str | None = None
    flux: float | None = None
    fluxerr: float | None = None


@dataclass
class SSMatch:
    """One known solar system object predicted near a detection.

    Built by match_ss_predictions() from the KONA per-visit predictions;
    becomes one entry of the alert's ssMatches array.
    """
    designation: str
    ra: float            # predicted ICRS position at the exposure epoch [deg]
    dec: float
    sep: float           # angular separation from the source position [arcsec]
    pa: float            # position angle source -> object, East of North [deg]
    predvmag: float | None = None  # predicted V mag; None if no catalogued H


@dataclass
class RefMatch:
    """One reference-image catalog source near a detection.

    Built by match_refcat() from the field's mosaic SExtractor catalog
    (see the reference-catalog cross-match section below); becomes one
    entry of the alert's refStarMatches or refGalaxyMatches array. The
    star/galaxy split is made at match time by CLASS_STAR, but each match
    keeps its own class_star so consumers can re-cut.
    """
    source_id: str       # SExtractor NUMBER, unique within the field catalog
    ra: float            # catalog position, ICRS [deg]
    dec: float
    sep: float           # angular separation from the detection [arcsec]
    pa: float            # position angle detection -> catalog source,
                         # East of North [deg]
    class_star: float    # SExtractor star/galaxy score (1 = point-like)
    flags: int           # SExtractor extraction flags of the catalog source
    mag_auto: float | None = None       # MAG_AUTO [instrumental mag]
    mag_err_auto: float | None = None
    elong: float | None = None          # A/B axis ratio
    fwhm: float | None = None           # [arcsec]
    half_light_radius: float | None = None  # FLUX_RADIUS at 0.5 [arcsec]
    kron_radius: float | None = None    # Kron factor [units of A_IMAGE]


@dataclass
class Cutouts:
    """Raw FITS bytes for the three image stamps (any may be missing).

    Parse with ``fits.open(io.BytesIO(cutouts.difference))`` or write
    straight to disk for DS9."""
    difference: bytes | None = None
    science: bytes | None = None
    template: bytes | None = None

    def __repr__(self) -> str:
        """Summarize each stamp as its byte count.
            (avoids byte dump when reading).
        """
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
# pixel position is valid in all of them.
#
# WHICH difference image feeds cutoutDifference is no longer a caller's
# choice. The job runs several differencing algorithms and the release binds
# exactly one of them to the difference-image role; that is the one that
# registered, so `diffimages.filename` already names it. Cutting stamps from
# a different algorithm's image than the row describes would make an alert's
# cutout and its measurements come from two different difference images.
#
# This is the ruling's third gate: no consumer carries an algorithm literal
# (decisions.md § Difference-image product vocabulary). The role is resolved
# through the registered filename rather than through release content
# directly, because the alert layer reads the database and an archived alert
# must keep pointing at the image its row was built from even after a later
# release rebinds the role.
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


def load_fits_image(path: str | None) -> LoadedImage:
    """Load the pixels and header of a FITS image.

    Reads the first HDU that has pixel data (primary for the pipeline
    products; Roman L2 cal files keep the pixels in a SCI extension).

    Parameters
    ----------
    path : str or None
        Path to the FITS file. None gives a missing image.

    Returns
    -------
    pixels : numpy.ndarray or None
        The image data, or None if the file is missing, unreadable, or
        has no image HDU.
    header : fitsio.header.FITSHDR or None
        The matching header, None whenever `pixels` is None.
    """
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


def extract_stamp(image_data: np.ndarray | None, x: float | None,
                  y: float | None, header: "FITSHDR | None" = None,
                  half_width: int = STAMP_HALF_WIDTH) -> bytes | None:
    """Cut a square stamp around a pixel position, as FITS-file bytes.

    Stamp pixels beyond the chip edge are set to STAMP_FILL_VALUE.

    Parameters
    ----------
    image_data : numpy.ndarray or None
        The full chip image.
    x, y : float or None
        FITS pixel coordinates of the stamp center (1-based).
    header : fitsio.header.FITSHDR, optional
        The parent image's header. If given, the stamp carries the parent
        WCS cards (see WCS_CARD_PREFIXES) with CRPIX shifted to the stamp
        frame.
    half_width : int, optional
        Stamp is ``2 * half_width + 1`` pixels square.

    Returns
    -------
    bytes or None
        The stamp as a complete FITS file, or None if there is no image
        or the stamp would not overlap the chip at all.
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
# Solar-system association (KONA)
#
# KONA (modules/solarsystem/rapid_kona.py) predicts, per exposure, where
# known solar system objects fall: {designation: (ra_deg, dec_deg, vmag)}.
# match_ss_predictions() associates those predictions with one detection
# position; the provider exposes it as get_ss_matches() using whatever
# kona_lookup callable it was constructed with.
# ---------------------------------------------------------------------------

ROMAN_PIXEL_SCALE_ARCSEC = 0.11  # WFI plate scale [arcsec/pixel]

# Report known objects within the cutout stamp's inscribed circle.
SS_MATCH_RADIUS_ARCSEC = STAMP_HALF_WIDTH * ROMAN_PIXEL_SCALE_ARCSEC  # ~7.0

# Keep at most this many matches per alert (nearest first).
SS_MATCH_NMAX = 3

# A match closer than this makes the detection a solar-system candidate
# (diaSource.isSSCandidate). TODO: tune once KONA runs on real fields;
# should comfortably exceed the ephemeris + astrometry error budget.
SS_CANDIDATE_SEP_ARCSEC = .5


def match_ss_predictions(ra: float, dec: float,
                         predictions: dict[str, tuple[float, float, float | None]],
                         radius_arcsec: float = SS_MATCH_RADIUS_ARCSEC,
                         n_max: int = SS_MATCH_NMAX) -> list[SSMatch]:
    """Associate KONA predictions with one detection position.

    Parameters
    ----------
    ra, dec : float
        Detection position, ICRS [deg].
    predictions : dict
        ``{designation: (ra_deg, dec_deg, vmag_or_None)}`` for the
        detection's exposure, as produced by rapid_kona.kona().
    radius_arcsec : float, optional
        Maximum angular separation to report.
    n_max : int, optional
        Keep at most this many matches, nearest first.

    Returns
    -------
    list of SSMatch
        The nearest predictions within `radius_arcsec`, sorted by
        separation; empty when none are close enough.
    """
    if not predictions:
        return []
    desigs = list(predictions)
    pred = np.array([predictions[d][:2] for d in desigs], dtype=float)
    ra0, dec0 = np.radians(ra), np.radians(dec)
    pra, pdec = np.radians(pred[:, 0]), np.radians(pred[:, 1])

    # Vincenty angular separation (numerically stable at all separations)
    dra = pra - ra0
    num = np.hypot(np.cos(pdec) * np.sin(dra),
                   np.cos(dec0) * np.sin(pdec)
                   - np.sin(dec0) * np.cos(pdec) * np.cos(dra))
    den = (np.sin(dec0) * np.sin(pdec)
           + np.cos(dec0) * np.cos(pdec) * np.cos(dra))
    sep_arcsec = np.degrees(np.arctan2(num, den)) * 3600.0

    # position angle detection -> prediction, East of North [0, 360) deg
    pa_deg = np.degrees(np.arctan2(
        np.sin(dra) * np.cos(pdec),
        np.cos(dec0) * np.sin(pdec)
        - np.sin(dec0) * np.cos(pdec) * np.cos(dra))) % 360.0

    keep = np.flatnonzero(sep_arcsec <= radius_arcsec)
    keep = keep[np.argsort(sep_arcsec[keep])][:n_max]
    # entry[2] is the predicted V mag; tolerate pre-vmag KONA output, which
    # wrote (ra, dec) 2-tuples
    return [SSMatch(designation=desigs[i],
                    ra=float(pred[i, 0]), dec=float(pred[i, 1]),
                    sep=float(sep_arcsec[i]), pa=float(pa_deg[i]),
                    predvmag=(entry[2] if len(entry := predictions[desigs[i]]) > 2
                              else None))
            for i in keep]


# ---------------------------------------------------------------------------
# Reference-catalog cross-match
#
# The reference-image pipeline runs SExtractor on each field's coadd mosaic
# and registers the catalog in the refimcatalogs table (cattype=1), so a
# chip's counterpart catalog is located from its diffimages row:
#     pid -> diffimages.rfid -> refimcatalogs.filename (s3://...)
# The catalog is staged and parsed once per rfid (many chips share one
# reference image), partitioned into star/galaxy KD-trees by CLASS_STAR,
# and all detections on a chip are matched in one vectorized pass (see
# AlertDataProvider.iter_sources / get_ref_matches).
#
# Matching is in sky coordinates: the mosaic does not share the chip's
# pixel grid, and every alertable source has reference coverage by
# construction (subtraction requires it), so "no match within radius"
# cleanly means "no counterpart above the reference-catalog depth".
# ---------------------------------------------------------------------------

REFCAT_CATTYPE = 1  # refimcatalogs.cattype of the mosaic SExtractor catalog

# Maximum separation for a reported match. TODO: tune against the measured
# chance-coincidence rate (rho * pi * r^2) once run over real fields.
REF_MATCH_RADIUS_ARCSEC = 5.0

# Keep at most this many matches per class (stars / galaxies), nearest first.
REF_MATCH_NMAX = 3

# CLASS_STAR partition: >= STAR_MIN -> star tree, < GALAXY_MAX -> galaxy
# tree. Equal thresholds make the split exhaustive; moving them apart
# excludes an unclassifiable middle band from both trees.
# TODO: settle the threshold(s) with the team (possible three-way split);
# CLASS_STAR is unreliable at faint mags, so each match carries its score.
REFCAT_STAR_MIN_CLASS = 0.9
REFCAT_GALAXY_MAX_CLASS = 0.9

# Mosaic pixel scale, to convert SExtractor pixel sizes to arcsec
# (awaicgen_pixelscale_absolute in cdf/awsBatchSubmitJobs_launch*.ini).
REFCAT_PIXEL_SCALE_ARCSEC = 0.11

# SExtractor writes 99.0 in MAG_*/MAGERR_* for failed measurements.
REFCAT_MAG_SENTINEL = 99.0

# Catalog columns kept by load_refcat(); the rest of the mosaic catalog's
# 115 columns are dropped at parse time. FLUX_RADIUS_1 is the second
# PHOT_FLUXFRAC entry (0.25,0.5,...) = the half-light radius; astropy's
# sextractor reader names vector-column elements NAME, NAME_1, NAME_2, ...
REFCAT_COLUMNS = (
    "NUMBER", "ALPHAWIN_J2000", "DELTAWIN_J2000", "FLAGS", "CLASS_STAR",
    "MAG_AUTO", "MAGERR_AUTO", "ELONGATION", "FWHM_IMAGE",
    "FLUX_RADIUS_1", "KRON_RADIUS",
)


@dataclass
class RefCatalog:
    """One reference image's catalog, partitioned for nearest-N matching.

    Built by load_refcat(). `coords` holds one astropy SkyCoord per class
    ("star" / "galaxy"; None when the class has no rows) -- astropy caches
    the KD-tree on the SkyCoord instance, so matching many detections
    against the same RefCatalog reuses one tree per class. `rows` maps a
    class-subset position back to its original catalog row, and `columns`
    holds the kept columns over the full catalog, in row order.
    """
    columns: dict[str, np.ndarray]
    coords: dict[str, Any]        # class -> SkyCoord of the subset, or None
    rows: dict[str, np.ndarray]   # class -> original row index per subset entry


def load_refcat(path: str) -> RefCatalog | None:
    """Parse a reference-image SExtractor catalog for cross-matching.

    Parameters
    ----------
    path : str
        Path to the staged catalog file (SExtractor ASCII_HEAD format).

    Returns
    -------
    RefCatalog or None
        The parsed, star/galaxy-partitioned catalog; None (with a logged
        warning) when the file is unreadable, empty, or missing expected
        columns -- the cross-match then degrades to "not run".
    """
    from astropy import units as u
    from astropy.coordinates import SkyCoord
    from astropy.io import ascii as astropy_ascii

    try:
        # typed Any: pylance mis-infers astropy's ascii.read overloads
        table: Any = astropy_ascii.read(path, format="sextractor")
    except Exception:
        logger.warning("Could not parse reference catalog %s", path,
                       exc_info=True)
        return None
    if len(table) == 0:
        logger.warning("Reference catalog %s is empty", path)
        return None
    missing = [c for c in REFCAT_COLUMNS if c not in table.colnames]
    if missing:
        logger.warning(
            "Reference catalog %s is missing columns %s (SExtractor "
            "parameter file changed?); cross-match skipped", path, missing)
        return None

    columns = {name: np.asarray(table[name], dtype=float)
               for name in REFCAT_COLUMNS}
    class_star = columns["CLASS_STAR"]
    masks = {"star": class_star >= REFCAT_STAR_MIN_CLASS,
             "galaxy": class_star < REFCAT_GALAXY_MAX_CLASS}
    coords: dict[str, Any] = {}
    rows: dict[str, np.ndarray] = {}
    for cls, mask in masks.items():
        # np.flatnonzero keeps the subset -> original-row mapping that
        # match_refcat() needs to recover full catalog rows from KD-tree
        # indices (which are positions within the subset)
        idx = np.flatnonzero(mask)
        rows[cls] = idx
        coords[cls] = (SkyCoord(columns["ALPHAWIN_J2000"][idx] * u.deg,
                                columns["DELTAWIN_J2000"][idx] * u.deg)
                       if idx.size else None)
    return RefCatalog(columns=columns, coords=coords, rows=rows)


def _ref_match_from_row(columns: dict[str, np.ndarray], row: int,
                        sep: float, pa: float) -> RefMatch:
    """Build one RefMatch from catalog row `row` at the given sep/PA."""
    def val(name: str) -> float:
        return float(columns[name][row])

    mag: float | None = val("MAG_AUTO")
    mag_err: float | None = val("MAGERR_AUTO")
    if mag is not None and mag >= REFCAT_MAG_SENTINEL:
        mag = mag_err = None
    return RefMatch(
        source_id=str(int(val("NUMBER"))),
        ra=val("ALPHAWIN_J2000"), dec=val("DELTAWIN_J2000"),
        sep=sep, pa=pa,
        class_star=val("CLASS_STAR"), flags=int(val("FLAGS")),
        mag_auto=mag, mag_err_auto=mag_err,
        elong=val("ELONGATION"),
        fwhm=val("FWHM_IMAGE") * REFCAT_PIXEL_SCALE_ARCSEC,
        half_light_radius=val("FLUX_RADIUS_1") * REFCAT_PIXEL_SCALE_ARCSEC,
        kron_radius=val("KRON_RADIUS"),
    )


def match_refcat(ra: Any, dec: Any, catalog: RefCatalog,
                 radius_arcsec: float = REF_MATCH_RADIUS_ARCSEC,
                 n_max: int = REF_MATCH_NMAX,
                 ) -> list[tuple[list[RefMatch], list[RefMatch]]]:
    """Match detection positions against a reference catalog.

    One vectorized pass for any number of detections: per class the
    nth-nearest catalog neighbor of every detection is queried for
    n = 1..n_max (astropy reuses the KD-tree cached on the class
    SkyCoord), then matches beyond `radius_arcsec` are dropped --
    nthneighbor always returns something, so the radius is a mask on the
    result, not a query parameter. This is also why the per-class match
    count doubles as a crowding diagnostic: len == n_max means the
    neighborhood may extend beyond what is reported.

    Parameters
    ----------
    ra, dec : float or array-like
        Detection position(s), ICRS [deg].
    catalog : RefCatalog
        The parsed catalog from load_refcat().
    radius_arcsec : float, optional
        Maximum separation to report.
    n_max : int, optional
        Keep at most this many matches per class, nearest first.

    Returns
    -------
    list of (list of RefMatch, list of RefMatch)
        Per detection, in input order: (star matches, galaxy matches),
        each nearest-first and at most `n_max` long.
    """
    from astropy import units as u
    from astropy.coordinates import SkyCoord

    ra = np.atleast_1d(np.asarray(ra, dtype=float))
    dec = np.atleast_1d(np.asarray(dec, dtype=float))
    src = SkyCoord(ra * u.deg, dec * u.deg)
    results: list[tuple[list[RefMatch], list[RefMatch]]] = \
        [([], []) for _ in range(ra.size)]

    for slot, cls in enumerate(("star", "galaxy")):
        coords = catalog.coords[cls]
        if coords is None:
            continue  # class has no catalog rows
        subset_to_row = catalog.rows[cls]
        # ascending nthneighbor keeps each result list nearest-first
        for n in range(1, min(n_max, len(subset_to_row)) + 1):
            idx, sep2d, _ = src.match_to_catalog_sky(coords, nthneighbor=n)
            pa = src.position_angle(coords[idx])
            idx = np.atleast_1d(idx)
            sep_arcsec = np.atleast_1d(sep2d.arcsec)
            pa_deg = np.atleast_1d(pa.deg) % 360.0
            for i in np.flatnonzero(sep_arcsec <= radius_arcsec):
                row = int(subset_to_row[idx[i]])
                results[i][slot].append(_ref_match_from_row(
                    catalog.columns, row,
                    float(sep_arcsec[i]), float(pa_deg[i])))
    return results


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

    One DB connection (held by RAPIDDB) persists for the provider's
    lifetime; each query uses a short-lived cursor.

    Two flows share the same get_* interface:
      - single-alert: each get_* call issues its own query
      - batch (per chip): iter_sources(pid) prefetches the whole chip's
        associations and histories with a few set-based queries, and the
        get_* calls below answer from that prefetch instead of querying
    """

    def __init__(self, db: Any, kona_lookup: Any = None,
                refcat: bool = True) -> None:
        """
        Parameters
        ----------
        db : database.modules.utils.rapid_db.RAPIDDB
            The database connection (anything exposing ``.conn.cursor()``).
        kona_lookup : callable, optional
            ``expid -> {designation: (ra, dec, vmag)} or None``, giving the
            KONA solar-system predictions for an exposure -- an index over
            the daily up-to-date positions download (file format TBD; a
            plain dict's ``.get`` works once it is loaded). While None,
            get_ss_matches() reports "association not run": ssMatches and
            isSSCandidate stay null.
        refcat : bool, optional
            Whether to cross-match detections against the field's
            reference-image catalog (see get_ref_matches). When False, or
            whenever a chip's catalog cannot be located/staged/parsed,
            matching reports "not run": refStarMatches and
            refGalaxyMatches stay null.

        Notes
        -----
        There is no differencing-algorithm argument. The release binds the
        difference-image role to one product, that product is what
        registered, and `diffimages.filename` names it — so the cutouts
        follow the row rather than a caller's opinion.
        """
        self.db = db
        self.kona_lookup = kona_lookup
        self.refcat_enabled = bool(refcat)
        # KONA predictions cache: one exposure's detections are processed
        # together, so cache the last expid's dict (None = no KONA data
        # for that exposure).
        self._kona_expid: int | None = None
        self._kona_predictions: dict[str, tuple] | None = None
        # Per-chip prefetch state, filled by iter_sources(pid). While the
        # current chip matches source.pid, get_object_for_source() and
        # get_prv_detections() answer from these dicts.
        self._chip_pid: int | None = None
        self._chip_objects: dict[int, dict[str, Any]] = {}  # sid -> astroobjects row dict
        self._chip_history: dict[int, list[Source]] = {}    # aid -> [Source, ...], oldest first
        self._chip_window_days = 0.0  # look-back window the prefetch covers
        # Full chip images for cutouts, loaded lazily by get_cutouts() and
        # held until a source from a different chip comes along. S3 files
        # are staged here before loading; constant product basenames mean
        # each chip's downloads replace the previous chip's files.
        self._images_pid: int | None = None
        self._images: dict[str, LoadedImage] = {}  # "diff"|"sci"|"ref" -> (pixels, header)
        self._staging_dir = tempfile.mkdtemp(prefix="rapid_cutouts_")
        self._s3: Any = None          # lazily built, retry-configured S3 client
        self._forced_phot_logged = False  # log the not-implemented note once
        # Reference-catalog cross-match state. The catalog is a per-
        # reference-image product shared by many chips, so it is staged and
        # parsed once per rfid (the parse/stage outcome is cached even when
        # it fails, so a broken catalog is not re-attempted per chip);
        # _chip_refmatches holds the current chip's per-sid results from
        # one vectorized match_refcat() pass (see _match_chip_refcat).
        self._refcat_rfid: int | None = None
        self._refcat: RefCatalog | None = None
        self._chip_refmatches: dict[
            int, tuple[list[RefMatch], list[RefMatch]]] = {}

    def _query(self, sql: str,
               params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        """Run one query on a short-lived cursor.

        Parameters
        ----------
        sql : str
            The query, with ``%s`` placeholders.
        params : tuple or list, optional
            Values for the placeholders.

        Returns
        -------
        list of dict
            One ``{column_name: value}`` dict per result row.
        """
        cur = self.db.conn.cursor()
        try:
            cur.execute(sql, params)
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
        finally:
            cur.close()

    def _partition_exists(self, field: int) -> bool:
        """Check that a field's merges/astroobjects partitions exist.

        Parameters
        ----------
        field : int
            Roman field identifier.

        Returns
        -------
        bool
            True if ``merges_<field>`` exists (a warning is logged when it
            does not).
        """
        rows = self._query("SELECT to_regclass(%s) AS reg",
                           (f"merges_{int(field)}",))
        exists = bool(rows) and rows[0]["reg"] is not None
        if not exists:
            logger.warning(
                "merges_%s does not exist; sources in field %s are "
                "treated as unassociated", int(field), field)
        return exists

    def resolve_pid(self, expid: int, sca: int) -> int:
        """Map (exposure, SCA) to the difference-image pid to alert on.

        One pid is one processing of one (exposure, SCA), but the mapping
        back is not unique: reprocessing campaigns leave several
        diffimages rows per (expid, sca), each with vbest=1 (older
        campaigns' flags are not cleared). Take the newest such row -- in
        practice only the newest campaign's pid has sources loaded.

        TODO: remove this workaround once the database standards for
        vbest/reprocessing are settled; vbest > 0 should then identify
        exactly one row per (expid, sca).

        Parameters
        ----------
        expid : int
            Exposure identifier (diffimages.expid).
        sca : int
            SCA number, 1-18 (diffimages.sca).

        Returns
        -------
        int
            The newest vbest>0 diffimages.pid for the pair.

        Raises
        ------
        ValueError
            If no vbest>0 difference image exists for the pair.
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

    def get_detection(self, sid: int) -> Source:
        """Fetch one detection by source ID.

        Parameters
        ----------
        sid : int
            Source ID (sources.sid).

        Returns
        -------
        Source
            The detection, with `aid` still None

        Raises
        ------
        ValueError
            If no source with this ID exists.
        KeyError
            If the sources row is missing an expected column (renamed or
            dropped in storage).
        """
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
        return Source.from_row(row, strict=True)

    def iter_sources(self, pid: int) -> Iterator[Source]:
        """Iterate over every detection on one difference image (chip).

        One SCA -> one difference image = one diffimages.pid. Fetches
        every detection with a single query, then prefetches the
        association and history rows. The per-source get_* calls then
        fill the data.

        Parameters
        ----------
        pid : int
            Processing ID of the difference image (diffimages.pid).

        Yields
        ------
        Source
            Each detection on the chip, in sid order.
        """
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
            sources.append(Source.from_row(row, strict=True))
        self._prefetch_chip(pid, sources)
        self._match_chip_refcat(pid, sources)
        yield from sources

    def _prefetch_chip(self, pid: int, sources: list[Source],
                       window_days: float = PRV_WINDOW_DAYS) -> None:
        """Caches an SCA's associations and histories.

        After this, the per-source get_* calls fill data from memory,
        unless the sid no longer matches the cache.

        Parameters
        ----------
        pid : int
            Processing ID of the chip (diffimages.pid).
        sources : list of Source
            Every detection on the chip (from iter_sources()).
        window_days : float, optional
            Look-back window for detection histories.
        """
        objects_by_sid = {}
        history_by_aid = {}
        # merges/astroobjects are partitioned by Roman field, and sources
        # near a field boundary can land in different partitions, so group
        # the sids by field first (usually a single group).
        for field in sorted({s.field for s in sources}):
            if not self._partition_exists(field):
                continue  # those sids stay unassociated
            sids = [s.sid for s in sources if s.field == field]
            # stdevra/stdevdec/nsources are STATISTICS' product and live on
            # astroobjectsmeta_<field>, not astroobjects_<field> (found
            # live, mission mock 2026-08-09: the first field whose
            # partitions existed failed every alert attempt with "column
            # a.stdevra does not exist"). Statistics runs after crossmatch,
            # so the meta table can legitimately not exist yet — LEFT JOIN
            # when it does, NULL stats when it does not: an object with no
            # computed statistics is still an association.
            meta_rows = self._query(
                "SELECT to_regclass(%s) AS reg",
                (f"astroobjectsmeta_{int(field)}",))
            meta_exists = bool(meta_rows) and meta_rows[0]["reg"] is not None
            # nsources maps to the schema's non-nullable nDiaSources; when
            # the meta row is absent it falls back to the aid's association
            # count in merges — the very thing statistics counts — while
            # stdevra/stdevdec map to nullable raErr/decErr and stay NULL.
            merge_count = (f"(SELECT count(*) FROM merges_{int(field)} m2 "
                           f"WHERE m2.aid = a.aid)::int")
            if meta_exists:
                stats_select = (f"am.stdevra, am.stdevdec, "
                                f"COALESCE(am.nsources, {merge_count}) "
                                f"AS nsources")
                stats_join = (f"LEFT JOIN astroobjectsmeta_{int(field)} am "
                              f"ON am.aid = a.aid")
            else:
                stats_select = (f"NULL::float8 AS stdevra, "
                                f"NULL::float8 AS stdevdec, "
                                f"{merge_count} AS nsources")
                stats_join = ""
            object_rows = self._query(f"""
                SELECT m.sid, a.aid, a.ra0, a.dec0,
                       {stats_select}
                FROM merges_{int(field)} m
                JOIN astroobjects_{int(field)} a ON m.aid = a.aid
                {stats_join}
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

    def get_object_for_source(self, detection: Source) -> ObjectRecord | None:
        """Associate a source with its AstroObject.

        Parameters
        ----------
        detection : Source
            The detection to resolve.

        Returns
        -------
        ObjectRecord or None
            A fresh ObjectRecord each call, or None for an unassociated
            detection -- the alert then has no diaObject.
        """
        # Batch flow: after iter_sources(pid), every association for the
        # chip is already in memory. An sid absent from the prefetch means
        # "no associated object" -- no fallback query needed.
        if self._chip_pid is not None and self._chip_pid == detection.pid:
            row = self._chip_objects.get(detection.sid)
            if row is None:
                return None
            return ObjectRecord.from_row(row, strict=True)

        # Single-alert flow: query for just this sid. Statistics live on
        # astroobjectsmeta_<field>, never astroobjects_<field> — the same
        # split the batch prefetch above handles (final convergence round,
        # 2026-08-09: this path still selected `a.*` and strict
        # ObjectRecord construction failed for every associated source).
        field = int(detection.field)
        if not self._partition_exists(field):
            return None  # no partition -> no association possible
        meta_rows = self._query(
            "SELECT to_regclass(%s) AS reg", (f"astroobjectsmeta_{field}",))
        meta_exists = bool(meta_rows) and meta_rows[0]["reg"] is not None
        merge_count = (f"(SELECT count(*) FROM merges_{field} m2 "
                       f"WHERE m2.aid = a.aid)::int")
        if meta_exists:
            stats_select = (f"am.stdevra, am.stdevdec, "
                            f"COALESCE(am.nsources, {merge_count}) "
                            f"AS nsources")
            stats_join = (f"LEFT JOIN astroobjectsmeta_{field} am "
                          f"ON am.aid = a.aid")
        else:
            stats_select = (f"NULL::float8 AS stdevra, "
                            f"NULL::float8 AS stdevdec, "
                            f"{merge_count} AS nsources")
            stats_join = ""
        rows = self._query(f"""
            SELECT m.aid, a.aid, a.ra0, a.dec0, {stats_select}
            FROM merges_{field} m
            JOIN astroobjects_{field} a ON m.aid = a.aid
            {stats_join}
            WHERE m.sid = %s
        """, (detection.sid,))
        if not rows:
            return None  # unassociated detection -> alert has no diaObject
        return ObjectRecord.from_row(rows[0], strict=True)

    def get_prv_detections(self, detection: Source, obj: ObjectRecord,
                           window_days: float = PRV_WINDOW_DAYS) -> list[Source]:
        """Fetch an object's previous detections within the lookback window.

        These become the alert's prvDiaSources.

        Parameters
        ----------
        detection : Source
            The triggering detection.
        obj : ObjectRecord
            The object the trigger is associated with.
        window_days : float, optional
            Look-back window before the trigger's mjdobs.

        Returns
        -------
        list of Source
            The object's previous detections, oldest first; empty when
            there are none.
        """
        # Batch flow: filter this object's prefetched history down to this
        # trigger's window. Only usable when the prefetch covered at least
        # as long a look-back window as requested.
        if (self._chip_pid is not None and self._chip_pid == detection.pid
                and window_days <= self._chip_window_days):
            cutoff = detection.mjdobs - window_days
            # Strict prior (ruled 2026-08-13): history is s.mjdobs <
            # detection.mjdobs only. A same-instant detection is not
            # history, and backfill/reprocessing can otherwise leave
            # later detections in the prefetch -- exclude both rather
            # than relying on sid inequality alone.
            return [s for s in self._chip_history.get(obj.aid, [])
                    if s.sid != detection.sid and cutoff <= s.mjdobs < detection.mjdobs]

        # Single-alert flow: same sources -> Source mapping as
        # get_detection(), but selecting the object's other detections.
        field = int(detection.field)
        if not self._partition_exists(field):
            return []  # no partition -> no recorded history
        # Strict prior (ruled 2026-08-13): history is s.mjdobs <
        # detection.mjdobs only. A same-instant detection is not
        # history, and backfill/reprocessing can otherwise leave later
        # detections in `sources` -- exclude both rather than relying
        # on sid inequality alone.
        rows = self._query(f"""
            SELECT s.*, f.filter AS filter_name, e.exptime
            FROM sources s
            JOIN merges_{field} m ON s.sid = m.sid
            JOIN filters f ON s.fid = f.fid
            JOIN exposures e ON s.expid = e.expid
            WHERE m.aid = %s AND s.sid != %s
              AND s.mjdobs >= %s AND s.mjdobs < %s
            ORDER BY s.mjdobs
        """, (obj.aid, detection.sid, detection.mjdobs - window_days,
              detection.mjdobs))
        detections = []
        for row in rows:
            row["band"] = row.get("filter_name")
            row["aid"] = obj.aid  # known from the join; save a lookup
            detections.append(Source.from_row(row, strict=True))
        return detections

    def get_forced_photometry(self, detection: Source,
                              obj: ObjectRecord) -> list[ForcedPhot]:
        """Fetch the forced-photometry history at an object position (STUB).

        Forced photometry in RAPID produces FITS files, not DB records;
        integration with alert packets is not yet implemented.

        Parameters
        ----------
        detection : Source
            The triggering detection.
        obj : ObjectRecord
            The object whose position the photometry was forced at.

        Returns
        -------
        list of ForcedPhot
            Always empty for now, so prvDiaForcedSources serializes null.
        """
        # Log once per provider, not once per source -- a batch run
        # reaches here tens of thousands of times.
        if not self._forced_phot_logged:
            logger.info(
                "Forced photometry not yet available for alert assembly")
            self._forced_phot_logged = True
        return []

    def get_ss_matches(self, detection: Source) -> list[SSMatch] | None:
        """Associate a detection with nearby known solar system objects.

        Matches the exposure's KONA predictions against the detection
        position (see match_ss_predictions) and sets
        ``detection.is_ss_candidate`` as a side effect: True when the
        nearest match is within SS_CANDIDATE_SEP_ARCSEC, False when the
        association ran clean, None when it could not run.

        Parameters
        ----------
        detection : Source
            The triggering detection; modified in place (is_ss_candidate).

        Returns
        -------
        list of SSMatch or None
            Nearest predictions within SS_MATCH_RADIUS_ARCSEC (at most
            SS_MATCH_NMAX, nearest first); empty list when the association
            ran and found nothing; None when there is no KONA data for the
            exposure (no kona_lookup configured, or it returned None).
        """
        if self.kona_lookup is None:
            detection.is_ss_candidate = None
            return None
        if self._kona_expid != detection.expid:
            self._kona_predictions = self.kona_lookup(detection.expid)
            self._kona_expid = detection.expid
        predictions = self._kona_predictions
        if predictions is None:
            detection.is_ss_candidate = None
            return None
        matches = match_ss_predictions(detection.ra, detection.dec,
                                       predictions)
        detection.is_ss_candidate = bool(
            matches and matches[0].sep <= SS_CANDIDATE_SEP_ARCSEC)
        return matches

    def get_cutouts(self, detection: Source) -> Cutouts:
        """Cut the three image stamps around a detection's position.

        Cutouts are generated on the fly: stamps sliced out of the chip's
        full difference/science/template images at the source position
        The images are loaded once per chip and reused for every source
        on it, in both the batch and single-alert flows.

        Parameters
        ----------
        detection : Source
            The detection to cut stamps around.

        Returns
        -------
        Cutouts
            The three stamps as FITS bytes; any stamp whose image is
            missing, unreadable, or off-grid is None.
        """
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

    def _stage(self, url: str, required: bool = True,
               retries: int = 5) -> str | None:
        """Cache a product file locally, retrying transient S3 failures.

        An ``s3://`` URL is downloaded into the staging directory; a plain
        path is returned untouched (tests and future non-AWS backends).
        Transient download failures are retried with exponential backoff
        (STAGE_BACKOFF_BASE_S), and the download is size-checked against
        the object's ContentLength so a truncated transfer is retried too.

        On a definitive error (404/403) or exhausted retries, behavior
        depends on `required`: a required file (the cutout images) raises
        CutoutStagingError so the chip aborts rather than silently
        shipping null cutouts; an optional file (e.g. the auxiliary
        catalog) logs a warning and returns None so its cross-reference is
        simply skipped.

        Parameters
        ----------
        url : str
            An ``s3://`` URL or a plain filesystem path.
        required : bool, default True
            Whether a retrieval failure aborts (True) or is skipped (False).
        retries : int, default 5
            Maximum number of download attempts before giving up. Each
            attempt additionally gets boto3's own standard-mode HTTP-level
            retries underneath.

        Returns
        -------
        str or None
            The local path, or None only when `required` is False and the
            file could not be retrieved.

        Raises
        ------
        CutoutStagingError
            If a required ``s3://`` file cannot be retrieved.
        """
        if not url.startswith("s3://"):
            return url
        parts = urlparse(url)
        bucket, key = parts.netloc, parts.path.lstrip("/")
        local = os.path.join(self._staging_dir, os.path.basename(url))

        def fail(reason: str, cause: Exception | None) -> None:
            if required:
                raise CutoutStagingError(reason) from cause
            logger.warning("%s; skipping (optional)", reason)
            return None

        # deferred imports so non-AWS providers/tests don't need boto3
        import boto3
        import botocore.exceptions
        from botocore.config import Config
        if self._s3 is None:
            # "standard" retry mode also retries connection errors, which the
            # default "legacy" mode does not -- our original outage was one
            self._s3 = boto3.client(
                "s3", config=Config(retries={"mode": "standard"}))

        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                self._s3.download_file(bucket, key, local)
                expected = self._s3.head_object(
                    Bucket=bucket, Key=key)["ContentLength"]
                actual = os.path.getsize(local)
                if actual != expected:
                    raise OSError(f"truncated download: {actual} of "
                                    f"{expected} bytes")
                return local
            except botocore.exceptions.ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code in ("404", "NoSuchKey", "403", "AccessDenied"):
                    return fail(
                        f"cutout image not retrievable [{code}]: {url}", exc)
                last_exc = exc
            except Exception as exc:
                last_exc = exc
            if attempt < retries:
                wait = STAGE_BACKOFF_BASE_S * 2 ** (attempt - 1)
                logger.warning(
                    "staging %s failed (attempt %d/%d): %s; retrying in %.0fs",
                    url, attempt, retries, last_exc, wait)
                time.sleep(wait)
        return fail(
            f"failed to stage {url} after {retries} attempts", last_exc)

    def registered_difference_image(self, pid: int) -> str | None:
        """The basename of the difference image registered for this chip.

        The provider's answer to "which difference image am I cutting
        from" — for run identity in reports and benchmarks, which must
        name what was actually read rather than what a caller asked for.
        ``None`` when the chip has no registered difference image.
        """
        rows = self._query(
            "SELECT filename FROM diffimages WHERE pid = %s", (pid,))
        return os.path.basename(rows[0]["filename"]) if rows else None

    def _chip_images(self, pid: int) -> dict[str, LoadedImage]:
        """Load (and cache) the chip's three full cutout-source images.

        diffimages.filename IS the difference image — the one the release
        bound to the difference-image role and registration recorded — and
        it also locates the job directory holding the two co-gridded
        companions. Staged and loaded on first use, held until a different
        chip is asked for.

        Parameters
        ----------
        pid : int
            Processing ID of the chip (diffimages.pid).

        Returns
        -------
        dict
            ``{"diff" | "sci" | "ref": (pixels, header)}``.

        Raises
        ------
        CutoutStagingError
            If the pid has no diffimages row, or any of the three cutout
            images cannot be staged or read. Every image is required: one
            missing/unreadable file would null that cutout for every
            source on the chip, so we abort the chip loudly instead (see
            CutoutStagingError). A wrong-grid image is a separate case,
            handled by _check_grids_match.
        """
        if self._images_pid == pid:
            return self._images

        rows = self._query(
            "SELECT filename FROM diffimages WHERE pid = %s", (pid,))
        if not rows:
            raise CutoutStagingError(
                f"no diffimages row for pid={pid}; cannot locate the job "
                f"directory for cutouts")

        # Get registered difference image
        registered = rows[0]["filename"]
        job_dir = os.path.dirname(registered)
        # The registered difference image itself — the role-bound one —
        # not a basename substituted for it.
        names = {"diff": os.path.basename(registered),
                 "sci": CUTOUT_FILES["sci"], "ref": CUTOUT_FILES["ref"]}
        images = {key: load_fits_image(self._stage(f"{job_dir}/{name}"))
                  for key, name in names.items()}
        unreadable = [key for key, (pixels, _) in images.items()
                      if pixels is None]
        if unreadable:
            raise CutoutStagingError(
                f"pid={pid}: cutout image(s) {unreadable} missing or "
                f"unreadable in {job_dir}; aborting chip")

        self._images = images
        self._images_pid = pid
        self._check_grids_match(pid)
        return self._images

    #TODO: I think we can assume this instead of checking in production
    def _check_grids_match(self, pid: int) -> None:
        """Drop cached images whose pixel grid differs from the diff image.

        Cutout positions assume the three images share one pixel grid.
        Verify it from the loaded WCS headers and drop (null) any image on
        a different grid rather than emit a cutout of the wrong sky
        position. Tolerances allow the header-writing rounding differences
        between the products (~1e-10 deg).

        Parameters
        ----------
        pid : int
            Processing ID of the chip, for the warning message only.
        """
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
                # Any: FITSHDR.get is untyped (fitsio ships no type info);
                # with the np.nan default these cards are always numeric
                val: Any = header.get(card, np.nan)
                ref: Any = diff_header.get(card, np.nan)
                if np.isclose(float(val), float(ref),
                              rtol=1e-6, atol=1e-8):
                    continue
                logger.warning(
                    "pid=%s: %s image grid differs from the difference "
                    "image (%s: %r vs %r); its cutouts will be null",
                    pid, key, card, header.get(card),
                    diff_header.get(card))
                self._images[key] = (None, None)
                break

    # -- Reference-catalog cross-match --------------------------------------

    def _load_refcat(self, rfid: int) -> RefCatalog | None:
        """Stage and parse (and cache) a reference image's catalog.

        Parameters
        ----------
        rfid : int
            The reference image (refimages.rfid) whose mosaic SExtractor
            catalog (refimcatalogs, REFCAT_CATTYPE) to load.

        Returns
        -------
        RefCatalog or None
            The parsed catalog, or None when it is not registered, cannot
            be staged, or does not parse -- each with a logged warning.
            The outcome (including None) is cached per rfid, so a broken
            catalog is not re-attempted for every chip that shares it.
        """
        if self._refcat_rfid == rfid:
            return self._refcat
        self._refcat = None
        self._refcat_rfid = rfid
        rows = self._query("""
            SELECT filename FROM refimcatalogs
            WHERE rfid = %s AND cattype = %s
            ORDER BY rfcatid DESC
        """, (rfid, REFCAT_CATTYPE))
        if not rows:
            logger.warning(
                "no refimcatalogs row (cattype=%s) for rfid=%s; "
                "reference-catalog matching not run", REFCAT_CATTYPE, rfid)
            return None
        if len(rows) > 1:
            logger.warning(
                "rfid=%s has %d cattype=%s refimcatalogs rows; using the "
                "newest", rfid, len(rows), REFCAT_CATTYPE)
        # optional product: a staging failure skips matching, never aborts
        path = self._stage(rows[0]["filename"], required=False)
        if path is not None:
            self._refcat = load_refcat(path)
        return self._refcat

    def _refcat_for_pid(self, pid: int) -> RefCatalog | None:
        """Locate, stage, and parse the reference catalog behind a chip."""
        rows = self._query(
            "SELECT rfid FROM diffimages WHERE pid = %s", (pid,))
        if not rows or rows[0]["rfid"] is None:
            logger.warning("no diffimages.rfid for pid=%s; "
                           "reference-catalog matching not run", pid)
            return None
        return self._load_refcat(int(rows[0]["rfid"]))

    def _match_chip_refcat(self, pid: int, sources: list[Source]) -> None:
        """Cross-match every chip detection against the reference catalog.

        One vectorized match_refcat() pass over the whole chip; the
        per-sid results land in _chip_refmatches, which get_ref_matches()
        answers from. Left empty when matching is disabled or the catalog
        is unavailable, so get_ref_matches() reports "not run".

        Parameters
        ----------
        pid : int
            Processing ID of the chip (diffimages.pid).
        sources : list of Source
            Every detection on the chip (from iter_sources()).
        """
        self._chip_refmatches = {}
        if not self.refcat_enabled or not sources:
            return
        catalog = self._refcat_for_pid(pid)
        if catalog is None:
            return
        results = match_refcat(np.array([s.ra for s in sources]),
                               np.array([s.dec for s in sources]), catalog)
        self._chip_refmatches = dict(zip((s.sid for s in sources), results))

    def get_ref_matches(
            self, detection: Source,
    ) -> tuple[list[RefMatch], list[RefMatch]] | None:
        """The nearest reference-catalog stars and galaxies to a detection.

        These become the alert's refStarMatches and refGalaxyMatches
        arrays. In the batch flow the whole chip was already matched in
        one pass (see _match_chip_refcat); the single-alert flow matches
        just this detection against the (cached per rfid) catalog.

        Parameters
        ----------
        detection : Source
            The triggering detection.

        Returns
        -------
        (list of RefMatch, list of RefMatch) or None
            Star and galaxy matches within REF_MATCH_RADIUS_ARCSEC (at
            most REF_MATCH_NMAX each, nearest first); empty lists when
            matching ran and found nothing nearby; None when it could not
            run (disabled, or the catalog is unavailable).
        """
        if not self.refcat_enabled:
            return None
        # Batch flow: the chip's results (or the fact that matching could
        # not run: sid absent) are already in memory.
        if self._chip_pid is not None and self._chip_pid == detection.pid:
            return self._chip_refmatches.get(detection.sid)
        catalog = self._refcat_for_pid(detection.pid)
        if catalog is None:
            return None
        return match_refcat(detection.ra, detection.dec, catalog)[0]
