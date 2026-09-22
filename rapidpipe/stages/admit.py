"""`admit`: the first stage. Turns a delivered l2 image into a pipeline product.

Per the products page ("For the l2 image"), `admit` has no upstream stage:
its input manifest is a *delivery* manifest, written by whoever stages the
delivered file, not by another `rapidpipe` stage. That manifest has
``stage == "delivery"``, one ``l2-image`` output entry of format version
``"delivered"`` whose key names the exposure, detector and delivered
version, and the delivered FITS file as that entry's one member.

`admit` verifies the delivered bytes against the manifest's declared size
and SHA-256, opens the file with astropy to check its FITS checksums, reads
the header and WCS the products page's l2-image field list needs, computes
the image centre and four sky corners, copies the file into this attempt's
output location, and publishes a new instance -- the delivery's own
instance id is never registered (products page: "a delivery is not a
registered product"), so it travels only inside the registration block,
not as a `products_read` dependency edge.

This module may import ``rapidpipe.products``, ``rapidpipe.db``,
``rapidpipe.runs`` and ``rapidpipe.science``; never another stage,
``rapidpipe.launch`` or ``rapidpipe.cli`` (stage contract, dependency
direction; see ``tests/rapidpipe/test_dependency_direction.py``).
"""

from __future__ import annotations

import hashlib
import re
import shutil
import warnings
from pathlib import Path
from typing import Any

from astropy.io import fits
from astropy.utils.exceptions import AstropyUserWarning
from astropy.wcs import WCS

from rapidpipe.db.ids import new_ulid
from rapidpipe.products.l2image import L2ImageRegistration
from rapidpipe.products.manifest import Manifest, Member, OutputEntry, member_for_file
from rapidpipe.stages.contract import (
    InputRejected,
    StageContext,
    StageDeclaration,
    StageResult,
    run_stage,
)

#: `admit`'s settings defaults file. `rapidpipe/settings/` sits beside
#: `rapidpipe/stages/`, not under it -- see the comment at the top of
#: `admit.toml` for why a sibling directory and not a `stages/settings/`
#: subdirectory that would shadow the `rapidpipe.stages.settings` module.
_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings" / "admit.toml"

#: Header keywords `admit` requires beyond the ones the [header] settings
#: table names: standard WCS keywords, read directly and never configurable
#: (design brief: "WCS keywords ... are standard and read directly, not
#: configurable").
_REQUIRED_WCS_KEYWORDS = (
    "CRVAL1", "CRVAL2", "CRPIX1", "CRPIX2",
    "CD1_1", "CD1_2", "CD2_1", "CD2_2",
    "CTYPE1", "CTYPE2", "CUNIT1", "CUNIT2",
    "NAXIS1", "NAXIS2",
)

#: [header]-configured keys that are required; a missing one is InputRejected.
_REQUIRED_HEADER_KEYS = (
    "sca", "dateobs", "mjdobs", "exptime", "filter", "equinox",
    "ra_targ", "dec_targ",
)

#: [header]-configured keys that are optional; absent means None, never 0
#: (products page: "nothing substitutes zero for an unavailable measurement").
_OPTIONAL_HEADER_KEYS = ("pa_obsy", "pa_fpa", "zptmag", "skymean")

_SIP_COEFF_RE = re.compile(r"^([AB])_(\d+)_(\d+)$")

DECLARATION = StageDeclaration(
    name="admit",
    unit="detector-image",
    argument_schema={
        "description": (
            "rapidpipe stage admit --run <run-id> --unit <unit-id> "
            "--attempt <attempt-id> --inputs <dir> --outputs <dir> "
            "[--settings <toml>] [--dry-run]. --inputs holds a delivery "
            "manifest.json (stage 'delivery', one l2-image entry, format "
            "version 'delivered')."
        ),
    },
    settings_schema_path=str(_SETTINGS_PATH),
    consumes=(),
    produces=("l2-image",),
    database_access="none",
    resource_defaults={"vcpus": 1, "memory_mib": 2048},
)


def _sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _md5_of_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _validate_delivery_manifest(manifest: Manifest) -> OutputEntry:
    """Return the delivery manifest's one l2-image entry, or raise InputRejected."""
    if manifest.stage != "delivery":
        raise InputRejected(
            f"input manifest has stage {manifest.stage!r}, expected 'delivery'")
    if manifest.unit.kind != "detector-image":
        raise InputRejected(
            f"input manifest unit kind is {manifest.unit.kind!r}, "
            "expected 'detector-image'")
    if len(manifest.outputs) != 1:
        raise InputRejected(
            f"delivery manifest has {len(manifest.outputs)} output entries, "
            "expected exactly one")
    entry = manifest.outputs[0]
    if entry.kind != "l2-image":
        raise InputRejected(
            f"delivery manifest entry has kind {entry.kind!r}, expected 'l2-image'")
    if entry.format_version != "delivered":
        raise InputRejected(
            f"delivery manifest entry has format_version {entry.format_version!r}, "
            "expected 'delivered'")
    for key_field in ("exposure", "detector", "version"):
        value = entry.key.get(key_field)
        if not isinstance(value, str) or not value:
            raise InputRejected(
                f"delivery manifest entry key is missing string field {key_field!r}")
    if not entry.members:
        raise InputRejected("delivery manifest entry has no members")
    if entry.primary is None:
        raise InputRejected("delivery manifest entry has no 'primary' member")
    return entry


def _verify_member_bytes(inputs_dir: Path, member: Member) -> Path:
    path = inputs_dir / member.path
    if not path.exists():
        raise InputRejected(f"delivered member file not found: {path}")
    actual_bytes = path.stat().st_size
    if actual_bytes != member.bytes:
        raise InputRejected(
            f"delivered member {member.path!r}: manifest declares "
            f"{member.bytes} bytes, file is {actual_bytes} bytes")
    expected_sha256 = member.sha256.removeprefix("sha256:")
    actual_sha256 = _sha256_of_file(path)
    if actual_sha256 != expected_sha256:
        raise InputRejected(
            f"delivered member {member.path!r}: SHA-256 mismatch "
            f"(manifest declares {expected_sha256}, file hashes to {actual_sha256})")
    return path


def _select_header_hdu(hdul: "fits.HDUList") -> tuple[int, Any]:
    """Return ``(hdu_index, header)`` for the primary HDU if it has data,
    else the first image extension. Per the design brief: "Read the header
    of the image HDU (the primary HDU if it has data, else the first image
    extension; record which as ``hdu`` in the registration block)."
    """
    if hdul[0].data is not None:
        return 0, hdul[0].header
    for index, hdu in enumerate(hdul):
        if index == 0:
            continue
        if getattr(hdu, "data", None) is not None:
            return index, hdu.header
    raise InputRejected("delivered FITS file has no image HDU with data")


def _verify_fits_checksums(path: Path, require: bool) -> int:
    """Open the FITS file with checksum verification; return the l2files
    ``status`` value (1 verified, 0 absent-and-not-required).

    astropy raises no exception for a missing or failed checksum -- it
    warns (``AstropyUserWarning``). This function turns "warned" into the
    contract's InputRejected only when ``require`` is true and the
    keywords are missing entirely; a *failed* verification (keywords
    present but wrong) is always InputRejected regardless of ``require``,
    since a present-but-wrong checksum indicates real corruption, not
    merely an unverified delivery.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", AstropyUserWarning)
        with fits.open(path, checksum=True) as hdul:
            hdu_index, header = _select_header_hdu(hdul)
            has_datasum = "DATASUM" in header or "CHECKSUM" in header
            # Force astropy to actually verify by touching the data of the
            # HDU the checksum keywords live on, and every HDU in the file
            # (checksum=True verifies lazily, on access).
            for hdu in hdul:
                _ = hdu.data

    messages = [str(w.message) for w in caught]
    failed = any(
        "checksum verification failed" in m.lower()
        or "datasum verification failed" in m.lower()
        for m in messages
    )
    missing = any(
        "'checksum' not found" in m.lower() or "'datasum' not found" in m.lower()
        for m in messages
    )

    if failed:
        raise InputRejected(
            f"delivered FITS file failed checksum verification: {messages}")
    if missing or not has_datasum:
        if require:
            raise InputRejected(
                "delivered FITS file has no DATASUM/CHECKSUM keywords to verify, "
                "and [verify] require_fits_checksums is true")
        return 0
    return 1


def _get_header_value(header: Any, keyword: str, *, required: bool, field_name: str):
    if keyword not in header:
        if required:
            raise InputRejected(
                f"delivered FITS header is missing required keyword {keyword!r} "
                f"(field {field_name!r})")
        return None
    return header[keyword]


def _extract_sip_block(header: Any, prefix: str) -> tuple[int | None, dict[str, float]]:
    order_keyword = f"{prefix}_ORDER"
    if order_keyword not in header:
        return None, {}
    order = int(header[order_keyword])
    coeffs: dict[str, float] = {}
    for keyword, value in header.items():
        match = _SIP_COEFF_RE.match(str(keyword))
        if match and match.group(1) == prefix:
            i, j = match.group(2), match.group(3)
            coeffs[f"{i}_{j}"] = float(value)
    return order, coeffs


def _compute_center_and_corners(
    wcs: WCS, naxis1: int, naxis2: int,
) -> tuple[dict[str, float], list[list[float]]]:
    """Reproduce the legacy script's pixel positions and corner order.

    Legacy ``compute_center_sky_position``/``compute_corner_sky_positions``
    (database/sims/db_register_socsim_files.py) use 1-based FITS pixel
    coordinates shifted to the 0-based convention astropy's
    ``pixel_to_world`` expects (``x = 0.5 * naxis + 0.5 - 1.0`` for the
    centre; ``x = 0.5 - 1.0`` and ``x = naxis + 0.5 - 1.0`` for the outer
    edges), i.e. ``origin=0``. Corner order is (bottom-left, bottom-right,
    top-right, top-left) in that (x, y) pixel sense.
    """
    cx = 0.5 * naxis1 + 0.5 - 1.0
    cy = 0.5 * naxis2 + 0.5 - 1.0
    center = wcs.pixel_to_world(cx, cy)

    corner_pixels = [
        (0.5 - 1.0, 0.5 - 1.0),
        (naxis1 + 0.5 - 1.0, 0.5 - 1.0),
        (naxis1 + 0.5 - 1.0, naxis2 + 0.5 - 1.0),
        (0.5 - 1.0, naxis2 + 0.5 - 1.0),
    ]
    corners = []
    for x, y in corner_pixels:
        sky = wcs.pixel_to_world(x, y)
        corners.append([float(sky.ra.degree % 360.0), float(sky.dec.degree)])

    return (
        {"ra": float(center.ra.degree % 360.0), "dec": float(center.dec.degree)},
        corners,
    )


def _body(context: StageContext) -> StageResult:
    header_settings = context.settings["header"]
    require_checksums = context.settings["verify"]["require_fits_checksums"]
    copy_subdirectory = context.settings["copy"]["subdirectory"]

    entry = _validate_delivery_manifest(context.input_manifest)
    key = entry.key
    primary_member = next(m for m in entry.members if m.path == entry.primary)
    other_members = [m for m in entry.members if m.path != entry.primary]

    primary_path = _verify_member_bytes(context.inputs_dir, primary_member)
    for member in other_members:
        _verify_member_bytes(context.inputs_dir, member)

    status = _verify_fits_checksums(primary_path, require_checksums)

    try:
        with fits.open(primary_path) as hdul:
            hdu_index, header = _select_header_hdu(hdul)
            data_shape = hdul[hdu_index].data.shape
    except OSError as exc:
        raise InputRejected(f"could not open delivered FITS file: {exc}") from exc

    try:
        wcs = WCS(header)
    except Exception as exc:  # noqa: BLE001 - any WCS construction failure is InputRejected
        raise InputRejected(f"could not build WCS from delivered header: {exc}") from exc

    for keyword in _REQUIRED_WCS_KEYWORDS:
        _get_header_value(header, keyword, required=True, field_name=keyword)

    header_values: dict[str, Any] = {}
    for field_name in _REQUIRED_HEADER_KEYS:
        keyword = header_settings[field_name]
        header_values[field_name] = _get_header_value(
            header, keyword, required=True, field_name=field_name)
    for field_name in _OPTIONAL_HEADER_KEYS:
        keyword = header_settings[field_name]
        header_values[field_name] = _get_header_value(
            header, keyword, required=False, field_name=field_name)

    sca_from_header = str(header_values["sca"]).strip()
    detector_from_key = str(key["detector"]).strip()
    if sca_from_header != detector_from_key:
        raise InputRejected(
            f"delivery key detector {detector_from_key!r} does not match "
            f"header {header_settings['sca']!r} value {sca_from_header!r}")

    naxis1 = int(header["NAXIS1"])
    naxis2 = int(header["NAXIS2"])
    a_order, a_coeffs = _extract_sip_block(header, "A")
    b_order, b_coeffs = _extract_sip_block(header, "B")
    centre, corners = _compute_center_and_corners(wcs, naxis1, naxis2)

    context.outputs_dir.mkdir(parents=True, exist_ok=True)
    copy_dir = context.outputs_dir / copy_subdirectory
    copy_dir.mkdir(parents=True, exist_ok=True)

    members: list[Member] = []
    primary_dest_rel = None
    for member in entry.members:
        source = context.inputs_dir / member.path
        dest = copy_dir / Path(member.path).name
        shutil.copyfile(source, dest)
        built_member = member_for_file(
            role="image" if member.path == entry.primary else "member",
            path=dest, relative_to=context.outputs_dir)
        members.append(built_member)
        if member.path == entry.primary:
            primary_dest_rel = built_member.path

    primary_dest_path = context.outputs_dir / primary_dest_rel
    md5_hex = _md5_of_file(primary_dest_path)

    registration = L2ImageRegistration(
        exposure_id=str(key["exposure"]),
        detector=int(sca_from_header),
        delivered_version=str(key["version"]),
        filter=str(header_values["filter"]),
        dateobs=str(header_values["dateobs"]),
        mjdobs=float(header_values["mjdobs"]),
        exptime=float(header_values["exptime"]),
        infobits=0,
        status=status,
        naxis1=naxis1,
        naxis2=naxis2,
        crval1=float(header["CRVAL1"]),
        crval2=float(header["CRVAL2"]),
        crpix1=float(header["CRPIX1"]),
        crpix2=float(header["CRPIX2"]),
        cd11=float(header["CD1_1"]),
        cd12=float(header["CD1_2"]),
        cd21=float(header["CD2_1"]),
        cd22=float(header["CD2_2"]),
        ctype1=str(header["CTYPE1"]),
        ctype2=str(header["CTYPE2"]),
        cunit1=str(header["CUNIT1"]),
        cunit2=str(header["CUNIT2"]),
        equinox=float(header_values["equinox"]),
        a_order=a_order,
        a=a_coeffs,
        b_order=b_order,
        b=b_coeffs,
        ra_targ=float(header_values["ra_targ"]) % 360.0,
        dec_targ=float(header_values["dec_targ"]),
        pa_obsy=(float(header_values["pa_obsy"])
                 if header_values["pa_obsy"] is not None else None),
        pa_fpa=(float(header_values["pa_fpa"])
                if header_values["pa_fpa"] is not None else None),
        zptmag=(float(header_values["zptmag"])
                if header_values["zptmag"] is not None else None),
        skymean=(float(header_values["skymean"])
                 if header_values["skymean"] is not None else None),
        centre=centre,
        corners=corners,
        md5=md5_hex,
        hdu=hdu_index,
        delivery_instance=entry.instance,
        delivery_source=entry.registration.get("source"),
    )
    registration.validate()

    output_entry = OutputEntry(
        kind="l2-image",
        format_version="1",
        instance=new_ulid(),
        key={
            "exposure": key["exposure"],
            "detector": key["detector"],
            "version": key["version"],
        },
        members=tuple(members),
        primary=primary_dest_rel,
        registration=registration.to_dict(),
    )

    return StageResult(outputs=[output_entry], products_read={}, result_sets_read=())


def main(argv: list[str]) -> int:
    return run_stage(DECLARATION, _body, argv)


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
