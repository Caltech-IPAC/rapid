# ported-from: none
"""The offset of a frame's stars from a Gaia copy, through the frame's own WCS.

New, not `dev`'s: the hand measurement of the WCS against the stars
(rapid_docs ``system/measurements.md``, WCS against the stars,
2026-10-24), made at intake so every dataset's value is seen without a
reader. Each Gaia star the WCS puts on the frame, :data:`MARGIN_PX` from
an edge and with no other Gaia star within :data:`ISOLATION_PX`, is
centroided on the background-subtracted image from its predicted position
(the Gaussian-weighted iterative centroid the difference metrics use); a
star whose window holds a NaN, whose peak is under :data:`PEAK_SIGMA`
times the clipped noise, or whose centroid moved more than
:data:`MAX_MOVE_PX` is dropped. The offset is the median of measured minus
predicted, in 0-based pixels, x along columns. On HLWAS F146 SCA 8 pass 4
(``001_0001``) that measurement found (+0.116, -0.989) over 30 stars; this
reads (+0.127, -0.986) over 34.

Positions are the caller's: the intake passes the copy's own (Gaia DR3
at epoch 2016.0, as the SOC simulations place their stars), or, under
``[intake] gaia_proper_motion``, the copy carried by its proper motions to
the frame's epoch (:mod:`rapidpipe.stages.intake`). The
constants are that measurement's, not settings. This module may import leaf
modules only.
"""

from __future__ import annotations

import numpy as np

#: A star this close to an edge, or to another Gaia star, is not measured, px.
MARGIN_PX, ISOLATION_PX = 20, 10.0
#: The window's peak, over the clipped noise, below which a star is not measured.
PEAK_SIGMA = 50.0
#: A centroid that moved further than this from its prediction is another source, px.
MAX_MOVE_PX = 3.0
_HALF = 7


def gaia_offset(image: np.ndarray, wcs, ra: np.ndarray, dec: np.ndarray) -> dict[str, float]:
    """``gaia_dx_px``, ``gaia_dy_px`` and ``n_gaia_stars``; only the count when no star is measured.

    ``wcs`` is an ``astropy.wcs.WCS`` of ``image``; ``ra`` and ``dec`` are the copy's degrees.
    """
    from astropy.stats import sigma_clipped_stats
    from scipy.spatial import cKDTree

    from rapidpipe.science.difference.metrics import centroid

    ny, nx = image.shape
    ra, dec = np.asarray(ra, float), np.asarray(dec, float)
    near = _near(wcs, nx, ny, ra, dec)
    x, y = (wcs.all_world2pix(ra[near], dec[near], 0, quiet=True) if near.any()
            else (np.empty(0), np.empty(0)))
    inside = (np.isfinite(x) & np.isfinite(y) & (x >= MARGIN_PX) & (x < nx - MARGIN_PX)
              & (y >= MARGIN_PX) & (y < ny - MARGIN_PX))
    x, y = x[inside], y[inside]
    if len(x) == 0:
        return {"n_gaia_stars": 0}
    _, sky, noise = sigma_clipped_stats(image[::4, ::4], sigma=3, maxiters=5)
    sub = image - sky
    neighbour = (cKDTree(np.c_[x, y]).query(np.c_[x, y], k=2)[0][:, 1] if len(x) > 1
                 else np.full(1, np.inf))
    dx, dy = [], []
    for px, py, gap in zip(x, y, neighbour):
        if gap < ISOLATION_PX:
            continue
        ix, iy = int(round(px)), int(round(py))
        window = sub[iy - _HALF:iy + _HALF + 1, ix - _HALF:ix + _HALF + 1]
        if not np.all(np.isfinite(window)) or window.max() < PEAK_SIGMA * noise:
            continue
        cx, cy = centroid(sub, px, py)
        if not np.isfinite(cx) or np.hypot(cx - px, cy - py) > MAX_MOVE_PX:
            continue
        dx.append(cx - px)
        dy.append(cy - py)
    if not dx:
        return {"n_gaia_stars": 0}
    return {"gaia_dx_px": float(np.median(dx)), "gaia_dy_px": float(np.median(dy)),
            "n_gaia_stars": len(dx)}


def _near(wcs, nx: int, ny: int, ra: np.ndarray, dec: np.ndarray) -> np.ndarray:
    """The copy's rows within the frame's circumscribed circle, so the inverse WCS sees only those."""
    centre = np.radians(wcs.all_pix2world([[nx / 2, ny / 2]], 0)[0])
    corners = np.radians(wcs.all_pix2world([[0, 0], [nx, 0], [0, ny], [nx, ny]], 0))
    radius = 1.05 * max(_separation(centre, c) for c in corners)
    return _separation(centre, np.radians(np.c_[ra, dec]).T) <= radius


def _separation(a, b) -> np.ndarray:
    """Angular separation in radians between ``(ra, dec)`` ``a`` and ``b`` (radians, broadcast)."""
    (ra1, dec1), (ra2, dec2) = a, b
    s = (np.sin((dec2 - dec1) / 2) ** 2
         + np.cos(dec1) * np.cos(dec2) * np.sin((ra2 - ra1) / 2) ** 2)
    return 2 * np.arcsin(np.sqrt(np.clip(s, 0, 1)))
