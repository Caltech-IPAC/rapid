"""A synthetic difference attempt's output location, as the finalize stage reads it.

:func:`build_difference_output` writes what ``rapidpipe.stages.difference``
publishes for one registered instance: the bundle's member files (small
64x64 float32 FITS images with a TAN WCS -- ``difference``,
``uncertainty``, ``significance`` and ``psf`` for ZOGY), the SExtractor
and Photutils catalogs for both signs (Photutils with its ``finder``
member), the attempt's execution record, and its completion manifest with
the shape the stage writes (the entry order, logical keys, member paths
under ``work/`` and a registration block that passes
:func:`rapidpipe.products.diffimage.validate_difference_entry`). Nothing
big is shipped: every file is generated here, deterministically.

The difference stage's own fakes (:mod:`rapidpipe.selftest.support.fakedifftools`)
build that stage's *inputs*; :func:`rapidpipe.selftest.support.fakeloaddb.build_load_input_set`
builds a difference manifest without FITS pixels or a full registration
block. Neither is what finalize needs, hence this module.

Packaged under ``rapidpipe.selftest.support`` (not ``tests/``, which the
pipeline image excludes at build time) so ``rapidpipe selftest --stage
finalize`` can import it inside the image.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits

from rapidpipe.products.diffimage import catalog_outcome_bit
from rapidpipe.selftest.support.fakedifftools import wcs_header

UNIT_ID = "e20260821001234/SCA07"
RUN = "01J8Y6QZ3MF1NA1E00000000RN"
ATTEMPT = "01J8Y6QZ3MF1NA1E00000000AT"
DIFFERENCE_INSTANCE = "01J8Y6QZ3MF1NA1E0000000D1F"
L2_INSTANCE = "01J8Y6QZ3MF1NA1E000000012A"
REFERENCE_INSTANCE = "01J8Y6QZ3MF1NA1E0000000REF"
SETTINGS_HASH = "sha256:" + "4f2a9c0e" * 8
CENTRE = {"ra": 269.4521, "dec": -28.7710}
CORNERS = [[269.4526, -28.7720], [269.4516, -28.7720],
           [269.4516, -28.7700], [269.4526, -28.7700]]
NAXIS = 64

#: ZOGY's members as `difference` names its files, role -> file name.
ZOGY_FILES = {
    "difference": "diffimage_masked.fits",
    "uncertainty": "diffimage_uncert_masked.fits",
    "significance": "scorr_masked.fits",
    "psf": "diffpsf.fits",
}

_CATALOG_INSTANCES = {
    ("sextractor", "positive"): "01J8Y6QZ3MF1NA1E000000SXP0",
    ("sextractor", "negative"): "01J8Y6QZ3MF1NA1E000000SXN0",
    ("photutils", "positive"): "01J8Y6QZ3MF1NA1E000000PHP0",
    ("photutils", "negative"): "01J8Y6QZ3MF1NA1E000000PHN0",
}


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _member(role: str, path: Path, base: Path) -> dict:
    return {"role": role, "path": path.relative_to(base).as_posix(),
            "bytes": path.stat().st_size, "sha256": _sha256(path)}


def _write_image(path: Path, data: np.ndarray, *, wcs: bool = True) -> None:
    header = wcs_header(NAXIS, sip=False) if wcs else fits.Header()
    header["BUNIT"] = "DN/s"
    fits.PrimaryHDU(data=data.astype(np.float32), header=header).writeto(path)


def _catalog_text(catalog_type: str, sign: str, rows: int) -> str:
    lines = [f"# {catalog_type} {sign} catalog (synthetic)", "# id x y flux"]
    for i in range(rows):
        lines.append(f"{i + 1} {10.0 + i:.1f} {20.0 + i:.1f} {100.0 + 7 * i:.2f}")
    return "\n".join(lines) + "\n"


def build_difference_output(inputs: Path, *, differencer: str = "zogy",
                            catalog_outcome_bits: int = 0, seed: int = 20260924,
                            execution_record: dict[str, Any] | None = None,
                            source_rows: int = 3) -> Path:
    """Write a difference attempt's output location under ``inputs``; return the manifest path.

    ``catalog_outcome_bits`` is the instance's mask: a Photutils sign whose
    bit is set gets no entry and a ``null`` source count, as `difference`
    writes it. ``execution_record`` replaces the default record's content;
    pass ``{}`` for a record with neither provenance field. Only ZOGY's
    bundle is built.
    """
    if differencer != "zogy":
        raise ValueError("build_difference_output builds ZOGY's bundle only")
    work = inputs / "work"
    work.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)

    paths = {}
    for role, name in ZOGY_FILES.items():
        path = work / name
        if role == "psf":
            yy, xx = np.mgrid[0:9, 0:9]
            data = np.exp(-((xx - 4) ** 2 + (yy - 4) ** 2) / (2 * 1.2 ** 2))
            _write_image(path, data / data.sum(), wcs=False)
        elif role == "uncertainty":
            _write_image(path, np.full((NAXIS, NAXIS), 2.5) + rng.normal(0, 0.01, (NAXIS, NAXIS)))
        else:
            _write_image(path, rng.normal(0.0, 1.0, (NAXIS, NAXIS)))
        paths[role] = path

    counts: dict[str, dict[str, int | None]] = {"sextractor": {}, "photutils": {}}
    catalog_entries = []
    for catalog_type in ("sextractor", "photutils"):
        for sign in ("positive", "negative"):
            if (catalog_type == "photutils"
                    and catalog_outcome_bits & catalog_outcome_bit(differencer, sign)):
                counts[catalog_type][sign] = None
                continue
            suffix = "" if sign == "positive" else "_negative"
            members = []
            if catalog_type == "sextractor":
                path = work / f"diffimage_masked{suffix}.txt"
                path.write_text(_catalog_text(catalog_type, sign, source_rows))
                members.append(_member("catalog", path, inputs))
            else:
                path = work / f"{differencer}_diffimage_masked_psfcat{suffix}.txt"
                path.write_text(_catalog_text(catalog_type, sign, source_rows))
                finder = work / f"{differencer}_diffimage_masked_psfcat_finder{suffix}.txt"
                finder.write_text(_catalog_text("finder", sign, source_rows))
                members += [_member("catalog", path, inputs), _member("finder", finder, inputs)]
            counts[catalog_type][sign] = source_rows
            catalog_entries.append({
                "kind": "source-catalog", "format_version": "1",
                "instance": _CATALOG_INSTANCES[(catalog_type, sign)],
                "key": {"difference": DIFFERENCE_INSTANCE, "catalog_type": catalog_type,
                        "sign": sign},
                "primary": members[0]["path"], "members": members,
                "registration": {"source_count": source_rows}})

    registration = {
        "detection_role": "significance",
        "centre": dict(CENTRE),
        "corners": [list(c) for c in CORNERS],
        "catalog_outcome_bits": catalog_outcome_bits,
        "infobits_science": 0,
        "infobits_reference": 0,
        "source_counts": counts,
        "registration_residual": {"x_rms": 0.012, "y_rms": 0.015,
                                  "x_median": 0.004, "y_median": -0.002},
        "reference_scale_factor": 0.998,
        "md5": hashlib.md5(paths["difference"].read_bytes()).hexdigest(),
        "reference_rfid": None,
    }
    difference_entry = {
        "kind": "difference-image", "format_version": "1", "instance": DIFFERENCE_INSTANCE,
        "key": {"l2": L2_INSTANCE, "reference": REFERENCE_INSTANCE,
                "differencer": differencer, "settings_hash": SETTINGS_HASH},
        "primary": f"work/{ZOGY_FILES['difference']}",
        "members": [_member(role, paths[role], inputs) for role in ZOGY_FILES],
        "registration": registration,
    }

    record_path = inputs / "exec" / f"{ATTEMPT}.json"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record = ({"settings_hash": SETTINGS_HASH.removeprefix("sha256:"),
               "source_revision": "0123456789abcdef0123456789abcdef01234567",
               "image_digest": "sha256:" + "ab" * 32}
              if execution_record is None else execution_record)
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")

    manifest = {
        "schema_version": "1", "run": RUN,
        "unit": {"kind": "detector-image", "id": UNIT_ID},
        "stage": "difference", "attempt": ATTEMPT,
        "execution_record": f"exec/{ATTEMPT}.json",
        "inputs": {"manifest": "input-set/manifest.json",
                   "products": {"l2-image": L2_INSTANCE, "reference-image": REFERENCE_INSTANCE},
                   "result_sets": []},
        "outputs": [difference_entry, *catalog_entries],
    }
    path = inputs / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return path
