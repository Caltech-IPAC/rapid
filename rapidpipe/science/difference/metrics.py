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
- :func:`bright_footprint` and :func:`footprint_counts`: the sources within
  a bright reference source's footprint (below), flagged by the stage and
  counted as ``n_in_bright_footprint`` (all kept sources) and
  ``bright_footprint_fraction`` (of the sources above S/N
  :data:`BRIGHT_SNR`); flag, not fix (Ben, 2026-10-24).
- :func:`near_bright_count`: ``n_sources_near_bright_px``, the uncapped
  PSF-fit sources within :data:`NEAR_BRIGHT_PX` of a reference pixel above
  :data:`BRIGHT_PIXEL_LEVEL` (the fixes' task 4 measure: 28 on run 44
  pass 4).
- :func:`ring_count`: ``n_sources_ring_snr_gt20``, the uncapped sources
  above S/N :data:`BRIGHT_SNR` whose nearest of the :data:`RING_TOP_STARS`
  brightest finder stars is :data:`RING_PX` away (the fixes' task 3
  measure, 10 to 20 px: 190 under the truncated stamp, 3 after).

**The bright footprint.** Every reference mosaic catalog source whose
aperture S/N (``FLUX_APER(4)``, the 6 px diameter, over its error) exceeds
:data:`FOOTPRINT_SNR` has a disc of radius
``max(FOOTPRINT_MIN_PX, FOOTPRINT_R15_PX * 10 ** (0.2 * (15 - m)))`` on the
science grid, ``m = zero point - 2.5 log10 FLUX_AUTO`` the source's AB
magnitude on the reference's own zero point, so the radius grows as the
square root of the flux. Derived on run 46's F146 SCA 8 passes 4 to 6
(``001_0001``, jobs 19864 to 19866, 825 such sources on the grid): every
source above S/N 20 near a bright source lies within that law's envelope
(at m 12.8, 22.8 and 24.0 px from the star; at m 15.4, 9.7 px; at m 17
to 18, 4.6 to 6.4 px; the faint ones on the star's core, under 2.5 px),
and the law flags 21 of 23, 15 of 19 and 11 of 15 of them, the re-count's
bright-star counts exactly (runbook part 10 step 5). The diffraction-spike
arms lie inside the disc at every measured star, so the footprint is a
disc. The sources it holds are 50 to 56% of all kept sources there: faint
sources on bright stars, a measurement, not a failure.

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
#: Reference sources above this aperture S/N have a footprint.
FOOTPRINT_SNR = 100.0
#: The footprint's floor and its radius at AB magnitude 15, px (the law above).
FOOTPRINT_MIN_PX, FOOTPRINT_R15_PX = 4.0, 9.5
#: A source within this of a reference pixel above the level is near a bright pixel, px and DN/s.
NEAR_BRIGHT_PX, BRIGHT_PIXEL_LEVEL = 6.0, 1000.0
#: The ring count's stars and its distance range, px.
RING_TOP_STARS, RING_PX = 100, (6.0, 20.0)
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


def footprint_radii(flux: np.ndarray, zero_point: float) -> np.ndarray:
    """The footprint radius of each reference source of ``flux`` (FLUX_AUTO) on ``zero_point``, px."""
    flux = np.asarray(flux, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        mag = zero_point - 2.5 * np.log10(np.where(flux > 0, flux, np.nan))
    radius = FOOTPRINT_R15_PX * 10 ** (0.2 * (15.0 - mag))
    return np.maximum(FOOTPRINT_MIN_PX, np.nan_to_num(radius, nan=0.0))


def bright_footprint(x: np.ndarray, y: np.ndarray, stars_xy: np.ndarray,
                     radii: np.ndarray) -> np.ndarray:
    """True for each source ``(x, y)`` within the radius of one of ``stars_xy``."""
    inside = np.zeros(len(x), dtype=bool)
    if len(x) == 0 or len(stars_xy) == 0:
        return inside
    from scipy.spatial import cKDTree

    tree = cKDTree(np.asarray(stars_xy, dtype=float))
    points = np.column_stack([x, y])
    for k, near in enumerate(tree.query_ball_point(points, float(np.max(radii)))):
        if near:
            d = np.hypot(stars_xy[near, 0] - x[k], stars_xy[near, 1] - y[k])
            inside[k] = bool(np.any(d <= radii[near]))
    return inside


def footprint_counts(inside: np.ndarray, flux: np.ndarray, flux_err: np.ndarray) -> dict[str, float]:
    """``n_in_bright_footprint`` and, with sources above S/N 20, ``bright_footprint_fraction``."""
    out: dict[str, float] = {"n_in_bright_footprint": int(np.sum(inside))}
    with np.errstate(divide="ignore", invalid="ignore"):
        bright = flux / flux_err > BRIGHT_SNR
    if bright.any():
        out["bright_footprint_fraction"] = float(np.mean(inside[bright]))
    return out


def near_bright_count(x: np.ndarray, y: np.ndarray, reference: np.ndarray) -> int:
    """The sources within :data:`NEAR_BRIGHT_PX` of a ``reference`` pixel above :data:`BRIGHT_PIXEL_LEVEL`."""
    rows, cols = np.nonzero(np.nan_to_num(reference) > BRIGHT_PIXEL_LEVEL)
    if len(x) == 0 or len(rows) == 0:
        return 0
    from scipy.spatial import cKDTree

    dist, _ = cKDTree(np.column_stack([cols, rows])).query(np.column_stack([x, y]))
    return int(np.sum(dist <= NEAR_BRIGHT_PX))


def ring_count(x: np.ndarray, y: np.ndarray, flux: np.ndarray, flux_err: np.ndarray,
               found: np.ndarray) -> int | None:
    """Sources above S/N 20 whose nearest top finder star lies :data:`RING_PX` away; ``None`` without stars."""
    if len(found) == 0:
        return None
    top = found[np.argsort(-found[:, 2])][:RING_TOP_STARS, :2]
    with np.errstate(divide="ignore", invalid="ignore"):
        bright = flux / flux_err > BRIGHT_SNR
    if not bright.any():
        return 0
    from scipy.spatial import cKDTree

    dist, _ = cKDTree(top).query(np.column_stack([x[bright], y[bright]]))
    return int(np.sum((dist >= RING_PX[0]) & (dist < RING_PX[1])))


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
