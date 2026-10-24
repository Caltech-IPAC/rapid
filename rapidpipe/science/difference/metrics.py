# ported-from: none
"""The difference job's quality numbers, from arrays the job already holds.

New, not `dev`'s: `dev` records no per-image quality numbers beyond the
gain-matching and catalog counts. These are the measurements the images
ledger made by hand on run 17 (``LEDGER-images.md``, 2026-10-23), which
would have shown the null test's detections to be artefacts on the first
run; the difference stage records them under ``metrics`` in its manifest
and the ``job_metrics`` view exposes them. Every value is a number; a
measurement this module cannot make (too few stars, no sources) is absent
from the dict it returns, never zero.

The star list is the reference PSF measurement's own finder output
(:class:`rapidpipe.science.difference.refpsf.ReferencePsf` ``found``,
DAOStarFinder at 8 sigma on the resampled, background-subtracted
reference), so no finder runs twice. All positions are on the science
grid, 0-based pixels.

- :func:`residuals`: ``core_fraction`` and ``ring_fraction``, the
  difference's sum within :data:`CORE_RADIUS_PX` and between it and
  :data:`RING_RADIUS_PX` of a star over the star's reference flux (within
  :data:`APERTURE_PX` less the :data:`SKY_ANNULUS_PX` median), each the
  median over the brightest :data:`TOP_STARS` of the
  :data:`LIST_STARS` brightest finder stars that have no other within
  :data:`ISOLATION_PX` and lie :data:`MARGIN_PX` from an edge, a star whose
  own second-moment sigma is below :data:`DEFECT_SIGMA_PX` set aside as a
  defect pixel; ``n_residual_stars`` beside them. Run 17 pass 4: +0.030,
  -0.027 over 26 stars.
- :func:`registration`: ``registration_dx_px`` and ``registration_dy_px``,
  the median centroid of the science image minus the reference's at the
  reference PSF's stars, ``n_registration_stars`` beside them. Run 17 pass
  4: -0.017, +0.014.
- :func:`usable_fraction`: the difference image's pixels that are finite
  and at or above the coverage threshold, over all its pixels, so fewer
  detections cannot hide less sky.
- :func:`source_counts`: ``n_sources``, ``n_sources_snr_gt20`` (fit flux
  over its error) and ``on_reference_star_fraction``, the share of the
  sources within :data:`ON_STAR_PX` of a reference mosaic catalog source
  mapped onto the science grid through the two WCSs (run 17 pass 4: 74%
  against the finder's list).

The constants are the ledger's, not settings: no measured case has needed
a different one. This module may import leaf modules only.
"""

from __future__ import annotations

import numpy as np

#: Residual stars: the brightest this many of the isolated finder stars.
TOP_STARS = 30
#: The finder stars the isolation is judged among, by flux.
LIST_STARS = 2000
#: No other of the :data:`LIST_STARS` within this, px; and this far from an edge, px.
ISOLATION_PX, MARGIN_PX = 12.0, 40
#: The difference's core and the ring's outer radius, px.
CORE_RADIUS_PX, RING_RADIUS_PX = 2.5, 6.0
#: The star's flux aperture and the sky annulus, px.
APERTURE_PX, SKY_ANNULUS_PX = 5.0, (8.0, 12.0)
#: A finder star whose own second-moment sigma is below this is a defect pixel, px.
DEFECT_SIGMA_PX = 0.8
#: A source within this of a reference catalog source sits on it, px.
ON_STAR_PX = 3.0
#: The fit S/N above which a source or packet counts as bright.
BRIGHT_SNR = 20.0
#: The centroid's Gaussian weight sigma and half window, px.
CENTROID_SIGMA_PX, CENTROID_HALF = 1.5, 7
_STAMP_HALF = 15


def residual_stars(found: np.ndarray, shape: tuple[int, int], reference: np.ndarray) -> list[tuple[float, float]]:
    """The residual stars' positions: ``found`` rows are ``(x, y, flux)``."""
    if len(found) == 0:
        return []
    found = found[np.argsort(-found[:, 2])][:LIST_STARS]
    from scipy.spatial import cKDTree

    tree = cKDTree(found[:, :2])
    ny, nx = shape
    chosen = []
    for x, y, _ in found:
        if len(chosen) == TOP_STARS:
            break
        if not (MARGIN_PX <= x < nx - MARGIN_PX and MARGIN_PX <= y < ny - MARGIN_PX):
            continue
        if len(tree.query_ball_point([x, y], ISOLATION_PX)) > 1:
            continue
        chosen.append((float(x), float(y)))
    return [(x, y) for x, y in chosen if _sigma(_stamp(reference, x, y)) >= DEFECT_SIGMA_PX]


def residuals(reference: np.ndarray, difference: np.ndarray,
              stars: list[tuple[float, float]]) -> dict[str, float]:
    """``core_fraction``, ``ring_fraction`` and ``n_residual_stars``; ``{}`` without stars."""
    cores, rings = [], []
    for x, y in stars:
        cx, cy = centroid(reference, x, y)
        ix, iy = int(round(x)), int(round(y))
        rows, cols = np.mgrid[iy - _STAMP_HALF:iy + _STAMP_HALF + 1,
                              ix - _STAMP_HALF:ix + _STAMP_HALF + 1]
        r = np.hypot(cols - cx, rows - cy)
        ref = _stamp(reference, x, y)
        sky = (r >= SKY_ANNULUS_PX[0]) & (r < SKY_ANNULUS_PX[1])
        flux = float((ref - np.nanmedian(ref[sky]))[r < APERTURE_PX].sum())
        diff = np.nan_to_num(_stamp(difference, x, y))
        diff = diff - np.median(diff[sky])
        if not flux > 0:
            continue
        cores.append(diff[r < CORE_RADIUS_PX].sum() / flux)
        rings.append(diff[(r >= CORE_RADIUS_PX) & (r < RING_RADIUS_PX)].sum() / flux)
    if not cores:
        return {}
    return {"core_fraction": float(np.median(cores)), "ring_fraction": float(np.median(rings)),
            "n_residual_stars": len(cores)}


def registration(science: np.ndarray, reference: np.ndarray, stars: np.ndarray) -> dict[str, float]:
    """``registration_dx_px``, ``registration_dy_px``, ``n_registration_stars``; ``{}`` without stars."""
    offsets = []
    for x, y in stars:
        sx, sy = centroid(science, x, y)
        rx, ry = centroid(reference, x, y)
        if np.isfinite([sx, sy, rx, ry]).all():
            offsets.append((sx - rx, sy - ry))
    if not offsets:
        return {}
    dx, dy = np.median(np.asarray(offsets), axis=0)
    return {"registration_dx_px": float(dx), "registration_dy_px": float(dy),
            "n_registration_stars": len(offsets)}


def usable_fraction(difference: np.ndarray, coverage: np.ndarray, threshold: float) -> float:
    """The share of the difference's pixels finite and covered at ``threshold``."""
    return float((np.isfinite(difference) & (coverage >= threshold)).mean())


def source_counts(x: np.ndarray, y: np.ndarray, flux: np.ndarray, flux_err: np.ndarray,
                  catalog_xy: np.ndarray | None) -> dict[str, float]:
    """``n_sources``, ``n_sources_snr_gt20`` and, given catalog positions, ``on_reference_star_fraction``."""
    out: dict[str, float] = {"n_sources": int(len(x)),
                             "n_sources_snr_gt20": int(np.sum(flux / flux_err > BRIGHT_SNR))}
    if len(x) and catalog_xy is not None and len(catalog_xy):
        from scipy.spatial import cKDTree

        dist, _ = cKDTree(catalog_xy).query(np.column_stack([x, y]))
        out["on_reference_star_fraction"] = float(np.mean(dist <= ON_STAR_PX))
    return out


def centroid(image: np.ndarray, x: float, y: float, iterations: int = 20) -> tuple[float, float]:
    """The Gaussian-weighted iterative centroid near ``(x, y)`` (the ledger's measure)."""
    h = CENTROID_HALF
    ny, nx = image.shape
    for _ in range(iterations):
        ix, iy = int(round(x)), int(round(y))
        if not (h <= ix < nx - h and h <= iy < ny - h):
            return float("nan"), float("nan")
        cut = np.nan_to_num(image[iy - h:iy + h + 1, ix - h:ix + h + 1])
        rows, cols = np.mgrid[iy - h:iy + h + 1, ix - h:ix + h + 1]
        w = np.exp(-((cols - x) ** 2 + (rows - y) ** 2) / (2 * CENTROID_SIGMA_PX ** 2)) * cut
        if not w.sum() > 0:
            return float("nan"), float("nan")
        nx_, ny_ = (w * cols).sum() / w.sum(), (w * rows).sum() / w.sum()
        if abs(nx_ - x) < 1e-4 and abs(ny_ - y) < 1e-4:
            return float(nx_), float(ny_)
        x, y = nx_, ny_
    return float(x), float(y)


def _stamp(image: np.ndarray, x: float, y: float) -> np.ndarray:
    ix, iy = int(round(x)), int(round(y))
    return image[iy - _STAMP_HALF:iy + _STAMP_HALF + 1, ix - _STAMP_HALF:ix + _STAMP_HALF + 1].astype(float)


def _sigma(stamp: np.ndarray, radius: float = 4.0) -> float:
    from rapidpipe.science.difference.refpsf import moments

    return moments(np.nan_to_num(stamp), radius)[2]
