# ported-from: alerts/providers.py @ 883e3876
"""The alert's normalized records, ported from `dev`'s ``alerts/providers.py``.

`dev`'s provider translates database rows into these dataclasses and the
registry-driven builders read them (``assemble``). They are copied from
`origin/dev`'s ``alerts/providers.py`` (the "Normalized records" section)
unchanged, with the constants and exceptions the port keeps:

- :data:`PRV_WINDOW_DAYS`: `dev`'s previous-detection look-back window; the
  stage exposes it as ``[alerts] prv_window_days``.
- :data:`ALERTABLE_FLAGS`: only ``flags = 0`` sources get alerts, `dev`'s
  rule (the cross-match associates only those).
- :class:`AssociationError`: a source with no usable object (no merges row,
  or a merges row whose astroobjects row is missing -- an orphan). `dev`'s
  batch path records it and skips the source; so does the stage.

No database, no files: plain records.
"""

import dataclasses
from dataclasses import dataclass
from typing import Any

PRV_WINDOW_DAYS = 365.25  # default look-back window for previous detections

ALERTABLE_FLAGS = 0   # the sources.flags value the cross-match associates

class AssociationError(RuntimeError):
    """A detection reached alert assembly without an associated object.

    Source cross-matching creates an object for every source it processes
    -- and it processes exactly the flags = 0 sources (ALERTABLE_FLAGS).
    The alert path selects that same population, so by alert time every
    detection it handles has a merges_<field> row by definition. A missing
    association -- or a missing merges/astroobjects partition -- means
    cross-matching did not run or failed for the field, or that the two
    components' flags rules have drifted apart (see ALERTABLE_FLAGS).
    Raised loudly rather than shipping an object-less alert. The
    single-alert path lets it propagate; produce.batch_produce() logs
    the source, records it in its BatchStats, and continues with the
    rest of the chip.
    """


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
    # alert-assembly timestamp (timeProcessedMjd), stamped by
    # produce.assemble_alert_for_source(); never a storage column
    time_proc: float | None = None
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
    # Roman observation identifiers, from the exposures row (migration
    # 20261015; null for exposures ingested before it). `pass` is a Python
    # keyword, hence obs_pass; the wire name is still "pass".
    program_id: int | None = None
    execution_plan: int | None = None
    obs_pass: int | None = None
    segment: int | None = None
    observation: int | None = None
    visit: int | None = None
    exposure: int | None = None
    observation_id: str | None = None
    survey: str | None = None           # surveys.program_title, via program_id
    # real/bogus: the score and label stored with the detection by the
    # science pipeline (sources.rb / sources.rblabel; null = not scored)
    rb: float | None = None
    rblabel: int | None = None
    # solar-system candidate flag, set by get_ss_matches(); None = KONA
    # association not run for this detection (e.g. all prv sources)
    is_ss_candidate: bool | None = None
    # real/bogus run-level facts, stamped at assembly from the provider's
    # configuration (the threshold and model in force for this run); None
    # on prior detections, whose scores may come from other model versions
    rb_threshold: float | None = None
    rb_version: str | None = None

    # fields filled after construction (association, assembly stamping,
    # KONA); never storage columns
    FILLED_LATER = frozenset({"aid", "time_proc", "is_ss_candidate",
                              "rb_threshold", "rb_version"})

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
            (except the FILLED_LATER fields, which are filled after
            construction). Errors on dropped columns instead of null.

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
            missing = names - set(row) - cls.FILLED_LATER
            if missing:
                raise KeyError(
                    f"Source row is missing expected columns: "
                    f"{sorted(missing)} (renamed or dropped in storage?)")
        return cls(**{key: value for key, value in row.items() if key in names})


@dataclass
class ObjectRecord:
    """Persistent astronomical object: an ``astroobjects_<field>`` row
    joined with its ``astroobjectsmeta_<field>`` statistics row.

    aid/ra0/dec0 come from astroobjects_<field> (the cross-match's
    product). stdevra/stdevdec/nsources come from astroobjectsmeta_<field>
    (the statistics stage's product, computed after cross-matching). When
    that table or the aid's row does not exist yet the sigmas are None --
    they map to the schema's nullable raSigma/decSigma -- and nsources,
    which maps to the non-nullable nDiaSources, falls back to the aid's
    merges_<field> row count (see AlertDataProvider._stats_sql).
    """
    aid: int
    ra0: float
    dec0: float
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
    """One forced-photometry epoch at an object position: the PSF flux on
    the difference image and on the science image of the same exposure,
    both at the same fixed (ra, dec).

    Filled by AlertDataProvider from the per-chip forced-photometry run
    (alerts.forced_phot), cached per chip as a table and expanded per
    object. Fluxes are instrumental (image units), like diaSource.psfFlux;
    the nJy calibration is pending for both. ``flags`` ORs the difference-
    and science-image fit flags (photutils bits plus forced_phot.FLAG_*).
    """
    forced_id: int
    aid: int
    expid: int
    sca: int
    ra: float
    dec: float
    mjdobs: float
    time_proc: float
    band: str | None = None
    flux: float | None = None
    fluxerr: float | None = None
    science_flux: float | None = None
    science_fluxerr: float | None = None
    flags: int = 0


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
class NedMatch:
    """One NED object near a detection, as a candidate host galaxy.

    One entry of the alert's nedMatches array, which the packet stage
    leaves null (no NED match runs); each match keeps its own ptype so
    consumers can re-cut.

    NED has no stable numeric object id in this product, so `prefname` is
    the identifier. It is NED's *preferred* name, which can in principle
    be reassigned between NED releases -- treat it as a lookup key, not as
    a permanent identifier for cross-release joins.
    """
    prefname: str        # NED preferred object name
    ra: float            # NED position, ICRS [deg]
    dec: float
    sep: float           # angular separation from the detection [arcsec]
    pa: float            # position angle detection -> object,
                         # East of North [deg]
    ptype: str | None = None    # NED preferred type; None = unclassified
    z: float | None = None      # preferred redshift (frame as published)
    zunc: float | None = None   # None when NED gives no uncertainty
    zflag: str | None = None    # e.g. "SLS"


@dataclass
class LvsMatch:
    """One NED-LVS galaxy near a detection.

    Built by match_lvscat() from the local NED-LVS table (see the NED-LVS
    cross-match section below); becomes one entry of the alert's
    lvsMatches array. NED-LVS is NED's vetted local-volume galaxy sample
    (D < 1000 Mpc) carrying the distances, angular diameters, photometry,
    SFR and stellar mass the object directory lacks. The NED-LVS `objname`
    column is the object directory's `prefname`, so it is stored as
    `prefname` here and emitted as `prefName`, both exactly as in NedMatch,
    and the two arrays join by it. Every other attribute is named after its
    NED-LVS FITS column (Cook et al. 2023).
    """
    prefname: str        # NED preferred name (NED-LVS `objname`; == NedMatch.prefname)
    ra: float            # NED position, ICRS [deg]
    dec: float
    sep: float           # angular separation from the detection [arcsec]
    pa: float            # position angle detection -> galaxy, E of N [deg]
    objtype: str | None = None      # NED preferred type (G, GPair, ...)
    z: float | None = None          # fiducial heliocentric redshift
    z_unc: float | None = None
    z_tech: str | None = None       # SPEC, PHOT, UNKN, INFD, MOD
    z_qual: bool = False            # True = redshift flagged unreliable
    DistMpc: float | None = None    # the distance NED-LVS adopts [Mpc]
    DistMpc_unc: float | None = None
    DistMpc_method: str | None = None   # "Redshift" or "zIndependent"
    Diam: float | None = None       # major-axis angular diameter 2a [arcsec]
    Diam_ba: float | None = None    # minor/major axis ratio
    Diam_pa: float | None = None    # ellipse position angle, E of N [deg]
    Diam_qual: bool = False         # True = diameter flagged highly uncertain
    ebv: float | None = None        # foreground E(B-V) [mag]
    m_Ks: float | None = None       # 2MASS Ks, Vega [mag]
    m_Ks_unc: float | None = None
    m_W1: float | None = None       # WISE W1, Vega [mag]
    m_W1_unc: float | None = None
    m_NUV: float | None = None      # GALEX NUV, AB [mag]
    m_NUV_unc: float | None = None
    SFR_hybrid: float | None = None     # FUV+W4 star-formation rate [Msun/yr]
    SFR_hybrid_unc: float | None = None
    Mstar: float | None = None      # stellar mass from W1 [Msun]
    Mstar_unc: float | None = None


@dataclass
class GaiaMatch:
    """One Gaia DR3 source near a detection.

    Built by match_gaiacat() from the local Gaia copy (see the Gaia
    cross-match section below); becomes one entry of the alert's
    gaiaMatches array. Gaia answers "is this a known star, and is it
    variable, moving or a known non-single system" -- it reaches G ~ 21,
    far shallower than Roman, so most detections match nothing, and that
    is the expected answer. `ra`/`dec` are the catalog position
    PROPAGATED by proper motion to the detection's epoch (see
    propagate_positions); sep/pa are measured against that. Attribute
    names follow the Gaia DR3 gaia_source columns.
    """
    source_id: int       # Gaia DR3 source_id
    ra: float            # position at the detection epoch, ICRS [deg]
    dec: float
    sep: float           # angular separation from the detection [arcsec]
    pa: float            # position angle detection -> source, E of N [deg]
    pmra: float | None = None       # proper motion in RA*cos(dec) [mas/yr]
    pmdec: float | None = None      # proper motion in Dec [mas/yr]
    parallax: float | None = None   # [mas]
    parallax_error: float | None = None
    ruwe: float | None = None       # renormalised unit weight error (>1.4: poor fit / binary)
    phot_g_mean_mag: float | None = None    # G [mag, Vega]
    phot_bp_mean_mag: float | None = None   # BP
    phot_rp_mean_mag: float | None = None   # RP
    bp_rp: float | None = None              # BP - RP colour [mag]
    phot_g_mean_flux_over_error: float | None = None  # G flux S/N; low = variable
    phot_variable_flag: bool = False        # Gaia's "VARIABLE" flag
    non_single_star: int = 0                # 0 = none; bit flags for astrometric/spectroscopic/eclipsing binary
    in_qso_candidates: bool = False
    in_galaxy_candidates: bool = False
    classprob_dsc_combmod_star: float | None = None     # DSC class probabilities
    classprob_dsc_combmod_galaxy: float | None = None
    classprob_dsc_combmod_quasar: float | None = None
    duplicated_source: bool = False         # another source within ~2" was removed
    ipd_frac_multi_peak: int = 0            # % of windows with >1 peak (blend)


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

