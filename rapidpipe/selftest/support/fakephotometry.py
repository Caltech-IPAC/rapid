"""A synthetic input-set manifest for the `photometry` stage's fixture.

``photometry`` is a declared stub (supervisor step 8, 2026-09-24, ruling
R9): a valid invocation exits 69 without reading any member file's bytes
beyond the validation ``rapidpipe.stages.photometry._read_input_set``
performs -- but that validation is real, so the fixture still needs a
structurally valid input-set manifest (stage ``input-set``, unit
``field``): one SFFT ``difference-image`` bundle (the smallest valid
bundle -- SFFT declares no required ``significance`` role, unlike ZOGY;
``rapidpipe.products.diffimage.DIFFERENCERS``), one standalone ``psf``
entry, and ``inputs.result_sets`` naming one object-set instance id. No
execution record file is written: ``photometry`` never reads one.

Packaged under ``rapidpipe.selftest.support`` (not ``tests/``, which the
pipeline image excludes at build time) so ``rapidpipe selftest --stage
photometry`` can import it inside the image.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from astropy.io import fits

from rapidpipe.selftest.support.fakedifftools import wcs_header

UNIT_ID = "4711398"
RUN = "01J8Y6QZ3MF1NA1E00000000RN"
ATTEMPT = "01J8Y6QZ3MF1NA1E00000000AT"
DIFFERENCE_INSTANCE = "01J8Y6QZ3MF1NA1E00000000D1"
L2_INSTANCE = "01J8Y6QZ3MF1NA1E00000000K2"
REFERENCE_INSTANCE = "01J8Y6QZ3MF1NA1E00000000RF"
PSF_INSTANCE = "01J8Y6QZ3MF1NA1E00000000P1"
OBJECT_SET_INSTANCE = "01J8Y6QZ3MF1NA1E00000000S1"
SETTINGS_HASH = "sha256:" + "4f2a9c0e" * 8
CENTRE = {"ra": 269.4521, "dec": -28.7710}
CORNERS = [[269.4526, -28.7720], [269.4516, -28.7720],
           [269.4516, -28.7700], [269.4526, -28.7700]]
NAXIS = 16


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _member(role: str, path: Path, base: Path) -> dict:
    return {"role": role, "path": path.relative_to(base).as_posix(),
            "bytes": path.stat().st_size, "sha256": _sha256(path)}


def _write_image(path: Path, data: np.ndarray, *, wcs: bool = True) -> None:
    header = wcs_header(NAXIS, sip=False) if wcs else fits.Header()
    header["BUNIT"] = "DN/s"
    fits.PrimaryHDU(data=data.astype(np.float32), header=header).writeto(path)


def build_photometry_input_set(
    inputs: Path, *, unit_id: str = UNIT_ID, object_set: str = OBJECT_SET_INSTANCE,
    difference_instance: str = DIFFERENCE_INSTANCE, extra_differences: int = 0,
    psf_instance: str = PSF_INSTANCE, extra_psfs: int = 0,
    named_result_sets: tuple[str, ...] | None = None,
) -> Path:
    """Write a synthetic photometry input-set manifest under ``inputs``.

    ``extra_differences``/``extra_psfs`` add that many more entries (a
    field spans several epochs); ``named_result_sets`` overrides the
    single named object set for a bad-input test (``()`` for none, or two
    ids to exercise "expected exactly one"). Returns the manifest path.
    """
    work = inputs / "work"
    work.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(20260924)

    outputs: list[dict] = []
    for i in range(1 + max(extra_differences, 0)):
        instance = difference_instance if i == 0 else f"{difference_instance[:-4]}D{i:03d}"
        diff_path = work / f"sfftdiffimage_{i}.fits"
        unc_path = work / f"sfftdiffimage_uncert_{i}.fits"
        _write_image(diff_path, rng.normal(0.0, 1.0, (NAXIS, NAXIS)))
        _write_image(unc_path, np.full((NAXIS, NAXIS), 2.5))
        members = [_member("difference", diff_path, inputs), _member("uncertainty", unc_path, inputs)]
        registration = {
            "detection_role": "difference", "centre": dict(CENTRE),
            "corners": [list(c) for c in CORNERS], "catalog_outcome_bits": 0,
            "infobits_science": 0, "infobits_reference": 0,
            "source_counts": {"sextractor": {"positive": 3, "negative": 3},
                              "photutils": {"positive": 3, "negative": 3}},
            "registration_residual": {"x_rms": 0.01, "y_rms": 0.01,
                                      "x_median": 0.0, "y_median": 0.0},
            "reference_scale_factor": 1.0,
            "md5": hashlib.md5(diff_path.read_bytes()).hexdigest(),
            "reference_rfid": None,
        }
        outputs.append({
            "kind": "difference-image", "format_version": "1", "instance": instance,
            "key": {"l2": L2_INSTANCE, "reference": REFERENCE_INSTANCE,
                    "differencer": "sfft", "settings_hash": SETTINGS_HASH},
            "primary": members[0]["path"], "members": members, "registration": registration,
        })

    for i in range(1 + max(extra_psfs, 0)):
        instance = psf_instance if i == 0 else f"{psf_instance[:-4]}P{i:03d}"
        yy, xx = np.mgrid[0:9, 0:9]
        data = np.exp(-((xx - 4) ** 2 + (yy - 4) ** 2) / (2 * 1.2 ** 2))
        path = work / f"psf_{i}.fits"
        _write_image(path, data / data.sum(), wcs=False)
        outputs.append({
            "kind": "psf", "format_version": "1", "instance": instance,
            "key": {"filter": "F146", "detector": "1", "version": "1"},
            "primary": path.relative_to(inputs).as_posix(),
            "members": [_member("psf", path, inputs)], "registration": {},
        })

    result_sets = list(named_result_sets) if named_result_sets is not None else [object_set]

    manifest = {
        "schema_version": "1", "run": RUN,
        "unit": {"kind": "field", "id": unit_id},
        "stage": "input-set", "attempt": ATTEMPT,
        "execution_record": "exec/input-set.json",
        "inputs": {"manifest": "composed/manifest.json", "products": {},
                   "result_sets": result_sets},
        "outputs": outputs,
    }
    path = inputs / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return path
