"""VARIANT 2 -- registry-style DSL. Pydantic models are the single source
of truth (like v1), but declared with param_registry.py's exact layout via
the v2_rapid_pydantic metaclass machinery. Checked by v2_check.py.

Ground-truth schema as Pydantic models, declared registry-style
(representative subset).

Same layout rules as param_registry.py:

  - one param per statement; the Avro type is a plain string (or union
    list / array dict) and is the single source of truth -- the Python
    type pydantic validates against is derived from it (see
    rapid_pydantic). A leading "@" references another record.
  - status sits in the registry position (start of the continuation
    line), followed by source provenance, then attr / transform.
  - declaration order IS the Avro wire order; reordering params is a
    schema change, exactly as in the registry.
  - params excluded from the schema (registry NOT_USED) are commented
    out where they would sit, with the reason.

What pydantic adds underneath (see rapid_pydantic.RapidRecord):
  - build_record is replaced by DiaSource.model_validate(source,
    from_attributes=True); a missing or None non-nullable raises a
    ValidationError naming the param.
  - STUB params are forced to null after validation even when attr /
    transform is staged (apFlux below stages one on purpose).
  - source_check() is the import-time attr guard (_validate_registry).
"""

from .v2_rapid_pydantic import IMPLEMENTED, STUB, RapidRecord, param

VERSION = "01.01"


class DiaSource(RapidRecord, name="diaSource"):
    """RAPID alert schema: individual source detection on a difference image"""

    # --- Identifiers & associations -------------------------------------
    diaSourceId = param("long",             "Unique identifier for this source detection",
                    IMPLEMENTED, "sources.sid",    attr="sid")
    visit       = param("long",             "Visit (exposure) identifier",
                    IMPLEMENTED, "sources.expid",  attr="expid")
    detector    = param("int",              "Detector (SCA) number",
                    IMPLEMENTED, "sources.sca",    attr="sca")
    diaObjectId = param(["null", "long"],   "Associated diaObject identifier",
                    IMPLEMENTED, "merges_<field>.aid", attr="aid")

    # --- Time ------------------------------------------------------------
    midpointMjd = param("double",           "Effective mid-observation time (UTC scale) [MJD]",
                    IMPLEMENTED, "sources.mjdobs", attr="mjdobs")
    # timeProcessedMjd  ["null", "double"]  "Time alert was processed (UTC scale) [MJD]"
    #     NOT USED: set at assembly time  #TODO: do we actually need this?

    # --- Position & photometry --------------------------------------------
    ra          = param("double",           "Right ascension; ICRS [deg]",
                    IMPLEMENTED, "sources.ra")
    band        = param(["null", "string"], "Filter band name (F062, F087, F106, F129, F146, F158, F184, F213)",
                    IMPLEMENTED, "filters.filter")
    psfFlux     = param(["null", "float"],  "Flux from PSF-fit on difference image [nJy]",
                    IMPLEMENTED, "sources.fluxfit (instrumental; nJy calibration pending)", attr="fluxfit")
    snr         = param(["null", "float"],  "Signal-to-noise ratio (psfFlux / psfFluxErr)",
                    IMPLEMENTED, "computed: fluxfit / fluxerr")
    isNegative  = param("boolean",          "true if source is from negative (ref minus sci) subtraction",
                    IMPLEMENTED, "sources.isdiffpos (inverted; renamed per schema spreadsheet)",
                    attr="isdiffpos", transform=lambda v: not v)
    # STUB with a *staged* attr: the provider value is already available,
    # but RapidRecord nulls it until the param is flipped to IMPLEMENTED
    # (mirrors the staged attrs on the diaForcedSource stubs).
    apFlux      = param(["null", "float"],  "Aperture flux on difference image (stub) [nJy]",
                    STUB, "aperture photometry (not in DB flow; SExtractor MAG_AUTO in file flow)",
                    attr="apflux")

    # --- Flags -----------------------------------------------------------
    flags       = param("long",             "Bitmask of processing flags",
                    IMPLEMENTED, "sources.flags")


class Alert(RapidRecord, name="alert"):
    """RAPID alert schema: top-level alert record"""

    schemaVersion = param(["null", "string"], "Version of the alert schema used to serialize this packet",
                    IMPLEMENTED, "param_registry.VERSION (set in produce.py)", default=VERSION)
    diaSourceId   = param("long",             "Identifier for the triggering diaSource",
                    IMPLEMENTED, "produce.assemble_alert()")
    diaSource     = param("@diaSource",       "Triggering source detection",
                    IMPLEMENTED, "produce.assemble_alert()")
    prvDiaSources = param(["null", {"type": "array", "items": "@diaSource"}],
                                              "Previous detections of the same object within 12 months",
                    IMPLEMENTED, "produce.assemble_alert()")
    cutoutDifference = param(["null", "bytes"], "FITS cutout of difference image",
                    IMPLEMENTED, "provider get_cutouts()")


# Avro dependency order (referenced records first), like RECORDS in the registry.
RECORDS = (DiaSource, Alert)
