"""The alert's cross-matches, ported from `dev`'s ``alerts/providers.py``.

Copied from `origin/dev` unchanged: the solar-system association against
KONA predictions (``match_ss_predictions``, L578-647), the reference-catalog
match (``load_refcat``/``match_refcat``, L668-855), and the NED matcher
(``select_host_candidates`` to ``match_nedcat``, L881-1254). Pure functions
over positions and column arrays; nothing here reaches a database.

NED access (lead's ruling, 2026-09-24: off by default, when on "use
astroquery as dev"): :class:`AstroqueryNedReader` and
:func:`ned_table_to_columns` are the astroquery reader as the `rebuild`
branch's copy of `dev`'s ``alerts/providers.py`` carries it. `origin/dev`
itself replaced that reader on 2026-09-23 with a local HATS copy of NED
(``alerts/ned_reader.py``), which is not ported. ``astroquery`` is imported
only when a reader is called, never at import and never in tests.

KONA: the stage loads `dev`'s nightly predictions JSON
(``{expid: {designation: [ra, dec, vmag]}}``, `dev`'s
``alerts/cli.py:load_kona_predictions``) only when ``[alerts] kona_file`` is
set; ``modules/solarsystem/rapid_kona.py`` is never imported.
"""

import logging
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from rapidpipe.science.alerts.cutouts import STAMP_HALF_WIDTH
from rapidpipe.science.alerts.records import NedMatch, RefMatch, SSMatch

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Solar-system association (KONA)
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
REFCAT_STAR_MIN_CLASS = 0.5
REFCAT_GALAXY_MAX_CLASS = 0.5

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
# NED cross-match
#
# Associates each detection with candidate host galaxies from NED
# (NASA/IPAC Extragalactic Database). Complements the reference-catalog
# match above: that one answers "is there a source here in OUR reference
# image", this one answers "is this a known extragalactic object, and what
# is its redshift".
#
# Catalog access is behind the NedSliceReader callable rather than wired to
# one backend: production reads the local copy of the object directory
# (alerts/ned_reader.py, one cone per chip); v1 used the NED web service,
# removed 2026-09-23. The matcher only ever sees column arrays. Geometry is
# identical to the reference-catalog match.
# ---------------------------------------------------------------------------

# Maximum separation for a reported match. Larger than
# REF_MATCH_RADIUS_ARCSEC because NED positions are heterogeneous (the
# preferred position comes from whichever catalog supplied the preferred
# name) and a transient sits at a physical offset from its host's
# catalogued center. TODO: tune with the shifted-position null test (offset
# every detection by +60" in Dec and count matches = the empirical
# false-association rate); see alerts/scratch/ned-crossmatch-design-notes.md
NED_MATCH_RADIUS_ARCSEC = 10.0

# Keep at most this many matches, nearest first.
NED_MATCH_NMAX = 3

# Column names the matcher works in, independent of access path; a reader
# maps its backend's names onto these (the NED web service and the HATS
# object directory disagree: "Object Name" vs "prefname", "Redshift Flag"
# vs "zflag"). Readers must deliver missing numerics as NaN and missing
# strings as None. Only prefname/ra/dec are required; the rest are filled
# with nulls when a backend lacks them.
NED_COLUMNS = ("prefname", "ra", "dec", "ptype", "z", "zunc", "zflag")
NED_REQUIRED_COLUMNS = ("prefname", "ra", "dec")

# A NED backend: (ra_deg, dec_deg, radius_arcsec) -> column arrays keyed by
# NED_COLUMNS for every NED object in the cone, or None when the slice
# could not be obtained (service unreachable, position outside a local
# copy's coverage). Raising is treated the same as returning None. An
# empty table is NOT None: it means "NED has nothing here", which matches
# to [] rather than to "not run".
NedSliceReader = Callable[[float, float, float], "dict[str, Any] | None"]

# Numerical slack added to every query cone so a NED object exactly at the
# match radius from the outermost detection is still inside the slice.
NED_CONE_SLACK_ARCSEC = 1.0

# Largest query cone one chip may issue. A Roman SCA is ~7.5' across (half-
# diagonal ~318"), so every detection that is actually on the chip lies
# within ~330" of the chip centre; a position farther out belongs to a
# detection whose fitted pixel position is off the array (PhotUtils off-image
# fits, flags bit 2) with sky coordinates extrapolated through the WCS.
# Measured 2026-09-15 on pid 338173: 2 of 25,836 detections sat 0.8-1.6 deg
# off (xfit=22834, yfit=27838 on a 4088-pixel chip) and stretched the cone to
# 98' -- a 1.6-degree NED query that timed out and cost every detection on
# the chip its matches. Detections beyond this radius are excluded from the
# cone and reported as "not run" (None), never as [].
NED_CONE_MAX_ARCSEC = 400.0


# ===========================================================================
# HOST-CANDIDATE SELECTION -- the main tunable of this cross-match.
# Change it in select_host_candidates() and nowhere else; the matcher, the
# provider and the schema all route through that one function. Set
# NED_SELECTION_ENABLED = False to match against all of NED.
#
# Why: NED's object directory is not a galaxy catalog, it is every source
# every ingested survey reported. Measured on the HLTDS-like field
# 2026-09-14 (3.88' cone, 566 objects): IrS 482 (85%), UvS 67 (12%),
# G 12 (2%), RadioS 3, SN 2. Chance-coincidence rho*pi*r^2 per detection
# at 10": 1.04 unselected (a spurious "host" on essentially every alert)
# vs 0.022 selected -- ~47x purity. Matching that by radius instead would
# need ~1.5", far too tight for host association. The two are not
# interchangeable levers. The excluded rows are not worthless, but "a
# survey detected something here" is already answered, from our own deeper
# mosaic, by refGalaxyMatches.
#
# Every match still carries its own `type`, and the schema records that a
# selection was applied, so consumers can re-cut downstream.
#
# SETTLED 2026-09-22: the local copy and NED's TAP table agree -- ptype /
# prefphytype is empty for 96-99% of rows; the web service's populated
# "Type" (IrS/UvS...) was the outlier. The measurements above were taken
# through the web service, so the type mix they quote is not what this
# code now sees; the row counts and chance-coincidence rates still hold.
# The rule is to be re-evaluated on the local copy (selection is OFF).
# ===========================================================================

# Set False to disable selection entirely and match against all of NED.
#
# DISABLED 2026-09-18 (Emily), pending a re-evaluation against the local
# HATS copy of NED (release 36.1_20260527_v2): in the two leaf files
# inspected, 96-99% of rows carry an empty ptype, so the allowlist-or-
# redshift rule keeps only 0.2-2.5% of NED. That is the same cut the rule
# made on the astroquery path (566 -> 12 on the HLTDS field), where the
# excluded rows arrived typed IrS/UvS instead of empty -- but the rule is
# to be rewritten rather than trusted as is. Cost while off, measured
# 2026-09-14: ~1 chance-coincidence match per detection at 10", so
# nedMatches is populated on essentially every alert and the emitted
# `type` is what tells a host from a catalogue detection. NED_MATCH_NMAX
# (3) caps the volume.
NED_SELECTION_ENABLED = False

# NED preferred types accepted as candidate hosts. Excludes GGroup/GClstr
# (system centroids, degree-scale), PofG (a knot inside a galaxy that is
# itself catalogued), stellar and "!"-prefixed Galactic types, and the
# wavelength-domain types IrS/UvS/RadioS/XrayS/GammaS/VisS (position
# uncertainties of 10"-1000").
NED_HOST_TYPES = frozenset({"G", "GPair", "GTrpl", "G_Lens", "QSO"})

# Escape hatch against the completeness cut: an untyped or IrS-typed entry
# carrying a redshift is almost certainly a real galaxy NED has not
# classified, and at Roman depth faint hosts are disproportionately the
# unclassified ones. Cheap, because redshifts are rare among those rows.
# Tighten to spectroscopic only by also requiring zflag[0] == "S".
NED_KEEP_ANY_TYPE_WITH_REDSHIFT = True


def select_host_candidates(ptype: Any, z: Any) -> np.ndarray:
    """Boolean mask of NED rows to treat as candidate host galaxies.

    The single point of change for which NED objects can become a match;
    see the comment block above for the measurements behind it.

    Parameters
    ----------
    ptype : array-like
        NED preferred object type per row; None/"" for unclassified.
    z : array-like
        NED preferred redshift per row; NaN where absent.

    Returns
    -------
    numpy.ndarray of bool
        True for rows eligible to be matched. All True when
        NED_SELECTION_ENABLED is False.
    """
    ptype = np.asarray(ptype, dtype=object)
    z = np.asarray(z, dtype=float)

    if not NED_SELECTION_ENABLED:
        return np.ones(ptype.shape, dtype=bool)

    # Explicit loop over a frozenset: row counts are hundreds, and `in`
    # handles None/"" without the dtype games np.isin would need.
    keep = np.array([p in NED_HOST_TYPES for p in ptype], dtype=bool)
    if NED_KEEP_ANY_TYPE_WITH_REDSHIFT:
        keep |= np.isfinite(z)
    return keep


def bounding_cone(ra: Any, dec: Any,
                  pad_arcsec: float) -> tuple[float, float, float]:
    """The smallest query cone that covers every match of a set of positions.

    Centre is the normalized mean unit vector of the positions; radius is
    the largest separation from that centre plus `pad_arcsec`. Any object
    within `pad_arcsec` of any input position lies inside the cone. Used to
    fetch one NED slice per chip instead of one per detection: a Roman SCA
    is ~7.5' across, so the cone is ~5' and the slice a few hundred rows.

    Parameters
    ----------
    ra, dec : array-like
        Positions, ICRS [deg].
    pad_arcsec : float
        Added to the radius; the match radius plus slack.

    Returns
    -------
    (ra_deg, dec_deg, radius_arcsec)
    """
    ra_r = np.radians(np.atleast_1d(np.asarray(ra, dtype=float)))
    dec_r = np.radians(np.atleast_1d(np.asarray(dec, dtype=float)))
    xyz = np.stack([np.cos(dec_r) * np.cos(ra_r),
                    np.cos(dec_r) * np.sin(ra_r),
                    np.sin(dec_r)])
    mean = xyz.mean(axis=1)
    mean /= np.linalg.norm(mean)
    # largest angular distance from the centre to any position
    cos_sep = np.clip(mean @ xyz, -1.0, 1.0)
    max_sep_arcsec = float(np.degrees(np.arccos(cos_sep).max())) * 3600.0
    ra0 = float(np.degrees(np.arctan2(mean[1], mean[0])) % 360.0)
    dec0 = float(np.degrees(np.arcsin(mean[2])))
    return ra0, dec0, max_sep_arcsec + pad_arcsec


def _sep_arcsec(ra: np.ndarray, dec: np.ndarray,
                ra0: float, dec0: float) -> np.ndarray:
    """Angular separation of positions from (ra0, dec0) [arcsec]."""
    ra_r, dec_r = np.radians(ra), np.radians(dec)
    ra0_r, dec0_r = np.radians(ra0), np.radians(dec0)
    cos_sep = (np.sin(dec_r) * np.sin(dec0_r)
               + np.cos(dec_r) * np.cos(dec0_r) * np.cos(ra_r - ra0_r))
    return np.degrees(np.arccos(np.clip(cos_sep, -1.0, 1.0))) * 3600.0


def chip_cone(ra: Any, dec: Any, pad_arcsec: float,
              max_arcsec: float = NED_CONE_MAX_ARCSEC,
              ) -> tuple[float, float, float, np.ndarray]:
    """bounding_cone() with a limit on how far one chip's cone may reach.

    A detection whose sky position is not on the chip (see
    NED_CONE_MAX_ARCSEC) would otherwise stretch the cone to cover it, and
    a single such row can turn a 5' query into a degree-scale one. When the
    all-inclusive cone exceeds `max_arcsec`, positions farther than
    ``max_arcsec - pad_arcsec`` from the chip centre are dropped and the
    cone rebuilt from the rest. The centre used for that cut is a
    componentwise-median unit vector, not the mean: with few detections a
    mean is pulled toward the outlier far enough to misclassify the real
    ones. Callers must treat dropped positions as "not run", never as
    "no match".

    Parameters
    ----------
    ra, dec : array-like
        Detection positions, ICRS [deg].
    pad_arcsec : float
        Added to the radius; the match radius plus slack.
    max_arcsec : float, optional
        Radius above which the clamp engages.

    Returns
    -------
    (ra_deg, dec_deg, radius_arcsec, inliers)
        `inliers` is a boolean mask over the input positions; all True
        when no clamping was needed, all False when no position survived
        (nothing to query).
    """
    ra = np.atleast_1d(np.asarray(ra, dtype=float))
    dec = np.atleast_1d(np.asarray(dec, dtype=float))
    ra0, dec0, radius = bounding_cone(ra, dec, pad_arcsec)
    inliers = np.ones(ra.size, dtype=bool)
    if radius <= max_arcsec:
        return ra0, dec0, radius, inliers

    ra_r, dec_r = np.radians(ra), np.radians(dec)
    xyz = np.stack([np.cos(dec_r) * np.cos(ra_r),
                    np.cos(dec_r) * np.sin(ra_r),
                    np.sin(dec_r)])
    med = np.median(xyz, axis=1)
    med /= np.linalg.norm(med)
    med_ra = float(np.degrees(np.arctan2(med[1], med[0])) % 360.0)
    med_dec = float(np.degrees(np.arcsin(med[2])))
    inliers = _sep_arcsec(ra, dec, med_ra, med_dec) <= max_arcsec - pad_arcsec
    if not inliers.any():
        return ra0, dec0, radius, inliers
    ra0, dec0, radius = bounding_cone(ra[inliers], dec[inliers], pad_arcsec)
    return ra0, dec0, radius, inliers


@dataclass
class NedCatalog:
    """One sky slice of NED, reduced to candidate hosts and ready to match.

    Built by build_nedcat(). `columns` holds NED_COLUMNS over the KEPT rows
    only -- selection is applied at build time, so the matcher never sees
    the rest. `coords` is one astropy SkyCoord over those rows (astropy
    caches the KD-tree on it, so every detection on a chip reuses one
    tree), or None when no rows survived -- which matches to [] rather
    than to "not run".
    """
    columns: dict[str, np.ndarray]
    coords: Any                   # SkyCoord over the kept rows, or None
    n_input: int = 0              # rows before selection, for logging


def build_nedcat(table: dict[str, Any]) -> NedCatalog | None:
    """Apply the host-candidate selection and build the match tree.

    Parameters
    ----------
    table : dict
        Column arrays keyed by NED_COLUMNS. prefname/ra/dec required;
        ptype/z/zunc/zflag filled with nulls when the backend lacks them.

    Returns
    -------
    NedCatalog or None
        None (with a logged warning) when required columns are missing --
        the cross-match then degrades to "not run", not to "no matches".
    """
    from astropy import units as u
    from astropy.coordinates import SkyCoord

    missing = [c for c in NED_REQUIRED_COLUMNS if c not in table]
    if missing:
        logger.warning("NED slice is missing required columns %s; "
                       "cross-match skipped", missing)
        return None

    n_input = len(np.asarray(table["prefname"], dtype=object))
    columns: dict[str, np.ndarray] = {}
    for name in NED_COLUMNS:
        numeric = name in ("ra", "dec", "z", "zunc")
        if name in table:
            values = table[name]
        else:
            values = np.full(n_input, np.nan if numeric else None,
                             dtype=float if numeric else object)
        columns[name] = np.asarray(values, dtype=float if numeric else object)

    keep = select_host_candidates(columns["ptype"], columns["z"])
    columns = {name: values[keep] for name, values in columns.items()}
    n_kept = int(keep.sum())
    logger.debug("NED slice: %d rows, %d candidate hosts kept",
                 n_input, n_kept)

    coords = (SkyCoord(columns["ra"] * u.deg, columns["dec"] * u.deg)
              if n_kept else None)
    return NedCatalog(columns=columns, coords=coords, n_input=n_input)


def _ned_match_from_row(columns: dict[str, np.ndarray], row: int,
                        sep: float, pa: float) -> NedMatch:
    """Build one NedMatch from slice row `row` at the given sep/PA.

    Absent values become None, not NaN or "": the alert schema types
    z/zunc/type/zflag as nullable unions, and a NaN would serialize as a
    number that is not one.
    """
    def text(name: str) -> str | None:
        value = columns[name][row]
        return (str(value).strip() or None) if value is not None else None

    def number(name: str) -> float | None:
        value = float(columns[name][row])
        return value if np.isfinite(value) else None

    return NedMatch(
        prefname=str(columns["prefname"][row]),
        ra=float(columns["ra"][row]), dec=float(columns["dec"][row]),
        sep=sep, pa=pa,
        ptype=text("ptype"), z=number("z"),
        zunc=number("zunc"), zflag=text("zflag"),
    )


def match_nedcat(ra: Any, dec: Any, catalog: NedCatalog,
                 radius_arcsec: float = NED_MATCH_RADIUS_ARCSEC,
                 n_max: int = NED_MATCH_NMAX,
                 ) -> list[list[NedMatch]]:
    """Match detection positions against a NED slice.

    Same vectorized nearest-N strategy as match_refcat(), with one tree
    instead of a star/galaxy pair: the nth-nearest candidate host of every
    detection is queried for n = 1..n_max, then matches beyond
    `radius_arcsec` are dropped -- nthneighbor always returns something, so
    the radius is a mask on the result, not a query parameter. A result of
    length n_max therefore means the neighborhood may extend beyond what is
    reported.

    Parameters
    ----------
    ra, dec : float or array-like
        Detection position(s), ICRS [deg].
    catalog : NedCatalog
        The slice from build_nedcat().
    radius_arcsec : float, optional
        Maximum separation to report.
    n_max : int, optional
        Keep at most this many matches, nearest first.

    Returns
    -------
    list of list of NedMatch
        Per detection, in input order, nearest-first and at most `n_max`
        long. An empty list means "no candidate host within the radius";
        "could not run" is signalled by the provider, not here.
    """
    from astropy import units as u
    from astropy.coordinates import SkyCoord

    ra = np.atleast_1d(np.asarray(ra, dtype=float))
    dec = np.atleast_1d(np.asarray(dec, dtype=float))
    results: list[list[NedMatch]] = [[] for _ in range(ra.size)]

    coords = catalog.coords
    if coords is None:
        return results  # no candidate hosts in this slice

    src = SkyCoord(ra * u.deg, dec * u.deg)
    n_rows = len(catalog.columns["prefname"])
    # ascending nthneighbor keeps each result list nearest-first
    for n in range(1, min(n_max, n_rows) + 1):
        idx, sep2d, _ = src.match_to_catalog_sky(coords, nthneighbor=n)
        pa = src.position_angle(coords[idx])
        idx = np.atleast_1d(idx)
        sep_arcsec = np.atleast_1d(sep2d.arcsec)
        pa_deg = np.atleast_1d(pa.deg) % 360.0
        for i in np.flatnonzero(sep_arcsec <= radius_arcsec):
            results[i].append(_ned_match_from_row(
                catalog.columns, int(idx[i]),
                float(sep_arcsec[i]), float(pa_deg[i])))
    return results


# -- NED backend (astroquery), as the rebuild branch's copy of dev carries it --

# NED web-service column names -> NED_COLUMNS.
ASTROQUERY_NED_COLUMNS = {
    "prefname": "Object Name",
    "ra": "RA",
    "dec": "DEC",
    "ptype": "Type",
    "z": "Redshift",
    "zflag": "Redshift Flag",
}


def ned_table_to_columns(table: Any) -> dict[str, np.ndarray]:
    """Convert an astroquery NED result table to NED_COLUMNS arrays.

    Masked cells (astroquery returns a masked Table) become NaN in the
    numeric columns and None in the string columns, which is the contract
    build_nedcat() and _ned_match_from_row() rely on. Columns the web
    service lacks (zunc) are filled with NaN. Pure function so the mapping
    is testable without a network.

    Parameters
    ----------
    table : astropy.table.Table
        As returned by ``astroquery.ipac.ned.Ned.query_region``.

    Returns
    -------
    dict
        NED_COLUMNS -> array, all the same length (possibly zero).
    """
    n = len(table)
    out: dict[str, np.ndarray] = {}
    for name in NED_COLUMNS:
        numeric = name in ("ra", "dec", "z", "zunc")
        src = ASTROQUERY_NED_COLUMNS.get(name)
        if src is None or src not in table.colnames:
            out[name] = (np.full(n, np.nan)
                         if numeric else np.full(n, None, dtype=object))
            continue
        col = table[src]
        mask = np.ma.getmaskarray(col)
        data = np.ma.getdata(col)
        if numeric:
            values = np.asarray(data, dtype=float)
            values[mask] = np.nan
        else:
            values = np.array([None if m else str(v)
                               for v, m in zip(data, mask)], dtype=object)
        out[name] = values
    return out


class AstroqueryNedReader:
    """A NedSliceReader over the NED web service (astroquery).

    One HTTP cone search per call. Sized for one call per chip: a Roman
    SCA needs a ~5.3' cone, which returned ~1100 rows in ~12 s on the
    HLTDS-like field (2026-09-14); a ~3.9' cone took ~3 s. Exceptions
    propagate -- the provider treats them as "could not run".

    Known limits of this path, accepted for v1 (see
    alerts/scratch/ned-crossmatch-design-notes.md section 3): results
    are not reproducible across NED releases, an outage leaves nedMatches
    null for the affected chips, and many concurrent jobs are a burst
    against a shared production service. A local HATS copy replaces this
    class without touching the matcher.

    Parameters
    ----------
    timeout_s : float, optional
        HTTP timeout applied to the NED query.
    """

    def __init__(self, timeout_s: float = 120.0) -> None:
        self.timeout_s = float(timeout_s)

    def __call__(self, ra_deg: float, dec_deg: float,
                 radius_arcsec: float) -> dict[str, np.ndarray]:
        # deferred import: astroquery is not in the pipeline image, and
        # the module must import without it (the KONA/astropy pattern)
        from astropy import units as u
        from astropy.coordinates import SkyCoord
        from astroquery.ipac.ned import Ned

        Ned.TIMEOUT = self.timeout_s
        centre = SkyCoord(ra_deg * u.deg, dec_deg * u.deg)
        table = Ned.query_region(centre, radius=radius_arcsec * u.arcsec)
        logger.debug("NED cone (%.5f, %.5f) r=%.1f\": %d rows",
                     ra_deg, dec_deg, radius_arcsec, len(table))
        return ned_table_to_columns(table)



# ---------------------------------------------------------------------------
# Per-image passes: `dev`'s provider methods, over plain records
# ---------------------------------------------------------------------------

def associate_ss(detection: Any, predictions: "dict[str, tuple] | None") -> "list[SSMatch] | None":
    """`dev`'s ``AlertDataProvider.get_ss_matches`` (providers.py L1898-1934), given the predictions.

    Sets ``detection.is_ss_candidate`` as a side effect: True when the
    nearest match is within :data:`SS_CANDIDATE_SEP_ARCSEC`, False when the
    association ran clean, None when it could not run (no predictions for
    the exposure). Returns the matches, ``[]``, or None to match.
    """
    if predictions is None:
        detection.is_ss_candidate = None
        return None
    matches = match_ss_predictions(detection.ra, detection.dec, predictions)
    detection.is_ss_candidate = bool(
        matches and matches[0].sep <= SS_CANDIDATE_SEP_ARCSEC)
    return matches


def chip_ref_matches(sources: "list[Any]", catalog: "RefCatalog | None",
                     ) -> "dict[int, tuple[list[RefMatch], list[RefMatch]]]":
    """`dev`'s ``_match_chip_refcat`` (L2208-2231): one vectorized pass, per-sid results.

    Empty when there is no catalog, so every source's matches are "not run".
    """
    if catalog is None or not sources:
        return {}
    results = match_refcat(np.array([s.ra for s in sources]),
                           np.array([s.dec for s in sources]), catalog)
    return dict(zip((s.sid for s in sources), results))


def fetch_nedcat(reader: NedSliceReader, ra0: float, dec0: float,
                 radius: float) -> "NedCatalog | None":
    """`dev`'s ``_fetch_nedcat`` (L2269-2304): a failed or absent slice is "not run"."""
    try:
        table = reader(ra0, dec0, radius)
    except Exception as exc:  # noqa: BLE001 - dev: any reader failure means "not run"
        logger.warning("NED query failed for cone (%.5f, %.5f) r=%.1f\" "
                       "(%s: %s); NED matching not run", ra0, dec0, radius,
                       type(exc).__name__, str(exc).splitlines()[0] if str(exc) else "")
        logger.debug("NED query failure traceback", exc_info=True)
        return None
    if table is None:
        logger.warning("NED reader returned no slice for cone "
                       "(%.5f, %.5f) r=%.1f\"; NED matching not run",
                       ra0, dec0, radius)
        return None
    return build_nedcat(table)


def chip_ned_matches(sources: "list[Any]", reader: "NedSliceReader | None",
                     ) -> "dict[int, list[NedMatch]]":
    """`dev`'s ``_match_chip_ned`` (L2316-2358): one slice and one match pass per image.

    Detections off the chip (beyond :data:`NED_CONE_MAX_ARCSEC`) are left
    out, so their nedMatches are "not run", never "no match".
    """
    if reader is None or not sources:
        return {}
    ra = np.array([s.ra for s in sources])
    dec = np.array([s.dec for s in sources])
    ra0, dec0, radius, inliers = chip_cone(
        ra, dec, NED_MATCH_RADIUS_ARCSEC + NED_CONE_SLACK_ARCSEC)
    n_out = int((~inliers).sum())
    if n_out:
        logger.warning(
            "%d of %d detections lie more than %.0f\" from the chip centre -- "
            "positions not on the chip (off-image PSF fits?); excluded from the "
            "NED cone, nedMatches null for them", n_out, len(sources), NED_CONE_MAX_ARCSEC)
        if not inliers.any():
            return {}
    catalog = fetch_nedcat(reader, ra0, dec0, radius)
    if catalog is None:
        return {}
    results = match_nedcat(ra[inliers], dec[inliers], catalog)
    sids = [s.sid for s, ok in zip(sources, inliers) if ok]
    return dict(zip(sids, results))
