# ported-from: none
"""The reference-image PSF measured from the resampled reference's own stars.

New science, not `dev`'s: `dev` hands ZOGY a static, offline-compiled
reference PSF per detector, and the rebuild's ``psf`` stage handed it the
science ePSF for both images. ZOGY convolves each image with the other's
PSF, so the PSF it needs for the reference is the one of the image it is
given: the awaicgen coadd after the per-epoch SWarp resample onto the
science grid. Each of the two resamplings widens it (run 17, r00342 F146
SCA 8, pass 4: star sigma science 0.952 px, mosaic 1.039, resampled 1.100,
the 15% the images ledger found), and the widening depends on the
resampling, so the stamp is measured per job on the image ZOGY sees
(``LEDGER-fixes.md`` sections 2 and 5(a), 2026-10-23).

:func:`measure` takes the resampled, background-subtracted reference and
the pixels it may use and returns a ``SIZE x SIZE`` stamp, unit sum,
re-centred on the central pixel, with what it measured:

- stars: ``DAOStarFinder`` at :data:`THRESHOLD_SIGMA` times the clipped
  standard deviation of the usable pixels, kept when the peak is in
  :data:`PEAK_RANGE`, the sharpness in :data:`SHARPNESS_RANGE`, both
  roundnesses within :data:`ROUNDNESS_MAX`, no other detection within
  :data:`ISOLATION_PX`, every pixel of the star's stamp usable, and its own
  width between the science PSF's and that plus :data:`WIDTH_EXCESS_MAX_PX`;
  the brightest :data:`MAX_STARS` by flux;
- fewer than :data:`MIN_STARS` raises :class:`TooFewStars` (``refpsf: N
  stars, 10 needed``): no fallback, a sparse field is the next experiment;
- the ePSF: ``EPSFBuilder`` at :data:`OVERSAMPLE` on :data:`STAMP_PX`
  stamps, evaluated at the pixel centres of the output stamp (an effective
  PSF evaluated at a pixel centre is that pixel's value), shifted until its
  centroid is within :data:`CENTRING_TOLERANCE_PX` of the central pixel;
- the wings: the science PSF's beyond :data:`WING_RADII_PX`, cosine-tapered
  into the measured core between the two radii, the stamp normalised to
  unit sum. The ePSF is built on :data:`STAMP_PX` stamps, so it holds
  nothing beyond 15 px, where the science PSF still holds 3.3% of its flux
  (F146 SCA 8); ZOGY, given the truncated stamp alone, left a ring 10 to
  20 px out at every bright star (run 42 pass 4: 190 of 449 sources above
  S/N 20 there, against 3 of 80 before). A sub-pixel resampling kernel
  leaves the wings unchanged (6 to 10 px: science 0.047 of the flux, the
  measured stamp 0.050) and 38 stars do not measure them at the 1e-4
  level, so the science PSF's are the better estimate there. The window of
  :func:`moments` lies inside the measured core.

The values are constants, not settings: no measured case has needed a
different one (foundation plan Part 1). Their measurement, on run 17's
pass 4 resampled reference (the fixes ledger): 73 stars on the SCA pass the
cuts, sigma 1.064 to 1.091 px from 10 to 100 stars at oversample 4 and
31 px stamps (oversample 1 halves the peak and is not used), 4.7 s for the
whole chain. The ledger's sharpness and roundness values were not
recorded; on the same image a sharpness cut alone lets defect pixels and
galaxies in and the built stamp comes out at sigma 0.33 to 0.80, so the
width cut (fixes task 3, 2026-10-24) is what keeps the stars.

:func:`moments` is the one measure of a stamp the stage records for both
PSFs: the centroid and the second-moment sigma within :data:`MOMENT_RADIUS_PX`
of it (the images ledger's reader's measure).

This module may import ``rapidpipe.products`` and leaf modules only.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: Detection threshold, in clipped standard deviations of the usable pixels.
THRESHOLD_SIGMA = 8.0
#: The finder's kernel FWHM, px: the resampled reference's star sigma 1.10 x 2.355.
FWHM_PX = 2.6
#: Peaks kept, in the image's units (DN/s for an awaicgen reference): faint stars are noisy, bright ones near saturation.
PEAK_RANGE = (20.0, 3500.0)
#: DAOStarFinder's sharpness range and roundness magnitude kept (its defaults). On a resampled
#: image the stars' sharpness spans 0.4 to 1.0 with the sub-pixel phase, overlapping defect
#: pixels and galaxies, so these are loose and the width cut below does the separating.
SHARPNESS_RANGE = (0.2, 1.0)
ROUNDNESS_MAX = 1.0
#: A star's own width (:func:`moments` on its stamp) is kept from the science PSF's sigma to
#: that plus this, px: resampling only widens a PSF, so narrower is a defect pixel, and much
#: wider is a galaxy. Run 17 pass 4 (F146 SCA 8, science 0.947): stars 1.06 to 1.14,
#: defect pixels 0.48 to 0.66, galaxies 1.55 to 1.84.
WIDTH_EXCESS_MAX_PX = 0.4
#: No other detection within this distance, px: a neighbour's wings stay out of the stamp.
ISOLATION_PX = 14.0
#: The builder's oversampling and the side of each star's stamp, px.
OVERSAMPLE, STAMP_PX = 4, 31
#: Stars needed and stars used.
MIN_STARS, MAX_STARS = 10, 100
#: The output stamp's side, px: the science PSF's.
SIZE = 61
#: The centroid's largest offset from the central pixel after re-centring, px.
CENTRING_TOLERANCE_PX = 0.01
#: The window of :func:`moments`, px.
MOMENT_RADIUS_PX = 4.0
#: Measured core within the first radius, the science PSF's wings beyond the second, px.
WING_RADII_PX = (6.0, 10.0)


class TooFewStars(ValueError):
    """Fewer than :data:`MIN_STARS` stars passed the cuts."""

    def __init__(self, stars: int):
        super().__init__(f"refpsf: {stars} stars, {MIN_STARS} needed")
        self.stars = stars


@dataclass(frozen=True)
class ReferencePsf:
    """The measured stamp and what :func:`measure` recorded."""

    stamp: np.ndarray
    stars: int
    detections: int
    passed_cuts: int
    sigma_px: float
    centroid_px: tuple[float, float]


def moments(stamp: np.ndarray, radius: float = MOMENT_RADIUS_PX) -> tuple[float, float, float]:
    """``(x, y, sigma)``: the centroid relative to the central pixel and the second-moment sigma.

    Both within ``radius`` of the central pixel for the centroid, then of
    the centroid for the sigma, ``sigma = sqrt((Mxx + Myy) / 2)``.
    """
    rows, cols = np.indices(stamp.shape, dtype=float)
    cy, cx = (stamp.shape[0] - 1) / 2, (stamp.shape[1] - 1) / 2
    near = (cols - cx) ** 2 + (rows - cy) ** 2 <= radius ** 2
    w = np.where(near, stamp, 0.0)
    x = float((w * cols).sum() / w.sum())
    y = float((w * rows).sum() / w.sum())
    r2 = (cols - x) ** 2 + (rows - y) ** 2
    w = np.where(r2 <= radius ** 2, stamp, 0.0)
    sigma = float(np.sqrt((w * r2).sum() / w.sum() / 2))
    return x - cx, y - cy, sigma


def measure(image: np.ndarray, usable: np.ndarray, science_psf: np.ndarray) -> ReferencePsf:
    """The reference PSF of ``image`` from its stars.

    ``usable`` marks the pixels it may read; ``science_psf`` is the science
    PSF's ``SIZE x SIZE`` stamp: its :func:`moments` sigma is the floor of a
    star's width and its wings are the stamp's.
    """
    if science_psf.shape != (SIZE, SIZE):
        raise ValueError(f"refpsf: the science PSF is {science_psf.shape}, not {SIZE} x {SIZE}")
    science_psf = science_psf / science_psf.sum()
    science_sigma_px = moments(science_psf)[2]
    from astropy.nddata import NDData
    from astropy.stats import sigma_clipped_stats
    from astropy.table import Table
    from photutils.detection import DAOStarFinder
    from photutils.psf import EPSFBuilder, extract_stars

    usable = usable & np.isfinite(image)
    data = np.where(usable, image, 0.0).astype(np.float64)
    _, median, std = sigma_clipped_stats(data[usable], sigma=3.0, maxiters=5)
    data = np.where(usable, data - median, 0.0)
    finder = DAOStarFinder(THRESHOLD_SIGMA * std, FWHM_PX, sharpness_range=None,
                           roundness_range=None, exclude_border=True)
    found = finder(data, mask=~usable)
    if found is None or len(found) == 0:
        raise TooFewStars(0)
    x = np.asarray(found["x_centroid"], dtype=float)
    y = np.asarray(found["y_centroid"], dtype=float)
    peak = np.asarray(found["peak"], dtype=float)
    sharp = np.asarray(found["sharpness"], dtype=float)
    round1 = np.asarray(found["roundness1"], dtype=float)
    round2 = np.asarray(found["roundness2"], dtype=float)
    flux = np.asarray(found["flux"], dtype=float)

    keep = ((peak >= PEAK_RANGE[0]) & (peak <= PEAK_RANGE[1])
            & (sharp >= SHARPNESS_RANGE[0]) & (sharp <= SHARPNESS_RANGE[1])
            & (np.abs(round1) <= ROUNDNESS_MAX) & (np.abs(round2) <= ROUNDNESS_MAX))
    keep &= _isolated(x, y, ISOLATION_PX)
    keep &= _stamp_usable(usable, x, y, STAMP_PX)
    half = STAMP_PX // 2
    for i in np.flatnonzero(keep):
        xi, yi = int(round(x[i])), int(round(y[i]))
        width = moments(data[yi - half:yi + half + 1, xi - half:xi + half + 1])[2]
        keep[i] = science_sigma_px <= width <= science_sigma_px + WIDTH_EXCESS_MAX_PX
    passed = int(keep.sum())
    if passed < MIN_STARS:
        raise TooFewStars(passed)
    chosen = np.flatnonzero(keep)[np.argsort(-flux[keep])][:MAX_STARS]

    stars = extract_stars(NDData(data), Table({"x": x[chosen], "y": y[chosen]}),
                          size=(STAMP_PX, STAMP_PX))
    built = EPSFBuilder(oversampling=OVERSAMPLE, progress_bar=False)(stars)
    stamp = _with_wings(_evaluate_centred(built.epsf, SIZE), science_psf)
    dx, dy, sigma = moments(stamp)
    return ReferencePsf(stamp=stamp, stars=len(chosen), detections=len(found), passed_cuts=passed,
                        sigma_px=sigma, centroid_px=(dx, dy))


def _with_wings(core: np.ndarray, science_psf: np.ndarray) -> np.ndarray:
    """``core`` within the first of :data:`WING_RADII_PX`, ``science_psf`` beyond the second, unit sum."""
    inner, outer = WING_RADII_PX
    centre = (core.shape[0] - 1) / 2
    rows, cols = np.indices(core.shape, dtype=float)
    r = np.hypot(cols - centre, rows - centre)
    w = np.clip((r - inner) / (outer - inner), 0.0, 1.0)
    taper = 0.5 * (1 + np.cos(np.pi * w))
    stamp = taper * core + (1 - taper) * science_psf
    return stamp / stamp.sum()


def _isolated(x: np.ndarray, y: np.ndarray, radius: float) -> np.ndarray:
    """True for each point with no other point within ``radius``."""
    from scipy.spatial import cKDTree

    pairs = cKDTree(np.column_stack([x, y])).query_pairs(radius, output_type="ndarray")
    crowded = np.zeros(len(x), dtype=bool)
    crowded[pairs.ravel()] = True
    return ~crowded


def _stamp_usable(usable: np.ndarray, x: np.ndarray, y: np.ndarray, size: int) -> np.ndarray:
    """True for each position whose ``size x size`` stamp lies inside ``usable`` entirely."""
    half = size // 2
    ny, nx = usable.shape
    out = np.zeros(len(x), dtype=bool)
    for i, (xi, yi) in enumerate(zip(np.rint(x).astype(int), np.rint(y).astype(int))):
        if half <= xi < nx - half and half <= yi < ny - half:
            out[i] = bool(usable[yi - half:yi + half + 1, xi - half:xi + half + 1].all())
    return out


def _evaluate_centred(epsf, size: int, iterations: int = 10) -> np.ndarray:
    """``epsf`` at the pixel centres of a ``size x size`` stamp, centroid on the centre, unit sum."""
    centre = (size - 1) / 2
    cols, rows = np.meshgrid(np.arange(size, dtype=float), np.arange(size, dtype=float))
    x0 = y0 = centre
    for _ in range(iterations):
        stamp = np.asarray(epsf.evaluate(cols, rows, 1.0, x0, y0), dtype=np.float64)
        dx, dy, _ = moments(stamp)
        if max(abs(dx), abs(dy)) < CENTRING_TOLERANCE_PX:
            return stamp / stamp.sum()
        x0, y0 = x0 - dx, y0 - dy
    raise ValueError(f"refpsf: centroid still ({dx:.4f}, {dy:.4f}) px off centre after "
                     f"{iterations} shifts")
