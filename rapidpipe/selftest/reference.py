"""The reference stage's :class:`rapidpipe.selftest.runner.StageFixture`.

``prepare`` copies the packaged input set (three gzipped L2-shaped frames
and their input-set manifest, ``rapidpipe/selftest/fixtures/reference/``,
written by :func:`rapidpipe.selftest.support.fakereftools.write_frames`)
and the settings overlay; ``check`` asserts what rulings R5 and R6 fix
(supervisor step 8, 2026-09-24): one ``reference-image`` entry (members
image/coverage/uncertainty, the logical key with the selection digest,
exactly R6's registration fields, the ordered constituents), one
``reference-catalog`` entry keyed to it, ``inputs.products`` naming the
constituents, the stamped header (verified checksums) on the image and
the uncertainty image, and the numbers ``expected.json`` fixes for the
tool set (``fake``: exact to tolerance; ``real``: ranges).
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import warnings
from pathlib import Path
from typing import Any

from astropy.io import fits
from astropy.utils.exceptions import AstropyUserWarning

from rapidpipe.products.manifest import Manifest
from rapidpipe.science.reference import header as stamp
from rapidpipe.science.reference.identity import selection_digest
from rapidpipe.selftest.runner import CheckContext, Checks, StageFixture, fixture_dir
from rapidpipe.selftest.support.fakedifftools import cdf_dir
from rapidpipe.selftest.support.fakereftools import RTID, UNIT_FILTER

FAKE_TOOLKIT = "rapidpipe.selftest.support.fakereftools:fake_toolkit"
TOOLKIT_ENV = "RAPIDPIPE_REFERENCE_TOOLKIT"
UNIT_ID = f"{RTID}/{UNIT_FILTER}"


def _prepare(work: Path, expected: dict[str, Any], fake: bool) -> tuple[Path, Path, dict[str, str]]:
    source = fixture_dir("reference")
    inputs = work / "inputs"
    inputs.mkdir(parents=True)
    shutil.copyfile(source / "manifest.json", inputs / "manifest.json")
    shutil.copytree(source / "l2", inputs / "l2")
    overlay = work / "settings.toml"
    text = (source / "settings.toml").read_text()
    if fake:
        text += f'\n[paths]\ncfg_path = "{cdf_dir()}"\n'
    overlay.write_text(text)
    return inputs, overlay, {}


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _check_value(checks: Checks, spec: Any, actual: Any, label: str, tolerances: dict) -> None:
    """``spec`` is an exact value, ``[lo, hi]`` (a range), or ``{"close": x}``."""
    if isinstance(spec, dict) and "close" in spec:
        checks.close(spec["close"], actual, rel=tolerances["scalar_rel"],
                     abs_=tolerances["scalar_abs"], label=label)
    elif isinstance(spec, list) and len(spec) == 2 and all(
            isinstance(v, (int, float)) for v in spec):
        ok = isinstance(actual, (int, float)) and spec[0] <= actual <= spec[1]
        checks.check(ok, f"{label}: expected in {spec}, got {actual!r}")
    else:
        checks.check(actual == spec, f"{label}: expected {spec!r}, got {actual!r}")


def _open_verified(checks: Checks, path: Path, label: str) -> fits.Header | None:
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", AstropyUserWarning)
            with fits.open(path, checksum=True) as hdul:
                _ = hdul[0].data
                header = hdul[0].header.copy()
    except Exception as exc:  # noqa: BLE001
        checks.check(False, f"{label} opens with checksum=True: {exc}")
        return None
    problems = [str(w.message) for w in caught if "checksum" in str(w.message).lower()
                or "datasum" in str(w.message).lower()]
    checks.check(not problems, f"{label} CHECKSUM/DATASUM verify: {problems}")
    return header


def _check(checks: Checks, manifest: Manifest, expected: dict[str, Any],
           context: CheckContext) -> None:
    spec = expected[context.tools]
    tolerances = expected["tolerances"]
    outputs = context.outputs
    source = Manifest.read(context.inputs / "manifest.json")
    constituents = [e.instance for e in source.outputs]

    refs = [e for e in manifest.outputs if e.kind == "reference-image"]
    cats = [e for e in manifest.outputs if e.kind == "reference-catalog"]
    checks.check(len(refs) == 1 and len(cats) == 1 and len(manifest.outputs) == 2,
                 f"one reference-image and one reference-catalog entry, got "
                 f"{[e.kind for e in manifest.outputs]}")
    if len(refs) != 1 or len(cats) != 1:
        return
    ref, cat = refs[0], cats[0]

    # The reference-image entry.
    checks.check(sorted(m.role for m in ref.members) == ["coverage", "image", "uncertainty"],
                 "reference-image member roles")
    image = next(m for m in ref.members if m.role == "image")
    checks.check(ref.primary == image.path, "primary member is the image")
    checks.check(Path(image.path).name == "awaicgen_output_mosaic_image.fits",
                 f"image member name, got {image.path}")
    record = json.loads((outputs / manifest.execution_record).read_text())
    settings_hash = str(record.get("settings_hash"))
    digest = selection_digest(constituents, settings_hash)
    checks.check(ref.key == {"field": str(RTID), "filter": UNIT_FILTER, "recipe": "awaicgen",
                             "version": digest},
                 f"reference-image key, got {ref.key}")
    registration = ref.registration
    checks.check(sorted(registration) == sorted(expected["registration_fields"]),
                 f"registration fields are exactly R6's, got {list(registration)}")
    checks.check(registration.get("md5") == _md5(outputs / image.path),
                 "registration md5 is the published image's MD5")
    checks.check(registration.get("constituents") == constituents,
                 "registration constituents are the input set's l2-image ids in order")
    checks.check(registration.get("settings_hash") == "sha256:" + settings_hash,
                 "registration settings_hash is the attempt's")
    for name, value in spec["registration"].items():
        _check_value(checks, value, registration.get(name), f"registration {name}", tolerances)

    # The reference-catalog entry.
    checks.check(cat.key == {"reference": ref.instance, "catalog_type": "sextractor"},
                 f"reference-catalog key, got {cat.key}")
    checks.check([m.role for m in cat.members] == ["catalog"] and cat.primary == cat.members[0].path,
                 "reference-catalog has one member, role catalog")
    checks.check(Path(cat.primary).name == "awaicgen_output_mosaic_refimsexcat.txt",
                 f"catalog member name, got {cat.primary}")
    checks.check(sorted(cat.registration) == ["catalog_type", "md5", "source_count", "status"],
                 f"reference-catalog registration fields, got {sorted(cat.registration)}")
    checks.check(cat.registration.get("md5") == _md5(outputs / cat.primary),
                 "catalog md5 is the published catalog's MD5")
    checks.check(cat.registration.get("status") == 1
                 and cat.registration.get("catalog_type") == "sextractor",
                 "catalog status 1, catalog_type sextractor")
    checks.check(cat.registration.get("source_count") == registration.get("nsxcatsources"),
                 "catalog source_count equals nsxcatsources")

    # inputs.products: each constituent.
    expected_products = {f"l2-image/{i:03d}": c for i, c in enumerate(constituents, start=1)}
    checks.check(manifest.inputs.products == expected_products,
                 f"inputs.products names the constituents, got {manifest.inputs.products}")

    # The stamp, on the image and on the uncertainty image.
    uncertainty = next(m for m in ref.members if m.role == "uncertainty")
    for role, member in (("image", image), ("uncertainty", uncertainty)):
        hdr = _open_verified(checks, outputs / member.path, f"stamped {role}")
        if hdr is None:
            continue
        for keyword, value in spec["stamp"].items():
            _check_value(checks, value, hdr.get(keyword), f"{role} {keyword}", tolerances)
        checks.check("FID" not in hdr, f"{role}: FID is not stamped")
        checks.check(hdr.get("COV5PERC") == registration.get("cov5percent"),
                     f"{role} COV5PERC equals registration cov5percent")
        infiles = [hdr.get(stamp.infile_keyword(i)) for i in range(1, len(constituents) + 1)]
        names = [Path(e.primary).name for e in source.outputs]
        checks.check(infiles == names, f"{role} INFILnnn name the frames, got {infiles}")
        for keyword, value in (("RPRUN", manifest.run), ("RPATTMPT", manifest.attempt),
                               ("RPINST", ref.instance), ("RPSTAGE", "reference")):
            checks.check(hdr.get(keyword) == value,
                         f"{role} {keyword}: expected {value!r}, got {hdr.get(keyword)!r}")
    coverage = next(m for m in ref.members if m.role == "coverage")
    with fits.open(outputs / coverage.path) as hdul:
        shape = list(hdul[0].data.shape)
    checks.check(shape == spec["mosaic_shape"], f"mosaic shape {spec['mosaic_shape']}, got {shape}")
    checks.check("notes" not in record, "no execution notes (every frame coadded)")
    for name in ("ra_center", "dec_center"):
        value = registration.get(name)
        checks.check(isinstance(value, float) and math.isfinite(value), f"{name} is a float")


FIXTURE = StageFixture(
    stage="reference",
    module="rapidpipe.stages.reference",
    unit_kind="field",
    unit_id=UNIT_ID,
    fake_toolkit_env={TOOLKIT_ENV: FAKE_TOOLKIT},
    prepare=_prepare,
    check=_check,
)
