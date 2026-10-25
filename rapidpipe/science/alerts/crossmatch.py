# ported-from: alerts/providers.py @ 54fd7ea8
"""The alert's cross-matches, ported from `dev`'s ``alerts/providers.py``.

Copied from `origin/dev` unchanged: the solar-system association against
KONA predictions (``match_ss_predictions``), the reference-catalog match
(``load_refcat``/``match_refcat``), the nearest-N geometry the matchers
use (``_nearest_within``, `dev`'s 2026-09-23 replacement of the
``nthneighbor`` loop, which returned one row per rank at tied positions)
and the Gaia DR3 matcher (``propagate_positions`` to ``match_gaiacat``,
schema 00.06). Pure functions over positions and column arrays; nothing
here reaches a database.

KONA: the stage loads `dev`'s nightly predictions JSON
(``{expid: {designation: [ra, dec, vmag]}}``, `dev`'s
``alerts/cli.py:load_kona_predictions``) only when ``[alerts] kona_file`` is
set; ``modules/solarsystem/rapid_kona.py`` is never imported.
"""

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np

from rapidpipe.science.alerts.cutouts import STAMP_HALF_WIDTH
from rapidpipe.science.alerts.records import GaiaMatch, RefMatch, SSMatch

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


def load_refcat(path: str) -> RefCatalog:
    """Parse a reference-image SExtractor catalog for cross-matching.

    Parameters
    ----------
    path : str
        Path to the staged catalog file (SExtractor ASCII_HEAD format).

    Returns
    -------
    RefCatalog
        The parsed, star/galaxy-partitioned catalog.

    Raises
    ------
    ValueError
        When the file is unreadable, empty, or missing expected columns: a
        recorded reference catalog is a required input, and `dev`'s
        degradation to "not run" shipped every alert with null matches.
    """
    from astropy import units as u
    from astropy.coordinates import SkyCoord
    from astropy.io import ascii as astropy_ascii

    try:
        # typed Any: pylance mis-infers astropy's ascii.read overloads
        table: Any = astropy_ascii.read(path, format="sextractor")
    except Exception as exc:
        raise ValueError(f"reference catalog {path} could not be parsed: {exc}") from exc
    if len(table) == 0:
        raise ValueError(f"reference catalog {path} is empty")
    missing = [c for c in REFCAT_COLUMNS if c not in table.colnames]
    if missing:
        raise ValueError(f"reference catalog {path} is missing columns {missing}"
                         " (SExtractor parameter file changed?)")

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


def _nearest_within(src: Any, coords: Any, radius_arcsec: float,
                    n_max: int) -> list[tuple[int, int, float, float]]:
    """The nearest `n_max` catalog rows within `radius_arcsec` of each source.

    Shared by match_refcat() and match_gaiacat(). One KD-tree query on
    unit vectors for the ``n_max`` nearest rows per source with a chord
    distance bound, then separation and position angle (astropy, on the
    hits only) and the same ``sep <= radius`` mask as before.

    This replaced a loop over ``match_to_catalog_sky(nthneighbor=n)`` on
    2026-09-23. That call is the same KD-tree query keeping only rank n,
    and when catalog rows share a position the ranks tie, so it returned
    the SAME row for every rank (3C 273's 16 absorption-line rows came
    back as one row three times; ~0.1% of rows in that pixel, none in
    others). One query for k rows returns k distinct rows. Measured on a
    chip's 20k detections vs 300-200k catalog rows: 1.3-3.7x faster than
    the loop, identical indices wherever positions are not tied.

    Parameters
    ----------
    src, coords : astropy.coordinates.SkyCoord
        Detection positions (1-D) and catalog positions (1-D, non-empty).
    radius_arcsec : float
        Maximum separation to report.
    n_max : int
        Rows kept per source.

    Returns
    -------
    list of (source index, catalog index, sep [arcsec], pa [deg E of N])
        Grouped by source in input order, nearest first; ties in
        separation are broken by catalog index, so the order is
        deterministic.
    """
    from scipy.spatial import cKDTree

    n_src, n_cat = len(src), len(coords)
    if n_src == 0 or n_cat == 0 or n_max <= 0:
        return []
    k = min(n_max, n_cat)
    # plain arrays from here on: indexing SkyCoord objects per hit pair
    # cost more than the whole old loop (measured 2026-09-23)
    src_ra, src_dec = np.radians(src.ra.deg), np.radians(src.dec.deg)
    cat_ra, cat_dec = np.radians(coords.ra.deg), np.radians(coords.dec.deg)
    cat_xyz = _unit_vectors(cat_ra, cat_dec)
    src_xyz = _unit_vectors(src_ra, src_dec)
    chord = 2.0 * np.sin(np.radians(radius_arcsec / 3600.0) / 2.0)
    dist, idx = cKDTree(cat_xyz).query(src_xyz, k=k,
                                       distance_upper_bound=chord * (1.0 + 1e-9))
    dist = np.asarray(dist).reshape(n_src, k)
    idx = np.asarray(idx).reshape(n_src, k)
    ii, jj = np.nonzero(np.isfinite(dist))                  # misses are inf
    if ii.size == 0:
        return []
    cat_idx = idx[ii, jj]
    sep, pa = _sep_pa(src_ra[ii], src_dec[ii], cat_ra[cat_idx], cat_dec[cat_idx])
    keep = sep <= radius_arcsec                             # exact angular test
    ii, cat_idx, sep, pa = ii[keep], cat_idx[keep], sep[keep], pa[keep]
    order = np.lexsort((cat_idx, sep, ii))
    return [(int(ii[o]), int(cat_idx[o]), float(sep[o]), float(pa[o]))
            for o in order]


def _unit_vectors(ra_rad: np.ndarray, dec_rad: np.ndarray) -> np.ndarray:
    """(N, 3) unit vectors for positions in radians."""
    cos_dec = np.cos(dec_rad)
    return np.column_stack((cos_dec * np.cos(ra_rad), cos_dec * np.sin(ra_rad),
                            np.sin(dec_rad)))


def _sep_pa(ra1: np.ndarray, dec1: np.ndarray, ra2: np.ndarray,
            dec2: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Separation [arcsec] and position angle [deg, E of N] from 1 to 2,
    all in radians in; the Vincenty and position-angle formulas astropy's
    ``separation`` / ``position_angle`` use, so results agree to rounding."""
    dra = ra2 - ra1
    sin_dra, cos_dra = np.sin(dra), np.cos(dra)
    sin_d1, cos_d1 = np.sin(dec1), np.cos(dec1)
    sin_d2, cos_d2 = np.sin(dec2), np.cos(dec2)
    num1 = cos_d2 * sin_dra
    num2 = cos_d1 * sin_d2 - sin_d1 * cos_d2 * cos_dra
    den = sin_d1 * sin_d2 + cos_d1 * cos_d2 * cos_dra
    sep = np.degrees(np.arctan2(np.hypot(num1, num2), den)) * 3600.0
    pa = np.degrees(np.arctan2(num1, num2)) % 360.0
    return sep, pa


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
        # nearest-first, distinct rows even at tied positions
        for i, sub_idx, sep_arcsec, pa_deg in _nearest_within(
                src, coords, radius_arcsec, n_max):
            results[i][slot].append(_ref_match_from_row(
                catalog.columns, int(subset_to_row[sub_idx]), sep_arcsec, pa_deg))
    return results


@dataclass
class NedCatalog:
    """One sky slice of a catalog, ready to match (the Gaia match's).

    Built by build_gaiacat(). `columns` holds the catalog's columns over
    the kept rows. `coords` is one astropy SkyCoord over those rows
    (astropy caches the KD-tree on it, so every detection on a chip reuses
    one tree), or None when no rows survived -- which matches to [] rather
    than to "not run".
    """
    columns: dict[str, np.ndarray]
    coords: Any                   # SkyCoord over the kept rows, or None
    n_input: int = 0              # rows before selection, for logging


# ---------------------------------------------------------------------------
# Gaia cross-match
#
# The nearest Gaia DR3 sources to each detection: a star veto / variable-
# star tag for the bulge, where NED has nothing. The local copy is the
# 28-column projection of LINCC's HATS catalog that alerts/catalog_store.py
# (ingest-gaia) writes, read per chip by alerts/catalog_readers.py
# GaiaReader, one cone per chip. The geometry (_nearest_within) and the
# three-state null/[]/populated contract are the reference-catalog match's.
#
# What differs is the epoch. Gaia positions are given at the catalog's
# reference epoch (J2016.0 for DR3) and bulge stars move; by Roman's first
# season that is 11 years of drift. So, once per chip, every Gaia row in
# the cone is moved to the exposure's epoch by its proper motion BEFORE the
# tree is built (propagate_positions), and the fetched cone is widened by
# the largest drift any Gaia star can have over that interval so a star
# that moves INTO the chip's cone is not missed (GAIA_PM_MAX_ARCSEC_PER_YEAR).
# A new Gaia release resets the clock: the reference epoch comes from the
# copy's ingest record, not from here.
# ---------------------------------------------------------------------------

# Maximum separation for a reported match. Gaia's astrometry is ~mas and
# Roman's ~0.1", so a true counterpart sits well inside 1"; the radius is
# generous for unmodelled motion. In the bulge (1.27 M Gaia sources per
# deg^2) the chance-coincidence rate rho*pi*r^2 at 1" is ~0.3 per
# detection -- consumers must cut on `sep`.
GAIA_MATCH_RADIUS_ARCSEC = 1.0

# Keep at most this many matches, nearest first.
GAIA_MATCH_NMAX = 3

# Largest proper motion in Gaia: Barnard's star, 10.4"/yr. The chip cone
# is widened by this times the epoch offset so any star that could drift
# into the cone is fetched; ~120" for an 11-year offset, which roughly
# doubles the bulge cone's rows (still one parquet row group).
GAIA_PM_MAX_ARCSEC_PER_YEAR = 11.0

# Reference epoch used when the reader does not state one (a bare
# callable in tests). Gaia DR3 positions are at J2016.0.
GAIA_DEFAULT_REF_EPOCH = 2016.0

# Columns the matcher works in -- the Gaia DR3 gaia_source names, as the
# local copy keeps them and the GaiaMatch attributes are spelled. The
# reader delivers exactly these: missing numerics as NaN, ints as 0,
# bools as False, the variability flag as its string. The copy also holds
# the position/proper-motion errors and astrometric_excess_noise, which
# nothing reads yet.
GAIA_INT_COLUMNS = ("source_id", "non_single_star", "ipd_frac_multi_peak")
GAIA_BOOL_COLUMNS = ("duplicated_source", "in_qso_candidates", "in_galaxy_candidates")
GAIA_STRING_COLUMNS = ("phot_variable_flag",)
GAIA_NUMERIC_COLUMNS = ("ra", "dec", "pmra", "pmdec", "parallax", "parallax_error",
                        "ruwe", "phot_g_mean_mag", "phot_bp_mean_mag",
                        "phot_rp_mean_mag", "bp_rp", "phot_g_mean_flux_over_error",
                        "classprob_dsc_combmod_star", "classprob_dsc_combmod_galaxy",
                        "classprob_dsc_combmod_quasar")
GAIA_COLUMNS = GAIA_INT_COLUMNS + GAIA_BOOL_COLUMNS + GAIA_STRING_COLUMNS + GAIA_NUMERIC_COLUMNS
GAIA_REQUIRED_COLUMNS = ("source_id", "ra", "dec")

# Gaia's phot_variable_flag value meaning "classified variable" (the other
# value is "NOT_AVAILABLE": not analysed, which is most sources).
GAIA_VARIABLE_FLAG = "VARIABLE"


def epoch_offset_years(mjd: float, ref_epoch: float) -> float:
    """Years from the Julian epoch `ref_epoch` (e.g. 2016.0) to `mjd`."""
    ref_mjd = 51544.5 + (ref_epoch - 2000.0) * 365.25     # J2000.0 = MJD 51544.5
    return (mjd - ref_mjd) / 365.25


def propagate_positions(ra: np.ndarray, dec: np.ndarray, pmra: np.ndarray,
                        pmdec: np.ndarray, dt_years: float,
                        ) -> tuple[np.ndarray, np.ndarray]:
    """Move catalog positions by proper motion over `dt_years`.

    Linear in time: ``dec += pmdec * dt`` and ``ra += pmra * dt / cos(dec)``,
    with Gaia's `pmra` already including the cos(dec) factor and both in
    mas/yr. A missing proper motion (NaN) moves nothing. Parallactic
    motion is ignored (< 1 mas for all but the nearest stars; a Roman
    pixel is 110 mas). Accurate to well under 0.1" for any Gaia star over
    decades, except the handful within a few parsecs, which a 1" match
    radius still absorbs.
    """
    ra = np.asarray(ra, dtype=float)
    dec = np.asarray(dec, dtype=float)
    dra = np.nan_to_num(np.asarray(pmra, dtype=float)) * dt_years / 3.6e6
    ddec = np.nan_to_num(np.asarray(pmdec, dtype=float)) * dt_years / 3.6e6
    new_dec = np.clip(dec + ddec, -90.0, 90.0)
    new_ra = (ra + dra / np.cos(np.radians(dec))) % 360.0
    return new_ra, new_dec


def build_gaiacat(table: dict[str, Any], dt_years: float = 0.0) -> NedCatalog | None:
    """Propagate a Gaia slice to the detection epoch and build its tree.

    Parameters
    ----------
    table : dict
        Column arrays keyed by GAIA_COLUMNS. source_id/ra/dec required; the
        rest filled with nulls (NaN / 0 / False / None) when absent.
    dt_years : float, optional
        Years from the catalog's reference epoch to the exposure; the
        slice's `ra`/`dec` are replaced by the propagated positions.

    Returns
    -------
    NedCatalog or None
        The tree-built slice; None (with a
        logged warning) when required columns are missing -- the cross-
        match then degrades to "not run". An empty slice has coords=None
        and matches to [].
    """
    from astropy import units as u
    from astropy.coordinates import SkyCoord

    missing = [c for c in GAIA_REQUIRED_COLUMNS if c not in table]
    if missing:
        logger.warning("Gaia slice is missing required columns %s; "
                       "cross-match skipped", missing)
        return None

    n = len(np.asarray(table["source_id"]))
    columns: dict[str, np.ndarray] = {}
    for name in GAIA_COLUMNS:
        if name in GAIA_NUMERIC_COLUMNS:
            dtype, fill = float, np.nan
        elif name in GAIA_INT_COLUMNS:
            dtype, fill = np.int64, 0
        elif name in GAIA_BOOL_COLUMNS:
            dtype, fill = bool, False
        else:
            dtype, fill = object, None
        values = table[name] if name in table else np.full(n, fill, dtype=dtype)
        columns[name] = np.asarray(values, dtype=dtype)
    if n and dt_years:
        columns["ra"], columns["dec"] = propagate_positions(
            columns["ra"], columns["dec"], columns["pmra"], columns["pmdec"], dt_years)
    logger.debug("Gaia slice: %d rows, propagated %.2f yr", n, dt_years)

    coords = (SkyCoord(columns["ra"] * u.deg, columns["dec"] * u.deg)
              if n else None)
    return NedCatalog(columns=columns, coords=coords, n_input=n)


def _gaia_match_from_row(columns: dict[str, np.ndarray], row: int,
                         sep: float, pa: float) -> GaiaMatch:
    """Build one GaiaMatch from slice row `row` at the given sep/PA.

    Absent numerics become None (the schema types them as nullable
    unions); ints and bools keep their fill values; the variability flag
    string becomes the boolean "is VARIABLE".
    """
    def number(name: str) -> float | None:
        value = float(columns[name][row])
        return value if np.isfinite(value) else None

    optional: dict[str, Any] = {}
    optional.update({name: int(columns[name][row]) for name in GAIA_INT_COLUMNS
                     if name != "source_id"})
    optional.update({name: bool(columns[name][row]) for name in GAIA_BOOL_COLUMNS})
    optional.update({name: number(name) for name in GAIA_NUMERIC_COLUMNS
                     if name not in ("ra", "dec")})
    flag = columns["phot_variable_flag"][row]
    optional["phot_variable_flag"] = (flag is not None
                                      and str(flag).strip() == GAIA_VARIABLE_FLAG)
    return GaiaMatch(source_id=int(columns["source_id"][row]),
                     ra=float(columns["ra"][row]), dec=float(columns["dec"][row]),
                     sep=sep, pa=pa, **optional)


def match_gaiacat(ra: Any, dec: Any, catalog: NedCatalog,
                  radius_arcsec: float = GAIA_MATCH_RADIUS_ARCSEC,
                  n_max: int = GAIA_MATCH_NMAX,
                  ) -> list[list[GaiaMatch]]:
    """Match detection positions against a (propagated) Gaia slice.

    One KD-tree query for the
    n_max nearest sources of every detection within `radius_arcsec` (see
    _nearest_within). Per detection, in input order, nearest first and at
    most `n_max` long; an empty list means "no Gaia source within the
    radius"; "could not run" is signalled by the provider, not here.
    """
    from astropy import units as u
    from astropy.coordinates import SkyCoord

    ra = np.atleast_1d(np.asarray(ra, dtype=float))
    dec = np.atleast_1d(np.asarray(dec, dtype=float))
    results: list[list[GaiaMatch]] = [[] for _ in range(ra.size)]

    coords = catalog.coords
    if coords is None:
        return results  # no Gaia sources in this slice

    src = SkyCoord(ra * u.deg, dec * u.deg)
    for i, cat_idx, sep_arcsec, pa_deg in _nearest_within(src, coords,
                                                          radius_arcsec, n_max):
        results[i].append(_gaia_match_from_row(catalog.columns, cat_idx,
                                               sep_arcsec, pa_deg))
    return results


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
