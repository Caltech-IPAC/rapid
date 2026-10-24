# ported-from: pipeline/referenceImageSubs.py, modules/utils/rapid_pipeline_subs.py @ fdc7fa79
"""The reference image's InfoBits and status, as `dev` grades a new reference.

`dev`: ``evaluate_refimage_quality``, ``compute_refimage_infobits``,
``get_refimage_infobit_cards`` and ``compute_robust_spread``
(``pipeline/referenceImageSubs.py``, dev cb53d2c7 and fdc7fa79) and
``format_infobits_comment`` (``modules/utils/rapid_pipeline_subs.py``,
dev 2851627b). The bits are defined in ``cdf/infobitsref.txt``, their
thresholds and the fatal bits in ``[reference.infobits]`` (`dev`'s
``[REFIMAGE_INFOBITS]``). A bit is set when its condition holds; a metric
that could not be computed (NaN or absent) sets none. The status is 0
(bad) when a fatal bit is set, else 1.

Departure (reference page): `dev` always makes the Photutils reference
catalog and has a reference PSF, so it measures the PSF-fit catalog's
source count and limiting magnitude and the formula limiting magnitude.
This build makes neither (``[reference.psfcat] enabled = false``), so
those metrics are absent and bits 5, 6, 7 and 16 are never set: a catalog
that was not attempted is not one that failed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS
from astropy.wcs.utils import proj_plane_pixel_scales

#: ``[reference.infobits]``'s thresholds, by key.
THRESHOLDS = ("t00", "t01", "t02", "t03", "t04", "t05", "t06", "t07", "t08", "t10")
#: The file in ``[paths] cfg_path`` defining the bits.
DEFINITIONS_FILE = "infobitsref.txt"

_CARD_FORMATS = {"t00": "{:.1f}", "t01": "{:.2f}", "t02": "{:.1f}", "t03": "{:.2f}",
                 "t04": "{:.2f}", "t05": "{:.1f}", "t06": "{:.1f}", "t07": "{:d}",
                 "t08": "{:d}", "t10": "{:.2f}"}


@dataclass(frozen=True)
class Grade:
    """A reference's InfoBits, status, header definition cards and the metrics behind them."""

    infobits: int
    status: int
    cards: list[tuple[str, int, str]]
    metrics: dict[str, Any]

    @property
    def bits(self) -> list[int]:
        return set_bits(self.infobits)


def check_params(table: Mapping[str, Any]) -> dict[str, Any]:
    """Validate ``[reference.infobits]``; every problem is a ``ValueError``."""
    params: dict[str, Any] = {}
    for key in THRESHOLDS + ("fwhm_percentile", "maglim_snr"):
        value = table[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"[reference.infobits] {key} must be a number, got {value!r}")
        params[key] = int(value) if key in ("t07", "t08") else float(value)
    fatal = table["fatalinfobits"]
    if (not isinstance(fatal, list)
            or not all(isinstance(b, int) and not isinstance(b, bool) and 0 <= b < 32
                       for b in fatal)):
        raise ValueError(f"[reference.infobits] fatalinfobits must be a list of bit numbers "
                         f"0 to 31, got {fatal!r}")
    params["fatalinfobits"] = list(fatal)
    return params


def set_bits(infobits: int) -> list[int]:
    return [bit for bit in range(32) if int(infobits) & 2**bit]


def format_infobits_comment(infobits: int) -> str:
    """`dev` ``format_infobits_comment``: ``infoBits string; bit(s) = 4, 9`` or ``= none``."""
    bits = [str(bit) for bit in set_bits(infobits)]
    return "infoBits string; bit(s) = " + (", ".join(bits) if bits else "none")


def compute_robust_spread(values) -> float:
    """``0.5 * (p84 - p16)`` of the finite values; NaN for fewer than two."""
    v = np.asarray(values, dtype=np.float64)
    v = v[np.isfinite(v)]
    if len(v) < 2:
        return np.nan
    p16, p84 = np.percentile(v, [16.0, 84.0])
    return float(0.5 * (p84 - p16))


def compute_refimage_infobits(metrics: Mapping[str, Any],
                              params: Mapping[str, Any]) -> tuple[int, int]:
    """`dev` ``compute_refimage_infobits``: ``(infobits, status)``.

    ``metrics``: ``gmedian``, ``datascale`` (DN/s), ``pctbad`` (per cent of
    covered pixels), ``fwhm_med_arcsec``, ``fwhm_pctl_arcsec``,
    ``maglimcat``, ``maglimit`` (mag), ``npsfcat``, ``nsexcat``,
    ``zptmag_rms`` (mag) and ``psfcat_flag``; absent or NaN sets no bit, and
    an absent ``psfcat_flag`` is not a failed catalog.
    """
    def above(name: str, threshold: float) -> bool:
        v = metrics.get(name)
        return v is not None and bool(np.isfinite(v)) and v > threshold

    def below(name: str, threshold: float) -> bool:
        v = metrics.get(name)
        return v is not None and bool(np.isfinite(v)) and v < threshold

    conditions = {0: above("gmedian", params["t00"]),
                  1: above("datascale", params["t01"]),
                  2: above("pctbad", params["t02"]),
                  3: above("fwhm_med_arcsec", params["t03"]),
                  4: above("fwhm_pctl_arcsec", params["t04"]),
                  5: below("maglimcat", params["t05"]),
                  6: below("maglimit", params["t06"]),
                  7: below("npsfcat", params["t07"]),
                  8: below("nsexcat", params["t08"]),
                  10: above("zptmag_rms", params["t10"]),
                  16: not bool(metrics.get("psfcat_flag", True))}
    infobits = 0
    for bit, condition in conditions.items():
        if condition:
            infobits |= 2**bit
    fatal = sum(2**b for b in params["fatalinfobits"])
    status = 0 if (infobits & fatal) else 1
    return infobits, status


def infobit_cards(definitions_path: str | Path,
                  params: Mapping[str, Any]) -> list[tuple[str, int, str]]:
    """`dev` ``get_refimage_infobit_cards``: ``(BITnn, nn, definition)`` per bit not RESERVED."""
    cards = []
    with open(definitions_path) as f:
        for line in f:
            if "RESERVED" in line or "=" not in line:
                continue
            keyword, rest = line.split("=", 1)
            value, comment = rest.split("/", 1)
            comment = comment.strip()
            for key, fmt in _CARD_FORMATS.items():
                comment = comment.replace(key, fmt.format(params[key]))
            comment = comment.replace("pXX", f"{params['fwhm_percentile']:g}TH")
            cards.append((keyword.strip(), int(value), comment))
    return cards


def pixel_scale_arcsec(header: fits.Header) -> float:
    return float(np.mean(proj_plane_pixel_scales(WCS(header)))) * 3600.0


def bad_pixel_percent(image: np.ndarray, coverage: np.ndarray, satcount: int) -> float:
    """`dev`: NaN pixels inside the covered area plus the saturated pixels, per cent of covered."""
    covered = coverage > 0
    ncovered = int(np.count_nonzero(covered))
    if ncovered == 0:
        return np.nan
    nbad = int(np.count_nonzero(covered & ~np.isfinite(image))) + int(satcount)
    return 100.0 * nbad / ncovered


def grade(image_path: Path, coverage_path: Path, definitions_path: str | Path,
          params: Mapping[str, Any], *, gmedian: float, datascale: float, satcount: int,
          fwhm_mode_pix: float, fwhm_pctl_pix: float, nsexcat: int,
          input_zptmags) -> Grade:
    """`dev` ``evaluate_refimage_quality`` without the PSF-fit catalog's metrics (above)."""
    with fits.open(image_path) as hdul:
        scale = pixel_scale_arcsec(hdul[0].header)
        image = np.asarray(hdul[0].data)
        with fits.open(coverage_path) as cov:
            pctbad = bad_pixel_percent(image, np.asarray(cov[0].data), satcount)
        del image

    def arcsec(fwhm_pix: float) -> float:
        fwhm_pix = float(fwhm_pix)
        return fwhm_pix * scale if np.isfinite(fwhm_pix) and fwhm_pix > 0.0 else np.nan

    metrics = {"gmedian": float(gmedian), "datascale": float(datascale), "pctbad": pctbad,
               "fwhm_med_arcsec": arcsec(fwhm_mode_pix),
               "fwhm_pctl_arcsec": arcsec(fwhm_pctl_pix),
               "nsexcat": int(nsexcat), "zptmag_rms": compute_robust_spread(input_zptmags)}
    infobits, status = compute_refimage_infobits(metrics, params)
    return Grade(infobits=infobits, status=status,
                 cards=infobit_cards(definitions_path, params), metrics=metrics)
