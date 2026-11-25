# ported-from: none
"""The PSF model a fit uses: the science PSF, or the oversampled stamp kept beside it.

``[psf] fit_model`` (rapid_docs ``decision-flight-psf-fit-oversampled``):
``"native"``, the base's, fits every source with the PSF file it is given,
as `dev` does; ``"oversampled"``, the flight overlay's, fits SFFT's two
PSF-fit catalogs, the xsources and the packet's forced photometry with the
stamp the import keeps beside the science PSF as
``<science PSF stem>_oversampled.fits`` (commissioning's ``epsf_os3``
library, ``OVERSAMP`` 3), so each source is fitted at its own sub-pixel
phase. Every other reader of the science PSF keeps it. SFFT's difference
PSF is the science PSF only without cross-convolution, so
``"oversampled"`` is refused with ``[difference.sfft] crossconv_flag`` true.

A PSF file's oversampling is its ``OVERSAMP`` card, 1 when absent (the
phase-averaged library stamps carry none; the psf stage writes 1).
photutils' ``ImagePSF`` evaluates an oversampled stamp at detector pixels
without renormalising it, so a stamp whose samples sum to
``OVERSAMP ** 2`` has unit flux (:func:`unit_flux`).
"""

from __future__ import annotations

from typing import Any

import numpy as np

#: The values of ``[psf] fit_model``.
FIT_MODELS = ("native", "oversampled")
#: The companion's name is the science PSF's stem with this suffix.
COMPANION_SUFFIX = "_oversampled.fits"


def fit_model(settings: dict[str, Any]) -> str:
    """The run's ``[psf] fit_model``: ``"native"`` when the body has none (a run from before it)."""
    value = settings.get("psf", {}).get("fit_model", "native")
    if value not in FIT_MODELS:
        raise ValueError(f"[psf] fit_model must be one of {FIT_MODELS}, got {value!r}")
    return value


def check(settings: dict[str, Any]) -> str:
    """:func:`fit_model`, refused as ``"oversampled"`` with SFFT's cross-convolution on."""
    model = fit_model(settings)
    if model == "oversampled" and settings["difference"]["sfft"]["crossconv_flag"]:
        raise ValueError(
            "[psf] fit_model = \"oversampled\" with [difference.sfft] crossconv_flag = true: "
            "SFFT's difference PSF is then not the science PSF the oversampled stamp stands for")
    return model


def companion_name(psf_name: str) -> str:
    """``<stem>_oversampled.fits`` for the science PSF file or URL ``psf_name``."""
    name = psf_name.rsplit("/", 1)[-1]
    for ext in (".fits.gz", ".fits"):
        if name.endswith(ext):
            return name[: -len(ext)] + COMPANION_SUFFIX
    raise ValueError(f"{name}: a PSF file is .fits or .fits.gz")


def oversampling(header) -> int:
    """The file's ``OVERSAMP`` card, 1 when it has none."""
    value = int(header.get("OVERSAMP", 1))
    if value < 1:
        raise ValueError(f"OVERSAMP {value}: an oversampling is 1 or more")
    return value


def unit_flux(data: np.ndarray, oversampling: int) -> np.ndarray:
    """The stamp scaled so its samples sum to ``oversampling ** 2``, unit flux for ``ImagePSF``."""
    data = np.nan_to_num(np.asarray(data, dtype=np.float64))
    total = float(np.sum(data))
    if not np.isfinite(total) or total <= 0.0:
        raise ValueError(f"PSF stamp has non-positive total {total}")
    return data * (oversampling ** 2 / total)
