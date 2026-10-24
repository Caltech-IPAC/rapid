# ported-from: none
"""An OpenUniverse ``simple_model`` ASDF file as the intake's multi-extension FITS file.

The OpenUniverse 2024 Roman simulations (roman_imsim on GalSim) publish one
ASDF file per (filter, pointing, SCA), ``roman_<survey>_<filter>_<pointing>
_wfi<sca>_simple.asdf`` under ``<filter>/<pointing>/``, whose tree is a plain
mapping, not a Roman data model: ``data`` (counts per exposure, gain 1,
float64), ``err``, ``dq`` (uint32), ``meta`` (``optical_element`` in the
legacy spelling ``R062``, ``detector`` as ``SCA17``, ``exp_time``,
``mjd_obs``, ``ra_pointing``, ``dec_pointing``, ``zptmag``), ``wcs`` (a gWCS)
and ``catalogs`` (the truth: ``galaxies``, ``point_sources``,
``transients``). :func:`simple_model_to_fits` presents that tree as the data
model :func:`rapidpipe.science.intake.build_fits_file` converts, so the
layout, the SIP fit, the DQ mask and the HDU order are the SOC reader's,
unchanged; the truth tables become binary-table HDUs after the images.

What the adapter supplies, each from a measurement on
``TDS/simple_model/R062/10032/roman_tds_R062_10032_wfi17_simple.asdf``
(runbook part 11 step 2):

- The pixel origin. GalSim's images are one-based, and the file's gWCS takes
  GalSim's pixel coordinates (the truth index's ``x, y`` too): 69 stars sit
  at (-1.16, -1.06) px from their zero-based centroids through it, scatter
  (0.08, 0.04) px. The adapter's gWCS is the file's after a shift of
  :data:`GALSIM_ORIGIN` on each axis, so the SIP header is zero-based like
  every other input's.
- The units. ``data`` is counts in the exposure (integers, gain 1); the
  conversion multiplies by ``EXPTIME`` to write DN, so the adapter hands it
  counts per second, and ``err`` the same way. ``err`` is not a 1-sigma map
  (it equals ``data`` on 90% of pixels); nothing downstream reads ``ERR``.
- The zero point. ``meta.zptmag`` is ``2.5 log10(37570 cm^2 x exp_time)``,
  the catalog photon flux to counts, not an AB zero point, and the file has
  no ``meta.photometry``; the conversion therefore takes ``[intake.
  nominal_zptmag]``, which the dataset's overlay sets to galsim.roman's AB
  zero points.
- The identifiers. The file carries no Roman observation identifiers; the
  pointing (the index into the survey's observing sequence) is the file
  name's and is written as ``OBSERVTN``, the others are undefined.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import numpy as np
from astropy.time import Time, TimeDelta

from rapidpipe.products.filters import RAPID_TO_ROMAN_FILTER_NAMES
from rapidpipe.science.intake import build_fits_file

#: An OpenUniverse file stem: survey, filter (legacy spelling), pointing, SCA.
STEM = re.compile(r"roman_(?P<survey>tds|was)_(?P<filter>[A-Z]\d{3})_(?P<pointing>\d+)_wfi(?P<sca>\d{2})")

#: GalSim's first pixel: the file's gWCS takes one-based pixel coordinates.
GALSIM_ORIGIN = 1.0

#: The ``catalogs`` tables carried into the FITS file, in order.
CATALOGS = ("transients", "point_sources", "galaxies")


def parse_stem(stem: str) -> dict[str, int | str]:
    """The identifiers an OpenUniverse file name carries; ``ValueError`` for another name."""
    match = STEM.fullmatch(stem)
    if match is None:
        raise ValueError(f"{stem!r} is not an OpenUniverse simple_model file stem")
    return {"survey": match["survey"], "filter": match["filter"],
            "pointing": int(match["pointing"]), "sca": int(match["sca"])}


def source_key(stem: str) -> str:
    """The file's path under the dataset's source root: ``<filter>/<pointing>/<stem>_simple.asdf``."""
    ids = parse_stem(stem)
    return f"{ids['filter']}/{ids['pointing']}/{stem}_simple.asdf"


class _Node(dict):
    """A mapping read as attributes too, as the conversion reads a data model's ``meta``."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def model(tree: dict, stem: str) -> _Node:
    """The file's tree as the data model :func:`build_fits_file` reads."""
    from astropy.modeling.models import Shift
    from gwcs import wcs as gwcs_wcs

    ids = parse_stem(stem)
    meta = tree["meta"]
    filter_name = str(meta["optical_element"]).strip()
    if filter_name != ids["filter"]:
        raise ValueError(f"{stem}: the file's own filter {filter_name} is not its name's")
    roman_filter = RAPID_TO_ROMAN_FILTER_NAMES.get(filter_name, filter_name)
    sca = int(str(meta["detector"]).strip().removeprefix("SCA"))
    if sca != ids["sca"]:
        raise ValueError(f"{stem}: the file's own detector {meta['detector']} is not its name's")
    exptime = float(meta["exp_time"])
    start = Time(meta["mjd_obs"], format="mjd", scale="utc")  # the exposure's start (the sequence's date)
    file_wcs = tree["wcs"]
    origin = Shift(GALSIM_ORIGIN) & Shift(GALSIM_ORIGIN)
    wcs = gwcs_wcs.WCS(forward_transform=origin | file_wcs.forward_transform,
                       input_frame=file_wcs.input_frame, output_frame=file_wcs.output_frame)
    observation = {name: None for name in ("program", "execution_plan", "pass", "segment",
                                           "visit", "exposure", "visit_id", "observation_id")}
    dm = _Node(
        meta=_Node(
            instrument=_Node(optical_element=roman_filter, detector=f"WFI{sca:02d}"),
            exposure=_Node(exposure_time=exptime, start_time=start,
                           end_time=start + TimeDelta(exptime, format="sec")),
            pointing=_Node(target_ra=float(meta["ra_pointing"]), target_dec=float(meta["dec_pointing"])),
            observation=_Node(observation, observation=ids["pointing"]),
            openuniverse=_Node(meta),  # the file's own meta, as HIERARCH cards
            wcs=wcs),
        # counts per second, which the conversion's EXPTIME scaling returns to DN
        data=np.asarray(tree["data"], dtype=np.float32) / np.float32(exptime),
        err=np.asarray(tree["err"], dtype=np.float32) / np.float32(exptime),
        dq=np.asarray(tree["dq"]))
    for name in CATALOGS:
        if name in tree.get("catalogs", {}):
            dm[name] = tree["catalogs"][name]
    return dm


def simple_model_to_fits(asdf_path, fits_path, **conversion) -> dict[str, Any]:
    """Convert one OpenUniverse file, named ``<stem>_simple.asdf``, into the intake's FITS file
    (:func:`build_fits_file`)."""
    import asdf

    stem = Path(asdf_path).name.removesuffix("_simple.asdf")
    with asdf.open(asdf_path, memmap=False, lazy_load=False) as af:
        dm = model(af["roman"], stem)
        return build_fits_file(dm, asdf_path, fits_path, **conversion)
