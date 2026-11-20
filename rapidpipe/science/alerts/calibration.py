# ported-from: none
"""Flux calibration to nJy: an image's zero point and its pixel area map's value at a position.

A flux measured on a difference image is in DN/s on its science image's
scale. With that image's AB zero point ``zptmag`` (the magnitude of 1 DN/s)
and its pixel area map's value ``factor`` at the measurement's position,

    flux [nJy] = flux [DN/s] * 10 ** (-0.4 * (zptmag - 31.4)) * factor

(rapid_docs ``decision-fluxes-in-njy``). The zero point folds in the
detector's nominal pixel area, and the map is each pixel's area over the
nominal one, so no map (a factor of 1) is the correction by the nominal
area. No aperture or encircled-energy correction is applied here.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Hashable, Mapping
from pathlib import Path

import numpy as np

#: The AB magnitude of 1 nJy: -2.5 log10(1e-9 Jy / 3631 Jy).
NJY_ZERO_POINT = 31.4


def njy_per_dn_s(zptmag: float | None) -> float | None:
    """The flux in nJy of 1 DN/s on an image of AB zero point ``zptmag``; None when it has none."""
    if zptmag is None or not math.isfinite(zptmag):
        return None
    return 10.0 ** (-0.4 * (zptmag - NJY_ZERO_POINT))


def load_area_map(path: Path) -> np.ndarray:
    """The pixel area map of a CRDS ``area`` reference file: a 2-D array indexed ``[y, x]``."""
    import asdf

    with asdf.open(path, lazy_load=False, memmap=False) as af:
        return np.asarray(af["roman"]["data"], dtype=np.float64)


def interpolate_area_map(area_map: np.ndarray, x: float, y: float) -> float | None:
    """The map's value at a position, interpolated bilinearly between pixel centers.

    Parameters
    ----------
    area_map : numpy.ndarray
        The pixel area map, indexed ``[y, x]``.
    x, y : float
        The position [pix], zero-based, whole numbers at pixel centers.

    Returns
    -------
    float or None
        The factor; None for a position off the detector (more than half a
        pixel beyond the outer pixel centers) or not finite. A position in
        the outer half pixel takes the edge's value.
    """
    ny, nx = area_map.shape
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    if not (-0.5 <= x <= nx - 0.5 and -0.5 <= y <= ny - 0.5):
        return None
    x, y = min(max(x, 0.0), nx - 1.0), min(max(y, 0.0), ny - 1.0)
    x0, y0 = min(int(x), nx - 2), min(int(y), ny - 2)
    fx, fy = x - x0, y - y0
    value = (area_map[y0, x0] * (1 - fx) * (1 - fy) + area_map[y0, x0 + 1] * fx * (1 - fy)
             + area_map[y0 + 1, x0] * (1 - fx) * fy + area_map[y0 + 1, x0 + 1] * fx * fy)
    return float(value) if math.isfinite(value) else None


def area_factors(positions: Mapping[Hashable, tuple[str | None, float, float]],
                 load: Callable[[str], np.ndarray | None]) -> dict[Hashable, float]:
    """The pixel area factor of each position, each distinct map loaded once and one at a time.

    Parameters
    ----------
    positions : mapping
        Any key to ``(map name, x, y)``: the pixel area map's CRDS name, or
        None for an image that names none, and the position on that image
        [pix], zero-based.
    load : callable
        A map's name to its array (:func:`load_area_map`'s), or None when
        the map cannot be had.

    Returns
    -------
    dict
        The factor per key (:func:`interpolate_area_map`), only for the
        keys that have one: a key whose image names no map, whose map could
        not be loaded, or whose position is off the detector is left out.
    """
    by_name: dict[str, list[tuple[Hashable, float, float]]] = {}
    for key, (name, x, y) in positions.items():
        if name:
            by_name.setdefault(name, []).append((key, x, y))
    factors: dict[Hashable, float] = {}
    for name in sorted(by_name):
        area_map = load(name)
        if area_map is None:
            continue
        for key, x, y in by_name[name]:
            factor = interpolate_area_map(area_map, x, y)
            if factor is not None:
                factors[key] = factor
        del area_map  # one map in memory at a time
    return factors


def calibrate(flux: float | None, scale: float | None, factor: float | None) -> float | None:
    """A flux or its uncertainty in nJy: ``flux * scale * factor``, a null factor read as 1.

    Parameters
    ----------
    flux : float or None
        The instrumental value [DN/s].
    scale : float or None
        :func:`njy_per_dn_s` of the image's zero point.
    factor : float or None
        :func:`interpolate_area_map` at the measurement's position; None applies none.

    Returns
    -------
    float or None
        The value [nJy]; None when the flux or the scale is None.
    """
    if flux is None or scale is None:
        return None
    return flux * scale * (1.0 if factor is None else factor)
