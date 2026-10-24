# ported-from: none
"""The difference image's pixel-to-pixel noise correlation, one number.

New, not `dev`'s: the interpolating kernel of the per-epoch SWarp resample
correlates the reference's noise across neighbouring pixels, and a
difference whose noise is correlated has fewer independent pixels than its
per-pixel sigma says, so detections at a fixed per-pixel threshold rise
(the fixes ledger's caveat on LANCZOS3, fixes task 4, 2026-10-24).

:func:`noise_correlation` is ``BLOCK x sigma_block / sigma_pixel``: the
3-sigma-clipped standard deviation of the means of ``BLOCK x BLOCK`` blocks
with every pixel finite, times ``BLOCK``, over the clipped standard
deviation of the finite pixels. It is 1 for white noise and rises with the
correlation (1.75 for noise smoothed by a 2 x 2 box). Stars and residuals are
what the clip removes; a field with fewer than :data:`MIN_BLOCKS` full
blocks has no value.

This module may import ``rapidpipe.products`` and leaf modules only.
"""

from __future__ import annotations

import numpy as np

#: The side of the averaged blocks, px.
BLOCK = 4
#: Full blocks needed for a value.
MIN_BLOCKS = 100


def noise_correlation(image: np.ndarray, block: int = BLOCK) -> float | None:
    """``block x`` the clipped sigma of full-block means over the clipped sigma of the finite pixels."""
    from astropy.stats import sigma_clipped_stats

    finite = np.isfinite(image)
    ny, nx = (image.shape[0] // block) * block, (image.shape[1] // block) * block
    tiles = image[:ny, :nx].reshape(ny // block, block, nx // block, block)
    full = np.isfinite(tiles).all(axis=(1, 3))
    if int(full.sum()) < MIN_BLOCKS:
        return None
    means = tiles.mean(axis=(1, 3))[full]
    _, _, sigma_pixel = sigma_clipped_stats(image[finite], sigma=3.0, maxiters=5)
    _, _, sigma_block = sigma_clipped_stats(means, sigma=3.0, maxiters=5)
    return float(block * sigma_block / sigma_pixel)
