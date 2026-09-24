"""The finalize stage's :class:`rapidpipe.selftest.runner.StageFixture`.

``prepare`` writes a synthetic difference attempt's output location
(:func:`rapidpipe.selftest.support.fakefinalize.build_difference_output`)
and the settings overlay; ``check`` asserts what the supervisor's ruling
fixes (2026-09-24): one difference-image and four source-catalog entries
under new instance ids, the stamped primary member opening with checksum
verification and carrying every keyword with its expected value, every
other member byte-identical to its input, the registration ``md5`` equal
to the stamped file's, and the catalogs keyed to the new instance.

The stage runs no external tool and touches no database, so the fixture is
the same with ``--real-tools`` and without (one ``expected`` spec).
"""

from __future__ import annotations

import hashlib
import json
import re
import warnings
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits
from astropy.utils.exceptions import AstropyUserWarning

from rapidpipe.db.ids import is_valid_ulid
from rapidpipe.products.diffimage import validate_difference_entry, validate_source_catalog_entry
from rapidpipe.products.manifest import Manifest
from rapidpipe.science.finalize.headers import KEYWORDS
from rapidpipe.selftest.runner import CheckContext, Checks, StageFixture, fixture_dir
from rapidpipe.selftest.support.fakefinalize import UNIT_ID, build_difference_output

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


def _prepare(work: Path, expected: dict[str, Any], fake: bool) -> tuple[Path, Path, dict[str, str]]:
    inputs = work / "inputs"
    build_difference_output(inputs, **expected["inputs"])
    overlay = work / "settings.toml"
    overlay.write_text((fixture_dir("finalize") / "settings.toml").read_text())
    return inputs, overlay, {}


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _open_verified(checks: Checks, path: Path) -> fits.Header | None:
    """The stamped file's primary header, read with checksum verification."""
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", AstropyUserWarning)
            with fits.open(path, checksum=True) as hdul:
                for hdu in hdul:
                    _ = hdu.data
                header = hdul[0].header.copy()
    except Exception as exc:  # noqa: BLE001
        checks.check(False, f"stamped file opens with checksum=True: {exc}")
        return None
    problems = [str(w.message) for w in caught if "checksum" in str(w.message).lower()
                or "datasum" in str(w.message).lower()]
    checks.check(not problems, f"stamped file's CHECKSUM/DATASUM verify: {problems}")
    checks.check("CHECKSUM" in header and "DATASUM" in header,
                 "stamped file carries CHECKSUM and DATASUM")
    return header


def _check(checks: Checks, manifest: Manifest, expected: dict[str, Any],
           context: CheckContext) -> None:
    spec = expected["expected"]
    inputs, outputs = context.inputs, context.outputs
    source = Manifest.read(inputs / "manifest.json")
    source_by_instance = {e.instance: e for e in source.outputs}

    for kind, count in spec["entries"].items():
        actual = sum(1 for e in manifest.outputs if e.kind == kind)
        checks.check(actual == count, f"{kind} entries: expected {count}, got {actual}")
    instances = [e.instance for e in manifest.outputs]
    checks.check(all(is_valid_ulid(i) for i in instances), "every output instance is a ULID")
    checks.check(len(set(instances)) == len(instances), "output instances are distinct")
    checks.check(not set(instances) & set(source_by_instance),
                 "no output reuses an input instance id")

    diffs = [e for e in manifest.outputs if e.kind == "difference-image"]
    if len(diffs) != 1:
        return
    entry = diffs[0]
    (source_diff,) = [e for e in source.outputs if e.kind == "difference-image"]
    try:
        validate_difference_entry(entry.to_dict())
        checks.check(True, "")
    except ValueError as exc:
        checks.check(False, f"finalized difference-image validates: {exc}")
    checks.check(entry.key == source_diff.key, "difference-image logical key unchanged")
    checks.check(entry.primary == source_diff.primary, "primary member path unchanged")
    checks.check(sorted(m.role for m in entry.members) == spec["roles"], "bundle roles")

    # Registration: the input's, md5 recomputed, finalized_from and revision added.
    registration = dict(entry.registration)
    checks.check(registration.pop("finalized_from", None) == source_diff.instance,
                 "registration finalized_from is the input instance")
    checks.check(registration.pop("revision", None) == spec["revision"],
                 f"registration revision is {spec['revision']}")
    stamped_path = outputs / entry.primary
    md5 = hashlib.md5(stamped_path.read_bytes()).hexdigest()
    checks.check(registration.pop("md5", None) == md5,
                 "registration md5 is the stamped file's MD5")
    unchanged = {k: v for k, v in source_diff.registration.items() if k != "md5"}
    checks.check(registration == unchanged, "the rest of the registration block is copied")

    # Members: the primary stamped (same pixels), the others byte-identical.
    source_members = {m.role: m for m in source_diff.members}
    for member in entry.members:
        original = source_members.get(member.role)
        if original is None:
            checks.check(False, f"member role {member.role} exists in the input")
            continue
        if member.path == entry.primary:
            checks.check(member.sha256 != original.sha256,
                         "the difference member was rewritten")
            before = fits.getdata(inputs / original.path)
            after = fits.getdata(outputs / member.path)
            checks.check(np.array_equal(before, after, equal_nan=True)
                         and before.dtype == after.dtype,
                         "the difference member's pixels and dtype are unchanged")
        else:
            checks.check(member.sha256 == original.sha256
                         and _sha256(outputs / member.path) == original.sha256,
                         f"member {member.role} is byte-identical to its input")

    # The stamp.
    header = _open_verified(checks, stamped_path)
    if header is not None:
        for keyword, _, comment in KEYWORDS:
            checks.check(keyword in header and header.comments[keyword] == comment,
                         f"{keyword} is stamped with its full comment")
        for keyword, value in spec["stamp"].items():
            checks.check(header.get(keyword) == value,
                         f"{keyword}: expected {value!r}, got {header.get(keyword)!r}")
        for keyword, value in (("RPRUN", manifest.run), ("RPATTMPT", manifest.attempt),
                               ("RPINST", entry.instance), ("RPOUTLOC", str(outputs))):
            checks.check(header.get(keyword) == value,
                         f"{keyword}: expected {value!r}, got {header.get(keyword)!r}")
        checks.check(_DATE_RE.match(str(header.get("DATE", ""))) is not None,
                     f"DATE is ISO UTC to the second, got {header.get('DATE')!r}")
        source_header = fits.getheader(inputs / source_diff.primary)
        kept = [k for k in source_header if k not in ("CHECKSUM", "DATASUM")]
        checks.check(all(header.get(k) == source_header[k] for k in kept),
                     "the input header's own keywords are kept")

    # Catalogs.
    catalogs = [e for e in manifest.outputs if e.kind == "source-catalog"]
    for catalog in catalogs:
        label = f"source-catalog {catalog.key.get('catalog_type')}/{catalog.key.get('sign')}"
        try:
            validate_source_catalog_entry(catalog.to_dict())
            checks.check(True, "")
        except ValueError as exc:
            checks.check(False, f"{label} validates: {exc}")
        original = source_by_instance.get(catalog.registration.get("copied_from"))
        checks.check(original is not None and original.kind == "source-catalog",
                     f"{label} copied_from names an input catalog")
        if original is None:
            continue
        checks.check(catalog.key == {**original.key, "difference": entry.instance},
                     f"{label} key names the finalized instance")
        checks.check(catalog.registration == {**original.registration,
                                              "copied_from": original.instance},
                     f"{label} registration copied")
        checks.check([m.to_dict() for m in catalog.members]
                     == [m.to_dict() for m in original.members]
                     and all(_sha256(outputs / m.path) == m.sha256 for m in catalog.members),
                     f"{label} members byte-identical to the input's")

    checks.check(sorted(manifest.inputs.products) == spec["products_read"],
                 f"inputs.products names: got {sorted(manifest.inputs.products)}")
    checks.check(manifest.inputs.products.get("difference-image") == source_diff.instance,
                 "inputs.products names the input difference instance")
    record = json.loads((outputs / manifest.execution_record).read_text())
    checks.check("notes" not in record, "no execution notes")


FIXTURE = StageFixture(
    stage="finalize",
    module="rapidpipe.stages.finalize",
    unit_kind="detector-image",
    unit_id=UNIT_ID,
    fake_toolkit_env={},
    prepare=_prepare,
    check=_check,
    spec_key=lambda tools: "expected",
)
