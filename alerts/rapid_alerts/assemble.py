"""
Assemble one alert packet from provider data.

This module knows nothing about where the data lives -- it only talks to
the AlertDataProvider interface and the registry-driven builders.
"""

from .build import build_dia_source, build_dia_object, build_dia_forced_source
from .fields import VERSION

PRV_WINDOW_DAYS = 365.25  # look-back window for previous detections


def assemble_alert(provider, sid):
    """Assemble a complete alert packet for a given source ID.

    Args:
        provider: an AlertDataProvider instance.
        sid: source ID to build the alert for.

    Returns:
        dict conforming to the rapid alert schema.
    """
    detection = provider.get_detection(sid)
    obj = provider.get_object_for_source(detection)

    dia_object = None
    prv_dia_sources = None
    prv_dia_forced_sources = None

    if obj is not None:
        detection.aid = obj.aid

        prv = provider.get_prv_detections(detection, obj,
                                          window_days=PRV_WINDOW_DAYS)
        if prv:
            prv_dia_sources = [build_dia_source(p) for p in prv]

        mjds = [detection.mjdobs] + [p.mjdobs for p in prv]
        obj.first_mjd = min(mjds)
        obj.last_mjd = max(mjds)
        obj.validity_mjd = detection.mjdobs
        dia_object = build_dia_object(obj)

        forced = provider.get_forced_photometry(detection, obj)
        if forced:
            prv_dia_forced_sources = [build_dia_forced_source(fp)
                                      for fp in forced]

    cutouts = provider.get_cutouts(detection)

    return {
        "schemaVersion": VERSION,
        "pipelineVersion": None,
        "diaSourceId": detection.sid,
        "observation_reason": None,
        "target_name": None,
        "diaSource": build_dia_source(detection),
        "prvDiaSources": prv_dia_sources,
        "prvDiaForcedSources": prv_dia_forced_sources,
        "diaObject": dia_object,
        "ssSource": None,
        "mpc_orbits": None,
        "cutoutDifference": cutouts.difference,
        "cutoutScience": cutouts.science,
        "cutoutTemplate": cutouts.template,
    }
