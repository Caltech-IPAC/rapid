# ported-from: pipeline/crossMatchXSources.py @ d6364970
"""The xsources' cross-match: `dev`'s crossMatchXSources, on the in-memory matcher.

`dev`'s crossMatchXSources.py is crossMatchSources.py over the XSources
child tables, writing XAstroObjects and XMerges; this is
:func:`rapidpipe.science.crossmatch.memory.associate_epoch` over a
difference job's ``xsources`` in the same way, writing ``xobjects`` and
``xmemberships`` (``database/schema.sql``). As in `dev`: only xsources
with SExtractor ``flags`` equal to the setting (`dev` matches ``flags =
0``) are matched; positive and negative detections are matched together,
not partitioned on ``isdiffpos``; a membership's flux, and a founded
xobject's ``flux0``, is the xsource's ``fluxap``; an xobject's id is
``radec_index`` of its founding position. The field rule and the
reconcile at publish are the sources' (:mod:`rapidpipe.science.crossmatch.memory`,
:mod:`rapidpipe.foundation.catalog`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from rapidpipe.science.crossmatch.memory import associate_epoch


@dataclass
class XAssociation:
    """One difference job's xsources' cross-match: the xobjects it founded and its xmemberships.

    ``founded`` rows are ``xaid, field, ra0, dec0, flux0``; ``memberships``
    rows are ``object_job, xaid, source_job, xsid, dra, ddec, flux``.
    """

    founded: list[dict]
    memberships: list[dict]
    counts: dict


def accepted(xsources: Iterable[dict], flags: int) -> list[dict]:
    """The xsources the cross-match reads: SExtractor ``flags`` equal to ``flags``, with a finite position and ``fluxap``.

    Parameters
    ----------
    xsources : iterable of dict
        The job's xsources rows, each with at least ``xsid, expid, mjdobs,
        field, ra, dec, fluxap, flags``.
    flags : int
        The ``flags`` value matched (`dev`: 0).

    Returns
    -------
    list of dict
        The accepted rows, in the order given.
    """
    return [r for r in xsources if int(r["flags"]) == flags
            and all(r[c] is not None and math.isfinite(r[c]) for c in ("ra", "dec", "fluxap"))]


def xassociate_epoch(xobjects: dict[int, tuple[int, float, float]], xsources: Iterable[dict], *,
                     job: int, radius: float) -> XAssociation:
    """Match one epoch's accepted xsources against the xobjects as read, founding in each xsource's field.

    Parameters
    ----------
    xobjects : dict of int to tuple
        ``xaid`` to ``(founding job, ra0, dec0)``: the run's xobjects in every
        field the xsources fall in and those fields' neighbours, as read.
    xsources : iterable of dict
        The accepted xsources (:func:`accepted`), the job's own.
    job : int
        The difference job, whose source set the xsources belong to.
    radius : float
        The match radius, degrees.

    Returns
    -------
    XAssociation
        The founded xobjects (each with its founding xsource's ``fluxap`` as
        ``flux0``), the xmemberships and the counts.
    """
    xsources = list(xsources)
    epoch = associate_epoch(xobjects, [{**r, "sid": r["xsid"]} for r in xsources],
                            job=job, radius=radius, flux="fluxap")
    flux_of = {r["xsid"]: float(r["fluxap"]) for r in xsources}
    founded = [{"xaid": r["aid"], "field": r["field"], "ra0": r["ra0"], "dec0": r["dec0"],
                "flux0": flux_of[epoch.founders[r["aid"]]]} for r in epoch.founded]
    memberships = [{"object_job": m["object_job"], "xaid": m["aid"], "source_job": m["source_job"],
                    "xsid": m["sid"], "dra": m["dra"], "ddec": m["ddec"], "flux": m["flux"]}
                   for m in epoch.memberships]
    return XAssociation(founded=founded, memberships=memberships, counts=epoch.counts)


__all__ = ("XAssociation", "accepted", "xassociate_epoch")
